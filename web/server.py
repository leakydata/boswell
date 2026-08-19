#!/usr/bin/env python3
"""
Boswell web UI — a small local service that owns the BLE link and serves a
responsive front end.

Deliberately split so a phone app is a transport swap rather than a rewrite:
every piece of device state and every action is expressed as JSON over a
WebSocket, and the browser holds no logic that a native client could not
reimplement in a few dozen lines.

    uv run web/server.py          then open http://localhost:8000
"""

import asyncio
import json
import os
import struct
import sys
import time
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
from ble_capture import (AUDIO_UUID, CTRL_UUID, INFO_UUID, DEVICE_NAME,
                         HEADER_LEN, decode_block)
from bleak import BleakClient, BleakScanner

import pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
SEG_SECONDS = 30.0          # write a clip this long, then hand it to the pipeline


class Device:
    """Owns the BLE connection and publishes state to any listening clients."""

    def __init__(self):
        self.state = {
            "connected": False, "armed": False, "scanning": False,
            "rate": 8000, "frames": 0, "lost": 0, "backlog_bytes": 0,
            "backlog_seconds": 0.0, "qspi_mb": 0, "imu": False,
            "peak": 0, "rms": 0.0, "level": 0.0, "error": None,
            "clip_seconds": 0.0,
        }
        self.listeners: set[asyncio.Queue] = set()
        self.client: BleakClient | None = None
        self._pcm: list[np.ndarray] = []
        self._last_seq = None
        self._want = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- pub/sub -------------------------------------------------------
    def publish(self):
        msg = {"type": "state", **self.state}
        for q in list(self.listeners):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def event(self, kind, **kw):
        msg = {"type": kind, **kw}
        for q in list(self.listeners):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # ---- audio ---------------------------------------------------------
    def _on_audio(self, _sender, data: bytearray):
        if len(data) < HEADER_LEN:
            return
        seq, flags, index, predictor, nsamples = struct.unpack("<HBBhH", data[:HEADER_LEN])
        payload = data[HEADER_LEN:]
        if len(payload) < nsamples // 2:
            return
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) & 0xFFFF
            if gap and not (flags & 0x04):      # VAD gaps are intentional
                self.state["lost"] += gap
        self._last_seq = seq

        pcm = decode_block(payload, predictor, index, nsamples)
        self._pcm.append(pcm)
        self.state["frames"] += 1

        # A cheap level meter for the UI; full stats come from the clip.
        peak = int(np.abs(pcm).max())
        self.state["peak"] = peak
        self.state["level"] = round(min(1.0, peak / 32767 * 3), 3)

    def clip_seconds(self):
        return sum(len(p) for p in self._pcm) / self.state["rate"]

    def take_clip(self):
        if not self._pcm:
            return None
        audio = np.concatenate(self._pcm)
        self._pcm = []
        os.makedirs(DATA, exist_ok=True)
        path = os.path.join(DATA, f"clip_{int(time.time())}.wav")
        sf.write(path, audio, self.state["rate"], subtype="PCM_16")
        return path

    # ---- connection ----------------------------------------------------
    async def run(self):
        self._loop = asyncio.get_running_loop()
        while True:
            if not self._want:
                await asyncio.sleep(0.4)
                continue
            try:
                await self._session()
            except Exception as e:
                self.state["error"] = str(e)[:160]
                self.state["connected"] = False
                self.publish()
                await asyncio.sleep(2)

    async def _session(self):
        self.state.update(scanning=True, error=None)
        self.publish()
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
        self.state["scanning"] = False
        if dev is None:
            self.state["error"] = f"{DEVICE_NAME} not found"
            self.publish()
            await asyncio.sleep(2)
            return

        async with BleakClient(dev, timeout=30.0) as c:
            self.client = c
            self.state.update(connected=True, error=None)
            await self._read_info(c)
            await c.start_notify(AUDIO_UUID, self._on_audio)
            await self.set_armed(True)
            self.publish()
            self.event("log", text="connected to device")

            last_info = last_clip = time.time()
            while self._want and c.is_connected:
                await asyncio.sleep(0.25)
                now = time.time()
                self.state["clip_seconds"] = round(self.clip_seconds(), 1)
                if now - last_info > 1.0:
                    last_info = now
                    await self._read_info(c)
                    self.publish()
                if self.clip_seconds() >= SEG_SECONDS:
                    last_clip = now
                    path = self.take_clip()
                    if path:
                        self.event("clip", path=os.path.basename(path))

            self.client = None
            self.state["connected"] = False
            self.publish()

    async def _read_info(self, c):
        info = await c.read_gatt_char(INFO_UUID)
        if len(info) >= 6:
            self.state["rate"] = 16000 if info[1] else 8000
        if len(info) >= 8:
            self.state["imu"] = info[6] != 0
        if len(info) >= 32:
            pend = info[28] | (info[29] << 8) | (info[30] << 16)
            self.state["backlog_bytes"] = pend
            self.state["backlog_seconds"] = round(pend / 4500.0, 1)
            self.state["qspi_mb"] = round(info[31] * 65536 / 1048576)

    async def set_armed(self, on: bool):
        self.state["armed"] = bool(on)
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(
                CTRL_UUID, bytes([0x01, 1 if on else 0]), response=True)
        self.publish()

    async def set_gain(self, g: int):
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(
                CTRL_UUID, bytes([0x03, max(0, min(80, g))]), response=True)
            self.event("log", text=f"gain set to {g}")

    async def set_vad(self, on: bool):
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(
                CTRL_UUID, bytes([0x04, 1 if on else 0]), response=True)
            self.event("log", text=f"VAD {'on' if on else 'off'}")

    def want(self, on: bool):
        self._want = bool(on)
        if not on:
            self.state["connected"] = False
        self.publish()


device = Device()
worker = pipeline.Worker(notify=lambda kind, **kw: device.event(kind, **kw))


def clip_info(name):
    path = os.path.join(DATA, name)
    try:
        info = sf.info(path)
        dur = round(info.duration, 1)
    except Exception:
        dur = 0.0
    tp = pipeline.transcript_path(name)
    status = "none"
    preview, speakers = "", []
    if os.path.exists(tp):
        status = "done"
        try:
            t = json.load(open(tp))
            segs = t.get("segments", [])
            preview = " ".join(x["text"] for x in segs)[:180]
            speakers = sorted({x["speaker"] for x in segs if x.get("speaker")})
        except Exception:
            status = "error"
    if worker.busy == name:
        status = "running"
    return {"name": name, "seconds": dur,
            "modified": os.path.getmtime(path),
            "status": status, "preview": preview, "speakers": speakers}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(device.run())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/clips")
async def api_clips():
    os.makedirs(DATA, exist_ok=True)
    names = sorted((f for f in os.listdir(DATA) if f.endswith(".wav")),
                   key=lambda f: os.path.getmtime(os.path.join(DATA, f)),
                   reverse=True)
    return [clip_info(n) for n in names[:100]]


@app.get("/api/audio/{name}")
async def api_audio(name: str):
    if "/" in name or not name.endswith(".wav"):
        raise HTTPException(400, "bad name")
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/transcribe/{name}")
async def api_transcribe(name: str):
    if "/" in name or not os.path.exists(os.path.join(DATA, name)):
        raise HTTPException(404, "no such clip")
    worker.submit(name)
    return {"queued": name}


@app.get("/api/transcript/{name}")
async def api_transcript(name: str):
    tp = pipeline.transcript_path(name)
    if not os.path.exists(tp):
        raise HTTPException(404, "not transcribed yet")
    return JSONResponse(json.load(open(tp)))


@app.get("/api/speakers")
async def api_speakers():
    meta = {}
    if os.path.exists(pipeline.SPEAKER_META):
        meta = json.load(open(pipeline.SPEAKER_META))
    return [{"name": k, "samples": v.get("count", 1)} for k, v in sorted(meta.items())]


@app.post("/api/label")
async def api_label(body: dict):
    """Name a diarized speaker. Enrolment is a running mean, not training."""
    clip, spk, name = body.get("clip"), body.get("speaker"), (body.get("name") or "").strip()
    if not (clip and spk and name):
        raise HTTPException(400, "need clip, speaker and name")
    tp = pipeline.transcript_path(clip)
    if not os.path.exists(tp):
        raise HTTPException(404, "clip not transcribed")
    t = json.load(open(tp))
    vec = (t.get("embeddings") or {}).get(spk)
    if vec is None:
        raise HTTPException(400, f"no voiceprint stored for {spk}")
    count = pipeline.save_speaker(name, vec)

    # Re-resolve names in this transcript so the UI updates immediately.
    emb = {k: __import__("numpy").asarray(v)
           for k, v in (t.get("embeddings") or {}).items()}
    t["speakers"] = pipeline.identify(emb)
    json.dump(t, open(tp, "w"), indent=2)
    device.event("log", text=f"labelled {spk} as {name} ({count} sample(s))")
    return {"name": name, "samples": count, "speakers": t["speakers"]}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    device.listeners.add(q)
    await sock.send_json({"type": "state", **device.state})

    async def pump():
        while True:
            await sock.send_json(await q.get())

    pumper = asyncio.create_task(pump())
    try:
        while True:
            msg = json.loads(await sock.receive_text())
            cmd = msg.get("cmd")
            if cmd == "connect":
                device.want(True)
            elif cmd == "disconnect":
                device.want(False)
            elif cmd == "arm":
                await device.set_armed(bool(msg.get("on", True)))
            elif cmd == "gain":
                await device.set_gain(int(msg.get("value", 50)))
            elif cmd == "vad":
                await device.set_vad(bool(msg.get("on", False)))
            elif cmd == "save":
                path = device.take_clip()
                device.event("log", text=f"saved {os.path.basename(path)}" if path
                             else "nothing to save")
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        device.listeners.discard(q)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

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


def _load_env_file():
    """Read .env into the environment if it is not already there.

    Speaker diarization needs HF_TOKEN, and it degrades silently without it:
    every segment comes back with speaker None and an empty speakers map, so
    the transcript looks fine and simply has nobody in it. Relying on the
    launching shell to export it meant restarting the server a different way
    turned diarization off with no error anywhere -- which is exactly what
    happened, for 83 clips, before anyone noticed.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
from ble_capture import (AUDIO_UUID, CTRL_UUID, INFO_UUID, DEVICE_NAME,
                         HEADER_LEN, decode_block)
from bleak import BleakClient, BleakScanner

import agent_runner
import index_db
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
            "clip_seconds": 0.0, "source": None,
            "recovered_seconds": 0.0, "backlog_mode": 0,
            "led_level": 255, "led_mode": 1,
            "ring_overruns": 0,
            "battery_mv": 0, "battery_pct": 0, "charging": False,
            "fast_charge": False, "mic_running": True,
        }
        self.relay: WebSocket | None = None
        self.listeners: set[asyncio.Queue] = set()
        self.client: BleakClient | None = None
        self._pcm: list[np.ndarray] = []
        # Frames recovered from flash arrive out of order relative to live
        # audio, so they are collected separately and written as their own
        # clip rather than spliced into the middle of a conversation.
        self._recovered: list[np.ndarray] = []
        self._recovered_at = 0.0
        self._recovered_start = None
        self._recovered_tms = None
        # Mapping between the device's uptime clock and wall time.
        self._clock_host = None
        self._clock_dev = 0
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
    def consume(self, data: bytes):
        """Ingest one wire frame, whoever delivered it: our own BLE link or a
        phone relaying on our behalf. The format is identical either way."""
        self._on_audio(None, data)

    def take_recovered(self):
        if not self._recovered:
            return None
        audio = np.concatenate(self._recovered)
        self._recovered = []
        self.state["recovered_seconds"] = 0.0
        os.makedirs(DATA, exist_ok=True)
        # Name it for when it was spoken, not when it reached us. The device
        # clock is converted here rather than on arrival, because the anchor
        # to our clock may only have been established after the first live
        # frame -- which can come well after the backlog started replaying.
        when = None
        if self._recovered_tms is not None and self._clock_host is not None:
            when = self._clock_host - (self._clock_dev - self._recovered_tms) / 1000.0
        if when is None:
            when = self._recovered_start or time.time()
        when = int(when)
        self._recovered_start = None
        self._recovered_tms = None
        path = os.path.join(DATA, f"recovered_{when}.wav")
        sf.write(path, audio, self.state["rate"], subtype="PCM_16")
        # Set the file's mtime too, since the UI orders and dates by it.
        #
        # It has to be the END of the audio, not the start. A live clip's
        # mtime is the moment it was saved, which is when its audio finished,
        # and everything downstream derives a start as mtime minus duration.
        # Stamping a recovered clip with its start put it a whole clip-length
        # too early and interleaved it wrongly with the live clips around it.
        end = when + len(audio) / float(self.state["rate"] or 1)
        try:
            os.utime(path, (end, end))
        except OSError:
            pass
        return path

    def maybe_rotate(self):
        """Close off a clip once it is long enough. Runs for every source, so
        relayed audio is segmented and transcribed exactly like local audio."""
        self.state["clip_seconds"] = round(self.clip_seconds(), 1)
        if self.clip_seconds() < SEG_SECONDS:
            return None
        path = self.take_clip()
        if path:
            name = os.path.basename(path)
            index_db.upsert_clip(name)
            self.event("clip", path=name)
            if auto_transcribe:
                worker.submit(name)
        return path

    def maybe_rotate_recovered(self):
        """Close a recovered clip once the flash has stopped feeding us, or
        once it has grown long enough to stand on its own."""
        if not self._recovered:
            return
        secs = sum(len(p) for p in self._recovered) / self.state["rate"]
        quiet_for = time.time() - self._recovered_at
        if secs < SEG_SECONDS and quiet_for < 3.0:
            return
        path = self.take_recovered()
        if path:
            name = os.path.basename(path)
            index_db.upsert_clip(name)
            self.event("clip", path=name)
            self.event("log", text=f"recovered {secs:.0f}s from device flash")
            if auto_transcribe:
                worker.submit(name)

    def _on_audio(self, _sender, data: bytearray):
        if len(data) < HEADER_LEN:
            return
        seq, flags, index, predictor, nsamples, t_ms = struct.unpack(
            "<HBBhHI", data[:HEADER_LEN])
        payload = data[HEADER_LEN:]
        if len(payload) < nsamples // 2:
            return
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) & 0xFFFF
            if gap and not (flags & 0x04):      # VAD gaps are intentional
                self.state["lost"] += gap
        self._last_seq = seq

        # Anchor the device clock to ours on the first live frame, so a frame
        # replayed from flash can be placed at the moment it was captured
        # rather than the moment it was recovered.
        if not (flags & 0x08):
            self._clock_host = time.time()
            self._clock_dev = t_ms

        pcm = decode_block(payload, predictor, index, nsamples)
        if flags & 0x08:                     # recovered from device flash
            self._recovered.append(pcm)
            self._recovered_at = time.time()
            # Keep the device's own timestamp and convert it later. A host
            # that connects to a device with a backlog already queued sees
            # replayed frames before any live one, so at this point there may
            # be no anchor between the two clocks yet -- and falling back to
            # "now" stamps recovered audio with the moment it drained, which
            # is exactly the error this is meant to avoid.
            self._recovered_tms = (t_ms if self._recovered_tms is None
                                   else min(self._recovered_tms, t_ms))
            self.state["recovered_seconds"] = round(
                sum(len(p) for p in self._recovered) / self.state["rate"], 1)
            return
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
            # Counters are per-session: sequence numbers restart at each
            # connection, so carrying them across made a reconnect look
            # like tens of thousands of lost frames.
            self._last_seq = None
            self.state.update(connected=True, error=None, source="ble",
                              frames=0, lost=0)
            await self._read_info(c)
            await c.start_notify(AUDIO_UUID, self._on_audio)
            await self.set_armed(True)
            self.publish()
            self.event("log", text="connected to device")

            last_info = time.time()
            while self._want and c.is_connected:
                await asyncio.sleep(0.25)
                if time.time() - last_info > 1.0:
                    last_info = time.time()
                    await self._read_info(c)
                    self.publish()

            self.client = None
            if self.state.get("source") == "ble":
                self.state.update(connected=False, source=None)
            self.publish()

    async def _read_info(self, c):
        info = await c.read_gatt_char(INFO_UUID)
        if len(info) >= 6:
            self.state["rate"] = 16000 if info[1] else 8000
        if len(info) >= 8:
            self.state["imu"] = info[6] != 0
        if len(info) >= 34:
            self.state["led_level"] = info[32]
            self.state["led_mode"] = info[33]
        if len(info) >= 38:
            mv = info[34] | (info[35] << 8)
            self.state["battery_mv"] = mv
            self.state["battery_pct"] = info[36]
            flags = info[37]
            self.state["charging"] = bool(flags & 1)
            self.state["fast_charge"] = bool(flags & 2)
            self.state["mic_running"] = bool(flags & 4)
        if len(info) >= 39:
            # Samples the microphone produced with nowhere to put them. Any
            # value above zero is audible as a click.
            self.state["ring_overruns"] = info[38]
        if len(info) >= 32:
            pend = info[28] | (info[29] << 8) | (info[30] << 16)
            self.state["backlog_bytes"] = pend
            self.state["backlog_seconds"] = round(pend / 4500.0, 1)
            self.state["qspi_mb"] = round(info[31] * 65536 / 1048576)

    async def _ctrl(self, op: int, arg: int):
        """Write to the device control characteristic over whichever link we
        have. A relay forwards the same two bytes over its own BLE handle."""
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(CTRL_UUID, bytes([op, arg]), response=True)
            return True
        if self.relay is not None:
            try:
                await self.relay.send_json({"type": "ctrl", "op": op, "arg": arg})
                return True
            except Exception:
                return False
        return False

    async def set_armed(self, on: bool):
        self.state["armed"] = bool(on)
        await self._ctrl(0x01, 1 if on else 0)
        self.publish()

    async def set_gain(self, g: int):
        if await self._ctrl(0x03, max(0, min(80, g))):
            self.event("log", text=f"gain set to {g}")

    async def set_led(self, level: int, pulse: bool):
        self.state["led_level"] = max(0, min(255, int(level)))
        self.state["led_mode"] = 1 if pulse else 0
        await self._ctrl(0x0A, self.state["led_level"])
        await self._ctrl(0x0B, self.state["led_mode"])
        self.publish()

    async def set_fast_charge(self, on: bool):
        if await self._ctrl(0x0C, 1 if on else 0):
            self.event("log", text=f"charge current: {'100' if on else '50'} mA")

    async def set_mic_power_save(self, on: bool):
        if await self._ctrl(0x0D, 1 if on else 0):
            self.event("log", text=f"mic power saving {'on' if on else 'off'}")

    async def clear_buffer(self):
        if await self._ctrl(0x08, 1):
            self.event("log", text="discarded the device buffer")

    async def set_backlog_mode(self, live_first: bool):
        self.state["backlog_mode"] = 1 if live_first else 0
        if await self._ctrl(0x09, 1 if live_first else 0):
            self.event("log", text="backlog: " +
                       ("live first, recover alongside" if live_first
                        else "drain before live audio"))
        self.publish()

    async def set_vad(self, on: bool):
        if await self._ctrl(0x04, 1 if on else 0):
            self.event("log", text=f"VAD {'on' if on else 'off'}")

    def want(self, on: bool):
        self._want = bool(on)
        if not on:
            self.state["connected"] = False
        self.publish()


auto_transcribe = True
device = Device()
agent = agent_runner.ConversationAgent(
    notify=lambda kind, **kw: device.event(kind, **kw))
worker = pipeline.Worker(
    notify=lambda kind, **kw: device.event(kind, **kw),
    on_transcript=agent.add)


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
            # Show who was talking, not diarization's internal SPEAKER_xx ids.
            resolved = t.get("speakers") or {}
            seen = []
            for x in segs:
                sp = x.get("speaker")
                if not sp:
                    continue
                nm = (resolved.get(sp) or {}).get("name") or "unknown"
                if nm not in seen:
                    seen.append(nm)
            speakers = seen
        except Exception:
            status = "error"
    if worker.busy == name:
        status = "running"
    edited = False
    if os.path.exists(tp):
        try:
            edited = bool(json.load(open(tp)).get("edited"))
        except Exception:
            pass
    return {"name": name, "seconds": dur,
            "modified": os.path.getmtime(path),
            "status": status, "preview": preview, "speakers": speakers,
            "edited": edited,
            # None until transcribed -- "no speech found" and "not looked at
            # yet" are different things and the filter must not conflate them.
            "has_speech": (bool(preview.strip()) if status == "done" else None)}


async def rotator():
    """Segment whatever is arriving, from any source."""
    while True:
        await asyncio.sleep(0.5)
        try:
            device.maybe_rotate()
            device.maybe_rotate_recovered()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        st = index_db.sync()   # files on disk are authoritative
        print(f"index: {st}", flush=True)
    except Exception as e:
        print(f"index sync failed: {e}", flush=True)
    device.want(True)          # start looking for the board immediately
    tasks = [asyncio.create_task(device.run()), asyncio.create_task(rotator())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

# Optional shared secret. Unset means open, which is fine on a trusted LAN.
# Set BOSWELL_TOKEN before exposing this anywhere else: every endpoint below
# can arm the microphone or hand over recordings.
TOKEN = os.environ.get("BOSWELL_TOKEN", "").strip()


def token_ok(supplied: str | None) -> bool:
    if not TOKEN:
        return True
    if not supplied:
        return False
    # Constant-time compare so the token cannot be recovered by timing.
    import hmac
    return hmac.compare_digest(supplied, TOKEN)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """The UI changes constantly during development. Without this the browser
    caches index.html indefinitely and a refresh silently serves the old
    copy -- edits appear to do nothing."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static") or path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # The shell and its assets stay public so the token prompt can render.
    if not TOKEN or path == "/" or path.startswith("/static"):
        return await call_next(request)
    supplied = (request.query_params.get("token")
                or request.headers.get("x-boswell-token")
                or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                or request.cookies.get("boswell_token"))
    if not token_ok(supplied):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth")
async def api_auth():
    """Reached only when the token is valid, so a 200 means 'you are in'."""
    return {"ok": True, "auth_required": bool(TOKEN)}
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
async def index():
    """Serve the shell with a build stamp injected.

    A stale cached copy is indistinguishable from a current one otherwise,
    which wastes real time chasing changes that did in fact ship. The stamp
    is the file's own mtime, so it moves whenever the UI does.
    """
    path = os.path.join(HERE, "static", "index.html")
    html = open(path, encoding="utf-8").read()
    build = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(path)))
    html = html.replace("__BUILD__", build)
    return Response(content=html, media_type="text/html",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


ENVELOPES = os.path.join(DATA, "envelopes")


@app.get("/api/envelope/{name}")
async def api_envelope(name: str):
    """Per-frame loudness for the waveform view. Cached: the arithmetic is
    cheap but re-reading a WAV on every scrub is not."""
    if "/" in name or not name.endswith(".wav"):
        raise HTTPException(400, "bad name")
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        raise HTTPException(404, "no such clip")

    os.makedirs(ENVELOPES, exist_ok=True)
    cache = os.path.join(ENVELOPES, name + ".json")
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        return JSONResponse(json.load(open(cache)))

    audio, rate = sf.read(path, dtype="int16")
    if audio.ndim > 1:
        audio = audio[:, 0]
    n = max(1, int(rate * 0.02))                     # 20 ms, same as the firmware
    usable = len(audio) // n * n
    if usable == 0:
        out = {"env": [], "seconds": 0.0, "peak": 0}
    else:
        frames = audio[:usable].astype(np.float64).reshape(-1, n)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        # Reduce to something a phone can draw without janking.
        target = 400
        k = max(1, len(rms) // target)
        rms = rms[: len(rms) // k * k].reshape(-1, k).max(axis=1)
        # Normalise against a high percentile, not the max: one clipping
        # transient (a tap on the case) would otherwise scale every voice
        # down to a flat line. Values above the reference clamp to 1.
        ref = float(np.percentile(rms, 95)) or float(rms.max()) or 1.0
        ref = max(ref, 1.0)
        out = {"env": [round(min(1.0, float(v) / ref), 3) for v in rms],
               "seconds": round(len(audio) / rate, 2),
               "peak": int(np.abs(audio).max())}
    json.dump(out, open(cache, "w"))
    return JSONResponse(out)


@app.post("/api/transcribe_all")
async def api_transcribe_all():
    """Queue every clip that has no transcript yet."""
    os.makedirs(DATA, exist_ok=True)
    queued = []
    for f in sorted(os.listdir(DATA)):
        if not f.endswith(".wav"):
            continue
        tp = pipeline.transcript_path(f)
        if os.path.exists(tp):
            continue
        worker.submit(f)
        queued.append(f)
    device.event("log", text=f"queued {len(queued)} clip(s) for transcription")
    return {"queued": len(queued)}


@app.get("/api/queue")
async def api_queue():
    return {"pending": worker.q.qsize(), "busy": worker.busy,
            "auto": auto_transcribe}


@app.get("/api/clips")
async def api_clips(limit: int = 1000):
    """Served from the index. Reading every transcript per request did not
    scale past a few hundred clips."""
    rows = index_db.list_clips(limit)
    # A clip currently being transcribed is not yet reflected on disk.
    if worker.busy:
        for r in rows:
            if r["name"] == worker.busy:
                r["status"] = "running"
    return rows


@app.get("/api/search")
async def api_search(q: str, limit: int = 200):
    """Full text across every segment, with the matching lines returned."""
    if not q.strip():
        return []
    return index_db.search(q, limit)


@app.get("/api/conversations")
async def api_conversations(gap: int = 300, limit: int = 400):
    return index_db.conversations(gap, limit)


@app.get("/api/export/{name}")
async def api_export(name: str, format: str = "txt"):
    """Get a transcript out in a form other tools can read."""
    tp = pipeline.transcript_path(name)
    if not os.path.exists(tp):
        raise HTTPException(404, "not transcribed")
    t = json.load(open(tp))
    resolved = t.get("speakers") or {}

    def who(seg):
        return (seg.get("speaker_name")
                or (resolved.get(seg.get("speaker")) or {}).get("name")
                or seg.get("speaker") or "UNKNOWN")

    if format == "json":
        return JSONResponse(t)

    segs = t.get("segments", [])
    if format == "srt":
        def ts(x):
            h, r = divmod(x, 3600); m, sec = divmod(r, 60)
            return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{int((sec%1)*1000):03d}"
        body = "\n".join(
            f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{who(s)}: {s['text']}\n"
            for i, s in enumerate(segs, 1))
        media = "text/plain"
    else:
        body = "\n".join(f"[{s['start']:.0f}s] {who(s)}: {s['text']}" for s in segs)
        media = "text/plain"
    return Response(content=body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{os.path.splitext(name)[0]}.{format}"'})


@app.post("/api/export")
async def api_export_many(body: dict):
    """Several clips at once, as one document in time order."""
    names = body.get("names") or []
    fmt = body.get("format", "txt")
    parts = []
    for name in sorted(names, key=lambda n: os.path.getmtime(os.path.join(DATA, n))
                       if os.path.exists(os.path.join(DATA, n)) else 0):
        tp = pipeline.transcript_path(name)
        if not os.path.exists(tp):
            continue
        t = json.load(open(tp))
        resolved = t.get("speakers") or {}
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(os.path.getmtime(os.path.join(DATA, name))))
        parts.append(f"# {when}  ({name})")
        for s in t.get("segments", []):
            nm = (s.get("speaker_name")
                  or (resolved.get(s.get("speaker")) or {}).get("name")
                  or s.get("speaker") or "UNKNOWN")
            parts.append(f"[{s['start']:.0f}s] {nm}: {s['text']}")
        parts.append("")
    if fmt == "json":
        return JSONResponse({"clips": names, "text": "\n".join(parts)})
    return Response(content="\n".join(parts), media_type="text/plain",
                    headers={"Content-Disposition": 'attachment; filename="boswell-export.txt"'})


@app.get("/api/index")
async def api_index():
    return index_db.stats()


@app.post("/api/index/rebuild")
async def api_index_rebuild():
    return index_db.sync()


@app.get("/api/audio/{name}")
async def api_audio(name: str):
    if "/" in name or not name.endswith(".wav"):
        raise HTTPException(400, "bad name")
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/transcribe/{name}")
async def api_transcribe(name: str, force: bool = False):
    if "/" in name or not os.path.exists(os.path.join(DATA, name)):
        raise HTTPException(404, "no such clip")
    tp = pipeline.transcript_path(name)
    if os.path.exists(tp) and not force:
        try:
            if json.load(open(tp)).get("edited"):
                raise HTTPException(
                    409, "this transcript has your corrections in it — "
                         "re-transcribing would overwrite them")
        except HTTPException:
            raise
        except Exception:
            pass
    worker.submit(name)
    return {"queued": name}


@app.get("/api/transcript/{name}")
async def api_transcript(name: str):
    tp = pipeline.transcript_path(name)
    if not os.path.exists(tp):
        raise HTTPException(404, "not transcribed yet")
    return JSONResponse(json.load(open(tp)))


@app.post("/api/conversation")
async def api_conversation(body: dict):
    """Every transcribed line of one conversation, in order.

    A thirty-second clip is a storage unit. What someone wants to read -- and
    what the agent is handed when it summarises -- is the whole conversation,
    so opening one used to land on its first clip and leave the rest
    unreachable without clicking through parts one at a time.

    Lines keep their clip and their offset inside it, so the reader can jump
    straight to the audio for any line without the caller having to work out
    which clip a moment belongs to.
    """
    names = body.get("names") or []
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "need a list of clip names")

    segments, clips = [], []
    speakers: dict = {}
    missing = 0

    for name in names:
        if "/" in name or not name.endswith(".wav"):
            raise HTTPException(400, f"bad name: {name}")
        tp = pipeline.transcript_path(name)
        wav = os.path.join(DATA, name)
        started = os.path.getmtime(wav) if os.path.exists(wav) else 0
        dur = 0.0
        try:
            import wave as _wave
            with _wave.open(wav) as w:
                dur = w.getnframes() / float(w.getframerate() or 1)
        except Exception:
            dur = 0.0
        if not os.path.exists(tp):
            missing += 1
            clips.append({"name": name, "started": started, "seconds": round(dur, 2),
                          "transcribed": False})
            continue

        t = json.load(open(tp))
        segs = t.get("segments", [])
        clips.append({"name": name, "started": started, "seconds": round(dur, 2),
                      "transcribed": True, "lines": len(segs)})

        # Merge speaker records. A later clip's score wins only if it is more
        # confident, so one weak match cannot rename a whole conversation.
        for sid, rec in (t.get("speakers") or {}).items():
            cur = speakers.get(sid)
            if cur is None or (rec.get("score") or 0) > (cur.get("score") or 0):
                speakers[sid] = rec

        for i, seg in enumerate(segs):
            segments.append({
                "clip": name,
                "index": i,
                "start": seg.get("start", 0.0),
                "end": seg.get("end", 0.0),
                "text": seg.get("text", ""),
                "speaker": seg.get("speaker"),
                "speaker_name": seg.get("speaker_name"),
                "edited": bool(seg.get("edited")),
            })

    return {"clips": clips, "segments": segments, "speakers": speakers,
            "not_transcribed": missing}


@app.delete("/api/clip/{name}")
async def api_delete(name: str):
    if "/" in name or not name.endswith(".wav"):
        raise HTTPException(400, "bad name")
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        raise HTTPException(404, "no such clip")
    removed = []
    for f in (path, pipeline.transcript_path(name),
              os.path.join(ENVELOPES, name + ".json")):
        if os.path.exists(f):
            os.remove(f)
            removed.append(os.path.basename(f))
    index_db.remove_clip(name)
    device.event("log", text=f"deleted {name}")
    return {"deleted": name, "files": removed}


@app.post("/api/clips/delete")
async def api_delete_many(body: dict):
    """Delete a set of clips with their transcripts and cached waveforms.

    Voiceprints live in their own files and are untouched -- clearing out
    recordings should not cost you the people you have enrolled.
    """
    names = body.get("names") or []
    if not isinstance(names, list):
        raise HTTPException(400, "names must be a list")
    removed, missing = [], []
    for name in names:
        if "/" in name or not name.endswith(".wav"):
            continue
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            missing.append(name)
            continue
        for f in (path, pipeline.transcript_path(name),
                  os.path.join(ENVELOPES, name + ".json")):
            if os.path.exists(f):
                os.remove(f)
        index_db.remove_clip(name)
        removed.append(name)
    device.event("log", text=f"deleted {len(removed)} recording(s)")
    return {"deleted": len(removed), "missing": len(missing)}


@app.post("/api/split/{name}")
async def api_split(name: str):
    """Extract one clip per speaker from a diarized recording.

    Each speaker's segments are concatenated into their own file, giving a
    clean single-voice clip. That matters because enrolment quality is what
    limits speaker identification: a voiceprint built from 20 seconds of one
    person is far stronger than one built from a clip where two people
    overlap.
    """
    if "/" in name or not name.endswith(".wav"):
        raise HTTPException(400, "bad name")
    src = os.path.join(DATA, name)
    tp = pipeline.transcript_path(name)
    if not os.path.exists(src):
        raise HTTPException(404, "no such clip")
    if not os.path.exists(tp):
        raise HTTPException(400, "transcribe the clip first — the split follows "
                                 "the diarized speaker turns")

    t = json.load(open(tp))
    segs = [x for x in t.get("segments", []) if x.get("speaker")]
    speakers = sorted({x["speaker"] for x in segs})
    if len(speakers) < 2:
        raise HTTPException(400, f"only one speaker in this clip ({len(speakers)} found)")

    audio, rate = sf.read(src, dtype="int16")
    if audio.ndim > 1:
        audio = audio[:, 0]

    base = os.path.splitext(name)[0]
    made = []
    for spk in speakers:
        parts = []
        for x in segs:
            if x["speaker"] != spk:
                continue
            a = max(0, int(x["start"] * rate))
            b = min(len(audio), int(x["end"] * rate))
            if b > a:
                parts.append(audio[a:b])
        if not parts:
            continue
        out_name = f"{base}__{spk}.wav"
        sf.write(os.path.join(DATA, out_name), np.concatenate(parts), rate,
                 subtype="PCM_16")

        # Carry over this speaker's lines and voiceprint so the new clip is
        # immediately nameable without re-running the models.
        offset, new_segs = 0.0, []
        for x in segs:
            if x["speaker"] != spk:
                continue
            dur = max(0.0, x["end"] - x["start"])
            new_segs.append({"start": round(offset, 2), "end": round(offset + dur, 2),
                             "speaker": spk, "text": x["text"]})
            offset += dur
        emb = t.get("embeddings", {}).get(spk)
        json.dump({"clip": out_name, "created": time.time(), "segments": new_segs,
                   "speakers": {spk: (t.get("speakers", {}).get(spk)
                                      or {"name": None, "score": 0.0})},
                   "embeddings": {spk: emb} if emb else {}},
                  open(pipeline.transcript_path(out_name), "w"), indent=2)
        made.append({"name": out_name, "speaker": spk,
                     "seconds": round(sum(len(p) for p in parts) / rate, 1)})

    device.event("log", text=f"split {name} into {len(made)} single-voice clips")
    return {"source": name, "clips": made}


@app.patch("/api/transcript/{name}")
async def api_edit_transcript(name: str, body: dict):
    """Correct a line of transcript, or reassign it to a different speaker.

    Editing text is safe: voiceprints come from audio embeddings, and the
    waveform and split both key off timings, so none of them care what the
    words say. The one thing it would break is re-transcription silently
    overwriting the correction, so an edited transcript is marked and the
    bulk transcribe skips it.
    """
    tp = pipeline.transcript_path(name)
    if not os.path.exists(tp):
        raise HTTPException(404, "not transcribed yet")
    idx = body.get("index")
    if not isinstance(idx, int):
        raise HTTPException(400, "need a segment index")

    t = json.load(open(tp))
    segs = t.get("segments", [])
    if not (0 <= idx < len(segs)):
        raise HTTPException(400, "no such segment")

    seg = segs[idx]
    if "text" in body:
        new = (body["text"] or "").strip()
        if new != seg.get("text"):
            # Keep the machine's version so a correction can be compared or undone.
            seg.setdefault("text_asr", seg.get("text", ""))
            seg["text"] = new
            seg["edited"] = True
    if "speaker" in body and body["speaker"]:
        if body["speaker"] != seg.get("speaker"):
            seg.setdefault("speaker_asr", seg.get("speaker"))
            seg["speaker"] = body["speaker"]
            seg["edited"] = True
    if "speaker_name" in body:
        # A name pinned to one line only. Deliberately does NOT touch the
        # voiceprint database: an embedding describes a whole diarized cluster,
        # not a single line, so enrolling from here would teach the wrong
        # thing. Use the speaker chip when a name should apply to every line.
        nm = (body["speaker_name"] or "").strip()
        if nm:
            seg["speaker_name"] = nm
        else:
            seg.pop("speaker_name", None)
        seg["edited"] = True

    t["edited"] = True
    json.dump(t, open(tp, "w"), indent=2)
    index_db.upsert_clip(name)
    device.event("log", text=f"edited {name} line {idx}")
    return {"ok": True, "segment": seg, "edited": True}


@app.delete("/api/transcript/{name}/edits")
async def api_revert_edits(name: str):
    """Put every corrected line back to what the model originally produced."""
    tp = pipeline.transcript_path(name)
    if not os.path.exists(tp):
        raise HTTPException(404, "not transcribed yet")
    t = json.load(open(tp))
    n = 0
    for seg in t.get("segments", []):
        if seg.pop("edited", None):
            n += 1
            if "text_asr" in seg:
                seg["text"] = seg.pop("text_asr")
            if "speaker_asr" in seg:
                seg["speaker"] = seg.pop("speaker_asr")
            seg.pop("speaker_name", None)
    t["edited"] = False
    json.dump(t, open(tp, "w"), indent=2)
    return {"reverted": n}


@app.get("/api/agent")
async def api_agent_status():
    st = agent.status()
    st["last_result"] = agent.last_result
    return st


@app.post("/api/agent")
async def api_agent_config(body: dict):
    if "enabled" in body:
        agent.enabled = bool(body["enabled"])
        device.event("log", text=f"agent {'on' if agent.enabled else 'off'}")
    if "model" in body and body["model"]:
        agent.model = str(body["model"])
    if "idle_seconds" in body:
        agent.idle_seconds = max(10.0, float(body["idle_seconds"]))
    return agent.status()


@app.post("/api/agent/run")
async def api_agent_run():
    """Stop waiting for silence and review what has accumulated now."""
    if not agent.pending_chars():
        raise HTTPException(400, "nothing waiting to be reviewed")
    agent.flush_now()
    return {"ok": True, "pending_chars": agent.pending_chars()}


@app.get("/api/agent/items")
async def api_agent_items(kind: str | None = None, limit: int = 200):
    return agent_runner.load_items(kind, limit)


@app.delete("/api/agent/item/{kind}/{item_id}")
async def api_delete_item(kind: str, item_id: str):
    if kind not in ("tasks", "events", "notes", "facts"):
        raise HTTPException(400, "unknown kind")
    if not agent_runner.delete_item(kind, item_id):
        raise HTTPException(404, "no such item")
    return {"ok": True}


@app.delete("/api/agent/items")
async def api_clear_items(kind: str | None = None):
    n = agent_runner.clear_items(kind)
    device.event("log", text=f"cleared {n} agent item(s)")
    return {"cleared": n}


@app.get("/api/models")
async def api_models():
    """Ollama models that support tool calling."""
    try:
        import requests as rq
        r = rq.get("http://localhost:11434/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return {"models": [], "error": "ollama not reachable"}
    return {"models": sorted(names)}


@app.get("/api/speakers")
async def api_speakers():
    return pipeline.list_speakers()


@app.get("/api/vocabulary")
async def api_get_vocab():
    stored = []
    if os.path.exists(pipeline.VOCAB_PATH):
        try:
            stored = json.load(open(pipeline.VOCAB_PATH)).get("terms", [])
        except Exception:
            stored = []
    return {"terms": stored, "effective": pipeline.load_vocabulary()}


@app.put("/api/vocabulary")
async def api_put_vocab(body: dict):
    terms = pipeline.save_vocabulary(body.get("terms") or [])
    device.event("log", text=f"word list saved ({len(terms)} term(s))")
    return {"terms": terms, "effective": pipeline.load_vocabulary()}


@app.delete("/api/speaker/{name}")
async def api_delete_speaker(name: str):
    if not pipeline.delete_speaker(name):
        raise HTTPException(404, "no such person")
    device.event("log", text=f"removed {name}")
    return {"deleted": name}


@app.delete("/api/speaker/{name}/sample/{sample_id}")
async def api_delete_sample(name: str, sample_id: str):
    """Drop one enrolment sample and rebuild the reference without it."""
    if not pipeline.delete_sample(name, sample_id):
        raise HTTPException(404, "no such sample")
    device.event("log", text=f"removed a sample from {name}")
    return {"ok": True}


@app.post("/api/label")
async def api_label(body: dict):
    """Put a name to a diarized speaker.

    Naming and enrolling are separate steps. The name is applied
    unconditionally -- saying who someone is should never fail. Adding their
    audio to the voiceprint is the second step and keeps its quality checks;
    if the audio is unsuitable the name still sticks and the reason is
    reported. Previously a poor sample rejected the whole request, which made
    a speaker impossible to label at all.
    """
    clip, spk, name = body.get("clip"), body.get("speaker"), (body.get("name") or "").strip()
    if not (clip and spk and name):
        raise HTTPException(400, "need clip, speaker and name")
    tp = pipeline.transcript_path(clip)
    if not os.path.exists(tp):
        raise HTTPException(404, "clip not transcribed")
    t = json.load(open(tp))

    # 1. The label, unconditionally.
    entry = t.setdefault("speakers", {}).setdefault(spk, {})
    entry["name"] = name
    entry["manual"] = True
    entry.setdefault("score", 0.0)
    # A name set by hand is a correction like any other, so it gets the same
    # protection: bulk transcription skips this clip and re-transcribing it
    # asks first, instead of silently discarding the name.
    t["edited"] = True

    # 2. The voiceprint, only if this audio is worth learning from.
    enrolled, reason, count = False, None, None
    vec = (t.get("embeddings") or {}).get(spk)
    if vec is None:
        reason = "no voiceprint was extracted for this speaker"
    else:
        secs = sum(x["end"] - x["start"]
                   for x in t.get("segments", []) if x.get("speaker") == spk)
        res = pipeline.save_speaker(name, vec, clip=clip, speaker=spk,
                                    seconds=secs, force=bool(body.get("force")))
        if res.get("ok"):
            enrolled, count = True, res["count"]
        else:
            reason = res.get("detail")

    # Re-resolve everyone else, leaving names set by hand alone.
    emb = {k: np.asarray(v) for k, v in (t.get("embeddings") or {}).items()}
    for k, v in pipeline.identify(emb).items():
        if not (t["speakers"].get(k) or {}).get("manual"):
            t["speakers"][k] = v
    json.dump(t, open(tp, "w"), indent=2, allow_nan=False)
    index_db.upsert_clip(clip)

    device.event("log", text=(f"named {spk} as {name}"
                              + (f", voiceprint now {count} sample(s)" if enrolled
                                 else " (voiceprint unchanged)")))
    return {"named": True, "name": name, "enrolled": enrolled,
            "samples": count, "reason": reason, "speakers": t["speakers"]}


@app.websocket("/ingest")
async def ingest(sock: WebSocket):
    """Audio in from a phone relaying the board's BLE stream, control out.

    Binary messages are wire frames, byte-identical to what the board sends
    over GATT -- the relay never decodes or re-encodes, so a dropped frame
    still costs exactly one frame and nothing downstream can tell the
    difference between relayed and local audio.

    Text messages are JSON status from the relay. The server replies with
    {"type":"ctrl","op":..,"arg":..} for the relay to write to the board's
    control characteristic.
    """
    if not token_ok(sock.query_params.get("token")):
        await sock.close(code=1008)
        return
    await sock.accept()

    device.relay = sock
    device.state.update(connected=True, source="relay", error=None,
                        frames=0, lost=0)
    device._last_seq = None
    device.publish()
    device.event("log", text="relay connected")

    try:
        while True:
            msg = await sock.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (b := msg.get("bytes")) is not None:
                device.consume(b)
            elif (t := msg.get("text")) is not None:
                try:
                    info = json.loads(t)
                except Exception:
                    continue
                if info.get("type") == "status":
                    for k in ("rate", "backlog_bytes", "backlog_seconds",
                              "qspi_mb", "imu", "armed"):
                        if k in info:
                            device.state[k] = info[k]
                    device.publish()
    except WebSocketDisconnect:
        pass
    finally:
        device.relay = None
        if device.state.get("source") == "relay":
            device.state.update(connected=False, source=None)
        device.publish()
        device.event("log", text="relay disconnected")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not token_ok(sock.query_params.get("token")):
        await sock.close(code=1008)
        return
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
            elif cmd == "clear_buffer":
                await device.clear_buffer()
            elif cmd == "fast_charge":
                await device.set_fast_charge(bool(msg.get("on")))
            elif cmd == "mic_power_save":
                await device.set_mic_power_save(bool(msg.get("on")))
            elif cmd == "led":
                await device.set_led(int(msg.get("level", 255)),
                                     bool(msg.get("pulse")))
            elif cmd == "backlog_mode":
                await device.set_backlog_mode(bool(msg.get("live_first")))
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

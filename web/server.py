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
import secrets
from urllib.parse import urlparse
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

import atomicio
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
# When each recording actually happened, according to the device's own clock.
# Kept beside the audio rather than inferred from file metadata, which can be
# rewritten by a copy, a backup or a sync -- and was, three times tonight.
TIMES = os.path.join(DATA, "times")
SEG_SECONDS = 30.0          # write a clip this long, then hand it to the pipeline


def safe_clip(name):
    """Validate a caller-supplied clip name and return its path.

    Eight endpoints each wrote their own version of this, and they had
    drifted: most required a .wav extension, one checked only for a slash,
    and none of them resolved the result to prove it lands in DATA. A single
    resolver means a new endpoint cannot quietly get it wrong.
    """
    if not name or os.path.basename(name) != name or not name.endswith(".wav"):
        raise HTTPException(400, "bad name")
    path = os.path.abspath(os.path.join(DATA, name))
    if os.path.dirname(path) != os.path.abspath(DATA):
        raise HTTPException(400, "bad name")
    return path


PREFS_PATH = os.path.join(DATA, "prefs.json")

# What the service remembers across restarts.
#
# Three separate things were being conflated. The device keeps its own
# settings in flash and restores them on boot. The browser has display
# choices -- grouping, filters, search mode -- that are per-browser and
# belong in the browser. And in between sits what this service intends the
# device to be doing, which was held only in memory.
#
# That last one was not merely forgotten on restart, it was actively harmful:
# the connect path asserts the settings this service intends, and with
# nothing remembered it asserted the defaults -- so restarting the service
# turned the voice gate off on a device that had correctly remembered it was
# on. Worse than not remembering.
PREF_KEYS = ("armed", "vad", "backlog_mode", "gain", "led_level", "led_mode",
             "fast_charge", "mic_power_save", "rate16",
             "agent_enabled", "agent_model", "agent_idle_seconds")


def load_prefs():
    try:
        with open(PREFS_PATH) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if k in PREF_KEYS}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # A corrupt file must not stop the recorder starting.
        print(f"prefs unreadable ({e}); using defaults", flush=True)
        return {}


def save_prefs(d):
    keep = {k: v for k, v in d.items() if k in PREF_KEYS}
    try:
        atomicio.write_json(PREFS_PATH, keep, indent=2)
    except Exception as e:
        print(f"could not save prefs: {e}", flush=True)


PREFS = load_prefs()


def parse_info(info):
    """Decode the 40-byte info characteristic into a state dictionary.

    Module level and free of any device, so both firmware layouts can be fed
    through the real parser in a test. The two builds disagree about what
    bytes 13-26 mean and about which optional fields exist at all, and every
    bug this has produced was a host reading one layout as the other.
    """
    out = {}

    # Capabilities first.
    #
    # These say which optional fields this firmware actually fills in, and
    # they used to be parsed last -- so every field that depends on them was
    # decoded using the capabilities from the *previous* read. On the first
    # read after connecting that meant the defaults; across a firmware change
    # it meant the other build's layout.
    caps, fw, version = 0, None, None
    if len(info) >= 22:
        version = info[18]
        fw = {1: "arduino", 2: "zephyr"}.get(info[19])
        caps = info[20] | (info[21] << 8)
    has_steps = bool(caps & 0x0001)
    has_overruns = bool(caps & 0x0020)
    has_state = bool(caps & 0x0040)
    has_bootid = bool(caps & 0x0080)
    out["boot_id"] = (info[22] | (info[23] << 8)) if (
        has_bootid and len(info) >= 24) else None
    out["info_version"] = version
    out["firmware"] = fw
    out["caps"] = caps
    out["has_steps"] = has_steps
    out["has_overruns"] = has_overruns

    if len(info) >= 6:
        out["rate"] = 16000 if info[1] else 8000
        # What the device is actually doing, as opposed to what this process
        # last asked for. They drift apart: the device boots with capture off
        # after a firmware update, and a host that only remembers having armed
        # it once will sit there believing it is recording while nothing is.
        #
        # Only where the firmware says the bit means something. A build that
        # never sets it reads as "not capturing" forever, and the caller then
        # re-sent the stream command every second -- which on the Arduino
        # build discards the microphone ring, so the fix for one silent
        # failure was causing a louder one.
        out["device_streaming"] = bool(info[5] & 4) if has_state else None
    if len(info) >= 8:
        out["imu"] = info[6] != 0
    if len(info) >= 18:
        # Steps and motion from the IMU's own embedded functions. On the
        # Arduino build the same bytes are tap diagnostics.
        if has_steps:
            out["steps"] = (info[13] | (info[14] << 8)
                            | (info[15] << 16) | (info[16] << 24))
            mflags = info[17]
            out["tilt"] = bool(mflags & 1)
            out["moving"] = bool(mflags & 2)
            out["tap_enabled"] = bool(mflags & 4)
        else:
            out["tilt"] = out["moving"] = out["tap_enabled"] = None
    if len(info) >= 32:
        pend = info[28] | (info[29] << 8) | (info[30] << 16)
        out["backlog_bytes"] = pend
        out["backlog_seconds"] = round(pend / 4500.0, 1)
        out["qspi_mb"] = round(info[31] * 65536 / 1048576)
    if len(info) >= 34:
        out["led_level"] = info[32]
        out["led_mode"] = info[33]
    if len(info) >= 38:
        out["battery_mv"] = info[34] | (info[35] << 8)
        out["battery_pct"] = info[36]
        flags = info[37]
        out["charging"] = bool(flags & 1)
        out["fast_charge"] = bool(flags & 2)
        out["mic_running"] = bool(flags & 4)
    if len(info) >= 39:
        # Samples the microphone produced with nowhere to put them. Any value
        # above zero is audible as a click. Only meaningful on a firmware that
        # says it keeps the counter.
        out["ring_overruns"] = info[38] if has_overruns else None
    return out


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
            "steps": 0, "tilt": False, "moving": False, "tap_enabled": True,
            "led_level": 255, "led_mode": 1,
            "ring_overruns": 0,
            "battery_mv": 0, "battery_pct": 0, "charging": False,
            "fast_charge": False, "mic_running": True,
            "vad": False,
        }
        # Anything remembered from a previous run wins over the defaults
        # above, so a restart resumes rather than resets.
        for k in ("vad", "backlog_mode", "led_level", "led_mode"):
            if k in PREFS:
                self.state[k] = PREFS[k]
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
        # The device stamps every frame with its own clock. Keeping the first
        # and last of those for each clip makes the recording itself say when
        # it happened, instead of the host inferring it from a file timestamp
        # and a duration -- an inference that broke three different ways.
        self._clip_tms_first = None
        self._clip_tms_last = None
        self._rec_tms_first = None
        self._rec_tms_last = None
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

    def _save_wav(self, prefix, when, audio, rate):
        """Write one clip, then hand back its path.

        Three things this has to get right, each of which was wrong:

        A name that cannot collide. Names carried a whole-second timestamp, so
        two clips finalized in the same second silently overwrote one another.

        A file that is either complete or absent. The WAV went straight to its
        final name, so an interrupted write left a truncated file that looks
        like a recording and indexes like one.

        Audio that survives a failed write. take_clip() emptied the buffer
        before writing, so anything that went wrong during the write took the
        only copy with it. The caller now clears its buffer only after this
        returns.
        """
        os.makedirs(DATA, exist_ok=True)
        name = f"{prefix}_{int(when)}"
        path = os.path.join(DATA, name + ".wav")
        n = 1
        while os.path.exists(path):
            path = os.path.join(DATA, f"{name}-{n}.wav")
            n += 1

        tmp = path + ".part"
        try:
            # format is explicit: soundfile infers it from the extension, and
            # the temporary name ends in .part.
            sf.write(tmp, audio, rate, format="WAV", subtype="PCM_16")
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return path

    def take_recovered(self):
        if not self._recovered:
            return None
        audio = np.concatenate(self._recovered)
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
        path = self._save_wav("recovered", when, audio, self.state["rate"])
        # Only now is the audio somewhere other than memory.
        self._recovered = []
        self.state["recovered_seconds"] = 0.0
        self._recovered_start = None
        self._recovered_tms = None
        # Set the file's mtime too, since the UI orders and dates by it.
        #
        # It has to be the END of the audio, not the start. A live clip's
        # mtime is the moment it was saved, which is when its audio finished,
        # and everything downstream derives a start as mtime minus duration.
        # Stamping a recovered clip with its start put it a whole clip-length
        # too early and interleaved it wrongly with the live clips around it.
        secs = len(audio) / float(self.state["rate"] or 1)
        end = when + secs
        try:
            os.utime(path, (end, end))
        except OSError:
            pass
        self._write_times(path, self._rec_tms_first, self._rec_tms_last,
                          "flash", secs)
        self._rec_tms_first = self._rec_tms_last = None
        return path

    def maybe_rotate(self):
        """Close off a clip once it is long enough. Runs for every source, so
        relayed audio is segmented and transcribed exactly like local audio."""
        self.state["clip_seconds"] = round(self.clip_seconds(), 1)
        if self.clip_seconds() < SEG_SECONDS:
            return None
        return self.finalize(self.take_clip())

    def finalize(self, path, note=None):
        """Index a finished clip, announce it, and queue transcription.

        One function for every way a clip can be closed. The WebSocket save
        command wrote the file and stopped there -- no index row, no clip
        event, no transcription -- so a manually saved recording was invisible
        in the interface that had just been asked to save it.
        """
        if not path:
            return None
        name = os.path.basename(path)
        index_db.upsert_clip(name)
        self.event("clip", path=name)
        if note:
            self.event("log", text=note)
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
        self.finalize(self.take_recovered(),
                      note=f"recovered {secs:.0f}s from device flash")

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
            if not self._recovered:
                # When the backlog started arriving. Used only to name a
                # recovered clip that has no device-clock anchor -- better
                # than the moment the drain happened to finish, which is what
                # it used before, because this field was never assigned.
                self._recovered_start = time.time()
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
            self._rec_tms_first = (t_ms if self._rec_tms_first is None
                                   else min(self._rec_tms_first, t_ms))
            self._rec_tms_last = (t_ms if self._rec_tms_last is None
                                  else max(self._rec_tms_last, t_ms))
            self.state["recovered_seconds"] = round(
                sum(len(p) for p in self._recovered) / self.state["rate"], 1)
            return
        self._pcm.append(pcm)
        if self._clip_tms_first is None:
            self._clip_tms_first = t_ms
        self._clip_tms_last = t_ms
        self.state["frames"] += 1

        # A cheap level meter for the UI; full stats come from the clip.
        #
        # Widen before abs(): in int16, abs(-32768) is -32768, so a full-scale
        # negative sample reports a negative peak and the meter reads empty on
        # the loudest audio the device can produce.
        peak = int(np.abs(pcm.astype(np.int32)).max())
        self.state["peak"] = peak
        self.state["level"] = round(min(1.0, peak / 32767 * 3), 3)

    def clip_seconds(self):
        return sum(len(p) for p in self._pcm) / self.state["rate"]

    def _wall(self, t_ms):
        """Device milliseconds to wall clock, using the anchor taken from the
        first live frame. Returns None if there is no anchor yet."""
        if t_ms is None or self._clock_host is None:
            return None
        return self._clock_host - (self._clock_dev - t_ms) / 1000.0

    def _write_times(self, path, first_ms, last_ms, source, seconds):
        """Record when the audio happened, according to the device.

        Written beside the clip so ordering never has to be reconstructed
        from filesystem metadata again. mtime can be rewritten by a copy, a
        backup or a sync; the device's own clock cannot.
        """
        started = self._wall(first_ms)
        ended = self._wall(last_ms)
        if started is None:
            return
        if ended is None or ended < started:
            ended = started + seconds
        os.makedirs(TIMES, exist_ok=True)
        rec = {"name": os.path.basename(path), "started": round(started, 3),
               "ended": round(ended, 3), "seconds": round(seconds, 3),
               "source": source, "device_ms": [first_ms, last_ms]}
        try:
            atomicio.write_json(os.path.join(TIMES, os.path.basename(path) + ".json"), rec)
        except Exception:
            pass
        try:
            os.utime(path, (ended, ended))     # keep mtime consistent too
        except OSError:
            pass

    def take_clip(self):
        if not self._pcm:
            return None
        audio = np.concatenate(self._pcm)
        path = self._save_wav("clip", time.time(), audio, self.state["rate"])
        self._pcm = []          # only once the audio is on disk
        self._write_times(path, self._clip_tms_first, self._clip_tms_last,
                          "live", len(audio) / float(self.state["rate"] or 1))
        self._clip_tms_first = self._clip_tms_last = None
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

            # Ask for the full sample rate.
            #
            # The firmware boots at 8 kHz because that was the safe choice for
            # a Bluetooth 4.0 host, and nothing ever asked it for anything
            # else -- so every recording ever made by this device was
            # telephone quality, which is exactly the band that speech
            # recognition and speaker separation have the least to work with.
            #
            # Measured on the 4.0 dongle after the radio got its own thread:
            # 8 kHz costs 4.5 KB/s and 16 kHz costs 8.4 KB/s, and both deliver
            # 100% of the audio live over 25 s. The link has the room.
            if RATE_16K and self.state.get("rate") != 16000:
                # Finish anything already buffered at the old rate first.
                # Samples from two rates concatenated into one file are
                # written under whichever rate was current at the end, so
                # half the clip plays at the wrong speed.
                if self._pcm:
                    self.finalize(self.take_clip())
                await self._ctrl(0x02, 1)
                self.state["rate"] = 16000

            # Assert the settings this service intends, rather than
            # inheriting whatever the device was last told by anything else.
            #
            # A voice gate left on by a separate tool survived a reconnect,
            # and the service then reported "connected, armed" beside a device
            # recording almost nothing -- the same shape as trusting a
            # remembered armed flag, which already cost a session of
            # recordings. Anything the host has an opinion about, it states.
            await self._ctrl(0x04, 1 if self.state.get("vad") else 0)
            await self._ctrl(0x09, 1 if self.state.get("backlog_mode") else 0)
            for op, key, default in ((0x03, "gain", None),
                                     (0x0A, "led_level", None),
                                     (0x0B, "led_mode", None),
                                     (0x0C, "fast_charge", None),
                                     (0x0D, "mic_power_save", None)):
                if key in PREFS:
                    await self._ctrl(op, int(PREFS[key]))

            await c.start_notify(AUDIO_UUID, self._on_audio)
            # Whatever was last chosen, not always on.
            #
            # This armed unconditionally, so turning recording off and
            # restarting the service turned it back on -- the wrong direction
            # for a surprise on a device that records the room. It also left
            # the re-arm safety net useless in the case it exists for: that
            # net only fires when this service already believes it is armed,
            # so once the flag was false nothing ever armed the device again,
            # and capture stayed off with both sides agreeing it should be.
            # Default on, because that is what an always-on recorder is for.
            await self.set_armed(bool(PREFS.get("armed", True)), by_user=False)
            self.publish()
            self.event("log", text="connected to device")

            last_info = time.time()
            info_errors = 0
            while self._want and c.is_connected:
                await asyncio.sleep(0.25)
                if time.time() - last_info > 1.0:
                    last_info = time.time()
                    # A failed status read is not a failed session.
                    #
                    # This call used to sit bare in the loop, so one transient
                    # GATT read exception escaped all the way out of the
                    # session, disconnected the device and restarted discovery
                    # -- throwing away a link whose audio notifications were
                    # arriving perfectly, because a once-a-second status poll
                    # hiccuped. Audio is the thing worth protecting; the info
                    # characteristic is telemetry.
                    try:
                        await self._read_info(c)
                        info_errors = 0
                    except Exception as e:
                        info_errors += 1
                        self.state["info_errors"] = info_errors
                        if info_errors >= INFO_ERROR_LIMIT:
                            self.event("log", text=(
                                f"status reads failing ({info_errors}); "
                                f"reconnecting: {str(e)[:80]}"))
                            raise
                        self.publish()
                        continue
                    if getattr(self, "_rearm_needed", False):
                        # Say it again rather than assume it landed. This
                        # costs one control write and is the difference
                        # between recording and quietly not recording.
                        self._rearm_needed = False
                        self.event("log", text="device was not capturing; re-arming")
                        await self._ctrl(0x01, 1)
                        self.state["armed"] = True
                    self.publish()

            self.client = None
            if self.state.get("source") == "ble":
                self.state.update(connected=False, source=None)
            self.publish()

    async def _read_info(self, c):
        info = await c.read_gatt_char(INFO_UUID)
        parsed = parse_info(info)

        # A new boot id means the device clock restarted at zero.
        #
        # Frame timestamps are milliseconds since the device booted, and this
        # service maps them to wall-clock time through an anchor captured when
        # it first saw one. After a reboot that anchor describes a clock that
        # no longer exists, so fresh audio gets mapped to a moment well in the
        # past -- and clip ordering, the thing those timestamps exist for,
        # goes quietly wrong. The device rebooted several times during
        # development tonight and nothing noticed.
        new_id = parsed.get("boot_id")
        old_id = self.state.get("boot_id")
        if new_id is not None and old_id is not None and new_id != old_id:
            self.event("log", text="device rebooted; re-anchoring its clock")
            self._clock_host = None
            self._clock_dev = 0
            self._clip_tms_first = self._clip_tms_last = None
            self._recovered_tms = None

        self.state.update(parsed)
        # Say it again rather than assume it landed, but only when the device
        # publishes a capture state to disagree with.
        if parsed["device_streaming"] is False and self.state.get("armed"):
            self._rearm_needed = True

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

    def remember(self, **kw):
        """Record an intended setting so a restart resumes it."""
        PREFS.update(kw)
        save_prefs(PREFS)

    async def set_armed(self, on: bool, *, by_user: bool = True):
        """Arm or disarm. Only a person's decision is remembered.

        Persisting every call made this self-perpetuating: the connect path
        asserts the remembered value, and asserting it wrote it back, so a
        single spurious "off" -- from a test, a race, whatever -- became
        permanent. The device then sat connected and subscribed with capture
        off, both sides agreeing it should be, until somebody noticed nothing
        had been recorded for hours. Twice.

        Re-asserting what was already read is not a decision, so it does not
        write. Turning it off from the interface is, so it does.
        """
        self.state["armed"] = bool(on)
        if by_user:
            self.remember(armed=bool(on))
            self.event("log", text=f"recording {'on' if on else 'off'}")
        await self._ctrl(0x01, 1 if on else 0)
        self.publish()

    async def set_gain(self, g: int):
        g = max(0, min(80, g))
        if await self._ctrl(0x03, g):
            self.remember(gain=g)
            self.event("log", text=f"gain set to {g}")

    async def set_led(self, level: int, pulse: bool):
        self.state["led_level"] = max(0, min(255, int(level)))
        self.state["led_mode"] = 1 if pulse else 0
        await self._ctrl(0x0A, self.state["led_level"])
        await self._ctrl(0x0B, self.state["led_mode"])
        self.remember(led_level=self.state["led_level"],
                      led_mode=self.state["led_mode"])
        self.publish()

    async def set_fast_charge(self, on: bool):
        if await self._ctrl(0x0C, 1 if on else 0):
            self.remember(fast_charge=bool(on))
            self.event("log", text=f"charge current: {'100' if on else '50'} mA")

    async def set_mic_power_save(self, on: bool):
        if await self._ctrl(0x0D, 1 if on else 0):
            self.remember(mic_power_save=bool(on))
            self.event("log", text=f"mic power saving {'on' if on else 'off'}")

    async def clear_buffer(self):
        if await self._ctrl(0x08, 1):
            self.event("log", text="discarded the device buffer")

    async def set_backlog_mode(self, live_first: bool):
        self.state["backlog_mode"] = 1 if live_first else 0
        self.remember(backlog_mode=self.state["backlog_mode"])
        if await self._ctrl(0x09, 1 if live_first else 0):
            self.event("log", text="backlog: " +
                       ("live first, recover alongside" if live_first
                        else "drain before live audio"))
        self.publish()

    async def set_vad(self, on: bool):
        if await self._ctrl(0x04, 1 if on else 0):
            # Remembered, so a reconnect re-asserts it rather than inheriting
            # whatever the device happens to be set to.
            self.state["vad"] = bool(on)
            self.remember(vad=bool(on))
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
# Restore what was chosen last time rather than starting from the defaults.
if "agent_enabled" in PREFS:
    agent.enabled = bool(PREFS["agent_enabled"])
if PREFS.get("agent_model"):
    agent.model = str(PREFS["agent_model"])
if "agent_idle_seconds" in PREFS:
    agent.idle_seconds = max(10.0, float(PREFS["agent_idle_seconds"]))
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

# Record at 16 kHz. Set BOSWELL_8K=1 to go back to the firmware default, which
# roughly halves the radio traffic at the cost of everything above 4 kHz.
RATE_16K = os.environ.get("BOSWELL_8K", "") != "1"

# Consecutive info-characteristic read failures tolerated before the link is
# treated as gone. At one poll a second this is a few seconds of telemetry
# loss, against tearing down a session that is still delivering audio.
INFO_ERROR_LIMIT = 5
# Loopback by default. This serves recordings of whoever happens to be in the
# room and can start the microphone remotely, so reaching it from another
# machine should be a decision somebody made rather than the default.
_BIND = os.environ.get("BOSWELL_HOST", "127.0.0.1")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
if not TOKEN and _BIND not in _LOOPBACK and \
        os.environ.get("BOSWELL_ALLOW_INSECURE_LAN") != "1":
    raise SystemExit(
        f"refusing to listen on {_BIND} with no BOSWELL_TOKEN set: anyone who "
        "can reach this machine could start the microphone and read every "
        "recording. Set BOSWELL_TOKEN, or BOSWELL_ALLOW_INSECURE_LAN=1 to "
        "accept that.")
if not TOKEN and _BIND not in _LOOPBACK:
    # Every endpoint here can arm the microphone or hand over recordings. The
    # README says so; the process should too, at the moment it happens, since
    # that is when somebody is in a position to do something about it.
    print(f"WARNING: listening on {_BIND} with no BOSWELL_TOKEN set -- "
          "anyone who can reach this machine can start the microphone and "
          "read every recording. Set BOSWELL_TOKEN, or BOSWELL_HOST=127.0.0.1 "
          "to keep it on this machine.", flush=True)


def token_ok(supplied: str | None) -> bool:
    if not TOKEN:
        return True
    if not supplied:
        return False
    # Constant-time compare so the token cannot be recovered by timing.
    import hmac
    return hmac.compare_digest(supplied, TOKEN)


# Short-lived, single-use tickets for the WebSocket handshake.
#
# The token used to travel in the WebSocket URL, where it lands in access
# logs and proxy diagnostics and stays valid forever. A ticket is exchanged
# for it over an ordinary authenticated request, is good once, and expires in
# under a minute, so a URL that leaks is worth nothing by the time anyone
# reads it.
_TICKETS: dict[str, float] = {}
TICKET_TTL = 30.0


def issue_ticket() -> str:
    now = time.time()
    for k, exp in list(_TICKETS.items()):
        if exp < now:
            _TICKETS.pop(k, None)
    t = secrets.token_urlsafe(24)
    _TICKETS[t] = now + TICKET_TTL
    return t


def spend_ticket(t: str | None) -> bool:
    if not t:
        return False
    exp = _TICKETS.pop(t, None)          # single use: gone once taken
    return exp is not None and exp >= time.time()


def ws_auth_ok(sock) -> bool:
    """A ticket, or the token itself for non-browser clients like the relay."""
    if not TOKEN:
        return True
    if spend_ticket(sock.query_params.get("ticket")):
        return True
    return token_ok(sock.query_params.get("token"))


@app.post("/api/ws-ticket")
async def api_ws_ticket():
    """Trade the token, sent as a header, for a ticket that may go in a URL."""
    return {"ticket": issue_ticket(), "expires_in": TICKET_TTL}


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


# Short-lived, single-use tickets for the WebSocket handshake.
#
# The token used to travel in the WebSocket URL, where it lands in access
# logs and proxy diagnostics and stays valid forever. A ticket is exchanged
# for it over an ordinary authenticated request, is good once, and expires in
# under a minute, so a URL that leaks is worth nothing by the time anyone
# reads it.
_TICKETS: dict[str, float] = {}
TICKET_TTL = 30.0


def issue_ticket() -> str:
    now = time.time()
    for k, exp in list(_TICKETS.items()):
        if exp < now:
            _TICKETS.pop(k, None)
    t = secrets.token_urlsafe(24)
    _TICKETS[t] = now + TICKET_TTL
    return t


def spend_ticket(t: str | None) -> bool:
    if not t:
        return False
    exp = _TICKETS.pop(t, None)          # single use: gone once taken
    return exp is not None and exp >= time.time()


def ws_auth_ok(sock) -> bool:
    """A ticket, or the token itself for non-browser clients like the relay."""
    if not TOKEN:
        return True
    if spend_ticket(sock.query_params.get("ticket")):
        return True
    return token_ok(sock.query_params.get("token"))


@app.post("/api/ws-ticket")
async def api_ws_ticket():
    """Trade the token, sent as a header, for a ticket that may go in a URL."""
    return {"ticket": issue_ticket(), "expires_in": TICKET_TTL}


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
    safe_clip(name)
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
    atomicio.write_json(cache, out)
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
        if worker.submit(f):
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


@app.get("/api/search/semantic")
async def api_search_semantic(q: str, limit: int = 25):
    """Search by meaning rather than by word.

    Keyword search only finds a conversation if you remember a word from it.
    This finds "the bit about the battery connector" without anyone having
    said "connector".
    """
    if not q.strip():
        return {"hits": []}
    import semantic
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, semantic.search, q, limit)


@app.get("/api/agent/duplicates")
async def api_agent_duplicates(kind: str | None = None,
                               threshold: float = 0.95, limit: int = 50):
    """Recorded items that say the same thing, as candidates -- not applied.

    Found by comparing stored embeddings, not by asking the model: shown its
    own duplicates it will say "duplicate fact already recorded" and then not
    merge them, so the noticing has to happen somewhere it can be checked.

    Proposed rather than applied because the line is a judgement. At 0.90 this
    pairs "has a shoulder impingement and received a cortisone injection" with
    "had a cortisone injection 2 weeks ago" -- close, but the second carries a
    detail the first does not, and merging them loses it. The default is
    deliberately tight.
    """
    if kind is not None and kind not in agent_runner.KINDS:
        raise HTTPException(400, f"unknown kind: {kind}")
    threshold = min(1.0, max(0.5, threshold))
    import semantic
    loop = asyncio.get_running_loop()
    clusters = await loop.run_in_executor(
        None, semantic.duplicate_clusters, kind, threshold, limit)
    return {"threshold": threshold,
            "clusters": clusters,
            "removable": sum(len(c["duplicates"]) for c in clusters)}


@app.post("/api/agent/merge")
async def api_agent_merge(body: dict):
    """Fold duplicates into one entry, keeping its id and provenance."""
    kind = body.get("kind")
    keep = body.get("keep_id")
    drop = body.get("drop_ids") or []
    if kind not in agent_runner.KINDS:
        raise HTTPException(400, f"unknown kind: {kind}")
    if not keep or not isinstance(drop, list) or not drop:
        raise HTTPException(400, "need keep_id and a non-empty drop_ids")
    import sys
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    import tools_impl
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(
        None, tools_impl.merge_items, kind, keep, drop, body.get("text"))
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "merge failed"))
    device.event("log", text=f"merged {r['merged']} duplicate {kind}")
    return r


@app.get("/api/topics")
async def api_topics(limit: int = 200):
    """Subjects the agent has labelled conversations with, commonest first.

    A conversation is not usefully identified by its timestamp. These are what
    it was about, attached to the clips it came from, so everything said on a
    subject over weeks can be pulled together.
    """
    counts, clips = {}, {}
    for it in agent_runner.load_items("topics", limit=10000):
        for t in it.get("topics") or []:
            counts[t] = counts.get(t, 0) + 1
            clips.setdefault(t, set()).update(it.get("_clips") or [])
    out = [{"topic": t, "count": n, "clips": sorted(clips.get(t, []))}
           for t, n in counts.items()]
    out.sort(key=lambda r: (-r["count"], r["topic"]))
    return {"topics": out[:limit]}


@app.get("/api/search/hybrid")
async def api_search_hybrid(q: str, limit: int = 25):
    """Both searches at once, fused into one ranking.

    Keyword search misses a conversation whose words you do not remember, or
    that the transcriber heard differently. Meaning search drifts past exact
    terms -- names, part numbers, figures. Run both and let agreement decide.
    """
    if not q.strip():
        return {"hits": []}
    import semantic
    loop = asyncio.get_running_loop()
    kw = await loop.run_in_executor(None, index_db.search, q, limit * 2)
    res = await loop.run_in_executor(None, semantic.hybrid, q, kw, limit)

    # Fill in what meaning-only rows arrive without. Those are precisely the
    # results keyword search could not find, so leaving them without a
    # modified time would have let the date filter drop them.
    hits = res.get("hits") or []
    missing = [h["name"] for h in hits if h.get("modified") is None]
    if missing:
        rows = await loop.run_in_executor(None, index_db.clips_by_name, missing)
        for h in hits:
            row = rows.get(h["name"])
            if row:
                for k, v in row.items():
                    h.setdefault(k, v)
                    if h.get(k) is None:
                        h[k] = v
    return res


@app.get("/api/search/semantic/stats")
async def api_semantic_stats():
    import semantic
    return semantic.stats()


@app.post("/api/search/semantic/rebuild")
async def api_semantic_rebuild():
    """Embed anything not yet indexed. Resumable: already-indexed lines are
    skipped, so this can be run again after an interrupted pass."""
    import semantic

    def work():
        total = failed = 0
        first_error = [None]
        for f in sorted(os.listdir(pipeline.TRANSCRIPTS)):
            if not f.endswith(".json"):
                continue
            try:
                t = json.load(open(os.path.join(pipeline.TRANSCRIPTS, f)))
            except Exception:
                continue
            segs = t.get("segments") or []
            if segs:
                r = semantic.index_clip(f[:-5] + ".wav", segs)
                total += r["added"]
                failed += r["failed"]
                if r.get("error") and first_error[0] is None:
                    first_error[0] = r["error"]
        return {"indexed": total, "failed": failed,
                "error": first_error[0], **semantic.stats()}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, work)


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
    safe_clip(name)
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/transcribe/{name}")
async def api_transcribe(name: str, force: bool = False):
    if not os.path.exists(safe_clip(name)):
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
    if not worker.submit(name):
        # Not an error: rotation, a conversation request and a bulk action can
        # all name the same clip, and running it twice costs a full ASR and
        # diarization pass and can overwrite an edit with a re-run.
        return {"queued": name, "already_queued": True}
    return {"queued": name}


@app.get("/api/transcript/{name}")
async def api_transcript(name: str):
    tp = pipeline.transcript_path(name)
    if not os.path.exists(tp):
        raise HTTPException(404, "not transcribed yet")
    return JSONResponse(json.load(open(tp)))


def _rematch_clips(names=None):
    """Re-run speaker matching from stored voiceprint embeddings.

    Diarized ids are per-clip: SPEAKER_00 in one clip is not SPEAKER_00 in the
    next. What ties a voice together across a conversation is the voiceprint,
    and that comparison only happened at transcription time -- so naming
    someone labelled the one clip the chip was tapped on and left the other
    fifty-three saying SPEAKER_00.

    Every transcript keeps its embeddings, so this is a few hundred dot
    products rather than a re-transcription. Names set by hand are never
    overwritten by a guess.
    """
    if names is None:
        names = [f[:-5] + ".wav" for f in os.listdir(pipeline.TRANSCRIPTS)
                 if f.endswith(".json")] if os.path.isdir(pipeline.TRANSCRIPTS) else []
    changed = 0
    for name in names:
        tp = pipeline.transcript_path(name)
        if not os.path.exists(tp):
            continue
        try:
            t = json.load(open(tp))
        except Exception:
            continue
        emb = t.get("embeddings")
        if not emb:
            continue
        ident = pipeline.identify(emb)
        dirty = False
        for sid, rec in ident.items():
            cur = t.setdefault("speakers", {}).setdefault(sid, {})
            if cur.get("manual"):
                continue                      # a hand-set name always wins
            if rec.get("name") != cur.get("name") or rec.get("score") != cur.get("score"):
                cur["name"] = rec.get("name")
                cur["score"] = rec.get("score")
                dirty = True
        if dirty:
            atomicio.write_json(tp, t)
            index_db.upsert_clip(name)
            changed += 1
    return changed


@app.post("/api/rematch")
async def api_rematch(body: dict | None = None):
    names = (body or {}).get("names") if isinstance(body, dict) else None
    changed = _rematch_clips(names)
    return {"rematched": changed}


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
    clip_speakers: dict = {}
    missing = 0

    for name in names:
        safe_clip(name)
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

        # Speaker records are kept per clip, not merged into one map.
        #
        # A diarized id only means something inside the clip it came from:
        # SPEAKER_00 here is not SPEAKER_00 in the next clip. Merging them
        # made one clip's SPEAKER_00 being Nathan label every other clip's
        # SPEAKER_00 as Nathan too, which put the wrong name on other
        # people's speech -- three different ids in one conversation all
        # resolved to the same person.
        clip_speakers[name] = t.get("speakers") or {}

        for i, seg in enumerate(segs):
            sid = seg.get("speaker")
            rec = (t.get("speakers") or {}).get(sid) or {}
            segments.append({
                "clip": name,
                "index": i,
                "start": seg.get("start", 0.0),
                "end": seg.get("end", 0.0),
                "text": seg.get("text", ""),
                "speaker": sid,
                "speaker_name": seg.get("speaker_name"),
                # Resolved against this line's own clip, so the reader never
                # has to guess which clip an id belonged to.
                "name": seg.get("speaker_name") or rec.get("name"),
                "score": rec.get("score"),
                "edited": bool(seg.get("edited")),
            })

    # Voices offered for naming. People already identified collapse to one
    # entry each; anyone still unknown stays tied to the clip and id that can
    # actually be named, since that pair is what /api/label takes.
    voices: dict = {}
    for seg in segments:
        secs = max(0.0, (seg["end"] or 0) - (seg["start"] or 0))
        # A line diarization gave no speaker at all cannot be named: there is
        # no id for /api/label to attach a name to. Offering it as a voice
        # produced a chip reading "null".
        if not seg["name"] and not seg["speaker"]:
            continue
        if seg["name"]:
            key = "name:" + seg["name"]
            v = voices.setdefault(key, {"name": seg["name"], "seconds": 0.0,
                                        "clip": seg["clip"], "speaker": seg["speaker"]})
        else:
            key = f"{seg['clip']}:{seg['speaker']}"
            v = voices.setdefault(key, {"name": None, "seconds": 0.0,
                                        "clip": seg["clip"], "speaker": seg["speaker"]})
        v["seconds"] += secs
    for v in voices.values():
        v["seconds"] = round(v["seconds"], 1)
    ranked = sorted(voices.values(), key=lambda v: -v["seconds"])

    return {"clips": clips, "segments": segments, "voices": ranked,
            "clip_speakers": clip_speakers, "not_transcribed": missing}


def derived_paths(name):
    """Every file that exists only because this clip does.

    One list, because there are two delete endpoints and they had drifted:
    both removed the audio, transcript and waveform, and neither removed the
    device-time sidecar. An orphaned sidecar is worse than a missing one --
    filenames carry a timestamp to the second, so a later recording can be
    given the wrong clip's authoritative timing.
    """
    return (pipeline.transcript_path(name),
            os.path.join(ENVELOPES, name + ".json"),
            os.path.join(TIMES, name + ".json"))


def delete_clip_files(name):
    """Remove a clip and its derived files. Returns what was actually there."""
    removed = []
    for f in (os.path.join(DATA, name),) + derived_paths(name):
        if os.path.exists(f):
            os.remove(f)
            removed.append(os.path.basename(f))
    index_db.remove_clip(name)
    try:
        import semantic
        semantic.remove_clip(name)
    except Exception:
        pass
    return removed


@app.delete("/api/clip/{name}")
async def api_delete(name: str):
    safe_clip(name)
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        raise HTTPException(404, "no such clip")
    removed = delete_clip_files(name)
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
        if os.path.basename(name) != name or not name.endswith(".wav"):
            continue
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            missing.append(name)
            continue
        delete_clip_files(name)
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
    safe_clip(name)
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
    atomicio.write_json(tp, t, indent=2)
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
    atomicio.write_json(tp, t, indent=2)
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
    # Turning the agent off and finding it back on after a restart is the
    # kind of surprise that matters: it decides what gets read by a model.
    PREFS.update(agent_enabled=agent.enabled, agent_model=agent.model,
                 agent_idle_seconds=agent.idle_seconds)
    save_prefs(PREFS)
    return agent.status()


@app.post("/api/agent/run")
async def api_agent_run():
    """Stop waiting for silence and review what has accumulated now."""
    if not agent.pending_chars():
        raise HTTPException(400, "nothing waiting to be reviewed")
    agent.flush_now()
    return {"ok": True, "pending_chars": agent.pending_chars()}


@app.post("/api/agent/review")
async def api_agent_review(body: dict):
    """Review a conversation on demand rather than waiting for silence.

    The scheduler fires after a gap in speech, which is right for capturing
    the day as it happens but useless for anything already recorded. This is
    how you point the agent at a conversation you can see on screen.
    """
    names = body.get("names") or []
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "need a list of clip names")

    batch = []
    for name in names:
        safe_clip(name)
        tp = pipeline.transcript_path(name)
        if not os.path.exists(tp):
            continue
        try:
            t = json.load(open(tp))
        except Exception:
            continue
        segs = t.get("segments") or []
        if segs:
            batch.append((name, segs, t.get("speakers") or {}))
    if not batch:
        raise HTTPException(400, "none of those clips are transcribed")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, agent.review_now, batch)
    return result


@app.get("/api/agent/items")
async def api_agent_items(kind: str | None = None, limit: int = 200):
    try:
        return agent_runner.load_items(kind, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/agent/item/{kind}/{item_id}")
async def api_delete_item(kind: str, item_id: str):
    if kind not in agent_runner.KINDS:
        raise HTTPException(400, "unknown kind")
    if not agent_runner.delete_item(kind, item_id):
        raise HTTPException(404, "no such item")
    return {"ok": True}


@app.delete("/api/agent/items")
async def api_clear_items(kind: str | None = None):
    try:
        n = agent_runner.clear_items(kind)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    atomicio.write_json(tp, t, indent=2, allow_nan=False)
    index_db.upsert_clip(clip)

    # A name is only useful once it reaches the rest of the recordings. The
    # voiceprint that was just enrolled is what ties this voice to the other
    # clips it appears in, so re-resolve them from their stored embeddings --
    # otherwise naming somebody labelled one clip out of fifty-four and every
    # other line kept saying SPEAKER_00.
    propagated = 0
    if enrolled:
        try:
            propagated = _rematch_clips()
        except Exception as e:
            print(f"rematch after naming failed: {e}", flush=True)

    device.event("log", text=(f"named {spk} as {name}"
                              + (f", voiceprint now {count} sample(s)" if enrolled
                                 else " (voiceprint unchanged)")))
    return {"named": True, "name": name, "enrolled": enrolled,
            "samples": count, "reason": reason, "speakers": t["speakers"],
            "propagated": propagated}


def origin_ok(sock):
    """Reject a browser page that is not this interface.

    Same-origin rules do not cover WebSockets: a page on any other site can
    open one to localhost, and with no token configured it would have been
    accepted -- able to arm the microphone, clear the buffer, and read the
    live audio state. The browser tells the truth about where it came from in
    the Origin header, and it cannot be forged by page script.

    Only browsers send Origin. The relay is not a browser, so a request
    without one is allowed through; that is what the token is for.
    """
    origin = sock.headers.get("origin")
    if not origin:
        return True
    try:
        u = urlparse(origin)
    except Exception:
        return False
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        return True
    # Reached over the network on purpose: accept the host it was asked for.
    allowed = {h.strip() for h in
               os.environ.get("BOSWELL_ALLOWED_ORIGINS", "").split(",") if h.strip()}
    return origin in allowed


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
    if not origin_ok(sock) or not ws_auth_ok(sock):
        await sock.close(code=1008)
        return
    await sock.accept()

    # One source of audio at a time.
    #
    # The local Bluetooth loop starts on its own and a relay could connect
    # alongside it, both writing into the same PCM buffer, sequence counter
    # and clock anchor -- two different moments of the same room interleaved
    # into one recording, with nothing in the result to show it happened. A
    # second relay was worse still: it replaced the first, and when the first
    # disconnected it cleared the second's reference on the way out.
    if device.relay is not None:
        await sock.close(code=1013)      # try again later
        return
    if device.state.get("source") == "ble" and device.state.get("connected"):
        device.event("log", text="relay refused: the local link owns this session")
        await sock.close(code=1013)
        return

    device.relay = sock
    device.state.update(connected=True, source="relay", error=None,
                        frames=0, lost=0)
    # Reset what belongs to a session, so a relay does not inherit the
    # previous source's clock or partial clip.
    device._clock_host = None
    device._clock_dev = 0
    device._clip_tms_first = device._clip_tms_last = None
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
        # Only if it is still ours: a later relay may have taken over.
        if device.relay is sock:
            device.relay = None
        if device.state.get("source") == "relay":
            device.state.update(connected=False, source=None)
        device.publish()
        device.event("log", text="relay disconnected")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not origin_ok(sock) or not ws_auth_ok(sock):
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
                path = device.finalize(device.take_clip())
                device.event("log", text=f"saved {os.path.basename(path)}" if path
                             else "nothing to save")
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        device.listeners.discard(q)


if __name__ == "__main__":
    import uvicorn
    # Honours BOSWELL_HOST so the warning above is actionable: setting it to
    # 127.0.0.1 actually keeps the service on this machine.
    uvicorn.run(app, host=_BIND, port=int(os.environ.get("BOSWELL_PORT", "8000")),
                log_level="warning")

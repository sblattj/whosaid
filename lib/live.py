#!/usr/bin/env python3
"""
live.py: a live transcript of the call being recorded (`whosaid live`, issue #131).

  run    Follow the active `record --app` capture (autorecord included): every
         --chunk-seconds read the new audio from its growing mic.wav and app.wav,
         transcribe it locally with MLX Whisper (the model loads once), and append
         "me:" / "them:" lines with wall-clock times to
         <into>/.live/<start>.md and <start>.jsonl. Keeps following until the
         capture ends, then waits for the next one (--once: exit instead).
  show   Print the newest live transcript, or its last --minutes N.

Left/right are known, so "me" (your mic) and "them" (the meeting app) need no
diarization; names come from the normal post-call pipeline, which this does not
change. The mic also hears the other side through the speakers, so mic frames
not clearly louder than the app are silenced first (the post-call rule). Silent chunks and no-speech segments are skipped (Whisper invents
"Thank you." from silence), and chunks are cut at the quietest moment near the
boundary so words are not split. The capture folder is only ever read: files are
opened read-only and kept open, so audio written just before the recorder
deletes the folder is still read.

Reading (latest_session, read_entries) is stdlib only, for the MCP server;
following needs numpy and mlx-whisper (`whosaid live` runs it under uv).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

RATE = 16_000            # record_app.swift writes 16 kHz mono 16-bit PCM per leg
BYTES_PER_SEC = RATE * 2
LEGS = (("mic", "me"), ("app", "them"))
LIVE_DIR = ".live"
DEFAULT_INTO = Path.home() / "Recordings"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
RMS_FLOOR = 0.004        # about -48 dBFS: below it a chunk is silence, not sent to Whisper
NO_SPEECH_MAX = 0.6      # segments Whisper itself rates as probably silence are dropped
ENDED_AFTER = 20.0       # no growth on any leg for this long = the capture is over
ACTIVE_WINDOW = 90.0     # a session written within this many seconds counts as live
LOCAL_MARGIN_DB = 15.0   # same bleed rule as stereo_channels.py (mic this far over the app = you)
SILENCE_DB = -60.0


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} live: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- capture discovery

def capture_dirs() -> list[Path]:
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return [Path(p) for p in glob.glob(os.path.join(tmp, "whosaid-record-app.*")) if os.path.isdir(p)]


def active_capture(fresh: float = 15.0) -> Path | None:
    """The newest recorder temp folder whose WAVs grew in the last `fresh` seconds."""
    now = time.time()
    best, best_m = None, 0.0
    for d in capture_dirs():
        for name in ("app.wav", "mic.wav"):
            try:
                m = (d / name).stat().st_mtime
            except OSError:
                continue
            if now - m <= fresh and m > best_m:
                best, best_m = d, m
    return best


def data_offset(head: bytes) -> int | None:
    """Byte offset of the PCM data in a WAV that may still be written: the RIFF
    and data sizes are placeholders until the writer closes it, so walk the
    chunks (fmt, FLLR padding, ...) to the 'data' id instead of trusting them."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(head):
        cid, size = head[pos:pos + 4], int.from_bytes(head[pos + 4:pos + 8], "little")
        if cid == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


class Leg:
    """One growing WAV, read incrementally from a read-only handle."""

    def __init__(self, path: Path, speaker: str):
        self.path, self.speaker = path, speaker
        self.fh = None
        self.offset: int | None = None
        self.read_bytes = 0       # PCM bytes consumed so far
        self.pending = b""        # PCM not yet transcribed
        self.pending_start = 0    # sample index of pending[0]
        self.last_growth = time.monotonic()

    def poll(self) -> int:
        """Read whatever was appended; returns the number of new bytes."""
        if self.fh is None:
            try:
                self.fh = open(self.path, "rb")
            except OSError:
                return 0
        if self.offset is None:
            self.fh.seek(0)
            self.offset = data_offset(self.fh.read(65536))
            if self.offset is None:
                return 0
        self.fh.seek(self.offset + self.read_bytes)
        new = self.fh.read()
        new = new[: len(new) - (len(new) % 2)]  # whole samples only
        if new:
            self.read_bytes += len(new)
            self.pending += new
            self.last_growth = time.monotonic()
        return len(new)

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


# ---------------------------------------------------------------- chunking + ASR

def quiet_cut(samples, search: int) -> int:
    """Index in the last `search` samples where a 200 ms window is quietest."""
    import numpy as np
    n = len(samples)
    lo = max(0, n - search)
    win = RATE // 5
    best, best_e = n, None
    for start in range(lo, n - win, win // 2):
        e = float(np.mean(samples[start:start + win] ** 2))
        if best_e is None or e < best_e:
            best, best_e = start + win // 2, e
    return best


def take_chunks(legs: list[Leg], chunk_seconds: float, final: bool) -> dict | None:
    """{speaker: (start_sample, float32 samples)} for every open leg, cut at one
    shared point (both legs share a timeline), or None when not enough is buffered."""
    import numpy as np
    open_legs = [leg for leg in legs if leg.offset is not None]
    if not open_legs:
        return None
    sizes = [len(leg.pending) // 2 for leg in open_legs]
    have = min(sizes)
    if final or max(sizes) - have > 2 * chunk_seconds * RATE:  # a stalled leg must not hold up the other
        have = max(sizes)
    if have == 0 or (not final and have < chunk_seconds * RATE):
        return None
    arrs = {leg.speaker: np.frombuffer(leg.pending[: have * 2], dtype="<i2").astype(np.float32) / 32768.0
            for leg in open_legs}
    if final:
        cut = have
    else:
        mix = np.zeros(have, dtype=np.float32)
        for a in arrs.values():
            mix[: len(a)] += a
        cut = quiet_cut(mix, search=4 * RATE)
    out = {}
    for leg in open_legs:
        a = arrs[leg.speaker][:cut]
        out[leg.speaker] = (leg.pending_start, a)
        leg.pending = leg.pending[len(a) * 2:]
        leg.pending_start += len(a)
    return out


def mask_bleed(mic, app):
    """The mic with every 100 ms frame zeroed unless it is clearly the local voice.

    The mic also hears the other side through the speakers. Same rule as the
    post-call stereo attribution (stereo_channels.py): a frame is local only when
    the mic is LOCAL_MARGIN_DB louder than the app; one frame of padding on each
    side keeps word edges."""
    import numpy as np
    if app is None or not len(app):
        return mic
    app = np.pad(app, (0, max(0, len(mic) - len(app))))[: len(mic)]
    frame = RATE // 10
    n = (len(mic) + frame - 1) // frame
    local = np.zeros(n, dtype=bool)
    for i in range(n):
        m, r = mic[i * frame:(i + 1) * frame], app[i * frame:(i + 1) * frame]
        local[i] = db(m) >= SILENCE_DB and db(m) - db(r) >= LOCAL_MARGIN_DB
    keep = local.copy()
    keep[1:] |= local[:-1]
    keep[:-1] |= local[1:]
    out = mic.copy()
    for i in np.flatnonzero(~keep):
        out[i * frame:(i + 1) * frame] = 0.0
    return out


def db(x) -> float:
    import numpy as np
    if not len(x):
        return -120.0
    v = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(v) if v > 1e-6 else -120.0


def rms(samples) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0


def whisper_transcriber(model: str):
    import mlx_whisper

    def run(samples) -> list[dict]:
        r = mlx_whisper.transcribe(samples, path_or_hf_repo=model, language="en", verbose=None,
                                   condition_on_previous_text=False, hallucination_silence_threshold=2.0)
        return r.get("segments") or []
    return run


# ---------------------------------------------------------------- output

def session_paths(into: Path, started: float) -> tuple[Path, Path]:
    stem = datetime.fromtimestamp(started).strftime("%Y%m%dT%H%M%S")
    d = into / LIVE_DIR
    return d / f"{stem}.md", d / f"{stem}.jsonl"


def fmt_line(e: dict) -> str:
    return f"[{e['clock']}] {e['who']}: {e['text']}"


def write_session(md: Path, jl: Path, started: float, entries: list[dict], ended: bool) -> None:
    md.parent.mkdir(parents=True, exist_ok=True)
    entries.sort(key=lambda e: e["t"])
    head = (f"# Live transcript, call started {datetime.fromtimestamp(started):%Y-%m-%d %H:%M:%S}\n"
            "me = your mic, them = the meeting app. Rough and unnamed; the full transcript "
            "with names comes after the call.\n" + ("\n(call ended)\n" if ended else "\n"))
    tmp = md.with_suffix(".md.tmp")
    tmp.write_text(head + "\n".join(fmt_line(e) for e in entries) + "\n")
    os.replace(tmp, md)
    tmp = jl.with_suffix(".jsonl.tmp")
    meta = {"started": started, "ended": ended, "updated": time.time()}
    tmp.write_text(json.dumps({"meta": meta}) + "\n" + "".join(json.dumps(e) + "\n" for e in entries))
    os.replace(tmp, jl)


# ---------------------------------------------------------------- follow one capture

def follow(capture: Path, into: Path, transcribe, chunk_seconds: float = 20.0,
           poll_seconds: float = 2.0, stop=lambda: False, ended_after: float = ENDED_AFTER) -> Path | None:
    legs = [Leg(capture / f"{name}.wav", who) for name, who in LEGS]
    started = None
    for name in ("app.wav", "mic.wav"):
        try:
            st = (capture / name).stat()
            started = getattr(st, "st_birthtime", st.st_mtime)
            break
        except OSError:
            continue
    if started is None:
        return None
    md, jl = session_paths(into, started)
    entries: list[dict] = []
    log(f"following {capture} -> {md}")
    write_session(md, jl, started, entries, ended=False)
    while True:
        grew = sum(leg.poll() for leg in legs)
        quiet_for = time.monotonic() - max(leg.last_growth for leg in legs)
        final = stop() or (grew == 0 and quiet_for >= ended_after)
        added = 0
        while (chunks := take_chunks(legs, chunk_seconds, final)) is not None:
            if "me" in chunks:
                start, mic = chunks["me"]
                chunks["me"] = (start, mask_bleed(mic, chunks.get("them", (0, None))[1]))
            for who, (start, samples) in chunks.items():
                if rms(samples) < RMS_FLOOR:
                    continue
                for seg in transcribe(samples):
                    text = (seg.get("text") or "").strip()
                    if not text or float(seg.get("no_speech_prob") or 0) > NO_SPEECH_MAX:
                        continue
                    t = start / RATE + float(seg.get("start") or 0)
                    entries.append({"t": round(t, 2), "clock": datetime.fromtimestamp(started + t).strftime("%H:%M:%S"),
                                    "who": who, "text": text})
                    added += 1
        if added or final:
            write_session(md, jl, started, entries, ended=final)
        if final:
            for leg in legs:
                leg.close()
            log(f"capture over; {len(entries)} line(s) in {md}")
            return md
        time.sleep(poll_seconds)


# ---------------------------------------------------------------- reading (stdlib)

def sessions(into: Path) -> list[Path]:
    return sorted((into / LIVE_DIR).glob("*.jsonl"))


def read_entries(jl: Path) -> tuple[dict, list[dict]]:
    meta, entries = {}, []
    for line in jl.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "meta" in obj:
            meta = obj["meta"]
        else:
            entries.append(obj)
    return meta, entries


def latest_session(into: Path, minutes: float | None = None) -> dict:
    """The newest live transcript: started, active, path, and lines (last N minutes)."""
    found = sessions(into)
    if not found:
        return {"ok": False, "error": f"no live transcript under {into / LIVE_DIR}",
                "hint": "start one with `whosaid live` while a call is being recorded"}
    jl = found[-1]
    meta, entries = read_entries(jl)
    active = not meta.get("ended") and time.time() - float(meta.get("updated") or 0) < ACTIVE_WINDOW
    if minutes and entries:
        last = entries[-1]["t"]
        entries = [e for e in entries if e["t"] >= last - minutes * 60]
    return {
        "ok": True,
        "path": str(jl.with_suffix(".md")),
        "started": datetime.fromtimestamp(float(meta.get("started") or 0)).isoformat(timespec="seconds"),
        "active": active,
        "ended": bool(meta.get("ended")),
        "lines": len(entries),
        "transcript": "\n".join(fmt_line(e) for e in entries),
    }


# ---------------------------------------------------------------- CLI

def cmd_run(a: argparse.Namespace) -> int:
    into = Path(a.into).expanduser()
    state = {"stop": False}

    def on_signal(_s, _f):
        state["stop"] = True
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    transcribe = whisper_transcriber(a.model)
    deadline = time.monotonic() + a.max_hours * 3600
    done: set[Path] = set()
    log(f"model {a.model}; waiting for a call being recorded (Ctrl-C to quit)")
    waiting_logged = False
    if a.capture and not Path(a.capture).is_dir():
        log(f"no capture at {a.capture}")
        return 1
    while not state["stop"] and time.monotonic() < deadline:
        cap = Path(a.capture) if a.capture else active_capture()
        if cap and cap not in done:
            follow(cap, into, transcribe, a.chunk_seconds, stop=lambda: state["stop"])
            done.add(cap)
            waiting_logged = False
            if a.once or a.capture:
                return 0
            continue
        if not waiting_logged:
            log("no call is being recorded; waiting")
            waiting_logged = True
        time.sleep(3)
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    r = latest_session(Path(a.into).expanduser(), a.minutes)
    if not r["ok"]:
        print(f"live: {r['error']}; {r['hint']}", file=sys.stderr)
        return 1
    state = "live" if r["active"] else ("ended" if r["ended"] else "stale")
    print(f"# {r['path']} (started {r['started']}, {state})")
    print(r["transcript"])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid live")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    for q in (p, r):
        q.add_argument("--into", default=str(DEFAULT_INTO), help="recordings folder (default ~/Recordings)")
    r.add_argument("--model", default=os.environ.get("WHOSAID_LIVE_MODEL") or DEFAULT_MODEL)
    r.add_argument("--chunk-seconds", type=float, default=20.0)
    r.add_argument("--capture", help="follow this recorder temp folder instead of finding it")
    r.add_argument("--once", action="store_true", help="exit after one call")
    r.add_argument("--max-hours", type=float, default=12.0)
    s = sub.add_parser("show")
    s.add_argument("--into", default=str(DEFAULT_INTO))
    s.add_argument("--minutes", type=float)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "show", "-h", "--help"):
        argv.insert(0, "run")  # bare `whosaid live [flags]` means run
    a = p.parse_args(argv)
    if a.cmd == "show":
        return cmd_show(a)
    if a.chunk_seconds < 5:
        p.error("--chunk-seconds must be >= 5")
    return cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())

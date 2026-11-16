#!/usr/bin/env python3
"""
live.py: a live transcript of the call being recorded (`whosaid live`, issue #131).

  run    Follow the active `record --app` or `record --browser` capture (autorecord
         included): every --chunk-seconds read the new audio from its growing
         mic.wav and app.wav (a browser capture's capture.webm is decoded into
         those two legs by an ffmpeg -follow bridge),
         transcribe it locally with MLX Whisper (the model loads once), and append
         "me:" / "them:" lines with wall-clock times to
         <into>/.live/<start>.md and <start>.jsonl. Keeps following until the
         capture ends, then waits for the next one (--once: exit instead).
  show   Print the newest live transcript, or its last --minutes N.
  follow Print each new line of the newest live transcript once, as it is written,
         then `ended` (#187). The files are rewritten whole each update, so it reads
         the .jsonl with a cursor (see "follow" below), never tails the .md.

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
import shutil
import signal
import subprocess
import sys
import tempfile
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
LOGPROB_MIN = -1.0       # Whisper's own fallback bar: a lower average log-prob is a guess
COMPRESSION_MAX = 2.4    # Whisper's own fallback bar: text this repetitive is a loop
MIN_LOCAL_SECONDS = 0.8  # a mic chunk with less of your own voice than this is not sent
GAP_KEEP = 0.3           # silence kept between your speech islands once the rest is cut out
REPEAT_WINDOW = 4        # the same short text as one of a leg's last few lines is a loop
# what Whisper says to silence and noise; never kept on its own
HALLUCINATIONS = frozenset({"you", "thank you", "thanks", "thank you so much", "thanks for watching",
                            "thank you for watching", "the", "bye", "i", "s", "so", "and", "to", "a"})
ENDED_AFTER = 20.0       # no growth on any leg for this long = the capture is over
ACTIVE_WINDOW = 90.0     # a session written within this many seconds counts as live
LOCAL_MARGIN_DB = 15.0   # same bleed rule as stereo_channels.py (mic this far over the app = you)
SILENCE_DB = -60.0


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} live: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- capture discovery

BROWSER_WEBM = "capture.webm"  # record_browser.py: one stereo webm/opus, L = mic, R = tab audio


def capture_dirs() -> list[Path]:
    """Recorder temp folders: `record --app` (whosaid-record-app.*, two WAV legs) and
    `record --browser` (whosaid-browser-*, one growing capture.webm)."""
    tmp = os.environ.get("TMPDIR") or "/tmp"
    pats = ("whosaid-record-app.*", "whosaid-browser-*")
    return [Path(p) for pat in pats for p in glob.glob(os.path.join(tmp, pat)) if os.path.isdir(p)]


def is_browser_capture(d: Path) -> bool:
    return (d / BROWSER_WEBM).exists() and not (d / "app.wav").exists()


def active_capture(fresh: float = 15.0) -> Path | None:
    """The newest recorder temp folder whose audio grew in the last `fresh` seconds."""
    now = time.time()
    best, best_m = None, 0.0
    for d in capture_dirs():
        for name in ("app.wav", "mic.wav", BROWSER_WEBM):
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


def compact(samples, keep: float = GAP_KEEP):
    """Cut every all-zero stretch longer than `keep` seconds down to `keep` seconds.

    The masked mic is mostly zeros with short islands of your voice; 20 s of that
    makes Whisper invent words and loop. Returns the shorter samples and the runs
    [(compact_start, original_start, length)] that map a time back."""
    import numpy as np
    n, k = len(samples), int(keep * RATE)
    nz = np.flatnonzero(samples)
    if not len(nz):
        return samples[:0], []
    runs, parts, pos = [], [], 0
    gaps = np.flatnonzero(np.diff(nz) > k)
    starts = np.concatenate(([nz[0]], nz[gaps + 1]))
    ends = np.concatenate((nz[gaps], [nz[-1]])) + 1
    for a, b in zip(starts, ends):
        a, b = max(0, a - k // 2), min(n, b + k // 2)
        if parts and a < runs[-1][1] + runs[-1][2]:
            a = runs[-1][1] + runs[-1][2]
        if b <= a:
            continue
        runs.append((pos, int(a), int(b - a)))
        parts.append(samples[a:b])
        pos += b - a
    return np.concatenate(parts), runs


def original_time(runs, t: float) -> float:
    """Seconds in the compacted samples -> seconds in the original chunk."""
    x = t * RATE
    for cs, os_, ln in runs:
        if x < cs + ln:
            return (os_ + max(0.0, x - cs)) / RATE
    cs, os_, ln = runs[-1]
    return (os_ + ln) / RATE


def norm_text(text: str) -> str:
    return " ".join("".join(c for c in text.lower() if c.isalnum() or c in " '").split())


def keep_segment(seg: dict) -> bool:
    """False for what Whisper produces from silence, noise or a decoding loop."""
    text = norm_text(seg.get("text") or "")
    if sum(c.isalpha() for c in text) < 2 or text in HALLUCINATIONS:
        return False
    if float(seg.get("no_speech_prob") or 0) > NO_SPEECH_MAX:
        return False
    if seg.get("avg_logprob") is not None and float(seg["avg_logprob"]) < LOGPROB_MIN:
        return False
    if seg.get("compression_ratio") is not None and float(seg["compression_ratio"]) > COMPRESSION_MAX:
        return False
    return True


def repeated(entries: list[dict], who: str, text: str) -> bool:
    """The same short text as one of this leg's last few lines: a decoding loop."""
    key = norm_text(text)
    if len(key.split()) > 8:
        return False
    recent = [e for e in entries[-4 * REPEAT_WINDOW:] if e["who"] == who][-REPEAT_WINDOW:]
    return any(norm_text(e["text"]) == key for e in recent)


def transcribe_chunks(chunks: dict, transcribe, prior: list[dict], voices=None) -> list[dict]:
    """New {t, who, text} lines from one cut of both legs. Your mic has the other
    side's bleed masked out and only your speech islands are sent. With `voices`
    (live_voices.Voices), each "them" line also gets the running voice it sounds like."""
    import numpy as np
    out: list[dict] = []
    for who, (start, samples) in chunks.items():
        runs = None
        if who == "me":
            samples = mask_bleed(samples, chunks.get("them", (0, None))[1])
            if np.count_nonzero(samples) < MIN_LOCAL_SECONDS * RATE:
                continue
            samples, runs = compact(samples)
        if rms(samples) < RMS_FLOOR:
            continue
        for seg in transcribe(samples):
            text = (seg.get("text") or "").strip()
            if not keep_segment(seg) or repeated(prior + out, who, text):
                continue
            off = float(seg.get("start") or 0)
            t = start / RATE + (original_time(runs, off) if runs else off)
            if voices is None or who != "them":
                out.append({"t": round(t, 2), "who": who, "text": text})
                continue
            out.extend(placed_lines(seg, text, off, start / RATE, samples, voices))
    return out


def split_words(seg: dict, at: float) -> list[tuple[float, float, str]] | None:
    """The segment's words cut at `at` (seconds, the segment's own clock) into two
    (start, end, text) parts, or None without word times or when a side has no words."""
    words = [w for w in (seg.get("words") or []) if (w.get("word") or "").strip()]
    if len(words) < 2:
        return None
    i = next((j for j, w in enumerate(words) if (float(w["start"]) + float(w["end"])) / 2 >= at), len(words))
    if i in (0, len(words)):
        return None
    parts = [(float(ws[0]["start"]), float(ws[-1]["end"]), "".join(w["word"] for w in ws).strip())
             for ws in (words[:i], words[i:])]
    return parts if all(sum(c.isalpha() for c in p[2]) >= 2 for p in parts) else None


def placed_lines(seg: dict, text: str, off: float, base: float, samples, voices) -> list[dict]:
    """One "them" segment as line(s) with their running voice (#187). A speaker change
    inside the segment splits it at the nearest word when Whisper gave word times;
    without them the line is kept whole, placed on its longer side and marked
    mixed_why="change" with change_at (seconds since the call start)."""
    end = float(seg.get("end") or off)
    seg_samples = samples[int(off * RATE):int(end * RATE)]

    def place(e: dict, part) -> dict:
        try:
            if (v := voices.place(part)):
                e["voice"] = v
            if getattr(voices, "last_close", False) is True:
                e["mixed_why"] = "close voices"
        except Exception as err:  # noqa: BLE001  a name is never worth the transcript
            log(f"live names: {err}")
        return e

    cp = None
    finder = getattr(voices, "change_point", None)
    if finder is not None:
        try:
            cp = finder(seg_samples)
        except Exception as err:  # noqa: BLE001
            log(f"live speaker change: {err}")
    if cp is None:
        return [place({"t": round(base + off, 2), "who": "them", "text": text}, seg_samples)]
    parts = split_words(seg, off + cp)
    if parts:
        lines = [place({"t": round(base + a, 2), "who": "them", "text": words},
                       samples[int(a * RATE):int(b * RATE)]) for a, b, words in parts]
        if lines[0].get("voice") and lines[0].get("voice") == lines[1].get("voice"):
            return [dict(lines[0], text=text)]  # both sides are one running voice: a false change, keep it whole
        return lines
    cut = int(cp * RATE)
    longer = seg_samples[:cut] if cut >= len(seg_samples) - cut else seg_samples[cut:]
    e = place({"t": round(base + off, 2), "who": "them", "text": text}, longer)
    e["mixed_why"] = "change"
    e["change_at"] = round(base + off + cp, 2)
    return [e]


def db(x) -> float:
    import numpy as np
    if not len(x):
        return -120.0
    v = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(v) if v > 1e-6 else -120.0


def rms(samples) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0


MODEL_FETCH_TRIES = 3
MODEL_FETCH_BACKOFF = 5.0  # seconds, doubled per retry


def resolve_model(model: str, download=None, sleep=time.sleep) -> str:
    """A local folder for `model`. A path is used as is; a Hub repo id comes from
    the local cache with no network call, and only a model that isn't cached is
    downloaded, with retries. Without this, mlx_whisper asks the Hub for repo info
    on every load, so a flaky network (a proxy dropping the connection) killed the
    live transcript at its first chunk even though the model was on disk."""
    if Path(model).expanduser().exists():
        return str(Path(model).expanduser())
    if download is None:
        from huggingface_hub import snapshot_download as download
    try:
        return download(repo_id=model, local_files_only=True)
    except Exception:  # noqa: BLE001  not cached (or a partial cache): fetch it below
        pass
    for i in range(MODEL_FETCH_TRIES):
        try:
            return download(repo_id=model)
        except Exception as err:  # noqa: BLE001
            if i == MODEL_FETCH_TRIES - 1:
                raise
            wait = MODEL_FETCH_BACKOFF * 2 ** i
            log(f"model {model}: download failed ({err}); retrying in {wait:.0f}s")
            sleep(wait)
    raise RuntimeError("unreachable")


def whisper_transcriber(model: str, words: bool = False):
    """words=True asks Whisper for word times so a speaker change can split a segment
    in two (--split-words / WHOSAID_LIVE_SPLIT_WORDS=1). Off by default: it costs decode
    time and switches on hallucination_silence_threshold, which changes live output.
    Without it a segment holding a change is kept whole and flagged mixed."""
    import mlx_whisper
    path = resolve_model(model)

    def run(samples) -> list[dict]:
        r = mlx_whisper.transcribe(samples, path_or_hf_repo=path, language="en", verbose=None,
                                   condition_on_previous_text=False, hallucination_silence_threshold=2.0,
                                   word_timestamps=words)
        return r.get("segments") or []
    return run


# ---------------------------------------------------------------- output

def session_paths(into: Path, started: float) -> tuple[Path, Path]:
    stem = datetime.fromtimestamp(started).strftime("%Y%m%dT%H%M%S")
    d = into / LIVE_DIR
    return d / f"{stem}.md", d / f"{stem}.jsonl"


def speaker(e: dict) -> str:
    """"me" for your mic; for the other side the name its voice matched, else "them"."""
    return e["who"] if e["who"] == "me" else e.get("name") or e["who"]


def fmt_line(e: dict) -> str:
    return f"[{e['clock']}] {speaker(e)}: {e['text']}"


def write_session(md: Path, jl: Path, started: float, entries: list[dict], ended: bool) -> None:
    md.parent.mkdir(parents=True, exist_ok=True)
    entries.sort(key=lambda e: e["t"])
    head = (f"# Live transcript, call started {datetime.fromtimestamp(started):%Y-%m-%d %H:%M:%S}\n"
            "me = your mic, them = the meeting app; names are a live guess from known voices. "
            "The full transcript with names comes after the call.\n" + ("\n(call ended)\n" if ended else "\n"))
    tmp = md.with_suffix(".md.tmp")
    tmp.write_text(head + "\n".join(fmt_line(e) for e in entries) + "\n")
    os.replace(tmp, md)
    tmp = jl.with_suffix(".jsonl.tmp")
    meta = {"started": started, "ended": ended, "updated": time.time()}
    tmp.write_text(json.dumps({"meta": meta}) + "\n" + "".join(json.dumps(e) + "\n" for e in entries))
    os.replace(tmp, jl)


# ---------------------------------------------------------------- follow one capture

def webm_channels(webm: Path) -> int:
    """Channel count of the browser capture: 2 (mic + tab) or 1 (--no-mic)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=channels", "-of", "csv=p=0", str(webm)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out.splitlines()[0]) if out else 2
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 2


def bridge_cmd(webm: Path, out: Path, channels: int = 2) -> list[str]:
    """ffmpeg argv that decodes the growing browser webm (-follow keeps reading
    as the recorder appends) into the two 16 kHz mono legs follow() reads:
    left = mic -> mic.wav, right = tab audio -> app.wav. A mono (--no-mic)
    capture has only the tab, so only app.wav is written."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y",
           "-follow", "1", "-i", f"file:{webm}"]
    pcm = ["-c:a", "pcm_s16le", "-flush_packets", "1"]
    if channels < 2:
        return cmd + ["-ac", "1", "-ar", str(RATE)] + pcm + [str(out / "app.wav")]
    graph = (f"[0:a]pan=mono|c0=c1,aresample={RATE}[app];"
             f"[0:a]pan=mono|c0=c0,aresample={RATE}[mic]")
    return cmd + ["-filter_complex", graph,
                  "-map", "[app]", *pcm, str(out / "app.wav"),
                  "-map", "[mic]", *pcm, str(out / "mic.wav")]


def follow_browser(capture: Path, into: Path, transcribe, chunk_seconds: float = 20.0,
                   poll_seconds: float = 2.0, stop=lambda: False, ended_after: float = ENDED_AFTER,
                   voices=None, popen=subprocess.Popen) -> Path | None:
    """Follow a `record --browser` capture: bridge its webm into WAV legs in a
    private temp folder (never named like a recorder folder, so discovery does
    not pick it up twice), follow those, then stop ffmpeg and remove the bridge.
    The capture folder itself is only read."""
    webm = capture / BROWSER_WEBM
    try:
        st = webm.stat()
    except OSError:
        return None
    started = getattr(st, "st_birthtime", st.st_mtime)
    bridge = Path(tempfile.mkdtemp(prefix="whosaid-live-bridge-"))
    proc = None
    try:
        proc = popen(bridge_cmd(webm, bridge, webm_channels(webm)),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while not (bridge / "app.wav").exists() and time.monotonic() < deadline:
            if proc.poll() is not None or stop():
                log(f"browser capture bridge failed (ffmpeg exit {proc.poll()})")
                return None
            time.sleep(0.2)
        log(f"browser capture {capture}: decoding {webm.name} live")
        return follow(bridge, into, transcribe, chunk_seconds, poll_seconds, stop, ended_after,
                      voices, started=started, label=capture)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(bridge, ignore_errors=True)


def follow(capture: Path, into: Path, transcribe, chunk_seconds: float = 20.0,
           poll_seconds: float = 2.0, stop=lambda: False, ended_after: float = ENDED_AFTER,
           voices=None, started: float | None = None, label: Path | None = None) -> Path | None:
    if is_browser_capture(capture):
        return follow_browser(capture, into, transcribe, chunk_seconds, poll_seconds, stop,
                              ended_after, voices)
    legs = [Leg(capture / f"{name}.wav", who) for name, who in LEGS]
    for name in ("app.wav", "mic.wav"):
        if started is not None:
            break
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
    log(f"following {label or capture} -> {md}")
    write_session(md, jl, started, entries, ended=False)
    while True:
        grew = sum(leg.poll() for leg in legs)
        quiet_for = time.monotonic() - max(leg.last_growth for leg in legs)
        final = stop() or (grew == 0 and quiet_for >= ended_after)
        added = 0
        while (chunks := take_chunks(legs, chunk_seconds, final)) is not None:
            for e in transcribe_chunks(chunks, transcribe, entries, voices):
                e["clock"] = datetime.fromtimestamp(started + e["t"]).strftime("%H:%M:%S")
                e["n"] = len(entries)  # the order lines were made in: the follow cursor (#187)
                entries.append(e)
                added += 1
        if added and voices is not None:
            voices.annotate(entries)  # a voice named later gets its earlier lines back
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


def stamp(ts: object) -> str | None:
    """RFC 3339 local time with its offset, or None when the meta has no time."""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds") if ts else None
    except (TypeError, ValueError, OSError):
        return None


def latest_session(into: Path, minutes: float | None = None, since: str | float | None = None,
                   keep=None) -> dict:
    """The newest live transcript: started, active, path, lines (last N minutes) and a
    `cursor`. With `since` (a cursor or a bare t), only the lines after it, plus `turns`
    (the entries) and `reset` (the cursor was from an earlier call: this is all of the
    newest). `keep` (turn_filter) filters the lines; the cursor still moves past the
    ones it drops. Raises ValueError for a malformed `since`."""
    found = sessions(into)
    if not found:  # the same keys as a found session, so readers need no special case
        return {"ok": False, "error": f"no live transcript under {into / LIVE_DIR}",
                "hint": "start one with `whosaid live` while a call is being recorded",
                "path": None, "started": None, "active": False, "ended": False, "lines": 0,
                "updated": None, "jsonl": None, "cursor": None}
    jl = found[-1]
    cur = parse_cursor(since) if since is not None else None
    meta, entries = read_entries(jl)
    active = not meta.get("ended") and time.time() - float(meta.get("updated") or 0) < ACTIVE_WINDOW
    new, cursor, reset = turns_after(entries, jl.stem, cur)
    picked = [e for e, _ in new] if cur is not None else entries
    if minutes and picked:
        last = entries[-1]["t"]
        picked = [e for e in picked if e["t"] >= last - minutes * 60]
    if keep is not None:
        picked = [e for e in picked if keep(e)]
    out = {
        "ok": True,
        "path": str(jl.with_suffix(".md")),
        "started": stamp(meta.get("started")),
        "active": active,
        "ended": bool(meta.get("ended")),
        "lines": len(picked),
        "updated": stamp(meta.get("updated")),
        "jsonl": str(jl),
        "cursor": cursor,
    }
    if cur is None and keep is None:
        out["transcript"] = "\n".join(fmt_line(e) for e in picked)
        return out
    started = meta.get("started")
    out["transcript"] = "\n".join(follow_line(e, started, marked=keep is not None) for e in picked)
    out["turns"] = [{k: e[k] for k in TURN_KEYS if k in e} for e in picked]
    if cur is not None:
        out["reset"] = reset
    return out


# ---------------------------------------------------------------- follow (#187), stdlib
#
# The .md and .jsonl are rewritten whole on every update (write_session), so they cannot
# be tailed, and voices.annotate can rename earlier lines. A cursor names a place in one
# session's lines that survives both:
#   "<stem>:n<N>"      the writer numbers each line in the order it was made ("n"); the
#                      lines after the cursor are those with n > N. A line a lagging leg
#                      sorts in before the newest one still has a higher n.
#   "<stem>:t<T>:<K>"  for a .jsonl written before "n" existed: the lines with t > T, plus
#                      those at exactly t == T after the first K (same-second lines).
#   "<T>"              a bare t (seconds since the call started): the lines with t > T.
# t is seconds since the call started, never the clock string, so a call crossing
# midnight or two lines in one second need nothing special. A cursor from another
# session (a new call started) means: everything in the newest one.

TURN_KEYS = ("n", "t", "clock", "who", "name", "role", "voice", "confidence", "nearest", "mixed",
             "mixed_why", "change_at", "text")
HINT_MIN = 0.45     # an unnamed voice this close (cosine) to a known one is worth a `v9 may be B`
HINT_AFTER = 2      # ...once it has said this many lines
STALE_SECONDS = 900.0  # a session not ended and not written for this long: its writer is gone


def parse_cursor(since) -> dict:
    """{"stem", "n"} | {"stem", "t", "k"} | {"t", "k": None} from a cursor or a bare t."""
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        return {"t": float(since), "k": None}
    s = str(since).strip()
    try:
        return {"t": float(s), "k": None}
    except ValueError:
        pass
    stem, sep, rest = s.partition(":")
    if sep and stem:
        try:
            if rest.startswith("n") and rest[1:].removeprefix("-").isdigit():
                return {"stem": stem, "n": int(rest[1:])}
            if rest.startswith("t"):
                t, sep2, k = rest[1:].partition(":")
                if sep2 and k.isdigit():
                    return {"stem": stem, "t": float(t), "k": int(k)}
        except ValueError:
            pass
    raise ValueError(f"not a live cursor: {since!r} (want <stem>:n<N>, <stem>:t<T>:<K> or a t value)")


def turns_after(entries: list[dict], stem: str, cur: dict | None) -> tuple[list[tuple[dict, str]], str, bool]:
    """The lines after `cur` in file (time) order, each with the cursor just past it, the
    cursor past every line, and whether `cur` belonged to another session."""
    reset = False
    if cur is not None and cur.get("stem") not in (None, stem):
        cur, reset = None, True
    seq = bool(entries) and all(isinstance(e.get("n"), int) for e in entries)
    if cur is None and not entries:  # `live run` writes an empty session first: start on the sequence
        return [], f"{stem}:n-1", reset
    if cur is not None and "n" in cur and entries and not seq:
        cur, reset = None, True  # a sequence cursor on a file without numbers: start over
    out: list[tuple[dict, str]] = []
    if (cur is None and seq) or (cur is not None and "n" in cur):
        top = run = cur["n"] if cur is not None else -1
        for e in entries:
            if e["n"] > top:
                run = max(run, e["n"])  # past every line emitted so far, even one sorted in earlier
                out.append((e, f"{stem}:n{run}"))
        best = max([top] + [e["n"] for e in entries])
        return out, f"{stem}:n{best}", reset
    T = cur["t"] if cur is not None else None
    K = cur["k"] if cur is not None else 0
    same: dict[float, int] = {}
    for e in entries:
        t = float(e["t"])
        same[t] = same.get(t, 0) + 1
        if T is None or t > T or (t == T and K is not None and same[t] > K):
            out.append((e, f"{stem}:t{t!r}:{same[t]}"))
    if entries:
        last = float(entries[-1]["t"])
        end = f"{stem}:t{last!r}:{same[last]}"
        if T is not None and last < T:
            end = f"{stem}:t{T!r}:{K if K is not None else 0}"
    else:
        end = f"{stem}:t{T!r}:{K if K is not None else 0}" if T is not None else f"{stem}:t-1.0:0"
    return out, end, reset


def _compile(patterns) -> list:
    import re
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as err:
            raise ValueError(f"bad --speaker regex {p!r}: {err}") from None
    return out


def named(e: dict) -> bool:
    """A line whose speaker is known: your mic, or a "them" line a voice put a name on."""
    return e.get("who") == "me" or bool(e.get("name"))


def turn_filter(speakers=None, exclude_self: bool = False, named_only: bool = False):
    """A predicate over lines. --speaker REGEX (any of them, case-insensitive) matches
    the live name or role ("me" for your own lines). Unnamed "them" lines are kept
    unless named_only: one person can be split over a named and an unnamed voice, and
    dropping the unnamed one loses what they said. Raises ValueError for a bad regex."""
    pats = _compile(speakers)

    def keep(e: dict) -> bool:
        if exclude_self and e.get("who") == "me":
            return False
        if not named(e):
            return not named_only
        if not pats:
            return True
        hay = [e.get("name") or "", e.get("role") or "", "me" if e.get("who") == "me" else ""]
        return any(p.search(h) for p in pats for h in hay if h)
    keep.patterns = pats
    return keep


def _clock_at(started, t) -> str:
    try:
        return datetime.fromtimestamp(float(started) + float(t)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return f"+{float(t):.0f}s"


def follow_label(e: dict, started=None, marked: bool = True) -> str:
    """Who said a line, for follow: the name, "me", or a marked unknown
    ("? (v9, nearest: B 0.71)"); a mixed line says why."""
    if e.get("who") == "me":
        who = "me"
    elif e.get("name"):
        who = e["name"]
    elif e.get("voice") or marked:
        bits = [e.get("voice") or "them"]
        if e.get("nearest"):
            bits[0] += f", nearest: {e['nearest']} {float(e.get('confidence') or 0):.2f}"
        who = f"? ({bits[0]})"
    else:
        who = "them"
    if e.get("mixed"):
        why = e.get("mixed_why")
        if e.get("change_at") is not None:
            why = f"change at {_clock_at(started, e['change_at'])}"
        elif not why:
            why = f"low confidence {float(e.get('confidence') or 0):.2f}"
        who += f" (mixed, {why})"
    return who


def follow_line(e: dict, started=None, marked: bool = True) -> str:
    clock = e.get("clock") or _clock_at(started, e["t"])
    return f"[{clock}] {follow_label(e, started, marked)}: {e['text']}"


class Notes:
    """One-time notes while following a session: `v9 may be B` for an unnamed voice
    that keeps sounding like a known one, `v9 is now B` when a voice that was shown
    unnamed gets a name (its earlier lines were already printed as "?")."""

    def __init__(self, keep):
        self.keep, self.close, self.hinted, self.shown_unnamed, self.renamed = keep, {}, set(), set(), set()

    def _wanted(self, name: str) -> bool:
        pats = getattr(self.keep, "patterns", None)
        return not pats or any(p.search(name) for p in pats)

    def scan(self, entries: list[dict]) -> list[dict]:
        out = []
        for e in entries:
            v = e.get("voice")
            if not v or e.get("who") == "me":
                continue
            if e.get("name") and v in self.shown_unnamed and v not in self.renamed and self._wanted(e["name"]):
                self.renamed.add(v)
                out.append({"event": "renamed", "voice": v, "name": e["name"]})
        return out

    def see(self, e: dict, shown: bool = True) -> list[dict]:
        """Notes due once `e` (just emitted, or filtered out: shown=False) is seen."""
        v = e.get("voice")
        if not v or e.get("who") == "me" or e.get("name"):
            return []
        if shown:
            self.shown_unnamed.add(v)
        near, score = e.get("nearest"), float(e.get("confidence") or 0)
        if not near or score < HINT_MIN or v in self.hinted:
            return []
        self.close[v] = self.close.get(v, 0) + 1
        if self.close[v] < HINT_AFTER or not self._wanted(near):
            return []
        self.hinted.add(v)
        return [{"event": "hint", "voice": v, "may_be": near, "score": round(score, 2)}]


def note_text(n: dict) -> str:
    if n["event"] == "hint":
        return f"# {n['voice']} may be {n['may_be']} ({n['score']:.2f})"
    return f"# {n['voice']} is now {n['name']}"


def load_follow_cursor(path) -> tuple:
    """(stem, cursor, done) saved by a plain `follow --cursor-file`; (None, None, False) if none."""
    if not path or not Path(path).exists() or not Path(path).read_text().strip():
        return None, None, False    # none yet, or an empty file (`mktemp`): start fresh
    try:
        obj = json.loads(Path(path).read_text())
        if not isinstance(obj, dict) or "agent" in obj or "version" in obj:
            raise ValueError("an --agent cursor file; plain follow keeps its own")
        cur = obj.get("cursor")
        if cur is not None:
            parse_cursor(cur)
        return obj.get("session") or None, cur, bool(obj.get("done"))
    except ValueError as err:
        raise ValueError(f"cursor file {path}: {err}") from None


def save_follow_cursor(path, stem, cursor, done: bool) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"session": stem, "cursor": cursor, "done": done}) + "\n")
    os.replace(tmp, path)


def follow_stream(into: Path, keep, fmt: str = "text", once: bool = False, poll: float = 5.0,
                  stale_seconds: float = STALE_SECONDS, deadline: float | None = None,
                  out=None, sleep=time.sleep, cursor_file: Path | None = None,
                  heartbeat: float | None = None, now=time.time) -> int:
    """Print each new line of the newest live session once, as it is written; an `ended`
    line when the call is over (meta ended, or no write for stale_seconds), then exit
    (once) or wait for the next session. cursor_file: resume from (and save after each
    line) {"session", "cursor", "done"} (#217); heartbeat: an `alive` line after that many
    quiet seconds. Returns 0; ValueError for a cursor file that is not one."""
    out = out or sys.stdout
    last = {"at": now()}

    def emit(text: str, obj: dict) -> None:
        print(json.dumps(obj) if fmt == "jsonl" else text, file=out, flush=True)
        last["at"] = now()
        if cursor_file and obj["event"] != "alive":
            save_follow_cursor(cursor_file, obj.get("session"), obj.get("cursor"), obj["event"] == "ended")

    stem, cursor, done = load_follow_cursor(cursor_file)
    notes = Notes(keep)
    while deadline is None or time.monotonic() < deadline:
        if heartbeat and now() - last["at"] >= heartbeat:
            emit(f"# alive {cursor or ''}".rstrip(), {"event": "alive", "session": stem, "cursor": cursor,
                                                       "at": round(now(), 3)})
        found = sessions(into)
        jl = found[-1] if found else None
        if jl is not None and jl.stem != stem:
            if stem is not None and not done:  # a new call started before the old one said so
                emit(f"ended {stem} (a newer call started)", {"event": "ended", "session": stem,
                                                              "cursor": cursor, "superseded": True})
                if once:
                    return 0
            stem, cursor, done, notes = jl.stem, None, False, Notes(keep)
            log(f"following {jl}")
        if jl is None or done:
            sleep(poll)
            continue
        try:
            meta, entries = read_entries(jl)
            mtime = jl.stat().st_mtime
        except (OSError, ValueError):  # replaced or removed between listing and reading
            sleep(poll)
            continue
        started = meta.get("started")
        before = cursor
        new, cursor, _ = turns_after(entries, stem, parse_cursor(cursor) if cursor else None)
        for n in notes.scan(entries):  # about lines already printed: resuming from it must not skip the new ones
            emit(note_text(n), dict(n, session=stem, cursor=before))
        for e, c in new:
            shown = keep(e)
            if shown:
                obj = {"event": "turn", "session": stem, "cursor": c,
                       **{k: e[k] for k in TURN_KEYS if k in e}}
                emit(follow_line(e, started, marked=bool(getattr(keep, "patterns", None))), obj)
            for n in notes.see(e, shown):
                emit(note_text(n), dict(n, session=stem, cursor=c))
        idle = time.time() - max(float(meta.get("updated") or 0), mtime)
        if meta.get("ended") or idle >= stale_seconds:
            stale = not meta.get("ended")
            emit(f"ended {stem}" + (f" (stale: no update for {idle:.0f}s)" if stale else ""),
                 {"event": "ended", "session": stem, "cursor": cursor, "stale": stale})
            done = True
            if once:
                return 0
            continue
        sleep(poll)
    return 0


# ---------------------------------------------------------------- CLI

def cmd_run(a: argparse.Namespace) -> int:
    into = Path(a.into).expanduser()
    state = {"stop": False}

    def on_signal(_s, _f):
        state["stop"] = True
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    transcribe = whisper_transcriber(a.model, words=a.split_words)
    import live_voices
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
            follow(cap, into, transcribe, a.chunk_seconds, stop=lambda: state["stop"],
                   voices=live_voices.load(log))  # per call: fresh voices, the registry as it is now
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


def cmd_follow(a: argparse.Namespace) -> int:
    """`live follow`: the newest session's new lines as they are written (#187)."""
    try:
        keep = turn_filter(a.speaker, a.exclude_self, a.named_only)
    except ValueError as err:
        print(f"live follow: {err}", file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)  # one line at a time into a pipe
        except (AttributeError, ValueError, OSError):
            pass
    deadline = time.monotonic() + a.max_hours * 3600
    cf = Path(a.cursor_file).expanduser() if a.cursor_file else None
    try:
        if a.agent:
            import live_agent  # lazily: plain follow stays as it was
            return live_agent.follow_agent(Path(a.into).expanduser(), a.format, a.once, a.poll, a.stale_seconds,
                                           deadline, cursor_file=cf, heartbeat=a.heartbeat, self_names=a.self_name,
                                           flush_seconds=a.flush_seconds)
        return follow_stream(Path(a.into).expanduser(), keep, a.format, a.once, a.poll,
                             a.stale_seconds, deadline, cursor_file=cf, heartbeat=a.heartbeat)
    except ValueError as err:
        print(f"live follow: {err}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


def cmd_status(a: argparse.Namespace) -> int:
    """`live status --json`: the newest session without its text; always exit 0."""
    r = latest_session(Path(a.into).expanduser())
    r.pop("transcript", None)
    if a.json:
        print(json.dumps(r, indent=1))
    elif r["ok"]:
        print(f"live: {'live' if r['active'] else ('ended' if r['ended'] else 'stale')}, {r['lines']} line(s), "
              f"started {r['started']}, updated {r['updated']}: {r['path']}")
    else:
        print(f"live: {r['error']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid live")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    for q in (p, r):
        q.add_argument("--into", default=str(DEFAULT_INTO), help="recordings folder (default ~/Recordings)")
    r.add_argument("--model", default=os.environ.get("WHOSAID_LIVE_MODEL") or DEFAULT_MODEL)
    r.add_argument("--chunk-seconds", type=float, default=20.0)
    r.add_argument("--split-words", action="store_true",
                   default=os.environ.get("WHOSAID_LIVE_SPLIT_WORDS") == "1",
                   help="ask Whisper for word times and split a segment at a speaker change "
                        "(slower; off: the segment is flagged mixed)")
    r.add_argument("--capture", help="follow this recorder temp folder instead of finding it")
    r.add_argument("--once", action="store_true", help="exit after one call")
    r.add_argument("--max-hours", type=float, default=12.0)
    s = sub.add_parser("show")
    s.add_argument("--into", default=str(DEFAULT_INTO))
    s.add_argument("--minutes", type=float)
    t = sub.add_parser("status")
    t.add_argument("--into", default=str(DEFAULT_INTO))
    t.add_argument("--json", action="store_true")
    f = sub.add_parser("follow", help="print each new line of the newest live transcript once",
                       description="Print each new line of the newest live transcript once, as it is "
                       "written, and an `ended` line when the call is over. Reads the .jsonl with a "
                       "cursor that survives the file being rewritten; never tail the .md.")
    f.add_argument("--into", default=str(DEFAULT_INTO), help="recordings folder (default ~/Recordings)")
    f.add_argument("--speaker", action="append", metavar="REGEX",
                   help="only lines whose live name or role matches (case-insensitive; repeatable). "
                   "Unnamed lines are kept and marked '? (v9, nearest: B 0.71)'")
    f.add_argument("--exclude-self", action="store_true", help="drop your own mic lines")
    f.add_argument("--named-only", action="store_true", help="drop lines no known voice was matched to")
    f.add_argument("--poll", type=float, default=5.0, help="seconds between reads (default 5)")
    f.add_argument("--format", choices=("text", "jsonl"), default="text")
    f.add_argument("--once", action="store_true", help="exit 0 after the call ends (an `ended` line)")
    f.add_argument("--stale-seconds", type=float, default=STALE_SECONDS,
                   help=f"a session not written for this long is over (default {STALE_SECONDS:.0f})")
    f.add_argument("--max-hours", type=float, default=12.0)
    f.add_argument("--agent", "--events", dest="agent", action="store_true",
                   help="events for an agent monitor instead of lines: utterance, topic_revisit, for_me, "
                   "decision, call_started / call_ended (#217)")
    f.add_argument("--cursor-file", metavar="PATH",
                   help="save the place after each event and resume from it on restart (no gap, no duplicate)")
    f.add_argument("--heartbeat", type=float, metavar="S", help="an `alive` event after S quiet seconds")
    f.add_argument("--self", dest="self_name", action="append", metavar="NAME",
                   help="--agent: your name for for_me (repeatable; default: the registry's self-roled voice)")
    f.add_argument("--flush-seconds", type=float, metavar="S", default=45.0,
                   help="--agent: the file not written for S seconds finishes the last utterance (default 45)")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "show", "status", "follow", "-h", "--help"):
        argv.insert(0, "run")  # bare `whosaid live [flags]` means run
    a = p.parse_args(argv)
    if a.cmd == "follow":
        if a.poll <= 0:
            p.error("--poll must be > 0")
        if a.heartbeat is not None and a.heartbeat <= 0:
            p.error("--heartbeat must be > 0")
        if a.agent and a.speaker or a.agent and (a.exclude_self or a.named_only):
            p.error("--agent emits every voice; --speaker, --exclude-self and --named-only are for line mode")
        return cmd_follow(a)
    if a.cmd == "show":
        return cmd_show(a)
    if a.cmd == "status":
        return cmd_status(a)
    if a.chunk_seconds < 5:
        p.error("--chunk-seconds must be >= 5")
    return cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())

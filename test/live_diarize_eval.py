#!/usr/bin/env python3
"""live_diarize_eval.py: score `whosaid live` speaker labels against ground truth and
against the retroactive pass (transcribe, diarize, name), on the same synthetic calls (#218).

    python3 test/live_diarize_eval.py --fast [--only handoff,near_far] [--out results.json]
        [--gate [--max-gap 5] [--baseline last-release.json]]
    python3 test/live_diarize_eval.py --record            # re-record the Whisper cassette (dev, local model)
    python3 test/live_diarize_eval.py --realtime --device "BlackHole 2ch" --app BUNDLE_ID [--only agree]
    python3 test/live_diarize_eval.py --list-devices

Calls (test/fixtures/live_diarize/calls.json) are assembled at run time from a small clip
bank of voices ElevenLabs rendered for test/diarize_eval (live_diarize_build.py copied them),
so the 42-minute call costs no disk. Each voice has a fake name; four are enrolled in a
throwaway registry (registry.json), the guest is not, and one person also speaks on a far mic.

--fast (CI, no network): feeds the call's two legs into lib/live.py's follow() as growing
WAVs, two seconds of audio per poll (a 1x clock, without the waiting), with the live naming
code as `whosaid live run` builds it. Whisper is replaced by a cassette of what the local
model returned for exactly those chunks (keyed by the chunk's samples; a miss is an error,
never a guess). The retroactive pass runs lib/diarize_sherpa.py on the whole call with the
cassette's whole-file Whisper JSON. Both are scored per 20 ms frame and printed side by side.

--realtime: plays one call through the actual capture path (a loopback output device that
`whosaid record --app` captures, `whosaid live run` following it) at 1x. It refuses any output
device that is not a known loopback driver (BlackHole, Loopback, Soundflower, VB-Cable), so
nothing is ever played to speakers or headphones.

Metrics (per system, per call and for the suite):
  name_acc         share of enrolled talk time under that person's name
  unnamed          share of enrolled talk time under no name (them / SPEAKER_NN)
  missing          share of enrolled talk time with no line at all
  guest_named      share of the guest's talk time given an enrolled name
  split_voice      share of enrolled people shown both named and unnamed (>= 5% each)
  handoff_acc      handoffs (a reply < 0.3 s after another person) where both the last 1.5 s
                   before it and the first 1.5 s of the reply carry the right name
  backchannel_acc  short replies at a turn boundary under the right name
  mixed_p/mixed_r  `mixed` flag precision / recall. Recall is over lines holding two people
                   (the second >= MIXED_MIN s and MIXED_SHARE of the line); a flag is
                   precise on such a line or on a line whose label is wrong, since the
                   flag tells the reader not to trust the label
  disagree         talk time where live and retroactive show different labels
  ttn              live only: seconds from a person's first word to their name showing
  flips            live only: label changes of an already named line, per line
Every check in a call's `expect` must hold for the live pass; the exit code is 1 otherwise.
--gate also fails when the suite's live name_acc is more than --max-gap points under the
retroactive one, or under --baseline's live name_acc by more than 0.5 points.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = HERE / "fixtures" / "live_diarize"
RATE = 16000
FPS = 50                       # scoring frames per second (20 ms)
STEP_SECONDS = 2.0             # audio appended per poll: live.follow's own poll interval
CHUNK_SECONDS = 20.0           # live run's default --chunk-seconds
LEAD, TAIL = 1.0, 3.0          # silence before the first and after the last clip
NOISE = 6                      # +-int16 dither so silence is not digital zero
HANDOFF_GAP = 0.3              # a reply this soon after another person is a handoff
HANDOFF_HEAD = 1.5             # ...and its first (and the previous line's last) this-many seconds are scored
SPLIT_SHARE = 0.05             # a label covering this share of a person counts as one of theirs
MIXED_MIN = 0.3                # seconds of each of two people that make a line mixed...
MIXED_SHARE = 0.2              # ...and the second person's least share of the line
SHERPA_SPEC = os.environ.get("WHOSAID_SHERPA_SPEC", "sherpa-onnx==1.13.8")
LIVE_MODEL = "mlx-community/whisper-large-v3-turbo"   # lib/live.py DEFAULT_MODEL
RETRO_MODEL = "mlx-community/whisper-large-v3-mlx"    # whosaid transcribe's default
REAL_REGISTRY = Path.home() / ".config" / "whosaid" / "speakers.json"
# Output devices --realtime may play to: loopback drivers only, matched by name prefix, with
# a "virtual" CoreAudio transport. A virtual transport alone is not enough (a meeting app's
# own virtual device would send the call audio into a meeting), and an aggregate or
# multi-output device ("BlackHole + Speakers") is refused: part of it is a speaker.
LOOPBACK_PREFIXES = ("blackhole", "loopback audio", "soundflower", "vb-cable")
COMBINED_WORDS = ("+", "multi-output", "aggregate", "speaker", "headphone", "airpods")

def log(msg: str) -> None:
    print(f"live-eval: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- environment

def reexec_if_needed(argv: list[str], heavy: bool, whisper: bool) -> None:
    """Re-run under uv with numpy + the pinned sherpa-onnx when plain python3 lacks them
    (the way `whosaid` runs its diarizer). --fast passes --offline: no network."""
    if not heavy or os.environ.get("WHOSAID_LIVE_EVAL_CHILD") == "1":
        return
    try:
        import numpy  # noqa: F401
        import sherpa_onnx  # noqa: F401
        if whisper:
            import mlx_whisper  # noqa: F401
        return
    except ImportError:
        pass
    uv = shutil.which("uv")
    if not uv:
        sys.exit("live-eval: needs numpy and sherpa-onnx (or uv to provide them)")
    cmd = [uv, "run", "--quiet"] + ([] if whisper else ["--offline"]) + \
          ["--with", "numpy", "--with", SHERPA_SPEC] + (["--with", "mlx-whisper"] if whisper else []) + \
          ["python", str(Path(__file__).resolve())] + argv
    os.environ["WHOSAID_LIVE_EVAL_CHILD"] = "1"
    os.execv(uv, cmd)


def fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"


# ---------------------------------------------------------------- calls

def load_bank() -> dict:
    bank = json.loads((FIX / "bank.json").read_text())
    return {(c["voice"], c["line"]): c for c in bank["clips"]}


def read_clip(path: Path):
    import numpy as np
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int32)


def cut_head(x, head: float | None):
    """The first `head` share of a clip, ended at the quietest 20 ms near that point (a gap
    between words, so no word is chopped)."""
    if not head:
        return x
    import numpy as np
    win, mid, reach = RATE // 50, int(len(x) * head), int(0.35 * RATE)
    best, at = None, mid
    for a in range(max(win, mid - reach), min(len(x) - win, mid + reach), win // 2):
        e = float(np.abs(x[a - win // 2:a + win // 2]).mean())
        if best is None or e < best:
            best, at = e, a
    return x[:at]


def clip_seconds(c: dict) -> float:
    return c["seconds"] if not c.get("head") else len(cut_head(read_clip(FIX / c["path"]), c["head"])) / RATE


def generate_events(spec: dict, bank: dict) -> list:
    """A long call: `topics` stretches, each between 2-3 people, earlier groups coming back."""
    rng = random.Random(spec["seed"])
    voices = sorted({v for v, _ in bank})
    people = [v for v in voices if not v.endswith("-far")]
    mediums = {v: sorted(l for (vv, l), c in bank.items() if vv == v and c["kind"] == "medium") for v in voices}
    shorts = {v: sorted(l for (vv, l), c in bank.items() if vv == v and c["kind"] == "short") for v in voices}
    per_topic = spec["minutes"] * 60.0 / spec["topics"]
    groups, events = [], []
    for k in range(spec["topics"]):
        if k >= 3 and rng.random() < 0.5:
            group = list(groups[rng.randrange(len(groups))])  # the call returns to an earlier topic
        else:
            group = rng.sample(people, rng.choice((2, 3)))
        groups.append(group)
        far = k >= spec["topics"] // 2 and "bea" in group  # Bea moves to a far mic halfway in
        t, prev = 0.0, None
        events.append(None)  # topic break marker, replaced below
        while t < per_topic:
            who = rng.choice([g for g in group if g != prev] or group)
            voice = "bea-far" if (who == "bea" and far) else who
            gap = rng.choice((0.06, 0.1, 0.2, 0.4, 0.7, 1.0, 1.4)) if rng.random() > 0.05 else -0.5
            line = rng.choice(mediums[voice])
            events.append([voice, line, gap])
            t += bank[(voice, line)]["seconds"] + max(0.0, gap)
            if rng.random() < 0.25 and len(group) > 1:
                other = rng.choice([g for g in group if g != who])
                ov = "bea-far" if (other == "bea" and far) else other
                bc = rng.choice(shorts[ov])
                events.append([ov, bc, rng.choice((0.1, 0.15, 0.2))])
                events.append([voice, rng.choice(mediums[voice]), rng.choice((0.12, 0.2, 0.3))])
                t += bank[(ov, bc)]["seconds"] + 4.5
            prev = who
    out, pause = [], 0.0
    for e in events:
        if e is None:
            pause = rng.uniform(3.0, 8.0)
            continue
        if pause:
            e = [e[0], e[1], pause]
            pause = 0.0
        out.append(e)
    return out


def assemble(call: dict, bank: dict):
    """(app leg int16 samples, truth events) for one call. Integer placement only, so the
    samples (and so the cassette keys) are the same on every machine."""
    import numpy as np
    events = call.get("events") or generate_events(call["generate"], bank)
    placed, t, prev_end = [], LEAD, LEAD
    for i, ev in enumerate(events):
        voice, line, gap = ev[:3]
        opt = ev[3] if len(ev) > 3 else {}
        c = bank[(voice, line)]
        if opt.get("head"):  # only the start of the sentence: the speaker is cut off mid-sentence
            c = dict(c, head=float(opt["head"]), text=c["text"] + " (cut off)")
        start = max(0.0, prev_end + float(gap)) if i else LEAD
        placed.append((c, round(start, 3)))
        prev_end = start + clip_seconds(c)
        t = max(t, prev_end)
    n = int((t + TAIL) * RATE)
    mix = np.zeros(n, dtype=np.int32)
    truth = []
    cache: dict = {}
    for i, (c, start) in enumerate(placed):
        x = cache.get((c["path"], c.get("head")))
        if x is None:
            x = cache[(c["path"], c.get("head"))] = cut_head(read_clip(FIX / c["path"]), c.get("head"))
        a = int(round(start * RATE))
        mix[a:a + len(x)] += x[: n - a]
        truth.append({"i": i, "person": c["person"], "voice": c["voice"], "kind": c["kind"],
                      "text": c["text"], "start": round(a / RATE, 3), "end": round((a + len(x)) / RATE, 3)})
    rs = np.random.RandomState(218)
    mix += rs.randint(-NOISE, NOISE + 1, size=n).astype(np.int32)
    return np.clip(mix, -32768, 32767).astype("<i2"), truth


def tag_events(truth: list) -> None:
    """Mark handoffs (a reply < HANDOFF_GAP after another person) and backchannels
    (a short line with another person on either side within HANDOFF_GAP)."""
    for k, e in enumerate(truth):
        prev = truth[k - 1] if k else None
        nxt = truth[k + 1] if k + 1 < len(truth) else None
        near_prev = prev and prev["person"] != e["person"] and e["start"] - prev["end"] < HANDOFF_GAP
        near_next = nxt and nxt["person"] != e["person"] and nxt["start"] - e["end"] < HANDOFF_GAP
        e["handoff"] = bool(near_prev) and e["kind"] != "short"
        e["backchannel"] = e["kind"] == "short" and bool(near_prev or near_next)


def write_wav(path: Path, x) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(x.tobytes())


# ---------------------------------------------------------------- cassette (recorded Whisper)

class CassetteMiss(BaseException):
    """Not an Exception: live.py keeps going past a failed transcription (`except
    Exception`), and a --fast run must stop on audio the cassette doesn't hold."""


def chunk_key(samples) -> str:
    return hashlib.sha1(samples.astype("<f4").tobytes()).hexdigest()[:20]


SEG_KEYS = ("start", "end", "text", "no_speech_prob", "avg_logprob", "compression_ratio")


def slim(segs: list) -> list:
    out = []
    for s in segs:
        d = {k: (round(float(s[k]), 3) if k != "text" else s[k]) for k in SEG_KEYS if s.get(k) is not None}
        if s.get("words"):
            d["words"] = [{"start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3),
                           "word": w["word"]} for w in s["words"]]
        out.append(d)
    return out


def cassette_path(name: str) -> Path:
    return FIX / "cassette" / f"{name}.json"


def load_cassette(name: str) -> dict:
    p = cassette_path(name)
    return json.loads(p.read_text()) if p.exists() else {"schema": 1, "live": {}, "retro": None}


def save_cassette(name: str, cas: dict) -> None:
    p = cassette_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cas, separators=(",", ":"), sort_keys=True) + "\n")


def cassette_transcriber(cas: dict, words: bool, record: bool):
    """transcribe(samples) -> segments, from the cassette; with record, from the local
    Whisper model (both word-time variants are stored, so a later default flip replays)."""
    variant = "words" if words else "plain"
    real = {}
    if record:
        import live
        real = {"plain": live.whisper_transcriber(LIVE_MODEL, words=False),
                "words": live.whisper_transcriber(LIVE_MODEL, words=True)}

    def run(samples) -> list:
        key = chunk_key(samples)
        cas.setdefault("_used", set()).add(key)
        got = cas["live"].get(key)
        if record and (got is None or variant not in got):
            got = {v: slim(fn(samples)) for v, fn in real.items()}
            cas["live"][key] = got
        if got is None or variant not in got:
            raise CassetteMiss(f"no recorded Whisper output for chunk {key} ({len(samples) / RATE:.1f} s, "
                               f"{variant}); re-record with --record")
        return json.loads(json.dumps(got[variant]))
    return run


# ---------------------------------------------------------------- live pass

def wav_header() -> bytes:
    """A WAV header with placeholder sizes, as a recorder that is still writing leaves it."""
    fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + RATE.to_bytes(4, "little") + \
          (RATE * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    return b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt + \
        b"data" + b"\xff\xff\xff\xff"


def make_voices():
    """live_voices.load(), as `whosaid live run` builds it per call, over the throwaway
    registry WHOSAID_SPEAKER_DB points at."""
    import live_voices
    v = live_voices.load(lambda m: None)
    if v is None:
        raise RuntimeError("live names are off (no registry, model or sherpa-onnx)")
    return v


def live_split_words_default() -> bool:
    """What `whosaid live run` asks Whisper for when no flag is given."""
    import live
    p = argparse.ArgumentParser(add_help=False)
    real_parse = argparse.ArgumentParser.parse_args
    seen = {}

    def grab(self, args=None, namespace=None):
        a = real_parse(self, args, namespace)
        seen["a"] = a
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = grab
    try:
        live.main(["run"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real_parse
    del p
    return bool(getattr(seen.get("a"), "split_words", False))


def run_live(app, transcribe, voices, tmp: Path) -> dict:
    """Feed one call through live.follow() and return its final entries, every snapshot
    of what the live view showed, the Whisper segments it got and the chunk lengths."""
    import numpy as np
    import live
    cap = tmp / "capture"
    cap.mkdir(parents=True)
    into = tmp / "ws"
    for leg in ("app.wav", "mic.wav"):
        (cap / leg).write_bytes(wav_header())
    mic = np.zeros_like(app)
    step = int(STEP_SECONDS * RATE)
    state = {"fed": 0, "done": False, "base": 0.0}
    segs_log, snapshots, chunk_lens = [], [], []

    def stop() -> bool:
        if state["fed"] >= len(app):
            if state["done"]:
                return True
            state["done"] = True  # one more poll picks up the last piece first
            return False
        a, b = state["fed"], min(len(app), state["fed"] + step)
        for leg, x in (("app.wav", app), ("mic.wav", mic)):
            with open(cap / leg, "ab") as f:
                f.write(x[a:b].tobytes())
        state["fed"] = b
        return False

    def logged(samples):
        out = transcribe(samples)
        for s in out:
            segs_log.append(dict(s, abs_start=round(state["base"] + float(s["start"]), 2),
                                 abs_end=round(state["base"] + float(s["end"]), 2),
                                 base=state["base"]))
        return out

    spans: dict = {}  # (t, text) -> (start, end) seconds since the call start, of each line made

    orig_pl = live.placed_lines

    def pl(seg, text, off, base, samples, voices_, *rest):
        lines = orig_pl(seg, text, off, base, samples, voices_, *rest)
        end = base + float(seg.get("end") or off)
        for k, ln in enumerate(lines):
            b = lines[k + 1]["t"] if k + 1 < len(lines) else end
            spans[(round(float(ln["t"]), 2), ln["text"])] = (float(ln["t"]), float(b))
        return lines

    orig_tc, orig_ws, orig_log = live.transcribe_chunks, live.write_session, live.log

    def tc(chunks, tr, prior, v=None):
        them = chunks.get("them")
        if them is not None:
            state["base"] = them[0] / RATE
            chunk_lens.append(round(len(them[1]) / RATE, 2))
        return orig_tc(chunks, tr, prior, v)

    def ws(md, jl, started, entries, ended):
        orig_ws(md, jl, started, entries, ended)
        snapshots.append({"clock": round(state["fed"] / RATE, 2),
                          "entries": json.loads(json.dumps(entries))})

    live.transcribe_chunks, live.write_session, live.log, live.placed_lines = tc, ws, (lambda m: None), pl
    try:
        live.follow(cap, into, logged, chunk_seconds=CHUNK_SECONDS, poll_seconds=0.0, stop=stop,
                    voices=voices)
    finally:
        live.transcribe_chunks, live.write_session, live.log, live.placed_lines = orig_tc, orig_ws, orig_log, orig_pl
    final = snapshots[-1]["entries"] if snapshots else []
    return {"entries": final, "snapshots": snapshots, "segs": segs_log, "chunks": chunk_lens, "spans": spans}


def live_label(e: dict) -> str:
    if e.get("who") == "me":
        return e.get("name") or "me"
    return e.get("name") or f"them:{e.get('voice') or '-'}"


def live_lines(run: dict, entries: list | None = None) -> list:
    """Each live line with the time span it covers: captured where placed_lines made it
    (a whole Whisper segment, or each part of a split one), else its segment's span."""
    by_start: dict = {}
    for s in run["segs"]:
        by_start.setdefault(s["abs_start"], []).append(s)
    out = []
    for e in (run["entries"] if entries is None else entries):
        t = round(float(e["t"]), 2)
        span = run["spans"].get((t, e["text"]))
        if span is None:
            got = by_start.get(t) or []
            same = [s for s in got if (s.get("text") or "").strip() == e["text"]] or got
            span = (same[0]["abs_start"], same[0]["abs_end"]) if same else None
        if span is None:
            continue
        out.append({"a": span[0], "b": span[1], "label": live_label(e), "mixed": bool(e.get("mixed")),
                    "n": e.get("n"), "voice": e.get("voice"), "why": e.get("mixed_why"), "text": e["text"]})
    return out


# ---------------------------------------------------------------- retroactive pass

def run_retro(wav: Path, whisper: dict, registry_db: Path, tmp: Path, name: str) -> dict:
    """lib/diarize_sherpa.py on the whole call (the post-call pass `whosaid transcribe` runs),
    then its own build_turns over the sidecar to get each turn's float start."""
    import diarize_sherpa as ds
    wj = tmp / f"{name}.json"
    wj.write_text(json.dumps(whisper))
    out = tmp / "retro"
    env = {k: v for k, v in os.environ.items() if k not in ("WHOSAID_OWNER", "WHOSAID_WORKSPACE")}
    env["WHOSAID_SPEAKER_DB"] = str(registry_db)
    cmd = [sys.executable, str(ROOT / "lib" / "diarize_sherpa.py"), str(wav), "--whisper-json", str(wj),
           "--outdir", str(out), "--name", name, "--snippets", "0", "--no-save"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"diarize_sherpa failed ({r.returncode}): {r.stderr[-2000:]}")
    sc = json.loads((out / f"{name}.diarization.json").read_text())
    import inspect
    if "stored" in inspect.signature(ds.build_turns).parameters:
        turns = ds.build_turns(sc["segments"], str(wj), stored=sc.get("backchannel") or [])
    else:  # lib/ before #216 (a --baseline-ref): no boundary backchannels
        turns = ds.build_turns(sc["segments"], str(wj))
    names = dict(sc.get("names") or {})
    for c, lab in (sc.get("local_labels") or {}).items():
        names[c] = lab.get("name") if isinstance(lab, dict) else lab
    flagged = {(round(float(d["start"]), 3), d["text"]) for d in sc.get("backchannel") or []
               if d.get("action") == "flagged"}
    words = sorted((float(w["start"]), float(w["end"])) for s in whisper.get("segments", [])
                   for w in (s.get("words") or []))
    lines = []
    starts = [float(st) for _, st, _ in turns] + [float("inf")]
    for k, (sp, st, texts) in enumerate(turns):
        ws = [w for w in words if starts[k] - 1e-6 <= w[0] < starts[k + 1] - 1e-6]
        if not ws:
            continue
        text = " ".join(texts)
        lines.append({"a": ws[0][0], "b": max(w[1] for w in ws), "label": names.get(sp, sp),
                      "mixed": (round(float(st), 3), text) in flagged, "text": text})
    return {"lines": lines, "sidecar": {k: sc.get(k) for k in ("names", "num_speakers", "backchannel")}}


# ---------------------------------------------------------------- scoring

def frames(t: float) -> int:
    return int(round(t * FPS))


class Track:
    """One system's label per 20 ms frame (-1 = no line there)."""

    def __init__(self, lines: list, n: int, enrolled: set):
        import numpy as np
        self.labels: list[str] = []
        self.code: dict[str, int] = {}
        self.t = np.full(n, -1, dtype=np.int32)
        for ln in sorted(lines, key=lambda x: x["a"]):
            c = self.code.setdefault(ln["label"], len(self.code))
            if c == len(self.labels):
                self.labels.append(ln["label"])
            self.t[frames(ln["a"]):max(frames(ln["a"]) + 1, frames(ln["b"]))] = c
        self.named = np.array([lab in enrolled for lab in self.labels] + [False], dtype=bool)

    def is_named(self):
        return self.named[self.t]          # index -1 lands on the trailing False

    def is_unnamed(self):
        return (self.t >= 0) & ~self.named[self.t]

    def of(self, label: str):
        c = self.code.get(label)
        import numpy as np
        return np.zeros_like(self.t, dtype=bool) if c is None else self.t == c


def ratio(a: float, b: float):
    return None if b <= 0 else a / b


def score_system(lines: list, truth: list, n: int, enrolled: set, guests: set) -> dict:
    import numpy as np
    tr = Track(lines, n, enrolled)
    who = {}
    for e in truth:
        who.setdefault(e["person"], np.zeros(n, dtype=bool))[frames(e["start"]):frames(e["end"])] = True
    num = {"name": 0, "unnamed": 0, "missing": 0, "den": 0, "guest_named": 0, "guest_den": 0}
    split, present = 0, 0
    for p, m in who.items():
        if p in enrolled:
            num["den"] += int(m.sum())
            num["name"] += int((m & tr.of(p)).sum())
            num["unnamed"] += int((m & tr.is_unnamed()).sum())
            num["missing"] += int((m & (tr.t < 0)).sum())
            present += 1
            got = tr.t[m & (tr.t >= 0)]
            if len(got):
                shares = np.bincount(got, minlength=len(tr.labels)) / max(1, int(m.sum()))
                named_share = shares[tr.code[p]] if p in tr.code else 0.0
                unnamed_share = sum(s for i, s in enumerate(shares) if not tr.named[i])
                split += int(named_share >= SPLIT_SHARE and unnamed_share >= SPLIT_SHARE)
        elif p in guests:
            num["guest_den"] += int(m.sum())
            num["guest_named"] += int((m & tr.is_named()).sum())

    def span_ok(person: str, t0: float, t1: float) -> bool:
        a = frames(t0)
        seg = tr.t[a:max(a + 1, frames(t1))]
        vals, counts = np.unique(seg, return_counts=True)
        top = int(vals[np.argmax(counts)])
        return top >= 0 and tr.labels[top] == person

    def event_ok(e, head: float | None) -> bool:
        return span_ok(e["person"], e["start"], min(e["end"], e["start"] + head) if head else e["end"])
    # a handoff is right when both sides are: the last HANDOFF_HEAD s of who stopped and
    # the first HANDOFF_HEAD s of who replied (one label over both gets one of them wrong)
    hand = []
    for k, e in enumerate(truth):
        if e["handoff"] and e["person"] in enrolled and truth[k - 1]["person"] in enrolled \
                and truth[k - 1]["kind"] != "short":  # a reply after a backchannel is scored as one
            p = truth[k - 1]
            hand.append(event_ok(e, HANDOFF_HEAD) and span_ok(p["person"], max(p["start"], p["end"] - HANDOFF_HEAD), p["end"]))
    bc = [event_ok(e, None) for e in truth if e["backchannel"] and e["person"] in enrolled]
    tp = fp = fn = 0
    for ln in lines:
        a, b = frames(ln["a"]), max(frames(ln["a"]) + 1, frames(ln["b"]))
        held = {p: int(m[a:b].sum()) for p, m in who.items()}
        ranked = sorted(held.items(), key=lambda kv: kv[1], reverse=True)
        # two people: the second holds MIXED_MIN s and MIXED_SHARE of the line (a split
        # line whose cut is a few hundred ms off is not a line holding two people)
        real = len(ranked) > 1 and ranked[1][1] >= MIXED_MIN * FPS and ranked[1][1] >= MIXED_SHARE * (b - a)
        top = ranked[0][0] if ranked and ranked[0][1] else None
        wrong = top is not None and not (ln["label"] == top or (top in guests and ln["label"].startswith("them")))
        # the flag says "don't trust this label": right on a mixed line or a wrong label
        tp += ln["mixed"] and (real or wrong)
        fp += ln["mixed"] and not (real or wrong)
        fn += real and not ln["mixed"]
    return {"name_acc": ratio(num["name"], num["den"]),
            "name_acc_shown": ratio(num["name"], num["den"] - num["missing"]),
            "unnamed": ratio(num["unnamed"], num["den"]),
            "missing": ratio(num["missing"], num["den"]),
            "guest_named": ratio(num["guest_named"], num["guest_den"]),
            "split_voice": ratio(split, present),
            "handoff_acc": ratio(sum(hand), len(hand)), "handoff_n": len(hand),
            "backchannel_acc": ratio(sum(bc), len(bc)), "backchannel_n": len(bc),
            "mixed_p": ratio(tp, tp + fp), "mixed_r": ratio(tp, tp + fn),
            "_num": num, "_hand": [sum(hand), len(hand)], "_bc": [sum(bc), len(bc)],
            "_mixed": [tp, fp, fn], "_split": [split, present]}


def disagreement(live_lines_: list, retro_lines: list, truth: list, n: int, enrolled: set) -> dict:
    """Where live and retroactive show different labels on the same speech (a name, or
    "unnamed" for any unnamed voice), per frame and per turn, and which side was right."""
    import numpy as np
    labels = sorted(enrolled) + ["unnamed"]
    code = {x: i for i, x in enumerate(labels)}

    def track(lines):
        tr = Track(lines, n, enrolled)
        lut = np.array([code[x] if x in enrolled else code["unnamed"] for x in tr.labels] + [-1])
        return lut[tr.t]
    a, b = track(live_lines_), track(retro_lines)
    who = np.full(n, -1)
    for e in truth:
        who[frames(e["start"]):frames(e["end"])] = code.get(e["person"], code["unnamed"])
    both = (who >= 0) & (a >= 0) & (b >= 0)
    diff = both & (a != b)
    turns = turns_diff = live_off = 0
    for e in truth:
        sa, sb = a[frames(e["start"]):frames(e["end"])], b[frames(e["start"]):frames(e["end"])]
        if len(sa) == 0 or (sa < 0).all() or (sb < 0).all():
            continue
        top = lambda x: int(np.bincount(x[x >= 0]).argmax())  # noqa: E731
        turns += 1
        turns_diff += top(sa) != top(sb)
        live_off += top(sa) != top(sb) and top(sb) == code.get(e["person"], code["unnamed"])
    return {"frames": [int(diff.sum()), int(both.sum())],
            "live_wrong": int((diff & (b == who)).sum()), "retro_wrong": int((diff & (a == who)).sum()),
            "turns": [turns_diff, turns], "live_off": [live_off, turns]}


def live_timing(run: dict, truth: list, n: int, enrolled: set) -> dict:
    """Seconds until each enrolled person's name first shows on their own speech, and how
    often an already named line changes its label, from every write of the live view."""
    import numpy as np
    first = {}
    for e in truth:
        if e["person"] in enrolled:
            first.setdefault(e["person"], e["start"])
    who = {}
    for e in truth:
        who.setdefault(e["person"], np.zeros(n, dtype=bool))[frames(e["start"]):frames(e["end"])] = True
    named_at: dict[str, float] = {}
    shown: dict = {}
    flips = changes = 0
    for snap in run["snapshots"]:
        lines = live_lines(run, snap["entries"])
        tr = Track(lines, n, enrolled)
        for p in first:
            if p not in named_at and int((who[p] & tr.of(p)).sum()) >= FPS:  # a second of them, named
                named_at[p] = snap["clock"]
        for e in snap["entries"]:
            lab = live_label(e)
            old = shown.get(e.get("n"))
            if old is not None and old != lab:
                changes += 1
                flips += old in enrolled  # a name taken away or swapped; unnamed -> named is naming
            shown[e.get("n")] = lab
    end = run["snapshots"][-1]["clock"] if run["snapshots"] else 0.0
    ttn = {p: round(named_at.get(p, end) - first[p], 1) for p in first}
    return {"ttn": ttn, "ttn_mean": round(sum(ttn.values()) / len(ttn), 1) if ttn else None,
            "never_named": sorted(p for p in first if p not in named_at),
            "flips": ratio(flips, len(shown)), "label_changes": changes, "lines": len(shown)}


# ---------------------------------------------------------------- report

ROWS = (("name_acc", "correct name (talk time)", True),
        ("name_acc_shown", "...of the time it shows a line", True), ("unnamed", "enrolled but unnamed", False),
        ("missing", "no line at all", False), ("guest_named", "guest given a name", False),
        ("split_voice", "people split named+unnamed", False), ("handoff_acc", "handoffs right (both sides)", True),
        ("backchannel_acc", "backchannels right", True), ("mixed_p", "mixed flag precision", True),
        ("mixed_r", "mixed flag recall", True))


def pct(v) -> str:
    return "   -  " if v is None else f"{100 * v:5.1f}%"


def print_call(name: str, res: dict) -> None:
    lv, rt = res["live"], res["retro"]
    print(f"\n== {name}  ({res['seconds'] / 60:.1f} min, {res['events']} clips)")
    print(f"  {'metric':30s} {'live':>7s} {'retro':>7s}")
    for k, label, _ in ROWS:
        extra = f"  (n={lv[k[:-4] + '_n']})" if k.endswith("_acc") and k[:-4] + "_n" in lv else ""
        print(f"  {label:30s} {pct(lv.get(k)):>7s} {pct(rt.get(k)):>7s}{extra}")
    dz = res["_disagree"]
    print(f"  {'live vs retro disagree':30s} {pct(res['disagree']):>7s}  (turns {pct(res['disagree_turns']).strip()}; "
          f"of the differing time live was wrong {pct(res['disagree_live_wrong']).strip()}, "
          f"retro wrong {pct(ratio(dz['retro_wrong'], dz['frames'][0])).strip()})")
    print(f"  {'turns live got wrong, retro right':30s} {pct(res['disagree_live_off']):>7s}")
    tm = res["timing"]
    print(f"  live: name shows after {tm['ttn_mean']} s on average {tm['ttn']}"
          + (f", never named: {', '.join(tm['never_named'])}" if tm["never_named"] else "")
          + f"; flips {pct(tm['flips']).strip()} of {tm['lines']} lines ({tm['label_changes']} label changes)")
    for x in res["checks"]:
        print(f"  {'PASS' if x['ok'] else 'FAIL'} {x['metric']} = {pct(x['value']).strip()} "
              f"({'>=' if 'min' in x else '<='} {pct(x.get('min', x.get('max'))).strip()})")


def aggregate(results: dict) -> dict:
    out = {}
    for sysname in ("live", "retro"):
        n = {"name": 0, "unnamed": 0, "missing": 0, "den": 0, "guest_named": 0, "guest_den": 0}
        h = [0, 0]
        b = [0, 0]
        mx = [0, 0, 0]
        sp = [0, 0]
        for r in results.values():
            s = r[sysname]
            for k in n:
                n[k] += s["_num"][k]
            h = [h[0] + s["_hand"][0], h[1] + s["_hand"][1]]
            b = [b[0] + s["_bc"][0], b[1] + s["_bc"][1]]
            mx = [x + y for x, y in zip(mx, s["_mixed"])]
            sp = [sp[0] + s["_split"][0], sp[1] + s["_split"][1]]
        out[sysname] = {"name_acc": ratio(n["name"], n["den"]),
                        "name_acc_shown": ratio(n["name"], n["den"] - n["missing"]),
                        "unnamed": ratio(n["unnamed"], n["den"]),
                        "missing": ratio(n["missing"], n["den"]),
                        "guest_named": ratio(n["guest_named"], n["guest_den"]),
                        "split_voice": ratio(sp[0], sp[1]),
                        "handoff_acc": ratio(*h), "backchannel_acc": ratio(*b),
                        "mixed_p": ratio(mx[0], mx[0] + mx[1]), "mixed_r": ratio(mx[0], mx[0] + mx[2])}
    d = [sum(r["_disagree"]["frames"][0] for r in results.values()),
         sum(r["_disagree"]["frames"][1] for r in results.values())]
    t = [sum(r["_disagree"]["turns"][0] for r in results.values()),
         sum(r["_disagree"]["turns"][1] for r in results.values())]
    lo = [sum(r["_disagree"]["live_off"][0] for r in results.values()),
          sum(r["_disagree"]["live_off"][1] for r in results.values())]
    out["disagree"], out["disagree_turns"], out["disagree_live_off"] = ratio(*d), ratio(*t), ratio(*lo)
    return out


# metrics the gate holds against the baseline: (key, higher is better)
GATED = (("name_acc", True), ("handoff_acc", True), ("backchannel_acc", True), ("unnamed", False),
         ("split_voice", False), ("guest_named", False), ("disagree_live_off", False))


def gate(suite: dict, max_gap: float, baseline: dict | None, tolerance: float = 0.5) -> list[str]:
    """Reasons the suite fails the release gate (empty = pass): live's correct-name share
    more than `max_gap` points under retroactive's, or any GATED metric more than
    `tolerance` points worse than in `baseline` (an --out JSON, e.g. of the last tag)."""
    bad = []
    lv, rt = suite["live"]["name_acc"] or 0.0, suite["retro"]["name_acc"] or 0.0
    if 100 * (rt - lv) > max_gap:
        bad.append(f"live name_acc {100 * lv:.1f}% is {100 * (rt - lv):.1f} points under retroactive "
                   f"{100 * rt:.1f}% (max gap {max_gap:g})")
    if baseline:
        old_suite = baseline.get("suite") or {}
        if sorted(baseline.get("calls") or []) != sorted(suite.get("_calls") or baseline.get("calls") or []):
            bad.append(f"the baseline scored other calls ({', '.join(sorted(baseline.get('calls') or []))}); "
                       f"compare the same --only")
        for key, up in GATED:
            new = suite[key] if key.startswith("disagree") else suite["live"].get(key)
            old = old_suite.get(key) if key.startswith("disagree") else (old_suite.get("live") or {}).get(key)
            if new is None or old is None:
                continue
            worse = 100 * ((old - new) if up else (new - old))
            if worse > tolerance:
                bad.append(f"live {key} {100 * new:.1f}% regressed from the baseline's {100 * old:.1f}% "
                           f"({worse:.1f} points, tolerance {tolerance:g})")
    return bad


def baseline_at(ref: str, argv: list[str]) -> tuple[dict | None, str]:
    """Scores of lib/ at git `ref` ("last-tag": the newest tag HEAD contains) on this
    eval, its fixtures and cassettes: a detached worktree of ref with today's test/
    copied over runs --fast --out. (None, why) when it can't run."""
    def git(*args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if ref == "last-tag":
        r = git("describe", "--tags", "--abbrev=0")
        if r.returncode != 0:
            return None, "no tag to compare with"
        ref = r.stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="whosaid-live-eval-base-"))
    wt = tmp / "wt"
    r = git("worktree", "add", "--detach", str(wt), ref)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, f"git worktree add {ref}: {r.stderr.strip()}"
    try:
        shutil.rmtree(wt / "test", ignore_errors=True)
        shutil.copytree(HERE, wt / "test", ignore=shutil.ignore_patterns("__pycache__"))
        out = tmp / "base.json"
        keep = []
        skip = {"--gate", "--baseline", "--baseline-ref", "--out", "--max-gap", "--tolerance"}
        it = iter(argv)
        for x in it:
            if x.split("=")[0] in skip:
                if "=" not in x and x != "--gate":
                    next(it, None)
                continue
            keep.append(x)
        log(f"baseline: lib/ at {ref}")
        env = {k: v for k, v in os.environ.items() if k != "WHOSAID_LIVE_EVAL_CHILD"}
        r = subprocess.run([sys.executable, str(wt / "test" / "live_diarize_eval.py"), *keep, "--out", str(out)],
                           capture_output=True, text=True, env=env, timeout=3600)
        if not out.exists():
            return None, f"the eval at {ref} wrote no scores (rc {r.returncode}): {r.stderr.strip()[-400:]}"
        data = json.loads(out.read_text())
        data["ref"] = ref
        return data, ref
    finally:
        git("worktree", "remove", "--force", str(wt))
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- realtime

def audio_devices() -> list[dict]:
    """[{index, name, transport, outputs}] for every CoreAudio device, in the order of
    kAudioHardwarePropertyDevices: the order ffmpeg's audiotoolbox output numbers them
    (-audio_device_index counts input devices too). Read-only, through CoreAudio's
    property API with ctypes: listing devices with ffmpeg itself would open the default
    output. [] when CoreAudio can't be read (not macOS)."""
    import ctypes
    import ctypes.util
    import struct
    try:
        ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio") or "")
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or "")
    except OSError:
        return []

    class Addr(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

    def cc(code: str) -> int:
        return struct.unpack(">I", code.encode())[0]
    u32p = ctypes.POINTER(ctypes.c_uint32)
    ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                                  ctypes.c_void_p, u32p]
    ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                              ctypes.c_void_p, u32p, ctypes.c_void_p]
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    def size(obj: int, sel: str, scope: str = "glob") -> int:
        n = ctypes.c_uint32(0)
        st = ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(Addr(cc(sel), cc(scope), 0)), 0, None, ctypes.byref(n))
        return n.value if st == 0 else 0

    def get(obj: int, sel: str, buf) -> bool:
        n = ctypes.c_uint32(ctypes.sizeof(buf))
        return ca.AudioObjectGetPropertyData(obj, ctypes.byref(Addr(cc(sel), cc("glob"), 0)), 0, None,
                                             ctypes.byref(n), ctypes.byref(buf)) == 0
    count = size(1, "dev#") // 4  # 1 = kAudioObjectSystemObject
    ids = (ctypes.c_uint32 * max(1, count))()
    if not count or not get(1, "dev#", ids):
        return []
    out = []
    for i, dev in enumerate(ids[:count]):
        name, ref = "", ctypes.c_void_p()
        if get(dev, "lnam", ref) and ref.value:
            buf = ctypes.create_string_buffer(1024)
            if cf.CFStringGetCString(ref, buf, 1024, 0x08000100):  # UTF-8
                name = buf.value.decode("utf-8", "replace")
            cf.CFRelease(ref)
        tran = ctypes.c_uint32(0)
        get(dev, "tran", tran)
        out.append({"index": i, "name": name, "outputs": size(dev, "stm#", "outp") // 4,
                    "transport": struct.pack(">I", tran.value).decode("latin-1") if tran.value else ""})
    return out


def check_output_device(name: str, devices: list[dict]) -> tuple[bool, str]:
    """(ok, reason): only one installed loopback driver with output streams may be played to.
    The name must match one device exactly, start with a LOOPBACK_PREFIXES entry, name no
    speaker or combination (COMBINED_WORDS), and the device's transport must be virtual."""
    want = (name or "").strip().lower()
    found = [d for d in devices if d["name"].strip().lower() == want]
    if not found:
        return False, f"no output device named {name!r}"
    if len(found) > 1:
        return False, f"{len(found)} devices are named {name!r}; can't tell which one ffmpeg would get"
    d = found[0]
    low = d["name"].lower()
    if d["outputs"] <= 0:
        return False, f"{d['name']!r} has no output streams"
    if not low.startswith(LOOPBACK_PREFIXES) or any(w in low for w in COMBINED_WORDS):
        return False, (f"{d['name']!r} is not a loopback device; --realtime only plays to a device whose "
                       f"name starts with {', '.join(LOOPBACK_PREFIXES)}, never to speakers, headphones "
                       f"or a combination of them")
    if d["transport"] != "virt":
        return False, f"{d['name']!r} has transport {d['transport'] or 'unknown'!r}, not a virtual (loopback) driver"
    return True, "loopback"


def run_realtime(a, call: dict, bank: dict) -> int:
    """Play one call at 1x to a loopback device while `whosaid record --app` captures the
    player and `whosaid live run --once` transcribes it, then print the live lines per
    person next to the call's turns. The device is checked again right before ffmpeg
    starts, and ffmpeg gets the index CoreAudio has for that name at that moment."""
    if not a.app:
        log("--realtime needs --app BUNDLE_ID: the app whose audio `whosaid record --app` captures "
            "(the one running ffmpeg, e.g. your terminal)")
        return 2
    app, truth = assemble(call, bank)
    tmp = Path(tempfile.mkdtemp(prefix="whosaid-live-eval-rt-"))
    wav = tmp / f"{call['name']}.wav"
    write_wav(wav, app)
    whosaid = str(ROOT / "whosaid")
    env = dict(os.environ)  # main() pointed WHOSAID_SPEAKER_DB at the throwaway registry
    rec = subprocess.Popen([whosaid, "record", "--app", a.app, "--no-mic", "--into", str(tmp / "rec")], env=env)
    follow = subprocess.Popen([whosaid, "live", "run", "--once", "--into", str(tmp / "ws")], env=env)
    try:
        time.sleep(5)
        devices = audio_devices()
        ok, why = check_output_device(a.device, devices)
        if not ok:
            log(f"refusing to play: {why}")
            return 3
        idx = next(d["index"] for d in devices if d["name"].strip().lower() == a.device.strip().lower())
        play = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", str(wav), "-f",
                               "audiotoolbox", "-audio_device_index", str(idx), "-"],
                              timeout=len(app) / RATE + 120)
    finally:
        rec.terminate()
    try:
        follow.wait(timeout=600)
    except subprocess.TimeoutExpired:
        follow.terminate()
    sessions = sorted((tmp / "ws" / ".live").glob("*.jsonl"))
    lines = [json.loads(x) for x in sessions[-1].read_text().splitlines() if x.strip()] if sessions else []
    said: dict = {}
    for e in truth:
        said[e["person"]] = said.get(e["person"], 0) + 1
    shown: dict = {}
    for e in lines:
        if e.get("who") == "them":
            k = e.get("name") or "them"
            shown[k] = shown.get(k, 0) + 1
    log(f"played {wav} to {a.device} (index {idx}, rc {play.returncode}); "
        f"{len(lines)} live line(s) in {sessions[-1] if sessions else tmp / 'ws'}")
    print(f"turns per person {said}\nlive lines per name {shown}")
    return 0 if play.returncode == 0 and lines else 1


# ---------------------------------------------------------------- main

def run_call(call: dict, bank: dict, registry: dict, reg_db: Path, words: bool, record: bool) -> dict:
    app, truth = assemble(call, bank)
    tag_events(truth)
    n = frames(len(app) / RATE) + 1
    enrolled = {s["name"] for s in registry["speakers"]}
    guests = {e["person"] for e in truth} - enrolled
    cas = load_cassette(call["name"])
    tmp = Path(tempfile.mkdtemp(prefix=f"whosaid-live-eval-{call['name']}-"))
    try:
        run = run_live(app, cassette_transcriber(cas, words, record), make_voices(), tmp / "live")
        wav = tmp / f"{call['name']}.wav"
        write_wav(wav, app)
        audio_key = hashlib.sha1(app.tobytes()).hexdigest()[:20]
        if record and (cas.get("retro") is None or cas.get("audio") != audio_key):
            log(f"{call['name']}: whole-file Whisper ({RETRO_MODEL})")
            r = subprocess.run([sys.executable, str(ROOT / "lib" / "transcribe_mlx.py"), str(wav), "--model",
                                RETRO_MODEL, "--outdir", str(tmp / "asr"), "--format", "json", "--name", "w"],
                               capture_output=True, text=True, timeout=7200)
            if r.returncode != 0:
                raise RuntimeError(f"transcribe_mlx failed: {r.stderr[-1500:]}")
            cas["retro"] = {"segments": slim(json.loads((tmp / "asr" / "w.json").read_text())["segments"])}
            cas["audio"] = audio_key
        if record:  # keep only what this run asked for: an edited call leaves no stale chunks
            cas["live"] = {k: v for k, v in cas["live"].items() if k in cas.get("_used", set())}
            cas.pop("_used", None)
            save_cassette(call["name"], cas)
        if cas.get("retro") is None or cas.get("audio") != audio_key:
            raise CassetteMiss(f"{call['name']}: the recorded whole-file Whisper is for other audio "
                               f"(the call changed); re-record with --record")
        retro = run_retro(wav, cas["retro"], reg_db, tmp, call["name"])
        ll = live_lines(run)
        res = {"seconds": len(app) / RATE, "events": len(truth),
               "live": score_system(ll, truth, n, enrolled, guests),
               "retro": score_system(retro["lines"], truth, n, enrolled, guests),
               "timing": live_timing(run, truth, n, enrolled),
               "chunks": run["chunks"], "words": words}
        res["_disagree"] = disagreement(ll, retro["lines"], truth, n, enrolled)
        dz = res["_disagree"]
        res["disagree"], res["disagree_turns"] = ratio(*dz["frames"]), ratio(*dz["turns"])
        res["disagree_live_wrong"] = ratio(dz["live_wrong"], dz["frames"][0])
        res["disagree_live_off"] = ratio(*dz["live_off"])  # turns where the after-call label was right and live's was not
        res["checks"] = []
        for x in call.get("expect") or []:
            v = res[x["metric"]] if x["metric"].startswith("disagree") else res["live"].get(x["metric"])
            ok = v is not None and (v >= x["min"] if "min" in x else v <= x["max"])
            res["checks"].append(dict(x, value=v, ok=ok))
        bad = [c for c in run["chunks"][:-1] if not (CHUNK_SECONDS - 4.5 <= c <= CHUNK_SECONDS + 2.5)]
        if bad:
            raise RuntimeError(f"{call['name']}: live chunks off the 1x cadence: {bad}")
        if os.environ.get("WHOSAID_LIVE_EVAL_DUMP"):
            d = Path(os.environ["WHOSAID_LIVE_EVAL_DUMP"]) / call["name"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "live.json").write_text(json.dumps(ll, indent=1))
            (d / "retro.json").write_text(json.dumps(retro["lines"], indent=1))
            (d / "truth.json").write_text(json.dumps(truth, indent=1))
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="live_diarize_eval.py", description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fast", action="store_true", help="CI: replay recorded Whisper, no network")
    mode.add_argument("--record", action="store_true", help="dev: re-record the Whisper cassette locally")
    mode.add_argument("--realtime", action="store_true", help="play one call through the capture path at 1x")
    mode.add_argument("--list-devices", action="store_true", help="print output devices and whether each is allowed")
    ap.add_argument("--device", help="with --realtime: the loopback output device name")
    ap.add_argument("--app", help="with --realtime: bundle ID of the player `whosaid record --app` captures")
    ap.add_argument("--only", help="comma-separated call names")
    ap.add_argument("--split-words", choices=("default", "on", "off"), default="default",
                    help="live Whisper word times: as `whosaid live run` does (default), or forced")
    ap.add_argument("--out", help="write the scores as JSON")
    ap.add_argument("--gate", action="store_true", help="exit 1 when live falls too far under retroactive")
    ap.add_argument("--max-gap", type=float, default=5.0, help="points live name_acc may trail retroactive")
    ap.add_argument("--baseline", help="with --gate: an --out JSON to hold live to (no metric worse)")
    ap.add_argument("--baseline-ref", help="with --gate: score lib/ at this git ref (or last-tag) with this "
                                           "eval and cassettes, and hold live to it")
    ap.add_argument("--tolerance", type=float, default=0.5, help="points a gated metric may slip vs the baseline")
    a = ap.parse_args(argv)

    if a.list_devices:
        devices = audio_devices()
        for d in devices:
            if d["outputs"]:
                ok, why = check_output_device(d["name"], devices)
                print(f"[{d['index']}] {'ALLOW' if ok else 'REFUSE'} {d['name']}: {why}")
        return 0
    calls = json.loads((FIX / "calls.json").read_text())["calls"]
    if a.only:
        want = set(a.only.split(","))
        calls = [c for c in calls if c["name"] in want]
        if not calls:
            ap.error(f"no call named {a.only}")
    if a.realtime:
        if not a.device:
            ap.error("--realtime needs --device NAME (a loopback output, see --list-devices)")
        ok, why = check_output_device(a.device, audio_devices())
        if not ok:  # refuse before anything heavy loads or plays
            log(f"refusing to play: {why}")
            return 3
    reexec_if_needed(argv, heavy=True, whisper=a.record)

    # a throwaway registry: the real one is never read (diarize_sherpa reads the env at import)
    tmp_reg = Path(tempfile.mkdtemp(prefix="whosaid-live-eval-reg-")) / "speakers.json"
    shutil.copy(FIX / "registry.json", tmp_reg)
    os.environ["WHOSAID_SPEAKER_DB"] = str(tmp_reg)
    os.environ.pop("WHOSAID_OWNER", None)
    sys.path.insert(0, str(ROOT / "lib"))
    import diarize_sherpa as ds
    if not ds.EMB_MODEL.exists() or not ds.SEG_MODEL.exists():
        log(f"missing local models ({ds.EMB_MODEL}, {ds.SEG_MODEL}); run `whosaid setup` once (this eval never downloads)")
        return 2
    ds.fetch = lambda *_a, **_k: False  # never download from here
    bank = load_bank()
    registry = json.loads(tmp_reg.read_text())
    if a.realtime:
        return run_realtime(a, calls[0], bank)

    words = live_split_words_default() if a.split_words == "default" else a.split_words == "on"
    before = fingerprint(REAL_REGISTRY)
    results, t0 = {}, time.time()
    try:
        for call in calls:
            t1 = time.time()
            results[call["name"]] = run_call(call, bank, registry, tmp_reg, words, a.record)
            print_call(call["name"], results[call["name"]])
            print(f"  ({time.time() - t1:.0f} s)", flush=True)
    except CassetteMiss as e:
        log(str(e))
        return 2
    finally:
        if fingerprint(REAL_REGISTRY) != before:
            log("REAL REGISTRY CHANGED during the eval: restore it from a backup")
    suite = aggregate(results)
    print(f"\n== suite ({len(results)} calls, live Whisper word times {'on' if words else 'off'}, "
          f"{time.time() - t0:.0f} s)")
    print(f"  {'metric':30s} {'live':>7s} {'retro':>7s}")
    for k, label, _ in ROWS:
        print(f"  {label:30s} {pct(suite['live'][k]):>7s} {pct(suite['retro'][k]):>7s}")
    print(f"  {'live vs retro disagree':30s} {pct(suite['disagree']):>7s}  (turns {pct(suite['disagree_turns']).strip()})")
    print(f"  {'turns live got wrong, retro right':30s} {pct(suite['disagree_live_off']):>7s}")
    failed = [f"{n}: {c['metric']}" for n, r in results.items() for c in r["checks"] if not c["ok"]]
    print(f"\nchecks: {sum(len(r['checks']) for r in results.values()) - len(failed)} pass, {len(failed)} fail"
          + (f" ({'; '.join(failed)})" if failed else ""))
    suite["_calls"] = sorted(results)
    base = None
    if a.gate and a.baseline:
        base = json.loads(Path(a.baseline).read_text())
    elif a.gate and a.baseline_ref:
        base, why = baseline_at(a.baseline_ref, argv)
        if base is None:
            log(f"baseline: {why}")
            print(f"GATE FAIL no baseline: {why}")
            return 1
        print(f"baseline lib/ at {why}: live name_acc {pct(base['suite']['live']['name_acc']).strip()}, "
              f"handoffs {pct(base['suite']['live']['handoff_acc']).strip()}, "
              f"backchannels {pct(base['suite']['live']['backchannel_acc']).strip()}")
    bad = gate(suite, a.max_gap, base, a.tolerance) if a.gate else []
    for b in bad:
        print(f"GATE FAIL {b}")
    if a.gate and not bad:
        print("GATE PASS")
    if a.out:
        strip = lambda d: {k: v for k, v in d.items() if not k.startswith("_")}  # noqa: E731
        Path(a.out).write_text(json.dumps({"suite": {k: v for k, v in suite.items() if k != "_calls"}, "calls": {
            n: dict(strip(r), live=strip(r["live"]), retro=strip(r["retro"])) for n, r in results.items()}},
            indent=1) + "\n")
    shutil.rmtree(tmp_reg.parent, ignore_errors=True)
    return 1 if failed or bad else 0


if __name__ == "__main__":
    sys.exit(main())

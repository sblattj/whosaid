#!/usr/bin/env python3
"""Render the synthetic-voice pool for the diarization eval (stdlib + ffmpeg only).

For every voice in voices.json, deterministically pick dialogue lines and one
enrollment passage from lines.json, synthesize them through the voice's TTS
vendor, convert to 16 kHz mono s16le WAV, trim edge silence, and cache the
result outside the repo. Cache hits cost nothing. Finally writes <cache>/pool.json.

Keys come from the environment only: ELEVENLABS_API_KEY, OPENAI_API_KEY.

  python3 test/diarize_eval/render.py [--voices id,id|all]
      [--per-voice short=3,medium=6,long=2] [--budget-chars 45000]
      [--dry-run] [--cache DIR]
"""
import argparse
import array
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 16000
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"
HTTP_TIMEOUT = 60
OA_INSTRUCTIONS = [
    "Speak in a natural, conversational tone, like a colleague in a working meeting.",
    "Speak casually and at a relaxed pace, like someone talking over a video call.",
    "Speak clearly with a calm, friendly tone, as in an everyday team discussion.",
]
EL_MODEL = "eleven_flash_v2_5"


def default_cache():
    return os.environ.get("WHOSAID_DIARIZE_EVAL_CACHE") or os.path.expanduser(
        "~/.cache/whosaid/diarize-eval")


def load_json(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


def seed_int(*parts):
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


def pick_lines(voice, lines, counts):
    """Deterministic per-voice subset of dialogue lines, seeded by sha256(voice id)."""
    chosen = []
    for kind, n in counts.items():
        pool = sorted((l for l in lines if l["kind"] == kind), key=lambda l: l["id"])
        rng = random.Random(seed_int(voice["id"], kind))
        chosen.extend(rng.sample(pool, min(n, len(pool))))
    return chosen


def pick_enroll(voice, enroll):
    return enroll[seed_int(voice["id"], "enroll") % len(enroll)]


def parse_counts(s):
    out = {}
    for part in s.split(","):
        if part.strip():
            k, v = part.split("=")
            out[k.strip()] = int(v)
    return out


# ---------- audio helpers ----------

def trim_pcm(pcm, pad_ms=50, frame_ms=10):
    """Trim leading/trailing silence from s16le mono bytes; keep <= pad_ms each side."""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    fl = SR * frame_ms // 1000
    nfr = len(a) // fl
    if nfr == 0:
        return a
    rms = []
    for i in range(nfr):
        seg = a[i * fl:(i + 1) * fl]
        rms.append((sum(x * x for x in seg) / fl) ** 0.5)
    thr = max(60.0, 0.02 * max(rms))
    idx = [i for i, r in enumerate(rms) if r >= thr]
    if not idx:
        return a
    pad = pad_ms // frame_ms
    lo = max(0, idx[0] - pad)
    hi = min(nfr, idx[-1] + 1 + pad)
    return a[lo * fl:hi * fl]


def write_wav_atomic(path, samples):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    a = samples
    if sys.byteorder == "big":
        a = array.array("h", samples)
        a.byteswap()
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(a.tobytes())
    os.replace(tmp, path)


def to_pcm16k(data, fmt):
    """Convert vendor audio bytes to raw 16 kHz mono s16le via ffmpeg (stdin->stdout)."""
    if fmt == "pcm_16000":
        return data
    if fmt == "pcm_24000":
        inp = ["-f", "s16le", "-ar", "24000", "-ac", "1"]
    else:  # container formats (mp3, wav): ffmpeg probes
        inp = []
    cmd = [FFMPEG, "-v", "error"] + inp + ["-i", "pipe:0", "-ar", str(SR), "-ac", "1",
                                          "-f", "s16le", "pipe:1"]
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout


# ---------- HTTP ----------

class HttpError(Exception):
    def __init__(self, code, body):
        super().__init__("HTTP %s: %s" % (code, body[:300]))
        self.code = code
        self.body = body


def http_post(url, headers, payload):
    """POST JSON with retry on 429/5xx/network errors (3 retries, backoff)."""
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=dict(headers, **{"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            last = HttpError(e.code, txt)
            if e.code != 429 and e.code < 500:
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt < 3:
            time.sleep(2 * (attempt + 1) ** 2)
    raise last


def synth_elevenlabs(voice, text, state):
    key = os.environ["ELEVENLABS_API_KEY"]
    fmt = state.get("el_fmt", "pcm_16000")
    url = "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=%s" % (voice["voice_id"], fmt)
    try:
        data = http_post(url, {"xi-api-key": key}, {"text": text, "model_id": EL_MODEL})
    except HttpError as e:
        if fmt == "pcm_16000" and e.code in (400, 401, 403, 422):
            state["el_fmt"] = "mp3_44100_128"  # pcm not allowed on this tier: fall back
            return synth_elevenlabs(voice, text, state)
        raise
    return data, state.get("el_fmt", "pcm_16000")


def synth_openai(voice, text, state):
    key = os.environ["OPENAI_API_KEY"]
    instr = OA_INSTRUCTIONS[seed_int(voice["id"], "instr") % len(OA_INSTRUCTIONS)]
    data = http_post("https://api.openai.com/v1/audio/speech",
                     {"Authorization": "Bearer " + key},
                     {"model": "gpt-4o-mini-tts", "voice": voice["voice_id"], "input": text,
                      "response_format": "pcm", "instructions": instr})
    return data, "pcm_24000"


def el_subscription():
    req = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription",
                                 headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        d = json.load(r)
    return d["character_count"], d["character_limit"]


# ---------- planning ----------

def build_jobs(voices, lines, enroll, counts_for):
    """Return list of jobs: dicts with voice, rel path, line_id, text, kind."""
    jobs = []
    for v in voices:
        counts = counts_for(v)
        for l in pick_lines(v, lines, counts):
            jobs.append({"voice": v, "rel": "clips/%s/%s.wav" % (v["id"], l["id"]),
                         "line_id": l["id"], "text": l["text"], "kind": l["kind"]})
        e = pick_enroll(v, enroll)
        jobs.append({"voice": v, "rel": "enroll/%s.wav" % v["id"], "line_id": e["id"],
                     "text": e["text"], "kind": "enroll"})
    return jobs


def wav_dur(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def write_pool(cache, voices, lines, enroll):
    by_id = {l["id"]: l for l in lines}
    pool = {"schema": 1, "voices": {}}
    for v in voices:
        cdir = os.path.join(cache, "clips", v["id"])
        clips = []
        if os.path.isdir(cdir):
            for fn in sorted(os.listdir(cdir)):
                lid, ext = os.path.splitext(fn)
                if ext != ".wav" or lid not in by_id:
                    continue
                p = os.path.join(cdir, fn)
                clips.append({"line_id": lid, "kind": by_id[lid]["kind"], "text": by_id[lid]["text"],
                              "path": "clips/%s/%s" % (v["id"], fn), "dur": round(wav_dur(p), 3)})
        ep = os.path.join(cache, "enroll", v["id"] + ".wav")
        if not clips and not os.path.exists(ep):
            continue
        ent = {"name": v["name"], "vendor": v["vendor"], "gender": v["gender"],
               "accent": v["accent"], "clips": clips}
        if os.path.exists(ep):
            e = pick_enroll(v, enroll)
            ent["enroll"] = {"id": e["id"], "path": "enroll/%s.wav" % v["id"],
                             "dur": round(wav_dur(ep), 3), "text": e["text"]}
        pool["voices"][v["id"]] = ent
    tmp = os.path.join(cache, "pool.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, indent=1, ensure_ascii=False)
    os.replace(tmp, os.path.join(cache, "pool.json"))
    return pool


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voices", default="all")
    ap.add_argument("--per-voice", default="short=3,medium=6,long=2")
    ap.add_argument("--per-voice-elevenlabs", default=None,
                    help="override --per-voice for ElevenLabs voices only (e.g. short=3,medium=6,long=1)")
    ap.add_argument("--budget-chars", type=int, default=45000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cache", default=None)
    args = ap.parse_args(argv)

    cache = args.cache or default_cache()
    vdata = load_json("voices.json")["voices"]
    ldata = load_json("lines.json")
    lines, enroll = ldata["lines"], ldata["enroll"]
    if args.voices != "all":
        want = [x.strip() for x in args.voices.split(",") if x.strip()]
        missing = [w for w in want if w not in {v["id"] for v in vdata}]
        if missing:
            print("unknown voices: %s" % missing, file=sys.stderr)
            return 2
        sel = [v for v in vdata if v["id"] in want]
    else:
        sel = vdata
    base = parse_counts(args.per_voice)
    el_counts = parse_counts(args.per_voice_elevenlabs) if args.per_voice_elevenlabs else base
    jobs = build_jobs(sel, lines, enroll,
                      lambda v: el_counts if v["vendor"] == "elevenlabs" else base)
    todo = [j for j in jobs if not os.path.exists(os.path.join(cache, j["rel"]))]

    cost = {}
    for vendor in ("elevenlabs", "openai"):
        vj = [j for j in jobs if j["voice"]["vendor"] == vendor]
        vt = [j for j in todo if j["voice"]["vendor"] == vendor]
        cost[vendor] = sum(len(j["text"]) for j in vt)
        print("%-10s jobs=%d cached=%d to_render=%d chars_to_spend=%d" % (
            vendor, len(vj), len(vj) - len(vt), len(vt), cost[vendor]))
    if args.dry_run:
        print("dry-run: budget=%d elevenlabs_estimate=%d (%s)" % (
            args.budget_chars, cost["elevenlabs"],
            "OK" if cost["elevenlabs"] <= args.budget_chars else "OVER BUDGET"))
        return 0
    if cost["elevenlabs"] > args.budget_chars:
        print("refusing: elevenlabs cost %d > --budget-chars %d" % (cost["elevenlabs"], args.budget_chars),
              file=sys.stderr)
        return 2
    need = {j["voice"]["vendor"] for j in todo}
    for vendor, envk in (("elevenlabs", "ELEVENLABS_API_KEY"), ("openai", "OPENAI_API_KEY")):
        if vendor in need and not os.environ.get(envk):
            print("missing env %s" % envk, file=sys.stderr)
            return 2

    if "elevenlabs" in need:
        print("elevenlabs quota before: %d/%d" % el_subscription())
    state, spent, failed = {}, 0, []
    for i, j in enumerate(todo, 1):
        v = j["voice"]
        try:
            # TTS (flash especially) sometimes rambles on short inputs: reject a clip far
            # longer than the text could take to say and re-synthesize (max 3 attempts).
            max_dur = 1.0 + len(j["text"]) / 8.0
            if j["kind"] == "short":
                max_dur = min(max_dur, 2.5)
            for attempt in range(3):
                if v["vendor"] == "elevenlabs":
                    if spent + len(j["text"]) > args.budget_chars:
                        raise RuntimeError("budget exhausted")
                    data, fmt = synth_elevenlabs(v, j["text"], state)
                    spent += len(j["text"])
                else:
                    data, fmt = synth_openai(v, j["text"], state)
                pcm = trim_pcm(to_pcm16k(data, fmt))
                if len(pcm) < SR // 10:
                    raise RuntimeError("audio too short after trim (%d samples)" % len(pcm))
                if len(pcm) / SR <= max_dur:
                    break
                print("  retry %s %s: %.2fs > %.2fs plausible" % (
                    v["id"], j["rel"], len(pcm) / SR, max_dur), flush=True)
            else:
                raise RuntimeError("implausible duration %.2fs after 3 attempts" % (len(pcm) / SR))
            write_wav_atomic(os.path.join(cache, j["rel"]), pcm)
            print("[%d/%d] ok %s %s %.2fs (el chars spent %d)" % (
                i, len(todo), v["id"], j["rel"].split("/")[-1], len(pcm) / SR, spent), flush=True)
        except Exception as e:  # keep going; report at the end
            failed.append((v["id"], j["rel"], str(e)[:200]))
            print("[%d/%d] FAIL %s %s: %s" % (i, len(todo), v["id"], j["rel"], str(e)[:200]), flush=True)
    if "elevenlabs" in need:
        print("elevenlabs quota after: %d/%d" % el_subscription())
        print("elevenlabs output format used: %s" % state.get("el_fmt", "pcm_16000"))

    os.makedirs(cache, exist_ok=True)
    pool = write_pool(cache, vdata, lines, enroll)
    print("pool.json: %s (%d voices)" % (os.path.join(cache, "pool.json"), len(pool["voices"])))
    for f in failed:
        print("FAILED:", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

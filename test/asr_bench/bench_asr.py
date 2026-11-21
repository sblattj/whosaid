#!/usr/bin/env python3
"""ASR bench: Whisper (mlx-whisper) vs NVIDIA Parakeet (parakeet-mlx) on your own audio.

Every arm writes a Whisper-shaped <outdir>/<name>.json (segments with word timestamps)
and <name>.txt, so `whosaid <audio> -o <outdir> -n <name> --reuse-asr` diarizes it
unchanged, and `whosaid ab --compare` can diff two arms. Timing (model load vs decode)
goes to <name>.timing.json. Nothing here knows about any particular meeting: the term
list and file paths come from the command line.

Run each subcommand in its own ephemeral env, e.g.
  uv run --with mlx-whisper python test/asr_bench/bench_asr.py whisper AUDIO -o DIR
  uv run --with parakeet-mlx python test/asr_bench/bench_asr.py parakeet AUDIO -o DIR
  python3 test/asr_bench/bench_asr.py score --arm A=DIR ... --terms terms.tsv --rttm R
  uv run --with mlx-whisper --with parakeet-mlx python test/asr_bench/bench_asr.py live AUDIO

Subcommands
  whisper   mlx-whisper with the exact batch settings of lib/transcribe_mlx.py.
  parakeet  parakeet-mlx TDT; --boost-terms switches to beam search with word boosting.
  score     term spelling hits/misses, words outside speech (RTTM), repeat loops, segments.
  live      warm per-chunk latency for N fixed-length chunks (the `whosaid live` loop).
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))


def _write(outdir: Path, name: str, result: dict, timing: dict) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False))
    lines = [s["text"].strip() for s in result["segments"] if s["text"].strip()]
    (outdir / f"{name}.txt").write_text("\n".join(lines) + "\n")
    (outdir / f"{name}.timing.json").write_text(json.dumps(timing, indent=1))
    print(json.dumps(timing), file=sys.stderr)


def _duration(audio: str) -> float:
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", audio], capture_output=True, text=True).stdout
    return float(out.strip())


# ------------------------------------------------------------------ whisper
def cmd_whisper(a) -> int:
    sys.path.insert(0, str(REPO / "lib"))
    from transcribe_mlx import vocabulary_prompt  # same prompt shape as production
    import mlx_whisper
    import mlx.core as mx

    t0 = time.perf_counter()
    from mlx_whisper.transcribe import ModelHolder
    ModelHolder.get_model(a.model, mx.float16)  # warm the holder transcribe() uses
    t1 = time.perf_counter()
    r = mlx_whisper.transcribe(
        a.audio, path_or_hf_repo=a.model, language="en", task="transcribe",
        initial_prompt=vocabulary_prompt(a.initial_prompt),
        condition_on_previous_text=False, word_timestamps=True,
        hallucination_silence_threshold=2.0, verbose=None)
    t2 = time.perf_counter()
    dur = _duration(a.audio)
    result = {"text": r["text"], "language": r.get("language", "en"),
              "segments": [{k: s[k] for k in ("id", "start", "end", "text", "words",
                                               "avg_logprob", "no_speech_prob",
                                               "compression_ratio", "temperature") if k in s}
                           for s in r["segments"]],
              "asr": {"engine": "mlx-whisper", "model": a.model}}
    _write(Path(a.outdir), a.name, result,
           {"engine": "mlx-whisper", "model": a.model, "audio_s": dur,
            "load_s": t1 - t0, "decode_s": t2 - t1, "rtf": (t2 - t1) / dur,
            "prompt": bool(a.initial_prompt)})
    return 0


# ----------------------------------------------------------------- parakeet
def tokens_to_words(tokens) -> list[dict]:
    """Parakeet tokens as Whisper-style words: the same merge `whosaid live --asr parakeet` uses."""
    import live
    return live.parakeet_words(tokens)


def words_to_segments(words: list[dict], gap: float, max_s: float) -> list[dict]:
    """Whisper-like segments with ids, cut the way live does (lib/live.py parakeet_segments).
    The diarizer gives each segment one speaker, so segment length is part of the comparison."""
    import live
    return [{"id": i, **seg} for i, seg in enumerate(live.parakeet_segments(words, gap, max_s))]


def load_terms(path: str | None) -> list[str]:
    if not path:
        return []
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.split("\t")[0].strip())
    return out


def cmd_parakeet(a) -> int:
    import mlx.core as mx
    from parakeet_mlx import from_pretrained, DecodingConfig, Beam, Greedy, boost
    import importlib.metadata as md

    t0 = time.perf_counter()
    model = from_pretrained(a.model, dtype=mx.float32 if a.fp32 else mx.bfloat16)
    t1 = time.perf_counter()
    terms = load_terms(a.boost_terms)
    if terms:
        booster = boost.lift(boost.words({t: a.boost_weight for t in terms}), model.vocabulary)
        dec = DecodingConfig(decoding=Beam(beam_size=a.beam, booster=booster))
    elif a.beam > 1:
        dec = DecodingConfig(decoding=Beam(beam_size=a.beam))
    else:
        dec = DecodingConfig(decoding=Greedy())
    r = model.transcribe(a.audio, decoding_config=dec,
                         chunk_duration=a.chunk_duration or None,
                         overlap_duration=a.overlap_duration)
    t2 = time.perf_counter()
    dur = _duration(a.audio)
    words = tokens_to_words(r.tokens)
    segs = words_to_segments(words, a.seg_gap, a.seg_max)
    result = {"text": " ".join(s["text"].strip() for s in segs), "language": "en",
              "segments": segs,
              "asr": {"engine": "parakeet-mlx", "version": md.version("parakeet-mlx"),
                      "model": a.model, "decoding": type(dec.decoding).__name__,
                      "boost_terms": len(terms)}}
    _write(Path(a.outdir), a.name, result,
           {"engine": "parakeet-mlx", "version": md.version("parakeet-mlx"),
            "model": a.model, "audio_s": dur, "load_s": t1 - t0, "decode_s": t2 - t1,
            "rtf": (t2 - t1) / dur, "decoding": type(dec.decoding).__name__,
            "beam": a.beam if terms or a.beam > 1 else 1, "boost_terms": len(terms),
            "chunk_duration": a.chunk_duration, "overlap_duration": a.overlap_duration})
    return 0


# -------------------------------------------------------------------- score
def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def read_rttm(path: str) -> list[tuple[float, float]]:
    iv = []
    for line in Path(path).read_text().splitlines():
        p = line.split()
        if len(p) >= 5 and p[0] == "SPEAKER":
            s, d = float(p[3]), float(p[4])
            iv.append((s, s + d))
    iv.sort()
    merged: list[list[float]] = []
    for s, e in iv:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def outside_speech(words: list[dict], speech: list[tuple[float, float]], pad: float) -> list[dict]:
    import bisect
    starts = [s for s, _ in speech]
    out = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        i = bisect.bisect_right(starts, mid + pad) - 1
        if i < 0 or mid - pad > speech[i][1]:
            out.append(w)
    return out


def repeat_loops(words: list[str], n: int = 3, min_reps: int = 4) -> list[dict]:
    """Runs where the same n-gram (n=1..n) repeats back to back >= min_reps times."""
    loops, i = [], 0
    while i < len(words):
        found = None
        for k in range(1, n + 1):
            gram = words[i:i + k]
            if len(gram) < k or not all(gram):
                continue
            reps = 1
            while words[i + reps * k:i + (reps + 1) * k] == gram:
                reps += 1
            if reps >= min_reps and (found is None or reps * k > found[1] * found[0]):
                found = (k, reps, gram)
        if found:
            k, reps, gram = found
            loops.append({"at_word": i, "gram": " ".join(gram), "reps": reps})
            i += k * reps
        else:
            i += 1
    return loops


def load_term_table(path: str) -> list[tuple[str, list[str]]]:
    """terms.tsv: CANONICAL<TAB>variant regex 1<TAB>variant regex 2 ...
    A hit is a whole-word match of CANONICAL ignoring case (case_only counts the ones whose
    case differs); a miss is a case-insensitive whole-word match of a variant regex whose
    spelling is not CANONICAL."""
    rows = []
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = [x for x in line.split("\t") if x.strip()]
        rows.append((p[0].strip(), [x.strip() for x in p[1:]]))
    return rows


def term_counts(text: str, table) -> dict:
    out = {}
    for canon, variants in table:
        exact = len(re.findall(rf"(?<![\w-]){re.escape(canon)}(?![\w-])", text))
        c = len(re.findall(rf"(?<![\w-]){re.escape(canon)}(?![\w-])", text, flags=re.I))
        miss: dict[str, int] = {}
        for v in variants:
            for m in re.finditer(rf"(?<![\w-])(?:{v})(?![\w-])", text, flags=re.I):
                if m.group(0).lower() != canon.lower():
                    miss[m.group(0)] = miss.get(m.group(0), 0) + 1
        out[canon] = {"hit": c, "case_only": c - exact, "miss": miss}
    return out


def cmd_score(a) -> int:
    table = load_term_table(a.terms) if a.terms else []
    speech = read_rttm(a.rttm) if a.rttm else []
    res = {}
    for spec in a.arm:
        label, d = spec.split("=", 1)
        js = json.loads((Path(d) / f"{a.name}.json").read_text())
        words = [w for s in js["segments"] for w in (s.get("words") or [])]
        text = " ".join(s["text"].strip() for s in js["segments"])
        seg_d = sorted(s["end"] - s["start"] for s in js["segments"]) or [0]
        out_w = outside_speech(words, speech, a.pad) if speech else []
        res[label] = {
            "words": len(words),
            "segments": len(js["segments"]),
            "seg_median_s": seg_d[len(seg_d) // 2],
            "seg_p90_s": seg_d[int(len(seg_d) * 0.9)],
            "zero_len_words": sum(1 for w in words if w["end"] <= w["start"]),
            "nonmonotonic_words": sum(1 for x, y in zip(words, words[1:]) if y["start"] < x["start"] - 0.01),
            "outside_speech_words": len(out_w),
            "outside_speech_sample": " ".join(w["word"].strip() for w in out_w[:40]),
            "loops": repeat_loops([_norm(w["word"]) for w in words]),
            "terms": term_counts(text, table),
        }
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------- live
def cmd_live(a) -> int:
    """Warm per-chunk latency: decode N consecutive chunk_s windows of one recording with
    each engine, mirroring lib/live.py whisper_transcriber (word_timestamps off by default)."""
    import numpy as np
    import subprocess
    import mlx.core as mx
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", a.audio, "-ss", str(a.offset),
                          "-t", str(a.chunk_s * a.n), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    pcm = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    step = int(a.chunk_s * 16000)
    chunks = [pcm[i:i + step] for i in range(0, len(pcm) - step + 1, step)][:a.n]
    out = {"chunk_s": a.chunk_s, "n": len(chunks), "engines": {}}
    for eng in a.engines.split(","):
        if eng.startswith("whisper:"):
            import mlx_whisper
            model = eng.split(":", 1)[1]

            def run(x, words=a.words):
                r = mlx_whisper.transcribe(x, path_or_hf_repo=model, language="en", verbose=None,
                                           condition_on_previous_text=False,
                                           hallucination_silence_threshold=2.0,
                                           word_timestamps=words)
                return " ".join(s["text"].strip() for s in r["segments"])
        else:
            from parakeet_mlx import from_pretrained
            from parakeet_mlx.audio import get_logmel
            model = from_pretrained(eng.split(":", 1)[1])

            def run(x, model=model):
                mel = get_logmel(mx.array(x), model.preprocessor_config)
                return model.generate(mel)[0].text
        run(chunks[0])  # warm up (load + first compile), not timed
        times, texts = [], []
        for c in chunks:
            t = time.perf_counter()
            texts.append(run(c))
            times.append(time.perf_counter() - t)
        ts = sorted(times)
        out["engines"][eng] = {"mean_s": sum(times) / len(times), "p50_s": ts[len(ts) // 2],
                               "p90_s": ts[int(len(ts) * 0.9)], "max_s": ts[-1],
                               "times": times, "first_text": texts[0][:200]}
        print(eng, json.dumps({k: v for k, v in out["engines"][eng].items() if k != "times"}),
              file=sys.stderr)
    Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("whisper")
    w.add_argument("audio")
    w.add_argument("-o", "--outdir", required=True)
    w.add_argument("-n", "--name", default="transcript")
    w.add_argument("-m", "--model", default="mlx-community/whisper-large-v3-mlx")
    w.add_argument("--initial-prompt", default="")
    p = sub.add_parser("parakeet")
    p.add_argument("audio")
    p.add_argument("-o", "--outdir", required=True)
    p.add_argument("-n", "--name", default="transcript")
    p.add_argument("-m", "--model", default="mlx-community/parakeet-tdt-0.6b-v2")
    p.add_argument("--chunk-duration", type=float, default=120.0, help="0 = no chunking")
    p.add_argument("--overlap-duration", type=float, default=15.0)
    p.add_argument("--beam", type=int, default=1)
    p.add_argument("--boost-terms", default=None, help="file, one term per line (TSV col 1)")
    p.add_argument("--boost-weight", type=float, default=2.0)
    p.add_argument("--seg-gap", type=float, default=0.6)
    p.add_argument("--seg-max", type=float, default=12.0)
    p.add_argument("--fp32", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--arm", action="append", required=True, help="LABEL=DIR")
    s.add_argument("-n", "--name", default="transcript")
    s.add_argument("--terms")
    s.add_argument("--rttm")
    s.add_argument("--pad", type=float, default=0.5)
    s.add_argument("--out", required=True)
    lv = sub.add_parser("live")
    lv.add_argument("audio")
    lv.add_argument("--engines", default="whisper:mlx-community/whisper-large-v3-turbo,"
                                         "parakeet:mlx-community/parakeet-tdt-0.6b-v2")
    lv.add_argument("--chunk-s", type=float, default=20.0)
    lv.add_argument("--n", type=int, default=30)
    lv.add_argument("--offset", type=float, default=60.0)
    lv.add_argument("--words", action="store_true", help="whisper word_timestamps (--split-words)")
    lv.add_argument("--out", required=True)
    a = ap.parse_args()
    return {"whisper": cmd_whisper, "parakeet": cmd_parakeet, "score": cmd_score,
            "live": cmd_live}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

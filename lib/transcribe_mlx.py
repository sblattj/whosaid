#!/usr/bin/env python3
"""Robust MLX-Whisper transcription on Apple Silicon (GPU via Metal).

Invoked by the `whosaid` CLI through `uv run --with mlx-whisper`. We call the
library directly rather than its CLI for one reason: the bare `mlx_whisper`
CLI defaults `--temperature` to a single 0, which DISABLES the temperature
fallback. Whisper's anti-hallucination design relies on that fallback
(0.0 -> 0.2 -> ... -> 1.0) so a segment that trips the compression-ratio /
logprob check can retry at higher temperature instead of getting stuck.
Without it, greedy decoding can collapse into emitting one token
("Yeah." "Yeah." ...) for the rest of a long recording.

So here we keep the library default temperature tuple AND add:
  - condition_on_previous_text=False : a run of repeats can't prime the next window
  - hallucination_silence_threshold  : skip long silences where hallucinations spawn
  - word_timestamps=True             : required for the hallucination threshold

Everything runs locally on the GPU; no audio or text leaves the machine.
"""
import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Robust local MLX-Whisper transcription.")
    ap.add_argument("audio")
    ap.add_argument("--model", default="mlx-community/whisper-large-v3-mlx")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--name", default=None, help="output base name (no extension)")
    ap.add_argument("--lang", default="en", help="language code, or 'auto' to detect")
    ap.add_argument("--format", default="all", choices=["txt", "srt", "vtt", "tsv", "json", "all"])
    a = ap.parse_args()

    if not os.path.isfile(a.audio):
        print(f"transcribe_mlx: file not found: {a.audio}", file=sys.stderr)
        return 1

    import mlx_whisper
    from mlx_whisper.writers import get_writer

    result = mlx_whisper.transcribe(
        a.audio,
        path_or_hf_repo=a.model,
        language=(None if a.lang == "auto" else a.lang),
        task="transcribe",
        # temperature is intentionally left at the library default fallback tuple.
        condition_on_previous_text=False,
        word_timestamps=True,
        hallucination_silence_threshold=2.0,
        verbose=False,
    )

    os.makedirs(a.outdir, exist_ok=True)
    base = a.name or os.path.splitext(os.path.basename(a.audio))[0]
    opts = {
        "highlight_words": False,
        "max_line_width": None,
        "max_line_count": None,
        "max_words_per_line": None,
    }
    get_writer(a.format, a.outdir)(result, base, opts)

    text = result.get("text", "") or ""
    print(f"transcribe_mlx: OK lang={result.get('language')} chars={len(text)} -> {a.outdir}/{base}.*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""vocabulary.py: the corrections post-pass for workspace vocabulary (GitHub issue #216).

Whisper gets the workspace vocabulary as its initial prompt, but with
condition_on_previous_text=False only the first ~30 s window sees it. This pass
fixes known mishearings ("blobser" -> "blob store") over the whole transcript by
rewriting each segment's `text` in `<base>.json` BEFORE diarization reads it,
and keeps the untouched text in the segment's `raw_text`.

Matching: whole words, longest key first. A key with any uppercase letter matches
case-sensitively ("SD" never hits "sd"); an all-lowercase key matches
case-insensitively. The pass always recomputes from `raw_text` when present, so
running it twice, or with a changed rule set, never compounds.

CLI:
    python3 lib/vocabulary.py apply JSON [--txt TXT] [--correct WRONG=RIGHT ...]

Stdlib only, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_corrections(items) -> dict:
    """{"WRONG": "RIGHT"} from "WRONG=RIGHT" strings (split on the first "=")."""
    out: dict = {}
    for item in items or []:
        wrong, sep, right = str(item).partition("=")
        wrong, right = wrong.strip(), right.strip()
        if not sep or not wrong or not right:
            raise ValueError(f"correction must look like WRONG=RIGHT (got {item!r})")
        out[wrong] = right
    return out


def _pattern(key: str) -> re.Pattern:
    flags = 0 if any(c.isupper() for c in key) else re.IGNORECASE
    return re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)", flags)


def correct_text(text: str, corrections: dict) -> str:
    """Replace each key with its value in one pass (longest key first, no re-scan of output)."""
    if not corrections or not text:
        return text
    keys = sorted(corrections, key=len, reverse=True)
    pats = [(_pattern(k), corrections[k]) for k in keys]
    # one pass over the original text so a replacement is never itself rewritten
    spans: list[tuple[int, int, str]] = []
    taken: list[tuple[int, int]] = []
    for pat, repl in pats:
        for m in pat.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            spans.append((s, e, repl))
    for s, e, repl in sorted(spans, reverse=True):
        text = text[:s] + repl + text[e:]
    return text


def apply_to_whisper_json(path, corrections: dict, txt_path=None) -> int:
    """Rewrite segment texts in a Whisper json in place; returns changed-segment count."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for seg in data.get("segments") or []:
        raw = seg.get("raw_text", seg.get("text", ""))
        new = correct_text(raw, corrections)
        if new != raw:
            seg["text"] = new
            seg["raw_text"] = raw
            changed += 1
        else:
            seg["text"] = raw
            seg.pop("raw_text", None)
    data["text"] = "".join(s.get("text", "") for s in data.get("segments") or [])
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if txt_path is not None and Path(txt_path).exists():
        # same shape as mlx_whisper's WriteTXT: one stripped segment text per line
        Path(txt_path).write_text(
            "".join(s.get("text", "").strip() + "\n" for s in data.get("segments") or []),
            encoding="utf-8")
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Apply vocabulary corrections to a Whisper json.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply")
    p.add_argument("json")
    p.add_argument("--txt", default=None)
    p.add_argument("--correct", action="append", default=[], metavar="WRONG=RIGHT")
    a = ap.parse_args(argv)
    try:
        corrections = parse_corrections(a.correct)
    except ValueError as e:
        print(f"vocabulary: {e}", file=sys.stderr)
        return 2
    n = apply_to_whisper_json(a.json, corrections, a.txt)
    print(f"vocabulary: corrected {n} segment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

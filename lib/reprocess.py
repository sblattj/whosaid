#!/usr/bin/env python3
"""
whosaid reprocess helpers (stdlib only): a read-only detector for meetings
ingested by a pre-1.10 diarizer, and a pure label carry-over function.

Sidecars carry no version stamp, so the detector infers "ingested before the
v1.10 fixes" from the shape of `<folder>/transcript.diarization.json`:

  brief-fragments        (#59) auto count, count_estimate has no `brief_fold`
                         key, and a cluster made only of sub-2 s turns.
  short-hinted-undercount (#68) audio < 900 s, `--speakers N` hinted, fewer
                         than N clusters came out (whole-file FastClustering
                         ignored the hint).
  short-whole-file       (#68) audio < 900 s, auto mode, count_estimate null
                         (whole-file path; also what a v1.10 --no-chunk run
                         looks like, so only "possible").
  ref-order              (--ref) two or more distinct --ref names matched; they
                         were assigned in argument order before v1.10.
                         Skipped when `brief_fold` is present (that sidecar is
                         already v1.10).

Usage:
    python lib/reprocess.py scan <ws> [meeting ...] [--json]
    python lib/reprocess.py map-labels <old.json> <new.json>

`scan` is a read-only report: exit 0 always, exit 2 on a bad workspace path.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Mirrors lib/diarize_sherpa.py SUBSTANTIVE_TURN_SECONDS and the whole-file
# threshold the per-turn path replaced in v1.10.
SUBSTANTIVE_TURN_SECONDS = 2.0
SHORT_AUDIO_SECONDS = 900.0

_HINT_RE = re.compile(r"^as hinted \(--num-speakers (\d+)\)")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _finding(code: str, confidence: str, reason: str) -> dict:
    return {"code": code, "confidence": confidence, "reason": reason}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _cluster_turns(segments) -> dict:
    """{cluster: [turn seconds, ...]} from a sidecar `segments` list."""
    out: dict = {}
    if not isinstance(segments, list):
        return out
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        s, e, sp = _num(seg.get("start")), _num(seg.get("end")), seg.get("speaker")
        if s is None or e is None or not isinstance(sp, str) or e < s:
            continue
        out.setdefault(sp, []).append(e - s)
    return out


def scan_sidecar(data) -> list:
    """Return findings (dicts: code, confidence, reason) for one sidecar dict.
    Unknown shapes and missing keys yield no findings, never an exception."""
    if not isinstance(data, dict):
        return []
    mode = data.get("detect_mode")
    mode = mode if isinstance(mode, str) else ""
    source = data.get("source")
    duration = _num(source.get("duration_seconds")) if isinstance(source, dict) else None
    ce = data.get("count_estimate")
    resolved = data.get("num_speakers")
    resolved = resolved if isinstance(resolved, int) and not isinstance(resolved, bool) else None
    auto = mode.startswith("auto-detected")
    findings: list = []

    # #59: brief-only clusters inflated the automatic count.
    if auto and isinstance(ce, dict) and "brief_fold" not in ce:
        brief = []
        for sp, turns in sorted(_cluster_turns(data.get("segments")).items()):
            if turns and all(t < SUBSTANTIVE_TURN_SECONDS for t in turns):
                brief.append(f"{sp} ({sum(turns):.1f}s over {len(turns)} turns)")
        if brief:
            findings.append(_finding(
                "brief-fragments", "likely",
                f"auto count {resolved} includes clusters made only of turns "
                f"under {SUBSTANTIVE_TURN_SECONDS:g}s: {', '.join(brief)}"))

    short = duration is not None and duration < SHORT_AUDIO_SECONDS
    hint = _HINT_RE.match(mode)
    # #68: whole-file FastClustering ignored --speakers N on short audio.
    if short and hint and resolved is not None and resolved < int(hint.group(1)):
        findings.append(_finding(
            "short-hinted-undercount", "likely",
            f"{duration:.0f}s audio (< {SHORT_AUDIO_SECONDS:g}s) hinted "
            f"{hint.group(1)} speakers but got {resolved}"))
    if short and auto and ce is None:
        findings.append(_finding(
            "short-whole-file", "possible",
            f"{duration:.0f}s audio (< {SHORT_AUDIO_SECONDS:g}s) auto-detected with no "
            "count estimate: whole-file clustering before v1.10 (or a v1.10 "
            "--no-chunk run, which would not change)"))

    # --ref clips were matched in argument order before v1.10.
    matches = data.get("registry_matches")
    if isinstance(matches, list) and not (isinstance(ce, dict) and "brief_fold" in ce):
        refs = [m for m in matches if isinstance(m, dict) and m.get("pass") == "ref"]
        matched = {m.get("name") for m in refs if m.get("matched") is True and m.get("name")}
        names = {m.get("name") for m in refs if m.get("name")}
        if matched and len(names) >= 2:
            findings.append(_finding(
                "ref-order", "possible",
                f"{len(names)} --ref voices ({', '.join(sorted(names))}); {len(matched)} "
                "matched; before v1.10 names were assigned in argument order"))
    return findings


def scan_workspace(ws: Path, meetings=None) -> list:
    """Scan every meeting folder (immediate subdirs, `_*` and `.*` skipped, as
    roll-up does). Returns [{meeting, sidecar, findings}] with one row per
    sidecar; a folder with no sidecar gets one row with `skipped` set."""
    ws = Path(ws)
    wanted = set(meetings) if meetings else None
    rows: list = []
    for entry in sorted(ws.iterdir()):
        if entry.name.startswith(("_", ".")) or not entry.is_dir():
            continue
        if wanted is not None and entry.name not in wanted:
            continue
        sidecars = sorted(entry.glob("*.diarization.json"))
        if not sidecars:
            rows.append({"meeting": entry.name, "sidecar": None, "findings": [],
                         "skipped": "no diarization"})
            continue
        for sc in sidecars:
            try:
                data = json.loads(sc.read_text())
            except (OSError, ValueError):
                data = None
            rows.append({"meeting": entry.name, "sidecar": sc.name,
                         "findings": scan_sidecar(data)})
    return rows


def _talk_by_cluster(data) -> dict:
    return {sp: sum(t) for sp, t in _cluster_turns((data or {}).get("segments")).items()}


def _overlap(a: list, b: list) -> float:
    """Total overlap seconds between two lists of (start, end)."""
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0.0, min(e1, e2) - max(s1, s2))
    return total


def _spans(data) -> dict:
    out: dict = {}
    for seg in (data or {}).get("segments") or []:
        if isinstance(seg, dict) and isinstance(seg.get("speaker"), str):
            s, e = _num(seg.get("start")), _num(seg.get("end"))
            if s is not None and e is not None and e > s:
                out.setdefault(seg["speaker"], []).append((s, e))
    return out


def map_local_labels(old: dict, new: dict, min_share: float = 0.5):
    """Carry old `local_labels` onto the new diarization by time overlap.
    Returns ({new_cluster: {"name", "note"}}, [warning, ...]). Pure."""
    old_spans, new_spans = _spans(old), _spans(new)
    labels = (old or {}).get("local_labels")
    labels = labels if isinstance(labels, dict) else {}
    warnings: list = []
    best: dict = {}  # new_cluster -> (overlap, old_label)
    for lab in sorted(labels):
        spans = old_spans.get(lab, [])
        talk = sum(e - s for s, e in spans)
        if talk <= 0:
            warnings.append(f"{lab}: no segments in the old run; label not carried")
            continue
        scores = {nc: _overlap(spans, ns) for nc, ns in new_spans.items()}
        target = max(sorted(scores), key=lambda k: scores[k], default=None)
        if target is None or scores[target] < min_share * talk:
            got = scores[target] if target else 0.0
            warnings.append(
                f"{lab} ({(labels[lab] or {}).get('name')}): best overlap "
                f"{got:.1f}s of {talk:.1f}s is under {min_share:.0%}; label not carried")
            continue
        if target in best:
            prev_ov, prev_lab = best[target]
            if scores[target] > prev_ov:
                loser, best[target] = prev_lab, (scores[target], lab)
            else:
                loser = lab
            warnings.append(
                f"{lab} and {prev_lab} both map to {target}; kept the larger overlap, "
                f"dropped {loser}")
        else:
            best[target] = (scores[target], lab)
    mapping = {}
    for nc, (_, lab) in best.items():
        entry = labels[lab] if isinstance(labels[lab], dict) else {}
        mapping[nc] = {"name": entry.get("name"), "note": entry.get("note")}
    return mapping, warnings


def cmd_scan(args) -> int:
    ws = Path(args.workspace).expanduser()
    if not ws.is_dir():
        log(f"reprocess: not a workspace directory: {ws}")
        return 2
    rows = scan_workspace(ws, args.meeting or None)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    total = len({r["meeting"] for r in rows})
    affected: dict = {}
    for r in rows:
        if r["findings"]:
            affected.setdefault(r["meeting"], []).append(r)
    likely = 0
    for meeting, rs in affected.items():
        print(meeting)
        for r in rs:
            for f in r["findings"]:
                print(f"  [{f['confidence']}] {f['code']}: {f['reason']}")
        if any(f["confidence"] == "likely" for r in rs for f in r["findings"]):
            likely += 1
    print(f"{len(affected)} of {total} meetings may change ({likely} likely)")
    return 0


def cmd_map_labels(args) -> int:
    try:
        old = json.loads(Path(args.old).read_text())
        new = json.loads(Path(args.new).read_text())
    except (OSError, ValueError) as e:
        log(f"reprocess: cannot read sidecar: {e}")
        return 2
    mapping, warnings = map_local_labels(old, new, args.min_share)
    for w in warnings:
        log(f"reprocess: {w}")
    print(json.dumps(mapping, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="whosaid reprocess helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("scan", help="report meetings an older diarizer may have got wrong")
    sp.add_argument("workspace")
    sp.add_argument("meeting", nargs="*")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_scan)
    mp = sub.add_parser("map-labels", help="carry local_labels from an old sidecar to a new one")
    mp.add_argument("old")
    mp.add_argument("new")
    mp.add_argument("--min-share", type=float, default=0.5)
    mp.set_defaults(fn=cmd_map_labels)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

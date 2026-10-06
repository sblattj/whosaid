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

A sidecar stamped `whosaid_version` >= 1.11.0 (written by lib/diarize_sherpa.py
from that release on) is current and gets no findings.

Usage:
    python lib/reprocess.py scan <ws> [meeting ...] [--json]
    python lib/reprocess.py map-labels <old.json> <new.json>
    python lib/reprocess.py run <ws> [meeting ...] [flags]   (see `whosaid reprocess`)

`scan` is a read-only report: exit 0 always, exit 2 on a bad workspace path.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Mirrors lib/diarize_sherpa.py SUBSTANTIVE_TURN_SECONDS and the whole-file
# threshold the per-turn path replaced in v1.10.
SUBSTANTIVE_TURN_SECONDS = 2.0
SHORT_AUDIO_SECONDS = 900.0

_HINT_RE = re.compile(r"^as hinted \(--num-speakers (\d+)\)")

# First release whose diarizer stamps `whosaid_version` into the sidecar; any
# stamp at or above it carries every v1.10 diarization fix.
CURRENT_VERSION = (1, 11, 0)


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


def _version_tuple(v):
    """(major, minor, patch) from a version string like "1.11.0", else None."""
    m = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?", v) if isinstance(v, str) else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def scan_sidecar(data) -> list:
    """Return findings (dicts: code, confidence, reason) for one sidecar dict.
    Unknown shapes and missing keys yield no findings, never an exception."""
    if not isinstance(data, dict):
        return []
    stamp = _version_tuple(data.get("whosaid_version"))
    if stamp is not None and stamp >= CURRENT_VERSION:
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
                brief.append(f"{sp} ({sum(turns):.1f}s over {len(turns)} "
                             f"turn{'' if len(turns) == 1 else 's'})")
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


def print_report(rows: list) -> None:
    """The human scan report for scan_workspace rows (stdout)."""
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


def cmd_scan(args) -> int:
    ws = Path(args.workspace).expanduser()
    if not ws.is_dir():
        log(f"reprocess: not a workspace directory: {ws}")
        return 2
    rows = scan_workspace(ws, args.meeting or None)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print_report(rows)
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


# ---- run: re-diarize meetings in place -----------------------------------------

BACKUP_ROOT = "_reprocess-backups"  # `_` prefix: roll-up and every ws scan skip it
SKELETON_MARK = "so no action items were extracted"  # lib/workspace.py skeleton_markdown


def managed_files(base: str) -> list:
    """Every file reprocess may rewrite, relative to the meeting folder."""
    return [f"{base}.speakers.txt", f"{base}.speaker-cards.txt", f"{base}.rttm",
            f"{base}.diarization.json", "action-items.md", "action-items.json",
            "commitments.md", "commitments.json"]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _is_skeleton(path: Path) -> bool:
    try:
        return SKELETON_MARK in path.read_text()
    except OSError:
        return False


def locate_meeting(folder: Path):
    """(info, None) or (None, reason). info: base, audio, sidecar."""
    import workspace  # sibling module in lib/

    sidecars = sorted(folder.glob("*.diarization.json"))
    if not sidecars:
        return None, "no diarization sidecar"
    pick = next((s for s in sidecars if s.name == "transcript.diarization.json"), sidecars[0])
    base = pick.name[: -len(".diarization.json")]
    if not (folder / f"{base}.json").is_file():
        return None, f"no transcript json ({base}.json) to reuse"
    audio = workspace.find_source_audio(folder)
    if audio is None:
        return None, "no source audio in the folder"
    return {"base": base, "audio": audio, "sidecar": pick}, None


def make_backup(folder: Path, base: str, backup_root: Path) -> tuple:
    """Copy every existing managed file to <root>/<UTC stamp>/. Returns (dir, [names])."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest, n = backup_root / stamp, 1
    while dest.exists():
        n += 1
        dest = backup_root / f"{stamp}-{n}"
    dest.mkdir(parents=True)
    saved = []
    for name in managed_files(base):
        if (folder / name).is_file():
            shutil.copy2(folder / name, dest / name)
            saved.append(name)
    return dest, saved


def restore_backup(folder: Path, base: str, backup: Path, saved: list) -> None:
    """Put every managed file back as it was: saved ones restored, new ones removed."""
    for name in managed_files(base):
        if name in saved:
            shutil.copy2(backup / name, folder / name)
        else:
            (folder / name).unlink(missing_ok=True)


def _names(data) -> list:
    names = (data or {}).get("names")
    return [str(v) for _, v in sorted(names.items())] if isinstance(names, dict) else []


def _call(argv: list, what: str) -> int:
    argv = [str(a) for a in argv]
    log(f"reprocess: $ {' '.join(argv)}")
    try:
        return subprocess.run(argv).returncode
    except OSError as e:
        log(f"reprocess: {what} could not start: {e}")
        return 127


def reprocess_meeting(ws: Path, name: str, info: dict, args, bin_path: str, speaker_args: list) -> dict:
    """Run the per-meeting pipeline. A tool failure restores the backup and
    returns status FAILED instead of raising."""
    folder, base = ws / name, info["base"]
    res = {"meeting": name, "status": "ok", "speakers_changed": False,
           "ai_regenerated": False, "cm_regenerated": False}
    old = _read_json(info["sidecar"]) or {}
    backup, saved = make_backup(folder, base, ws / BACKUP_ROOT / name)
    res["backup"] = str(backup)
    spk = f"{base}.speakers.txt"
    sidecar = folder / f"{base}.diarization.json"

    def fail(reason: str, rc: int) -> dict:
        restore_backup(folder, base, backup, saved)
        res.update(status="FAILED", reason=reason, rc=rc)
        log(f"reprocess: {name}: FAILED ({reason}); restored from {backup}")
        return res

    rc = _call([bin_path, "transcribe", info["audio"], "-o", folder, "-n", base,
                "--reuse-asr", *speaker_args], "transcribe")
    if rc != 0:
        return fail(f"transcribe exited {rc}", rc)
    new = _read_json(sidecar) or {}

    # Transcript-only labels (relabel --no-save) live only in the old sidecar:
    # carry them onto the new clusters by time overlap. The user's label wins
    # even when the new cluster got a registry name.
    if isinstance(old.get("local_labels"), dict) and old["local_labels"]:
        carried, warnings = map_local_labels(old, new)
        for w in warnings:
            log(f"reprocess: {name}: WARN {w}")
        for cluster, ent in sorted(carried.items()):
            if not ent.get("name"):
                continue
            argv = [bin_path, "relabel", folder / base, f"{cluster}={ent['name']}", "--no-save"]
            if ent.get("note"):
                argv += ["--note", ent["note"]]
            rc = _call(argv, "relabel")
            if rc != 0:
                return fail(f"relabel {cluster} exited {rc}", rc)
        # Roles: the diarizer re-derives them from the registry by NAME, so a
        # registry-named speaker needs nothing. Only a sidecar-only role (set with
        # --no-save + --role on a local label) is lost; re-apply it by name.
        old_roles = old.get("roles") if isinstance(old.get("roles"), dict) else {}
        new_roles = (_read_json(sidecar) or {}).get("roles") or {}
        missing = []
        for ent in carried.values():
            nm = ent.get("name")
            tag = f"{nm}={old_roles[nm]}" if nm in old_roles else None
            if tag and nm not in new_roles and tag not in missing:
                missing.append(tag)
        if missing:
            argv = [bin_path, "relabel", folder / base]
            for r in missing:
                argv += ["--role", r]
            rc = _call(argv, "relabel --role")
            if rc != 0:
                return fail(f"relabel --role exited {rc}", rc)
        new = _read_json(sidecar) or {}

    res.update(old_count=old.get("num_speakers"), new_count=new.get("num_speakers"),
               old_names=_names(old), new_names=_names(new))
    old_spk = backup / spk
    if old_spk.is_file() and (folder / spk).is_file() \
            and old_spk.read_bytes() == (folder / spk).read_bytes():
        res["note"] = "speakers unchanged"
        return res
    res["speakers_changed"] = True
    if args.no_items:
        return res

    transcript = folder / spk
    wpy = Path(__file__).resolve().parent / "workspace.py"
    if "action-items.md" in saved:
        # Same call shape as `whosaid ingest --action-items`.
        ai = [sys.executable, wpy, "action-items", "--transcript", transcript,
              "--json-out", folder / "action-items.json"]
        if args.hook:
            ai += ["--hook", args.hook]
        if args.engine:
            ai += ["--engine", args.engine]
        rc = _call(ai, "action-items")
        md = folder / "action-items.md"
        if rc != 0 or not md.is_file() or (_is_skeleton(md) and not _is_skeleton(backup / "action-items.md")):
            for n in ("action-items.md", "action-items.json"):
                if n in saved:
                    shutil.copy2(backup / n, folder / n)
                else:
                    (folder / n).unlink(missing_ok=True)
            log(f"reprocess: {name}: WARN action-items regeneration "
                f"{'gave a skeleton (engine failure?)' if rc == 0 else f'exited {rc}'}; "
                "kept the previous action-items.md/.json")
        else:
            res["ai_regenerated"] = True
    if "commitments.json" in saved:
        cm = [sys.executable, wpy, "commitments", "--transcript", transcript,
              "--json-out", folder / "commitments.json"]
        rc = _call(cm, "commitments")
        if rc != 0:
            for n in ("commitments.md", "commitments.json"):
                if n in saved:
                    shutil.copy2(backup / n, folder / n)
            log(f"reprocess: {name}: WARN commitments regeneration exited {rc}; "
                "kept the previous commitments.json")
        else:
            res["cm_regenerated"] = True
    return res


def _summary_line(r: dict) -> str:
    if r["status"] == "FAILED":
        return f"{r['meeting']}: FAILED ({r.get('reason')}); backup {r.get('backup')}"
    items = ("action-items " + ("y" if r["ai_regenerated"] else "n")
             + ", commitments " + ("y" if r["cm_regenerated"] else "n"))
    note = f" ({r['note']})" if r.get("note") else ""
    return (f"{r['meeting']}: {r.get('old_count')} -> {r.get('new_count')} speakers{note}; "
            f"names [{', '.join(r['old_names'])}] -> [{', '.join(r['new_names'])}]; "
            f"regenerated {items}; backup {r['backup']}")


def cmd_run(args) -> int:
    ws = Path(args.workspace).expanduser()
    if not ws.is_dir():
        log(f"reprocess: not a workspace directory: {ws}")
        return 2
    explicit = [m.rstrip("/") for m in (args.meeting or [])]
    for m in explicit:
        if m.startswith(("_", ".")) or not (ws / m).is_dir():
            log(f"reprocess: not a meeting folder in {ws}: {m}")
            return 2
    rows = scan_workspace(ws, explicit or None)
    if explicit:
        selected = list(dict.fromkeys(explicit))
    elif args.all:
        selected = list(dict.fromkeys(r["meeting"] for r in rows if r["sidecar"]))
    else:
        selected = list(dict.fromkeys(r["meeting"] for r in rows if r["findings"]))

    # Speaker hints: an explicit flag replaces the workspace [diarize] hints
    # entirely (as ingest does); with none, the workspace hints apply.
    flags = []
    if args.speakers is not None:
        flags += ["--speakers", str(args.speakers)]
    if args.min_speakers is not None:
        flags += ["--min-speakers", str(args.min_speakers)]
    if args.max_speakers is not None:
        flags += ["--max-speakers", str(args.max_speakers)]
    if args.expected_speakers:
        flags += ["--expected-speakers", args.expected_speakers]
    if flags:
        speaker_args = flags
    else:
        import wsconfig  # sibling module in lib/

        speaker_args, err = wsconfig.workspace_speaker_args(ws)
        if err:
            log(f"reprocess: fix [diarize] in {ws}/whosaid.toml: {err}")
            return 1
        if speaker_args:
            log(f"reprocess: speaker hints from whosaid.toml [diarize]: {' '.join(speaker_args)}")
    if args.match_threshold is not None:
        speaker_args = [*speaker_args, "--match-threshold", str(args.match_threshold)]

    plan, skipped = [], []
    for name in selected:
        info, reason = locate_meeting(ws / name)
        if info is None:
            skipped.append((name, reason))
        else:
            plan.append((name, info))

    if args.dry_run:
        print_report(rows)
        for name, info in plan:
            print(f"would reprocess {name} (base {info['base']}, audio {info['audio'].name})")
        for name, reason in skipped:
            print(f"would skip {name}: {reason}")
        print(f"dry run: {len(plan)} meeting(s) would be reprocessed; nothing changed")
        return 0

    for name, reason in skipped:
        log(f"reprocess: skipping {name}: {reason}")
    if not plan:
        log("reprocess: nothing to reprocess"
            + ("" if selected else " (no meeting has a finding; --all re-runs every meeting)"))
        return 0

    bin_path = os.environ.get("WHOSAID_BIN") or str(Path(__file__).resolve().parent.parent / "whosaid")
    results = []
    for name, info in plan:
        log(f"reprocess: === {name}")
        r = reprocess_meeting(ws, name, info, args, bin_path, speaker_args)
        results.append(r)
        print(_summary_line(r))

    ai_done = [r["meeting"] for r in results if r["ai_regenerated"]]
    changed = [r for r in results if r["speakers_changed"] and r["status"] == "ok"]
    rollup = None
    if ai_done and (ws / "_action-items.json").exists():
        rollup = ["--action-items"] + [x for m in ai_done for x in ("--refold", m)]
    elif changed and (ws / "_commitments.json").exists():
        # A plain roll-up keeps an existing _action-items.json/_ACTION-ITEMS.md
        # (lib/workspace.py cmd_rollup rewrites them from the corpus) and refreshes
        # the commitments corpus by content fingerprint.
        rollup = []
    rc_rollup = 0
    if rollup is not None:
        log(f"reprocess: === roll-up {ws}")
        rc_rollup = _call([bin_path, "roll-up", ws, *rollup], "roll-up")
        print(f"roll-up {'ok' if rc_rollup == 0 else f'FAILED (exit {rc_rollup})'}: "
              f"whosaid roll-up {ws} {' '.join(rollup)}".rstrip())
    elif changed:
        print("no _action-items.json or _commitments.json in the workspace; roll-up not needed")

    failed = [r for r in results if r["status"] == "FAILED"]
    print(f"reprocessed {len(results) - len(failed)} meeting(s), "
          f"{len(changed)} with changed speakers, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed or rc_rollup else 0


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
    rp = sub.add_parser("run", help="re-diarize meetings in place (whosaid reprocess)")
    rp.add_argument("workspace")
    rp.add_argument("meeting", nargs="*")
    rp.add_argument("--all", action="store_true")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--no-items", action="store_true")
    rp.add_argument("--speakers", type=int)
    rp.add_argument("--min-speakers", type=int)
    rp.add_argument("--max-speakers", type=int)
    rp.add_argument("--expected-speakers")
    rp.add_argument("--match-threshold", type=float)
    rp.add_argument("--engine")
    rp.add_argument("--hook")
    rp.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

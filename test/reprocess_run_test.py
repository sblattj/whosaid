#!/usr/bin/env python3
"""
Offline test for `lib/reprocess.py run` (the engine behind `whosaid reprocess`).

The launcher is replaced by a FAKE `WHOSAID_BIN` written into a temp dir: it logs
its argv, and for `transcribe` writes a new sidecar/speakers.txt from a per-meeting
config (or fails with a chosen rc, after clobbering files, to prove the restore).
Action-items/commitments regeneration runs the REAL lib/workspace.py with a
`--hook` script (offline) or `--engine none` (skeleton). Synthetic workspaces only;
no models, no audio.

Run:
    uv run --quiet python test/reprocess_run_test.py
"""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
RUN_PY = REPO_DIR / "lib" / "reprocess.py"
CHECKS = 0

A, B, C = "2026-02-01-0900", "2026-02-02-0900", "2026-02-03-0900"
OLD_SPEAKERS = "# Speakers\n**SPEAKER_00:** old text\n"
NEW_SPEAKERS = ("# Speakers (1): Alice\n# Role: Alice = self\n\n"
                "[00:00:01] Alice: I'll send the budget proposal tomorrow.\n")
OLD_AI = "# Action items\n\n- **Bob:** Migrate the billing database to the new cluster\n"
HOOK_AI = "# Action items\n\n- **Alice:** Draft the quarterly budget proposal for finance\n"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    CHECKS += 1


FAKE = r'''#!__PY__
import json, os, sys
from pathlib import Path
d = Path(os.environ["FAKE_DIR"])
argv = sys.argv[1:]
with open(d / "log.jsonl", "a") as f:
    f.write(json.dumps(argv) + "\n")
cmd = argv[0]
if cmd == "transcribe":
    out = Path(argv[argv.index("-o") + 1])
    base = argv[argv.index("-n") + 1]
    cfg_path = d / (out.name + ".json")
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    # the real transcribe drops the stale speakers file first, then the diarizer rewrites
    (out / f"{base}.speakers.txt").write_text(cfg.get("speakers", "NEW\n"))
    (out / f"{base}.rttm").write_text("new rttm\n")
    (out / f"{base}.speaker-cards.txt").write_text("new cards\n")
    (out / f"{base}.diarization.json").write_text(json.dumps(cfg.get("sidecar", {})))
    sys.exit(cfg.get("rc", 0))
if cmd == "roll-up":
    sys.exit(int((d / "rollup_rc").read_text()) if (d / "rollup_rc").exists() else 0)
sys.exit(0)
'''


def seg(start, end, sp):
    return {"start": start, "end": end, "speaker": sp}


def old_sidecar(local_labels=None, roles=None, version=None):
    # brief-fragments finding: SPEAKER_02 has only sub-2 s turns in an auto-count run.
    d = {"detect_mode": "auto-detected", "num_speakers": 3,
         "names": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01", "SPEAKER_02": "SPEAKER_02"},
         "local_labels": local_labels or {}, "registry_matches": [],
         "source": {"path": "a.wav", "duration_seconds": 1800.0, "creation_time": None},
         "count_estimate": {"method": "gap", "k": 3, "raw_k": 3, "saturated": False},
         "segments": [seg(0, 30, "SPEAKER_00"), seg(30, 60, "SPEAKER_01"),
                      seg(60, 61, "SPEAKER_02")]}
    if roles:
        d["roles"] = roles
    if version:
        d["whosaid_version"] = version
    return d


def new_sidecar(roles=None):
    d = {"detect_mode": "auto-detected", "num_speakers": 2,
         "names": {"SPEAKER_00": "Alice", "SPEAKER_01": "SPEAKER_01"},
         "local_labels": {}, "segments": [seg(0, 30, "SPEAKER_01"), seg(30, 61, "SPEAKER_00")]}
    if roles:
        d["roles"] = roles
    return d


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class Env:
    """One temp workspace + fake launcher."""

    def __init__(self, tmp: str, tag: str):
        self.root = Path(tmp) / tag
        self.ws = self.root / "ws"
        self.fake_dir = self.root / "fake"
        self.ws.mkdir(parents=True)
        self.fake_dir.mkdir()
        self.bin = self.fake_dir / "whosaid-fake"
        self.bin.write_text(FAKE.replace("__PY__", sys.executable))
        self.bin.chmod(0o755)
        hook = self.fake_dir / "hook.sh"
        hook.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{HOOK_AI}'\n")
        hook.chmod(0o755)
        self.hook = str(hook)

    def meeting(self, name, sidecar, audio=True, full=True, speakers=OLD_SPEAKERS):
        f = self.ws / name
        f.mkdir()
        (f / "transcript.json").write_text('{"segments": []}')
        (f / "transcript.diarization.json").write_text(json.dumps(sidecar))
        if audio:
            (f / "a.wav").write_bytes(b"RIFF")
        if full:
            (f / "transcript.speakers.txt").write_text(speakers)
            (f / "transcript.speaker-cards.txt").write_text("old cards\n")
            (f / "transcript.rttm").write_text("old rttm\n")
            (f / "action-items.md").write_text(OLD_AI)
            (f / "action-items.json").write_text('{"items": ["old"]}\n')
            (f / "commitments.md").write_text("old commitments md\n")
            (f / "commitments.json").write_text('{"items": []}\n')
        return f

    def config(self, name, **cfg):
        (self.fake_dir / f"{name}.json").write_text(json.dumps(cfg))

    def run(self, *args):
        import os
        env = dict(os.environ, WHOSAID_BIN=str(self.bin), FAKE_DIR=str(self.fake_dir),
                   WHOSAID_SPEAKER_DB=str(self.fake_dir / "speakers.json"),
                   WHOSAID_VOICE_REFS=str(self.fake_dir / "no-refs"))
        env.pop("WHOSAID_ACTION_ITEMS_HOOK", None)
        return subprocess.run([sys.executable, str(RUN_PY), "run", str(self.ws), *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def calls(self):
        p = self.fake_dir / "log.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def of(self, cmd):
        return [c for c in self.calls() if c[0] == cmd]


def standard(tmp, tag, **kw):
    """A flagged (with audio), B clean-and-stamped, C flagged but audio-less."""
    e = Env(tmp, tag)
    e.meeting(A, old_sidecar(**kw))
    e.meeting(B, old_sidecar(version="1.11.0"), full=False)
    e.meeting(C, old_sidecar(), audio=False, full=False)
    e.config(A, speakers=NEW_SPEAKERS, sidecar=new_sidecar())
    e.config(B, speakers=NEW_SPEAKERS, sidecar=new_sidecar())
    return e


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # --- default selection = scan findings; skip reasons; transcribe argv; backup.
        e = standard(tmp, "default")
        before = {n: (e.ws / A / n).read_text() for n in
                  ("transcript.speakers.txt", "transcript.speaker-cards.txt", "transcript.rttm",
                   "transcript.diarization.json", "action-items.md", "action-items.json",
                   "commitments.md", "commitments.json")}
        r = e.run("--no-items")
        check(r.returncode == 0, f"default rc={r.returncode}: {r.stderr[-400:]}")
        t = e.of("transcribe")
        check(len(t) == 1, f"only A transcribed (B clean, C no audio): {t}")
        check(t[0] == ["transcribe", str(e.ws / A / "a.wav"), "-o", str(e.ws / A), "-n", "transcript",
                       "--reuse-asr"], f"transcribe argv: {t[0]}")
        check(f"skipping {C}: no source audio" in r.stderr, f"C skip reason: {r.stderr[-400:]}")
        backups = list((e.ws / "_reprocess-backups" / A).iterdir())
        check(len(backups) == 1 and len(backups[0].name) == 16 and backups[0].name.endswith("Z"),
              f"one UTC-stamped backup dir: {backups}")
        saved = {p.name: p.read_text() for p in backups[0].iterdir()}
        check(saved == before, f"backup holds all 8 originals verbatim: {sorted(saved)}")
        check(not (e.ws / "_reprocess-backups" / B).exists(), "B not selected, not backed up")
        check((e.ws / A / "transcript.speakers.txt").read_text() == NEW_SPEAKERS, "new speakers in place")
        check("2 speakers" in r.stdout and "3 -> 2" in r.stdout, f"summary line: {r.stdout}")
        check((e.ws / A / "action-items.md").read_text() == OLD_AI, "--no-items leaves action items alone")
        check(e.of("roll-up") == [], "no corpus files -> no roll-up")
        check("roll-up not needed" in r.stdout, r.stdout)
        # The backup tree is invisible to roll-up/scan (underscore prefix).
        import importlib
        sys.path.insert(0, str(REPO_DIR / "lib"))
        reprocess = importlib.import_module("reprocess")
        names = [x["meeting"] for x in reprocess.scan_workspace(e.ws)]
        check("_reprocess-backups" not in names, f"scan skips the backup dir: {names}")

        # --- --all selects every meeting with a sidecar; B has no action items file.
        e = standard(tmp, "all")
        r = e.run("--all", "--no-items")
        check(r.returncode == 0, r.stderr[-300:])
        check(sorted(Path(c[3]).name for c in e.of("transcribe")) == [A, B], "--all runs A and B")
        check(f"skipping {C}" in r.stderr, "--all still skips C with a reason")

        # --- explicit meeting; trailing slash tolerated; unknown / underscore dirs exit 2.
        e = standard(tmp, "explicit")
        r = e.run(f"{B}/", "--no-items")
        check(r.returncode == 0 and [Path(c[3]).name for c in e.of("transcribe")] == [B],
              f"explicit B only: {r.stderr[-300:]}")
        before = tree_hash(e.ws)
        for bad in ("2020-01-01-0000", "_reprocess-backups", "../x"):
            r = e.run(bad)
            check(r.returncode == 2, f"unknown meeting {bad!r} exits 2 (got {r.returncode})")
        check(tree_hash(e.ws) == before and len(e.of("transcribe")) == 1, "bad names change nothing")
        r = subprocess.run([sys.executable, str(RUN_PY), "run", str(e.ws / "nope")], capture_output=True, text=True)
        check(r.returncode == 2, "bad workspace exits 2")

        # --- failure restores every file, exits 1, and the next meeting still runs.
        e = standard(tmp, "fail")
        e.config(A, rc=3, speakers="GARBAGE\n", sidecar={"garbage": True})
        snap = tree_hash(e.ws / A)
        r = e.run("--all", "--no-items")
        check(r.returncode == 1, f"failure exits 1 (got {r.returncode})")
        check(tree_hash(e.ws / A) == snap, "failed meeting restored byte-for-byte")
        check(f"{A}: FAILED" in r.stdout and "transcribe exited 3" in r.stdout,
              f"FAILED line: {r.stdout}")
        check(len(e.of("transcribe")) == 2, "continued to the next meeting after the failure")
        check((e.ws / B / "transcript.speakers.txt").read_text() == NEW_SPEAKERS, "B still reprocessed")
        check("1 failed" in r.stdout, r.stdout)
        # a file the failed run created (not in the backup) is removed again
        e = standard(tmp, "fail2")
        (e.ws / A / "transcript.rttm").unlink()
        e.config(A, rc=3)
        r = e.run(A)
        check(r.returncode == 1 and not (e.ws / A / "transcript.rttm").exists(), "new file removed on failure")

        # --- label carry-over: old SPEAKER_00 label lands on the new SPEAKER_01 (time overlap).
        e = standard(tmp, "labels", local_labels={"SPEAKER_00": {"name": "Zed", "note": "the host"}})
        r = e.run("--no-items")
        check(r.returncode == 0, r.stderr[-300:])
        rl = e.of("relabel")
        check(rl == [["relabel", str(e.ws / A / "transcript"), "SPEAKER_01=Zed", "--no-save",
                      "--note", "the host"]], f"relabel call: {rl}")
        # no note -> no --note flag
        e = standard(tmp, "labels2", local_labels={"SPEAKER_00": {"name": "Zed", "note": None}})
        check(e.run("--no-items").returncode == 0, "labels2 rc")
        check(e.of("relabel") == [["relabel", str(e.ws / A / "transcript"), "SPEAKER_01=Zed", "--no-save"]],
              f"null note: {e.of('relabel')}")
        # a label that cannot be mapped warns and is not relabeled
        e = standard(tmp, "labels3", local_labels={"SPEAKER_00": {"name": "Zed", "note": None}})
        e.config(A, speakers=NEW_SPEAKERS, sidecar={"num_speakers": 1, "names": {"SPEAKER_09": "X"},
                                                      "segments": [seg(500, 530, "SPEAKER_09")]})
        r = e.run("--no-items")
        check(e.of("relabel") == [] and "label not carried" in r.stderr, f"unmappable: {r.stderr[-300:]}")
        # relabel failing is a failure with restore
        e = standard(tmp, "labels4", local_labels={"SPEAKER_00": {"name": "Zed", "note": None}})
        fail_bin = e.fake_dir / "whosaid-fake"
        fail_bin.write_text(fail_bin.read_text().replace(
            'if cmd == "roll-up":', 'if cmd == "relabel":\n    sys.exit(5)\nif cmd == "roll-up":'))
        snap = tree_hash(e.ws / A)
        r = e.run("--no-items")
        check(r.returncode == 1 and tree_hash(e.ws / A) == snap, "relabel failure restores and exits 1")

        # --- roles: a sidecar-only role is re-applied by name; a registry-derived one is not.
        e = standard(tmp, "roles", local_labels={"SPEAKER_00": {"name": "Zed", "note": None}},
                     roles={"Zed": "boss"})
        e.run("--no-items")
        check(["relabel", str(e.ws / A / "transcript"), "--role", "Zed=boss"] in e.of("relabel"),
              f"role re-applied: {e.of('relabel')}")
        e = standard(tmp, "roles2", local_labels={"SPEAKER_00": {"name": "Zed", "note": None}},
                     roles={"Zed": "boss"})
        e.config(A, speakers=NEW_SPEAKERS, sidecar=new_sidecar(roles={"Zed": "boss"}))
        e.run("--no-items")
        check(all("--role" not in c for c in e.of("relabel")),
              f"role already in the new sidecar (registry) -> no --role: {e.of('relabel')}")
        e = standard(tmp, "roles3", local_labels={"SPEAKER_00": {"name": "Zed", "note": None}},
                     roles={"Zed": "boss"})
        (e.fake_dir / "speakers.json").write_text(json.dumps({"speakers": [{"name": "Zed"}]}))
        e.run("--no-items")
        check(all("--role" not in c for c in e.of("relabel")),
              f"registry knows Zed (no role) -> no --role, registry untouched: {e.of('relabel')}")

        # --- speakers unchanged: no regeneration, no roll-up.
        e = standard(tmp, "same")
        (e.ws / "_action-items.json").write_text("{}")
        e.config(A, speakers=OLD_SPEAKERS, sidecar=new_sidecar())
        r = e.run(A, "--hook", e.hook, "--engine", "hook")
        check(r.returncode == 0 and "speakers unchanged" in r.stdout, f"unchanged: {r.stdout}{r.stderr[-300:]}")
        check((e.ws / A / "action-items.md").read_text() == OLD_AI, "action-items.md untouched")
        check(e.of("roll-up") == [], "no roll-up when nothing changed")
        check("regenerated action-items n, commitments n" in r.stdout, r.stdout)

        # --- regeneration + refold roll-up (real workspace.py with a hook).
        e = standard(tmp, "regen")
        (e.ws / "_action-items.json").write_text("{}")
        (e.ws / "_commitments.json").write_text("{}")
        r = e.run(A, "--hook", e.hook, "--engine", "hook")
        check(r.returncode == 0, f"regen rc={r.returncode}: {r.stderr[-500:]}")
        check((e.ws / A / "action-items.md").read_text().strip() == HOOK_AI.strip(), "action items regenerated")
        check(json.loads((e.ws / A / "action-items.json").read_text()).get("items"), "action-items.json rewritten")
        cm = json.loads((e.ws / A / "commitments.json").read_text())
        check(any("budget proposal" in str(i) for i in cm["items"]), f"commitments regenerated: {cm}")
        check("regenerated action-items y, commitments y" in r.stdout, r.stdout)
        ru = e.of("roll-up")
        check(len(ru) == 1 and ru[0][:4] == ["roll-up", str(e.ws), "--action-items", "--refold"],
              f"refold roll-up: {ru}")
        name, _, prev = ru[0][4].partition("=")
        check(name == A and prev.endswith("/action-items.md") and "_reprocess-backups" in prev,
              f"refold passes the backed-up action-items.md: {ru[0][4]}")
        check(Path(prev).read_text() == OLD_AI, "backup holds the old action-items.md")
        check("roll-up ok" in r.stdout, r.stdout)
        # roll-up failure propagates
        e2 = standard(tmp, "regen2")
        (e2.ws / "_action-items.json").write_text("{}")
        (e2.fake_dir / "rollup_rc").write_text("4")
        r = e2.run(A, "--hook", e2.hook, "--engine", "hook")
        check(r.returncode == 1 and "roll-up FAILED" in r.stdout, "roll-up failure exits 1")

        # --- two regenerated meetings -> one roll-up with both --refold.
        e = standard(tmp, "regen-two")
        (e.ws / B / "action-items.md").write_text(OLD_AI)
        (e.ws / B / "transcript.speakers.txt").write_text(OLD_SPEAKERS)
        (e.ws / "_action-items.json").write_text("{}")
        r = e.run(A, B, "--hook", e.hook, "--engine", "hook")
        ru = e.of("roll-up")
        check(len(ru) == 1 and ru[0][2:3] + ru[0][3::2] == ["--action-items", "--refold", "--refold"]
              and [x.partition("=")[0] for x in ru[0][4::2]] == [A, B],
              f"two refolds: {ru}")

        # --- skeleton guard: --engine none writes a skeleton; the old items come back.
        e = standard(tmp, "skeleton")
        r = e.run(A, "--engine", "none")
        check(r.returncode == 0, r.stderr[-300:])
        check((e.ws / A / "action-items.md").read_text() == OLD_AI, "skeleton rejected, old md restored")
        check((e.ws / A / "action-items.json").read_text() == '{"items": ["old"]}\n', "old json restored")
        check("kept the previous action-items" in r.stderr and "WARN" in r.stderr, r.stderr[-400:])
        check("regenerated action-items n" in r.stdout, r.stdout)
        check(e.of("roll-up") == [], "no corpus -> no roll-up")
        # an old skeleton stays acceptable (nothing to protect)
        e = standard(tmp, "skeleton2")
        (e.ws / A / "action-items.md").write_text("# Action items\n\n_x so no action items were extracted._\n")
        r = e.run(A, "--engine", "none")
        check("regenerated action-items y" in r.stdout, f"old skeleton replaced: {r.stdout}")

        # --- plain roll-up when only commitments changed / a corpus exists but no AI regen.
        e = standard(tmp, "plain")
        (e.ws / "_commitments.json").write_text("{}")
        (e.ws / "_action-items.json").write_text("{}")
        r = e.run(A, "--no-items")
        check(e.of("roll-up") == [["roll-up", str(e.ws)]], f"plain roll-up: {e.of('roll-up')}")
        e = standard(tmp, "plain2")
        (e.ws / "_commitments.json").write_text("{}")
        r = e.run(A, "--engine", "none")  # skeleton rejected -> nothing regenerated, speakers changed
        check(e.of("roll-up") == [["roll-up", str(e.ws)]], f"plain roll-up after rejected skeleton: {e.of('roll-up')}")

        # --- speaker hints: workspace [diarize] used when no flag; an explicit flag replaces it.
        e = standard(tmp, "hints")
        (e.ws / "whosaid.toml").write_text("[diarize]\nspeakers = 3\n")
        e.run(A, "--no-items")
        check(e.of("transcribe")[0][7:] == ["--speakers", "3"], f"workspace hint: {e.of('transcribe')[0]}")
        e = standard(tmp, "hints2")
        (e.ws / "whosaid.toml").write_text("[diarize]\nspeakers = 3\n")
        e.run(A, "--no-items", "--max-speakers", "4", "--match-threshold", "0.6")
        argv = e.of("transcribe")[0]
        check(argv[7:] == ["--max-speakers", "4", "--match-threshold", "0.6"],
              f"explicit flag replaces workspace hints: {argv}")
        e = standard(tmp, "hints3")
        e.run(A, "--no-items", "--speakers", "2", "--min-speakers", "1", "--expected-speakers", "Ann,Bob")
        check(e.of("transcribe")[0][7:] == ["--speakers", "2", "--min-speakers", "1",
                                              "--expected-speakers", "Ann,Bob"], e.of("transcribe")[0])
        e = standard(tmp, "hints4")
        (e.ws / "whosaid.toml").write_text("[diarize]\nspeakers = 3\n")
        e.run(A, "--no-items", "--match-threshold", "0.7")
        check(e.of("transcribe")[0][7:] == ["--speakers", "3", "--match-threshold", "0.7"],
              f"--match-threshold keeps workspace hints: {e.of('transcribe')[0]}")
        # --- no flag, no workspace hint: the meeting's original count flags are reused.
        for tag, mode, want in [
                ("orig1", "as hinted (--num-speakers 4)", ["--speakers", "4"]),
                ("orig2", "auto-detected, bounded 2-6, fold 7 -> 5",
                 ["--min-speakers", "2", "--max-speakers", "6"]),
                ("orig3", "auto-detected", [])]:
            e = Env(tmp, tag)
            e.meeting(A, dict(old_sidecar(), detect_mode=mode))
            e.config(A, speakers=NEW_SPEAKERS, sidecar=new_sidecar())
            e.run(A, "--no-items")
            check(e.of("transcribe")[0][7:] == want, f"{mode!r} -> {e.of('transcribe')[0][7:]}")
        e = Env(tmp, "orig4")
        e.meeting(A, dict(old_sidecar(), detect_mode="as hinted (--num-speakers 4)"))
        e.config(A, speakers=NEW_SPEAKERS, sidecar=new_sidecar())
        e.run(A, "--no-items", "--speakers", "3")
        check(e.of("transcribe")[0][7:] == ["--speakers", "3"], "explicit flag beats the original count")
        e = standard(tmp, "hints5")
        (e.ws / "whosaid.toml").write_text("[diarize]\nspeakers = 0\n")
        r = e.run(A, "--no-items")
        check(r.returncode == 1 and e.of("transcribe") == [], "bad [diarize] hint exits 1 before any work")

        # --- dry run changes nothing and says what would happen.
        e = standard(tmp, "dry")
        (e.ws / "_action-items.json").write_text("{}")
        before = tree_hash(e.root)
        r = e.run("--dry-run")
        check(r.returncode == 0, r.stderr[-300:])
        check(tree_hash(e.root) == before, "dry run: tree hash identical (no backups, no files)")
        check(e.calls() == [], "dry run: fake launcher never called")
        check(f"would reprocess {A}" in r.stdout and f"would skip {C}" in r.stdout, r.stdout)
        check("meetings may change" in r.stdout and "[likely] brief-fragments" in r.stdout, r.stdout)
        check(f"would reprocess {B}" not in r.stdout, "clean meeting not listed")
        r = e.run("--dry-run", "--all")
        check(f"would reprocess {B}" in r.stdout and tree_hash(e.root) == before, "dry run --all")

        # --- nothing flagged -> exit 0, no work.
        e = Env(tmp, "none")
        e.meeting(B, old_sidecar(version="1.11.0"), full=False)
        r = e.run()
        check(r.returncode == 0 and e.calls() == [] and "nothing to reprocess" in r.stderr, r.stderr)

    print(f"reprocess_run_test: {CHECKS} checks passed")


if __name__ == "__main__":
    main()

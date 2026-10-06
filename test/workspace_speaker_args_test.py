#!/usr/bin/env python3
"""
Workspace speaker hints (GitHub issue #59): whosaid.toml [diarize] gives every ingest
into the workspace (and MCP transcribe into it) a default speaker cap, so a watcher-only
[watch] setting is no longer the only way to stop a 7-person standup splitting into 20.

Covers wsconfig.find_workspace / workspace_speaker_args, the `wsconfig.py speaker-args`
CLI, and `whosaid ingest` picking the hints up (or not, when a flag is passed) via the
WHOSAID_INGEST_DRYRUN hook (no models).

Run:
    python3 test/workspace_speaker_args_test.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import wsconfig  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


def ingest(ws, *flags):
    audio = ws.parent / "call.m4a"
    audio.write_bytes(os.urandom(64))  # fresh sha each run, so ingest never skips it
    env = dict(os.environ, WHOSAID_INGEST_DRYRUN="1")
    return subprocess.run([str(REPO_DIR / "whosaid"), "ingest", str(audio), "--into", str(ws),
                           "--folder-by", "mtime", "--tz", "UTC", *flags],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    ws = root / "ws"
    (ws / "2026-01-01-0900").mkdir(parents=True)

    check("no workspace marker -> None", wsconfig.find_workspace(ws / "2026-01-01-0900") is None)
    check("no config -> no args", wsconfig.workspace_speaker_args(ws) == ([], None))

    (ws / "whosaid.toml").write_text('[diarize]\nmax_speakers = 8\nexpected_speakers = "Alice_Example, Bob_Example"\n')
    check("find_workspace walks up from a meeting folder",
          wsconfig.find_workspace(ws / "2026-01-01-0900") == ws.resolve())
    check("[diarize] -> args", wsconfig.workspace_speaker_args(ws) ==
          (["--max-speakers", "8", "--expected-speakers", "Alice_Example,Bob_Example"], None))

    r = subprocess.run([sys.executable, str(REPO_DIR / "lib" / "wsconfig.py"), "speaker-args", str(ws)],
                       capture_output=True, text=True)
    check("CLI prints one arg per line", r.returncode == 0 and r.stdout.split("\n")[:2] == ["--max-speakers", "8"],
          r.stdout + r.stderr)

    (ws / "whosaid.toml").write_text("[watch]\nmax_speakers = 4\n")
    check("[watch] alone does not leak into [diarize]", wsconfig.workspace_speaker_args(ws) == ([], None))

    (ws / "whosaid.toml").write_text("[diarize]\nmin_speakers = 5\nmax_speakers = 3\n")
    args, err = wsconfig.workspace_speaker_args(ws)
    check("bad range names [diarize]", args == [] and err and err.startswith("[diarize] min_speakers"), str(err))
    r = ingest(ws)
    check("ingest refuses a bad [diarize]", r.returncode == 1 and "[diarize]" in r.stderr, r.stderr[-300:])

    (ws / "whosaid.toml").write_text("[diarize]\nmax_speakers = 8\n")
    r = ingest(ws)
    check("ingest applies [diarize]", r.returncode == 0 and "[diarize]: --max-speakers 8" in r.stderr,
          r.stderr[-400:])
    r = ingest(ws, "--speakers", "2")
    check("an explicit speaker flag replaces [diarize]", r.returncode == 0 and "[diarize]" not in r.stderr,
          r.stderr[-400:])

    _, err = wsconfig.diarize_hints({"max_speakers": 0})
    check("watch section keeps its [watch] error prefix", err.startswith("[watch] max_speakers"), str(err))

sys.exit(1 if fails else 0)

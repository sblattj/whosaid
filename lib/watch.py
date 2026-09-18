#!/usr/bin/env python3
"""
watch.py: hands-free "new recording -> whosaid ingest" watcher, its launchd
installer, and the Voice Memos helpers (GitHub issue #14).

Subcommands (the `whosaid` bash CLI dispatches `whosaid watch ...` and
`whosaid memos ...` here):

  run       --into WS [--source DIR] [--seed] [--dry-run] [--accurate] [--offline]
            [--whosaid PATH] [--engine E]
      One watcher pass. Every audio file in the source folder that is not in
      <ws>/.watch_state.json and whose mtime has been stable for
      watch.stable_seconds is copied into <ws>/.watch_staging/, fed to
      `whosaid ingest <copy> --into <ws> --folder-by created --action-items`,
      and recorded as done (keyed name:size, recordings are immutable). After
      any success `whosaid roll-up <ws> --action-items` and `whosaid index <ws>`
      rebuild the aggregates. A file whose mtime is fresher than
      stable_seconds may still be syncing, so the pass stays alive (bounded by
      watch.max_wait_seconds) and rescans. <ws>/.watch.lock guards against
      overlapping passes. --seed marks every current recording as done so an
      existing library is never reprocessed; --dry-run only reports.

  install   --into WS [--source DIR] [--seed] [--dry-run] [--interpreter PATH]
            [--label L] [--interval S] [--offline] [--env KEY=VALUE ...] [--no-open]
      Write and load a launchd LaunchAgent (~/Library/LaunchAgents/<label>.plist)
      that runs `run` when the source folder changes (WatchPaths) and every
      --interval seconds as a safety net. Label default: com.whosaid.watch.<8 hex
      of sha256(workspace path)>, so several workspaces can coexist.

  uninstall [--into WS | --label L] [--purge]
  status    [--into WS | --label L] [--json]

  memos list   [--source DIR] [--json]
  memos pull   [--source DIR] [--latest | --title T] [-o DIR]
  memos delete "<title>" [--yes] [--source DIR]
  memos shortcut-recipe [--no-sign]

Source folder: --source, else [watch] source in <ws>/whosaid.toml, else the
macOS Voice Memos store (~/Library/Group Containers/group.com.apple.VoiceMemos.shared/
Recordings). Any folder of audio files works.

Why Full Disk Access is scoped to ONE dedicated binary
------------------------------------------------------
The Voice Memos store is TCC-protected: only executables the user has granted
Full Disk Access (FDA) can read it, and macOS grants FDA per executable path.
`install` therefore provisions a private interpreter
(~/.local/opt/whosaid-watch/bin/whosaid-watch: a `venv --copies` python, renamed so
the FDA list reads "whosaid-watch", ad-hoc codesigned for a stable TCC identity)
that runs nothing but this watcher. The watcher copies each recording OUT of the
store into <ws>/.watch_staging/ and hands the copy to whosaid, so whosaid and the
python/uv/ffmpeg it spawns read an ordinary file and need no grant at all. Adding
the one path in System Settings > Privacy & Security > Full Disk Access is the
single manual step; it cannot be automated, and this tool never asks for more.

Why deletes go through the app's own intent
-------------------------------------------
`memos delete` never touches CloudRecordings.db or the .m4a files. It runs the
Voice Memos "Delete Recordings" App Intent through a one-action Shortcut named
"Delete Voice Memo" (see `memos shortcut-recipe`), which is exactly what tapping
Delete in the app does: iCloud stays in sync across devices, the memo lands in
Recently Deleted (restorable for 30 days), and the app's database stays
consistent. Editing the store behind the app's back would desync iCloud and can
corrupt the library. Verification reads a COPY of CloudRecordings.db, read-only.

Exit codes: 0 ok, 1 error, 2 usage or verification failed, 3 cannot read the
source (Full Disk Access missing). Stdlib only, no network.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace import AUDIO_EXTS  # noqa: E402  (read-only import)
from wsconfig import DEFAULTS, load_config, log, resolve_workspace  # noqa: E402

VOICE_MEMOS_STORE = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
DB_NAME = "CloudRecordings.db"
SHORTCUT_NAME = "Delete Voice Memo"
SHORTCUT_OUT = Path.home() / ".local/share/whosaid/delete-voice-memo.shortcut"

STATE_NAME = ".watch_state.json"
LOCK_NAME = ".watch.lock"
STAGING_NAME = ".watch_staging"
LOG_NAME = ".watch.log"

LABEL_PREFIX = "com.whosaid.watch."
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
# WHOSAID_WATCH_AGENT_DIR relocates the dedicated interpreter (tests point it at a temp dir).
AGENT_DIR = Path(os.environ.get("WHOSAID_WATCH_AGENT_DIR") or Path.home() / ".local/opt/whosaid-watch").expanduser()
AGENT_BIN = AGENT_DIR / "bin/whosaid-watch"
AGENT_PATH = ":".join([
    str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])
FDA_PANE_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

# The Whisper weights live in the local HF cache; with --offline every child skips
# the doomed round-trip to huggingface.co (blocked networks, planes, privacy).
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TRANSFORMERS_OFFLINE": "1"}

CORE_DATA_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def tlog(msg: str) -> None:
    """Timestamped log line for the watcher pass (launchd captures it in .watch.log)."""
    print(f"{now_iso()} whosaid: {msg}", file=sys.stderr, flush=True)


def is_macos() -> bool:
    return sys.platform == "darwin"


# ---- workspace / source / label -----------------------------------------------------

def optional_config(into: str | None) -> tuple[Path | None, dict]:
    """Workspace + config when one can be resolved; DEFAULTS otherwise (memos
    commands work without a workspace)."""
    try:
        ws = resolve_workspace(into)
    except SystemExit:
        return None, copy.deepcopy(DEFAULTS)
    return ws, load_config(ws)


def resolve_source(arg: str | None, cfg: dict) -> Path:
    """--source > [watch] source in whosaid.toml > the macOS Voice Memos store."""
    if arg:
        return Path(arg).expanduser()
    configured = str((cfg.get("watch") or {}).get("source") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return VOICE_MEMOS_STORE


def is_store(source: Path) -> bool:
    """True when the source is a Voice Memos store (holds CloudRecordings.db)."""
    try:
        return (source / DB_NAME).is_file()
    except OSError:
        return False


def default_label(ws: Path) -> str:
    return LABEL_PREFIX + hashlib.sha256(str(ws).encode()).hexdigest()[:8]


def find_whosaid(arg: str | None) -> str:
    """--whosaid > $WHOSAID_BIN > `whosaid` on PATH > the launcher beside this lib/."""
    if arg:
        return str(Path(arg).expanduser())
    env = os.environ.get("WHOSAID_BIN", "").strip()
    if env:
        return env
    found = shutil.which("whosaid")
    if found:
        return found
    return str(Path(__file__).resolve().parent.parent / "whosaid")


def parse_env_pairs(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"whosaid: --env expects KEY=VALUE (got {pair!r})")
        out[key.strip()] = value
    return out


# ---- state ------------------------------------------------------------------------

def load_state(ws: Path) -> dict:
    try:
        data = json.loads((ws / STATE_NAME).read_text())
        if isinstance(data, dict) and isinstance(data.get("processed"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        tlog(f"WARN {STATE_NAME} unreadable ({e}); starting fresh")
    return {"processed": {}}


def save_state(ws: Path, state: dict) -> None:
    (ws / STATE_NAME).write_text(json.dumps(state, indent=2) + "\n")


def list_recordings(source: Path) -> list[tuple[str, Path, os.stat_result, str]]:
    """(key, path, stat, name) for every audio file in the source, sorted by
    name. key is name:size (recordings are immutable once synced). Raises
    FileNotFoundError when the folder is absent and PermissionError when TCC
    blocks the read."""
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    out = []
    for name in sorted(os.listdir(source)):
        if name.startswith("."):
            continue
        path = source / name
        if path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
            continue
        st = path.stat()
        out.append((f"{name}:{st.st_size}", path, st, name))
    return out


# ---- run --------------------------------------------------------------------------

def scan(source: Path, state: dict, stable_seconds: float):
    """Unseen recordings split into stable (ready) and still-syncing
    (name, seconds until stable)."""
    now = time.time()
    new, pending = [], []
    for key, path, st, name in list_recordings(source):
        if key in state["processed"]:
            continue
        age = now - st.st_mtime
        if age < stable_seconds:
            pending.append((name, stable_seconds - age))
        else:
            new.append((key, path, name))
    return new, pending


def child_env(offline: bool) -> dict[str, str]:
    env = dict(os.environ)
    if offline:
        env.update(OFFLINE_ENV)
    return env


def ingest_one(ws: Path, path: Path, whosaid: str, engine: str | None,
               accurate: bool, env: dict[str, str]) -> int:
    """Copy the recording OUT of the (possibly TCC-protected) source into staging
    and run whosaid on the copy, so only this process needs Full Disk Access."""
    staging = ws / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / path.name
    shutil.copy2(path, staged)  # keeps mtime; the container keeps its creation_time tag
    try:
        cmd = [whosaid, "ingest", str(staged), "--into", str(ws),
               "--folder-by", "created", "--action-items"]
        if engine:
            cmd += ["--engine", engine]
        if accurate:
            cmd.append("--accurate")
        tlog(f"  > {Path(whosaid).name} ingest {path.name}")
        return subprocess.run(cmd, env=env).returncode
    finally:
        try:
            staged.unlink()
        except OSError:
            pass


def refresh(ws: Path, whosaid: str, env: dict[str, str]) -> None:
    for cmd in ([whosaid, "roll-up", str(ws), "--action-items"],
                [whosaid, "index", str(ws)]):
        tlog(f"  > {Path(whosaid).name} {cmd[1]} {ws}")
        try:
            rc = subprocess.run(cmd, env=env).returncode
        except OSError as e:
            tlog(f"  ! {cmd[1]} could not start ({e})")
            continue
        if rc != 0:
            tlog(f"  ! {cmd[1]} exited {rc} (aggregates may be stale; ingest itself succeeded)")


def seed(ws: Path, source: Path, dry: bool) -> int:
    state = load_state(ws)
    added = 0
    for key, _path, _st, name in list_recordings(source):
        if key not in state["processed"]:
            state["processed"][key] = {"name": name, "seeded": True, "at": now_iso()}
            added += 1
    if dry:
        tlog(f"would seed {added} recording(s) as done ({len(state['processed'])} total); dry run, nothing written")
        return 0
    save_state(ws, state)
    tlog(f"seeded {added} recording(s) as done ({len(state['processed'])} total); none will be reprocessed.")
    return 0


def watch_pass(ws: Path, source: Path, cfg: dict, whosaid: str, engine: str | None,
               accurate: bool, offline: bool, dry: bool) -> int:
    wcfg = cfg.get("watch") or {}
    stable_seconds = float(wcfg.get("stable_seconds", 120))
    max_wait = float(wcfg.get("max_wait_seconds", 900))
    state = load_state(ws)
    deadline = time.time() + max_wait
    while True:
        new, pending = scan(source, state, stable_seconds)
        for name, left in pending:
            tlog(f"still syncing ({int(left)}s until stable): {name}")
        if new or not pending or dry or time.time() >= deadline:
            break
        # launchd fires on the directory change that CREATES a recording (mtime = now)
        # and will not fire again just because it stabilizes, so this pass waits for
        # it; a recording still being written keeps advancing its mtime and max_wait
        # bounds the wait.
        wait = min(max(left for _, left in pending) + 2, max(deadline - time.time(), 1))
        tlog(f"  waiting {int(wait)}s ...")
        time.sleep(wait)
    if not new:
        tlog("no new recordings." if not pending
             else f"no stable recordings yet ({len(pending)} still syncing).")
        return 0
    tlog(f"{len(new)} new recording(s): " + ", ".join(n for _, _, n in new))
    if dry:
        tlog("dry run: nothing ingested.")
        return 0
    env = child_env(offline)
    done = 0
    for key, path, name in new:
        try:
            rc = ingest_one(ws, path, whosaid, engine, accurate, env)
        except OSError as e:
            if isinstance(e, (PermissionError, FileNotFoundError)):
                raise
            tlog(f"  ! could not run {whosaid} ({e}); will retry next trigger.")
            continue
        if rc == 0:
            state["processed"][key] = {"name": name, "at": now_iso()}
            save_state(ws, state)
            done += 1
            tlog(f"  ok ingested {name}")
        else:
            tlog(f"  ! whosaid exited {rc} on {name}; will retry next trigger.")
    if done:
        refresh(ws, whosaid, env)
    tlog(f"done: {done}/{len(new)} ingested.")
    return 0


def fda_hint(source: Path, err: BaseException) -> None:
    tlog(f"! cannot read the source folder: {err}")
    if is_macos():
        tlog(f"  -> grant Full Disk Access to this binary: {sys.executable}")
        tlog("     (System Settings > Privacy & Security > Full Disk Access), then retry.")
        tlog("     `whosaid watch install` provisions a dedicated binary so the grant covers nothing else.")
    else:
        tlog(f"  -> check that {source} exists and is readable, or pass --source DIR.")


def cmd_run(args: argparse.Namespace) -> int:
    ws = resolve_workspace(args.into)
    if not ws.is_dir():
        log(f"run: workspace not found: {ws}")
        return 1
    cfg = load_config(ws)
    source = resolve_source(args.source, cfg)
    whosaid = find_whosaid(args.whosaid)
    accurate = args.accurate or os.environ.get("WHOSAID_ACCURATE") == "1"
    with open(ws / LOCK_NAME, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            tlog("another run is active; exiting.")
            return 0
        try:
            if args.seed:
                return seed(ws, source, args.dry_run)
            return watch_pass(ws, source, cfg, whosaid, args.engine, accurate,
                              args.offline, args.dry_run)
        except (FileNotFoundError, PermissionError) as e:
            # PermissionError (Errno 1, "Operation not permitted") is what macOS TCC
            # raises when the running binary lacks Full Disk Access; FileNotFoundError
            # when the store path is absent. Both mean the same fix.
            fda_hint(source, e)
            return 3


# ---- launchd: plist / interpreter / launchctl ---------------------------------------

def build_plist(label: str, interpreter: str, ws: Path, source: Path, interval: int,
                offline: bool, extra_env: dict[str, str], source_explicit: bool) -> dict:
    program = [interpreter, str(Path(__file__).resolve()), "run", "--into", str(ws)]
    if source_explicit:
        program += ["--source", str(source)]
    if offline:
        program.append("--offline")
    env = {"PATH": AGENT_PATH}
    if offline:
        env.update(OFFLINE_ENV)
    for key in ("WHOSAID_ACTION_ITEMS_HOOK", "WHOSAID_BIN"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(extra_env)
    return {
        "Label": label,
        "ProgramArguments": program,
        # WatchPaths fires when a recording lands; StartInterval is a cheap safety net
        # (a stat of the folder) in case a recording stabilizes with no further change.
        "WatchPaths": [str(source)],
        "RunAtLoad": True,
        "StartInterval": int(interval),
        "ThrottleInterval": 60,
        "Nice": 5,
        "StandardOutPath": str(ws / LOG_NAME),
        "StandardErrorPath": str(ws / LOG_NAME),
        "EnvironmentVariables": env,
    }


def plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def read_plist(label: str) -> dict | None:
    try:
        with open(plist_path(label), "rb") as fh:
            data = plistlib.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def plist_workspace(data: dict | None) -> Path | None:
    args = list((data or {}).get("ProgramArguments") or [])
    if "--into" in args and args.index("--into") + 1 < len(args):
        return Path(args[args.index("--into") + 1])
    return None


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def provision_interpreter(dry: bool) -> str:
    """A private, ad-hoc signed copy of python at a unique path, used ONLY by the
    watcher, so the Full Disk Access grant is scoped to it (TCC grants per path;
    a symlink would resolve back to the shared interpreter)."""
    if AGENT_BIN.is_file() and os.access(AGENT_BIN, os.X_OK):
        return str(AGENT_BIN)
    base = sys.executable
    steps = [
        f"rm -rf {AGENT_DIR}",
        f"{base} -m venv --copies {AGENT_DIR}",
        f"mv {AGENT_DIR / 'bin/python3'} {AGENT_BIN}   (so the FDA list reads 'whosaid-watch')",
        f"rm {AGENT_DIR / 'bin/python*'}   (leave only the one named binary to grant)",
        f"codesign -f -s - {AGENT_BIN}   (ad-hoc signature = stable TCC identity)",
    ]
    if dry:
        log(f"would provision the dedicated interpreter {AGENT_BIN}:")
        for s in steps:
            log(f"    {s}")
        return str(AGENT_BIN)
    log(f"provisioning the dedicated interpreter {AGENT_BIN} from {base} ...")
    shutil.rmtree(AGENT_DIR, ignore_errors=True)
    subprocess.run([base, "-m", "venv", "--copies", str(AGENT_DIR)], check=True)
    (AGENT_DIR / "bin/python3").rename(AGENT_BIN)
    for stray in (AGENT_DIR / "bin").glob("python*"):
        stray.unlink()
    subprocess.run(["codesign", "-f", "-s", "-", str(AGENT_BIN)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not (AGENT_BIN.is_file() and os.access(AGENT_BIN, os.X_OK)):
        raise SystemExit(f"whosaid: could not provision {AGENT_BIN}")
    return str(AGENT_BIN)


def needs_fda(source: Path) -> bool:
    """TCC guards everything under ~/Library (the Voice Memos store included)."""
    src = source.expanduser()
    if src == VOICE_MEMOS_STORE:
        return True
    try:
        src = src.resolve()
    except OSError:
        pass
    return str(src).startswith(str(Path.home() / "Library") + os.sep)


def fda_instructions(interpreter: str, label: str, ws: Path, source: Path) -> str:
    lines = [
        "",
        "=" * 70,
        "ONE manual step left: Full Disk Access (macOS will not let a script do it)",
        "=" * 70,
        "The agent runs this binary, and it must have Full Disk Access to read the",
        f"TCC-protected folder {source}:",
        "",
        f"    {interpreter}",
        "",
        "1. System Settings > Privacy & Security > Full Disk Access (opening now).",
        "2. Click +, press Cmd+Shift+G, paste the path above, add it, toggle it ON.",
        f"3. Then run:  launchctl kickstart -k {domain()}/{label}",
        "",
        "Why one dedicated binary: macOS grants Full Disk Access per executable path,",
        "so a private copy of python that runs only this watcher gets a grant that",
        "covers nothing else. whosaid and the python/uv/ffmpeg it spawns only ever",
        f"read the staged copy in {ws / STAGING_NAME}.",
        "",
        f"Tail the log with:   tail -f {ws / LOG_NAME}",
        f"Uninstall with:      whosaid watch uninstall --into {ws}",
        "=" * 70,
    ]
    return "\n".join(lines)


def cmd_install(args: argparse.Namespace) -> int:
    if not is_macos() and not args.dry_run:
        log("install: launchd LaunchAgents exist only on macOS; on other systems run "
            "`whosaid watch run --into WS` from cron or a systemd timer.")
        return 1
    ws = resolve_workspace(args.into)
    if not ws.is_dir():
        log(f"install: workspace not found: {ws}")
        return 1
    cfg = load_config(ws)
    source = resolve_source(args.source, cfg)
    label = args.label or default_label(ws)
    interval = int(args.interval if args.interval is not None
                   else (cfg.get("watch") or {}).get("interval_seconds", 900))
    extra_env = parse_env_pairs(args.env)
    dry = args.dry_run

    if args.interpreter:
        interpreter = str(Path(args.interpreter).expanduser())
        if not dry and not os.access(interpreter, os.X_OK):
            log(f"install: --interpreter is not executable: {interpreter}")
            return 1
        log(f"using the existing interpreter {interpreter} (grant it Full Disk Access if you have not)")
    else:
        interpreter = provision_interpreter(dry)

    data = build_plist(label, interpreter, ws, source, interval, args.offline, extra_env,
                       source_explicit=bool(args.source))
    xml = plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False).decode()
    target = plist_path(label)
    seed_cmd = [interpreter, str(Path(__file__).resolve()), "run", "--into", str(ws), "--seed"]
    if args.source:
        seed_cmd += ["--source", str(source)]

    if dry:
        log("== DRY RUN: nothing written, nothing loaded ==")
        log(f"label     -> {label}")
        log(f"plist     -> {target}")
        log(f"watch     -> {source}")
        log(f"workspace -> {ws}")
        log(f"runs      -> {' '.join(data['ProgramArguments'])}")
        log(f"log       -> {ws / LOG_NAME}")
        log(f"FDA on    -> {interpreter}" if needs_fda(source) else "FDA       -> not needed (source is outside ~/Library)")
        if args.seed:
            log(f"would seed first: {' '.join(seed_cmd)}")
        log(f"would run: launchctl bootout {domain()}/{label} (ignore errors); "
            f"launchctl bootstrap {domain()} {target}; launchctl enable {domain()}/{label}")
        if needs_fda(source) and not args.no_open:
            log(f"would open the Full Disk Access pane: open \"{FDA_PANE_URL}\"")
        sys.stdout.write(xml)
        return 0

    if args.seed:
        log("seeding: marking every current recording as already done (none will be reprocessed)")
        if subprocess.run(seed_cmd).returncode != 0:
            log("seed failed (likely no Full Disk Access yet). Grant it, then re-run install --seed.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(xml)
    log(f"wrote {target}")
    launchctl("bootout", f"{domain()}/{label}")
    boot = launchctl("bootstrap", domain(), str(target))
    if boot.returncode != 0:
        log(f"install: launchctl bootstrap failed ({boot.stderr.strip() or boot.returncode})")
        return 1
    launchctl("enable", f"{domain()}/{label}")
    log(f"loaded agent {label}")
    if needs_fda(source):
        print(fda_instructions(interpreter, label, ws, source))
        if not args.no_open:
            subprocess.run(["open", FDA_PANE_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"Watching {source} (outside ~/Library, so no Full Disk Access needed).")
        print(f"Tail the log with:   tail -f {ws / LOG_NAME}")
    return 0


def resolve_label(args: argparse.Namespace) -> tuple[str, Path | None]:
    """--label wins; the workspace then comes from --into or the plist's --into."""
    if args.label:
        ws = resolve_workspace(args.into) if args.into else plist_workspace(read_plist(args.label))
        return args.label, ws
    ws = resolve_workspace(args.into)
    return default_label(ws), ws


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not is_macos():
        log("uninstall: launchd LaunchAgents exist only on macOS.")
        return 1
    label, ws = resolve_label(args)
    data = read_plist(label)
    out = launchctl("bootout", f"{domain()}/{label}")
    log(f"unloaded {label}" if out.returncode == 0 else f"{label} was not loaded")
    target = plist_path(label)
    if target.exists():
        target.unlink()
        log(f"removed {target}")
    else:
        log(f"no plist at {target}")
    if args.purge:
        if ws is None:
            log("purge: workspace unknown (no --into and the plist was gone); state files kept")
        else:
            for name in (STATE_NAME, LOCK_NAME):
                p = ws / name
                if p.exists():
                    p.unlink()
            shutil.rmtree(ws / STAGING_NAME, ignore_errors=True)
            log(f"purged {STATE_NAME}, {LOCK_NAME} and {STAGING_NAME}/ in {ws} (log kept)")
        if AGENT_DIR.exists():
            shutil.rmtree(AGENT_DIR, ignore_errors=True)
            log(f"removed the dedicated interpreter {AGENT_DIR}")
    interpreter = (data or {}).get("ProgramArguments", [None])[0] or str(AGENT_BIN)
    log(f"remove the Full Disk Access entry yourself if you want: System Settings > "
        f"Privacy & Security > Full Disk Access > {interpreter}")
    return 0


def launchctl_status(label: str) -> tuple[bool, int | None, int | None, str]:
    """(loaded, pid, last exit status, state) from `launchctl print`."""
    out = launchctl("print", f"{domain()}/{label}")
    if out.returncode != 0:
        return False, None, None, ""
    pid = last = None
    state = ""
    for line in out.stdout.splitlines():
        s = line.strip()
        if m := re.match(r"pid = (\d+)", s):
            pid = int(m.group(1))
        elif m := re.match(r"last exit code = (-?\d+)", s):
            last = int(m.group(1))
        elif m := re.match(r"state = (\S+)", s):
            state = m.group(1)
    return True, pid, last, state


def cmd_status(args: argparse.Namespace) -> int:
    if not is_macos():
        log("status: launchd LaunchAgents exist only on macOS.")
        return 1
    label, ws = resolve_label(args)
    data = read_plist(label)
    program = list((data or {}).get("ProgramArguments") or [])
    loaded, pid, last, state = launchctl_status(label)
    if ws is None:
        ws = plist_workspace(data)
    if ws is not None:
        cfg = load_config(ws)
        source = ((data or {}).get("WatchPaths") or [None])[0]
        source = Path(source) if source else resolve_source(None, cfg)
    else:
        source = ((data or {}).get("WatchPaths") or [None])[0]
        source = Path(source) if source else VOICE_MEMOS_STORE
    st = load_state(ws) if ws is not None else {"processed": {}}
    processed = st["processed"]
    log_tail: list[str] = []
    if ws is not None and (ws / LOG_NAME).is_file():
        try:
            log_tail = (ws / LOG_NAME).read_text(errors="replace").splitlines()[-3:]
        except OSError:
            log_tail = []
    info = {
        "label": label,
        "plist": str(plist_path(label)),
        "installed": data is not None,
        "loaded": loaded,
        "state": state or None,
        "pid": pid,
        "last_exit_status": last,
        "interpreter": program[0] if program else None,
        "workspace": str(ws) if ws is not None else None,
        "source": str(source),
        "source_readable": source.is_dir() if source else False,
        "state_file": str(ws / STATE_NAME) if ws is not None else None,
        "processed": len(processed),
        "seeded": sum(1 for v in processed.values() if isinstance(v, dict) and v.get("seeded")),
        "log": str(ws / LOG_NAME) if ws is not None else None,
        "log_tail": log_tail,
    }
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"label:            {info['label']}")
    print(f"installed:        {'yes' if info['installed'] else 'no'}  ({info['plist']})")
    print(f"loaded:           {'yes' if loaded else 'no'}"
          + (f"  state={state}" if state else "") + (f"  pid={pid}" if pid else ""))
    print(f"last exit status: {last if last is not None else 'n/a'}")
    print(f"interpreter:      {info['interpreter'] or 'n/a'}")
    print(f"workspace:        {info['workspace'] or 'n/a'}")
    print(f"source:           {info['source']}  ({'readable' if info['source_readable'] else 'NOT readable from this shell'})")
    print(f"processed:        {info['processed']} recording(s), {info['seeded']} seeded")
    print(f"log:              {info['log'] or 'n/a'}")
    for line in log_tail:
        print(f"  | {line}")
    return 0


# ---- memos: CloudRecordings.db (read-only, on a copy) -------------------------------

def db_copy(source: Path, tmp: Path) -> Path:
    """Copy CloudRecordings.db (+ -wal/-shm) into tmp so sqlite never locks the
    live database. Raises OSError (PermissionError under TCC) when unreadable."""
    live = source / DB_NAME
    dest = tmp / DB_NAME
    shutil.copy2(live, dest)
    for suffix in ("-wal", "-shm"):
        try:
            shutil.copy2(f"{live}{suffix}", f"{dest}{suffix}")
        except OSError:
            pass
    return dest


def core_data_date(value) -> str:
    try:
        return (CORE_DATA_EPOCH + dt.timedelta(seconds=float(value))).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return ""


def db_rows(source: Path) -> list[dict]:
    """Every recording row in the store's database, oldest first."""
    with tempfile.TemporaryDirectory(prefix="whosaid-memos-") as tmp:
        db = db_copy(source, Path(tmp))
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "select ZENCRYPTEDTITLE, ZPATH, ZDURATION, ZDATE, ZEVICTIONDATE "
                "from ZCLOUDRECORDING order by ZDATE"
            )
            rows = cur.fetchall()
        finally:
            con.close()
    out = []
    for title, path, duration, date, evicted in rows:
        out.append({
            "title": title or "",
            "file": path or "",
            "seconds": int(round(duration)) if isinstance(duration, (int, float)) else None,
            "date": core_data_date(date),
            "recently_deleted": evicted is not None,
        })
    return out


def row_state(source: Path, title: str) -> tuple[int, int] | None:
    """(rows, rows marked Recently Deleted) for an exact title; None when the
    database cannot be read (no Full Disk Access in this shell)."""
    try:
        with tempfile.TemporaryDirectory(prefix="whosaid-memos-") as tmp:
            db = db_copy(source, Path(tmp))
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                n, ev = con.execute(
                    "select count(*), coalesce(sum(ZEVICTIONDATE is not null), 0) "
                    "from ZCLOUDRECORDING where ZENCRYPTEDTITLE = ?", (title,)
                ).fetchone()
            finally:
                con.close()
        return int(n), int(ev)
    except (OSError, sqlite3.Error):
        return None


def plain_files(source: Path) -> list[dict]:
    out = []
    for _key, path, st, name in list_recordings(source):
        out.append({
            "title": path.stem, "file": name, "path": str(path), "bytes": st.st_size,
            "mtime": dt.datetime.fromtimestamp(st.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M"),
        })
    out.sort(key=lambda r: r["mtime"])
    return out


def memos_source(args: argparse.Namespace) -> Path:
    _ws, cfg = optional_config(getattr(args, "into", None))
    return resolve_source(args.source, cfg)


def cmd_memos_list(args: argparse.Namespace) -> int:
    source = memos_source(args)
    try:
        if is_store(source):
            rows = db_rows(source)
            kind = "store"
        else:
            rows = plain_files(source)
            kind = "folder"
    except (FileNotFoundError, PermissionError) as e:
        fda_hint(source, e)
        return 3
    except sqlite3.Error as e:
        log(f"memos list: {DB_NAME} unreadable ({e}); listing files instead")
        rows, kind = plain_files(source), "folder"
    if args.json:
        print(json.dumps({"source": str(source), "kind": kind, "recordings": rows}, indent=2))
        return 0
    if not rows:
        print(f"(no recordings in {source})")
        return 0
    if kind == "store":
        print(f"{'title':<40} {'file':<44} {'secs':>6}  date              state")
        for r in rows:
            secs = "" if r["seconds"] is None else str(r["seconds"])
            state = "RECENTLY DELETED" if r["recently_deleted"] else ""
            print(f"{r['title'][:40]:<40} {r['file'][:44]:<44} {secs:>6}  {r['date']:<16}  {state}")
    else:
        print(f"{'file':<48} {'bytes':>10}  modified")
        for r in rows:
            print(f"{r['file'][:48]:<48} {r['bytes']:>10}  {r['mtime']}")
    return 0


def cmd_memos_pull(args: argparse.Namespace) -> int:
    source = memos_source(args)
    out_dir = Path(args.out or ".").expanduser()
    try:
        files = list_recordings(source)
    except (FileNotFoundError, PermissionError) as e:
        fda_hint(source, e)
        return 3
    if not files:
        log(f"memos pull: no recordings in {source}")
        return 1
    if args.title:
        chosen: Path | None = None
        if is_store(source):
            try:
                matches = [r for r in db_rows(source) if r["title"] == args.title]
            except (OSError, sqlite3.Error) as e:
                log(f"memos pull: cannot map the title through {DB_NAME} ({e})")
                return 3
            if len(matches) != 1:
                log(f"memos pull: {len(matches)} recording(s) titled exactly {args.title!r} (see memos list)")
                return 1
            chosen = source / matches[0]["file"]
        else:
            stems = [p for _k, p, _s, _n in files if p.stem == args.title]
            if len(stems) != 1:
                log(f"memos pull: {len(stems)} file(s) named {args.title!r} in {source}")
                return 1
            chosen = stems[0]
        if not chosen.is_file():
            log(f"memos pull: {chosen} is not on disk (still in iCloud? open it in the app once)")
            return 1
    else:
        chosen = max((p for _k, p, _s, _n in files), key=lambda p: p.stat().st_mtime)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / chosen.name
    if dest.exists():
        log(f"memos pull: {dest} already exists; not overwriting")
        return 1
    shutil.copy2(chosen, dest)
    log(f"copied {chosen.name} -> {dest}")
    print(dest)
    return 0


def have_shortcut() -> bool:
    try:
        out = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.strip() == SHORTCUT_NAME for line in out.stdout.splitlines())


def cmd_memos_delete(args: argparse.Namespace) -> int:
    source = memos_source(args)
    if not is_macos():
        log("memos delete: needs macOS (the Voice Memos app and its Shortcuts action).")
        return 1
    if not is_store(source):
        log(f"memos delete: {source} is not a Voice Memos store ({DB_NAME} missing). Deletes go "
            "through the Voice Memos app so iCloud stays in sync; delete plain files yourself.")
        return 1
    title = args.title
    before = row_state(source, title)
    if before is None:
        log(f"cannot read {DB_NAME} (this shell needs Full Disk Access); cannot pre-check the title")
    elif before[0] == 0:
        log(f"no memo titled exactly {title!r} in the store (see `whosaid memos list`).")
        return 1
    elif before[0] > 1:
        log(f"{before[0]} memos share the title {title!r}; rename one in the app first.")
        return 1
    if not args.yes:
        try:
            answer = input(f"Delete Voice Memo \"{title}\" (moves to Recently Deleted, 30 days)? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 1
    if not have_shortcut():
        log(f"Shortcut {SHORTCUT_NAME!r} is not installed. Run: whosaid memos shortcut-recipe")
        return 1
    run = subprocess.run(["shortcuts", "run", SHORTCUT_NAME], input=title, text=True)
    if run.returncode != 0:
        log(f"`shortcuts run` exited {run.returncode}; verifying anyway")
    time.sleep(1)
    after = row_state(source, title)

    def fmt(s: tuple[int, int] | None) -> str:
        return "?|?" if s is None else f"{s[0]}|{s[1]}"

    print(f">> ran {SHORTCUT_NAME!r} on \"{title}\"  (db rows|recently-deleted before: {fmt(before)}, after: {fmt(after)})")
    if after is None:
        print(">> (cannot verify without Full Disk Access in this shell)")
        return 0
    if after[0] == 0:
        print(">> gone from the store.")
        return 0
    if after[1] >= 1:
        print(">> now in Recently Deleted (auto-purged in 30 days; restorable in the app until then).")
        return 0
    log("still present and not marked deleted; check the Shortcuts app for a permission prompt.")
    return 2


def shortcut_plist() -> bytes:
    """The unsigned one-action Shortcut: Voice Memos > Delete Recording(s) with its
    `entities` parameter bound to the Shortcut Input, which Shortcuts resolves from
    text through the app's own recording-by-title query."""
    data = {
        "WFWorkflowClientVersion": "2607.0.3",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": 59511, "WFWorkflowIconStartColor": 4282601983},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowTypes": [],
        "WFWorkflowActions": [{
            "WFWorkflowActionIdentifier": "com.apple.VoiceMemos.DeleteRecording",
            "WFWorkflowActionParameters": {
                "AppIntentDescriptor": {
                    "AppIntentIdentifier": "DeleteRecording",
                    "BundleIdentifier": "com.apple.VoiceMemos",
                    "Name": "Delete Recording",
                    "TeamIdentifier": "0000000000",
                },
                "entities": {
                    "Value": {"Type": "ExtensionInput"},
                    "WFSerializationType": "WFTextTokenAttachment",
                },
                "UUID": "7E1D6E2A-5D1E-4C1C-9C1D-2A0F2E3B4C5D",
            },
        }],
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML)


RECIPE = f"""\
Why a Shortcut: `whosaid memos delete` removes a memo the way the Voice Memos app
does, through the app's own "Delete Recordings" App Intent. iCloud stays in sync,
the memo lands in Recently Deleted (restorable for 30 days), and the app's
database is never edited behind its back. The Shortcut is one action and is
named exactly: {SHORTCUT_NAME}

Build it by hand (Shortcuts app, once, about 30 seconds):
  1. Shortcuts > File > New Shortcut. Name it exactly:  {SHORTCUT_NAME}
  2. In the right-hand action search type "Delete Recordings" (Voice Memos; the
     app labels the action in the plural) and add it.
  3. Click the action's "Recordings" field and choose the "Shortcut Input" variable.
     (If a "Receive ... input from" line appears, leave it on Text.)
  4. Close the window. Then:  whosaid memos list  /  whosaid memos delete "<title>"
"""


def cmd_memos_shortcut_recipe(args: argparse.Namespace) -> int:
    if args.no_sign or not is_macos():
        print(RECIPE)
        return 0
    # An unsigned Shortcut is a plist; `shortcuts sign` makes it importable. Signing
    # needs an iCloud account on this Mac; without one the recipe above is the way.
    with tempfile.TemporaryDirectory(prefix="whosaid-shortcut-") as tmp:
        src = Path(tmp) / f"{SHORTCUT_NAME}.shortcut"
        src.write_bytes(shortcut_plist())
        SHORTCUT_OUT.parent.mkdir(parents=True, exist_ok=True)
        sign = subprocess.run(
            ["shortcuts", "sign", "--mode", "anyone", "--input", str(src), "--output", str(SHORTCUT_OUT)],
            capture_output=True, text=True,
        )
    if sign.returncode == 0:
        print(f">> signed Shortcut at {SHORTCUT_OUT}")
        subprocess.run(["open", str(SHORTCUT_OUT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f">> Shortcuts is opening it. Click \"Add Shortcut\" once, then: whosaid memos list")
        return 0
    print(f"!! could not sign the Shortcut: {(sign.stderr or sign.stdout).strip() or sign.returncode}")
    print(RECIPE)
    subprocess.run(["open", "-a", "Shortcuts"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 1


# ---- CLI --------------------------------------------------------------------------

def add_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default=None,
                   help="folder of recordings to read (default: [watch] source in whosaid.toml, "
                        "else the macOS Voice Memos store)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watch.py",
        description="whosaid watcher: new recordings -> ingest, hands-free (launchd on macOS), "
                    "plus Voice Memos list/pull/delete helpers (issue #14).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="one watcher pass: ingest every new, stable recording")
    pr.add_argument("--into", default=None, help="meeting workspace (default: $WHOSAID_WORKSPACE or cwd)")
    add_source(pr)
    pr.add_argument("--seed", action="store_true",
                    help="mark every current recording as done without ingesting (run once at setup)")
    pr.add_argument("--dry-run", action="store_true", help="report what would be ingested; change nothing")
    pr.add_argument("--accurate", action="store_true", help="pass --accurate to whosaid ingest")
    pr.add_argument("--offline", action="store_true",
                    help="set HF_HUB_OFFLINE/HF_HUB_DISABLE_TELEMETRY/TRANSFORMERS_OFFLINE=1 for whosaid")
    pr.add_argument("--whosaid", default=None,
                    help="whosaid launcher (default: $WHOSAID_BIN, `whosaid` on PATH, or the one beside lib/)")
    pr.add_argument("--engine", default=None, help="pass --engine to whosaid ingest")
    pr.set_defaults(func=cmd_run)

    pi = sub.add_parser("install", help="write + load the launchd LaunchAgent (macOS)")
    pi.add_argument("--into", default=None, help="meeting workspace (default: $WHOSAID_WORKSPACE or cwd)")
    add_source(pi)
    pi.add_argument("--seed", action="store_true", help="first mark every current recording as done")
    pi.add_argument("--dry-run", action="store_true",
                    help="print the steps (stderr) and the plist (stdout); change nothing")
    pi.add_argument("--interpreter", default=None,
                    help="reuse this python binary (one that already has Full Disk Access) instead of "
                         f"provisioning {AGENT_BIN}")
    pi.add_argument("--label", default=None,
                    help=f"launchd label (default: {LABEL_PREFIX}<8 hex of sha256(workspace path)>)")
    pi.add_argument("--interval", type=int, default=None,
                    help="StartInterval safety net in seconds (default: [watch] interval_seconds, 900)")
    pi.add_argument("--offline", action="store_true", help="bake the HF offline env vars into the agent")
    pi.add_argument("--env", action="append", metavar="KEY=VALUE",
                    help="extra EnvironmentVariables for the agent (repeatable, e.g. WHOSAID_ACCURATE=1)")
    pi.add_argument("--no-open", action="store_true", help="do not open the Full Disk Access pane")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="unload + remove the LaunchAgent")
    pu.add_argument("--into", default=None, help="meeting workspace the agent was installed for")
    pu.add_argument("--label", default=None, help="launchd label (overrides --into)")
    pu.add_argument("--purge", action="store_true",
                    help=f"also remove the dedicated interpreter and the workspace's {STATE_NAME}, "
                         f"{LOCK_NAME}, {STAGING_NAME}/ (never the log)")
    pu.set_defaults(func=cmd_uninstall)

    ps = sub.add_parser("status", help="is the agent installed, loaded, healthy?")
    ps.add_argument("--into", default=None, help="meeting workspace the agent was installed for")
    ps.add_argument("--label", default=None, help="launchd label (overrides --into)")
    ps.add_argument("--json", action="store_true", help="machine-readable output")
    ps.set_defaults(func=cmd_status)

    pm = sub.add_parser("memos", help="Voice Memos helpers: list, pull, delete, shortcut-recipe")
    msub = pm.add_subparsers(dest="memos_command", required=True)

    ml = msub.add_parser("list", help="titles in the store (read-only, from a copy of the database)")
    add_source(ml)
    ml.add_argument("--json", action="store_true")
    ml.set_defaults(func=cmd_memos_list)

    mp = msub.add_parser("pull", help="copy a recording out of the store")
    add_source(mp)
    g = mp.add_mutually_exclusive_group()
    g.add_argument("--latest", action="store_true", help="the newest recording (default)")
    g.add_argument("--title", default=None, help="the recording with exactly this title")
    mp.add_argument("-o", "--out", default=None, help="destination folder (default: current directory)")
    mp.set_defaults(func=cmd_memos_pull)

    md = msub.add_parser(
        "delete",
        help="delete a memo BY TITLE through the Voice Memos app's own action (iCloud-safe, "
             "Recently Deleted for 30 days)")
    md.add_argument("title")
    md.add_argument("--yes", "-y", action="store_true", help="no confirmation prompt")
    add_source(md)
    md.set_defaults(func=cmd_memos_delete)

    mr = msub.add_parser("shortcut-recipe",
                         help=f"build + sign the one-action '{SHORTCUT_NAME}' Shortcut, or print how to")
    mr.add_argument("--no-sign", action="store_true", help="only print the manual recipe")
    mr.set_defaults(func=cmd_memos_shortcut_recipe)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

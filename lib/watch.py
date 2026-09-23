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
      `whosaid ingest <copy> --into <ws> --folder-by created --action-items
      --commitments` (dev-commitments always ride along: the extractor is a
      stdlib heuristic, so it costs nothing) plus any `[watch]` speaker hints
      (speakers/min_speakers/max_speakers/expected_speakers), and recorded as
      done (keyed name:size, recordings are immutable). After any success
      `whosaid roll-up <ws> --action-items` (which also folds the commitments
      corpus and regenerates _WORKLIST-<Owner>.md) and `whosaid index <ws>`
      rebuild the aggregates. A file whose mtime is fresher than
      stable_seconds may still be syncing, so the pass stays alive (bounded by
      watch.max_wait_seconds) and rescans. <ws>/.watch.lock guards against
      overlapping passes. --seed marks every current recording as done so an
      existing library is never reprocessed; --dry-run only reports.

  install   --into WS [--source DIR] [--seed] [--dry-run] [--interpreter PATH]
            [--label L] [--interval S] [--offline] [--env KEY=VALUE ...] [--no-open]
            [--no-fda]
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

  menubar install [--workspace DIR] [--interval 10s]     (issue #24)
      Symlink the SwiftBar menu bar plugin (contrib/swiftbar/whosaid.10s.py)
      into SwiftBar's plugin directory: `defaults read com.ameba.SwiftBar
      PluginDirectory`; when unset, it configures ~/.config/swiftbar. A non-default
      --interval names the symlink whosaid.<interval>.py. When SwiftBar is
      absent (no app bundle, no PluginDirectory) it prints an
      install-then-retry message and exits non-zero. WHOSAID_SWIFTBAR_DIR and
      WHOSAID_SWIFTBAR_APP override the directory and the app-bundle check
      (tests point them at temp dirs). The plugin itself reads
      WHOSAID_WORKSPACE at refresh time, exactly like the MCP server.
  menubar uninstall   remove the plugin symlink(s) from the plugin directory
  menubar status      is SwiftBar installed/running, the plugin linked, the
                      Voice Memos store readable?

Source folder: --source, else [watch] source in <ws>/whosaid.toml, else the
macOS Voice Memos store (~/Library/Group Containers/group.com.apple.VoiceMemos.shared/
Recordings). Any folder of audio files works. --no-fda defaults an otherwise-unset
source to ~/Recordings: the FDA toggle is admin-gated on macOS, so a standard
user records into a plain folder instead (README 'No admin rights?').

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
import getpass
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
    str(Path.home() / ".local/bin"), str(Path.home() / "bin"),
    str(Path.home() / "homebrew/bin"), str(Path.home() / "homebrew/sbin"),
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])
FDA_PANE_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

# The Whisper weights live in the local HF cache; with --offline every child skips
# the doomed round-trip to huggingface.co (blocked networks, planes, privacy).
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TRANSFORMERS_OFFLINE": "1"}
CERTIFICATE_ENV = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                   "CURL_CA_BUNDLE", "UV_SYSTEM_CERTS", "UV_NATIVE_TLS")

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


def overlap_problem(ws: Path, source: Path) -> str | None:
    """None when the workspace and the recordings source are safely separate
    folders; otherwise an actionable message naming both real (symlink- and
    /var-resolved) paths. A watcher whose source is the workspace itself, or
    nested either way, would scan its own staging/output files (or the
    cross-meeting corpora and _search.db) and mix raw audio into meeting
    folders. Path.resolve() (default strict=False) does not require the
    target to exist, so this is safe to call before `source` exists (install
    may still need to create it, e.g. the --no-fda ~/Recordings default);
    any other OSError (a symlink loop) falls back to comparing the
    unresolved path rather than raising."""
    try:
        real_ws = ws.resolve()
    except OSError:
        real_ws = ws
    try:
        real_source = source.resolve()
    except OSError:
        real_source = source
    if not (real_ws == real_source
            or real_source in real_ws.parents
            or real_ws in real_source.parents):
        return None
    return (
        f"the recordings source ({real_source}) and the meeting workspace ({real_ws}) "
        "must be separate folders: neither the same directory nor nested inside the "
        "other. The watcher would otherwise scan its own staging/output files (or the "
        "workspace's transcripts, corpora, and _search.db) as if they were recordings. "
        "Keep them apart, e.g. recordings in ~/Recordings and the workspace in "
        "~/meetings."
    )


def is_store(source: Path) -> bool:
    """True when the source is a Voice Memos store (holds CloudRecordings.db)."""
    try:
        return (source / DB_NAME).is_file()
    except OSError:
        return False


def is_voice_memos_source(source: Path) -> bool:
    """Whether source is the TCC-protected Voice Memos recording store."""
    return source.expanduser() == VOICE_MEMOS_STORE or is_store(source)


def source_readable(source: Path) -> bool:
    """Whether this process can enumerate the selected recording source."""
    try:
        os.listdir(source)
        return True
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


def diarize_hints(wcfg: dict) -> tuple[list[str], str | None]:
    """Extra `whosaid ingest` args for the `[watch]` speaker hints (`speakers`,
    `min_speakers`, `max_speakers`, `expected_speakers`), or an error message
    naming the bad key. A watcher never gets to ask how many people were in
    the room, so blind auto-detect is the default; these let a workspace pin
    what it already knows (README 'Hands-free ingest').

    Unset or empty keys mean today's behavior exactly: no extra args. Each of
    `speakers`/`min_speakers`/`max_speakers` must be a whole number >= 1 (a
    bool is rejected even though Python's bool is an int subclass), and
    `min_speakers` may not exceed `max_speakers`. `expected_speakers` is a
    list of names or one comma-separated string (either way, an empty name
    after stripping is an error); the args are joined into one
    `--expected-speakers A,B`, which is how the whosaid launcher forwards a
    single occurrence's comma-separated value straight to the diarizer.

    Arg order is fixed: --speakers, --min-speakers, --max-speakers,
    --expected-speakers.
    """

    def positive_int(key: str) -> tuple[int | None, str | None]:
        value = wcfg.get(key)
        if value is None:
            return None, None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None, f"[watch] {key} must be a whole number >= 1 (got {value!r})"
        return value, None

    speakers, err = positive_int("speakers")
    if err:
        return [], err
    min_speakers, err = positive_int("min_speakers")
    if err:
        return [], err
    max_speakers, err = positive_int("max_speakers")
    if err:
        return [], err
    if min_speakers is not None and max_speakers is not None and min_speakers > max_speakers:
        return [], (f"[watch] min_speakers ({min_speakers}) must be <= "
                     f"max_speakers ({max_speakers})")

    raw_expected = wcfg.get("expected_speakers")
    names: list[str] = []
    if raw_expected:
        if isinstance(raw_expected, str):
            candidates = raw_expected.split(",")
        elif isinstance(raw_expected, list):
            candidates = raw_expected
        else:
            return [], (f"[watch] expected_speakers must be a list of names or a "
                         f"comma-separated string (got {raw_expected!r})")
        for candidate in candidates:
            name = str(candidate).strip()
            if not name:
                return [], f"[watch] expected_speakers has an empty name (got {raw_expected!r})"
            names.append(name)

    args: list[str] = []
    if speakers is not None:
        args += ["--speakers", str(speakers)]
    if min_speakers is not None:
        args += ["--min-speakers", str(min_speakers)]
    if max_speakers is not None:
        args += ["--max-speakers", str(max_speakers)]
    if names:
        args += ["--expected-speakers", ",".join(names)]
    return args, None


def ingest_one(ws: Path, path: Path, whosaid: str, engine: str | None, accurate: bool,
               hints: list[str], env: dict[str, str]) -> int:
    """Copy the recording OUT of the (possibly TCC-protected) source into staging
    and run whosaid on the copy, so only this process needs Full Disk Access.
    `hints` are the [watch] speaker-hint args from diarize_hints()."""
    staging = ws / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / path.name
    shutil.copy2(path, staged)  # keeps mtime; the container keeps its creation_time tag
    try:
        cmd = [whosaid, "ingest", str(staged), "--into", str(ws),
               "--folder-by", "created", "--action-items", "--commitments"]
        if engine:
            cmd += ["--engine", engine]
        if accurate:
            cmd.append("--accurate")
        cmd += hints
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
    # cmd_run already validated the hints before taking the lock; recomputing
    # here from the same cfg is deterministic and cannot fail.
    hints, _hint_err = diarize_hints(wcfg)
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
            rc = ingest_one(ws, path, whosaid, engine, accurate, hints, env)
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
        if isinstance(err, PermissionError) and not is_admin():
            tlog("  -> no admin available to grant Full Disk Access? Record into a plain folder instead: whosaid watch install --no-fda")
    else:
        tlog(f"  -> check that {source} exists and is readable, or pass --source DIR.")


def cmd_run(args: argparse.Namespace) -> int:
    ws = resolve_workspace(args.into)
    if not ws.is_dir():
        log(f"run: workspace not found: {ws}")
        return 1
    cfg = load_config(ws)
    _hints, hint_err = diarize_hints(cfg.get("watch") or {})
    if hint_err:
        log(f"run: {hint_err}")
        return 1
    source = resolve_source(args.source, cfg)
    problem = overlap_problem(ws, source)
    if problem:
        log(f"run: {problem}")
        return 1
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
                offline: bool, extra_env: dict[str, str], source_explicit: bool,
                preserved_env: dict[str, str] | None = None) -> dict:
    program = [interpreter, str(Path(__file__).resolve()), "run", "--into", str(ws)]
    if source_explicit:
        program += ["--source", str(source)]
    if offline:
        program.append("--offline")
    # launchd starts with a deliberately sparse environment.  Preserve values the
    # user explicitly supplied on an earlier install, while owning PATH/offline
    # policy here so reinstall cannot retain stale launcher settings.
    env = {k: v for k, v in (preserved_env or {}).items()
           if k not in {"PATH", *OFFLINE_ENV}}
    old_path = str((preserved_env or {}).get("PATH") or "")
    # A prior explicit --env PATH=... must survive reinstall. Add currently
    # supported prefixes after it, deduplicated, so old generated PATH values
    # also gain new user-local prefixes without changing explicit precedence.
    env["PATH"] = ":".join(dict.fromkeys(
        [part for part in old_path.split(":") + AGENT_PATH.split(":") if part]))
    if offline:
        env.update(OFFLINE_ENV)
    for key in ("WHOSAID_ACTION_ITEMS_HOOK", "WHOSAID_BIN", *CERTIFICATE_ENV):
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


def watcher_source(data: dict | None, ws: Path | None) -> tuple[Path | None, Path]:
    """Resolve a watcher's workspace and source from its installed plist.

    ``WatchPaths`` is the authoritative installed source, including the plain
    folder pinned by ``watch install --no-fda``. Configuration is only a
    fallback for a plist that has no watch path.
    """
    if ws is None:
        ws = plist_workspace(data)
    configured = ((data or {}).get("WatchPaths") or [None])[0]
    if configured:
        return ws, Path(configured)
    if ws is not None:
        return ws, resolve_source(None, load_config(ws))
    return None, VOICE_MEMOS_STORE


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def compatible_copying_interpreter() -> str:
    """Find a Python that can make a real copied venv (CLT Python cannot)."""
    candidates = [sys.executable]
    for directory in AGENT_PATH.split(":"):
        candidate = Path(directory) / "python3"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            candidates.append(str(candidate))
    uv = shutil.which("uv", path=AGENT_PATH)
    if uv:
        try:
            found = subprocess.run([uv, "python", "find", "3.12"], capture_output=True,
                                   text=True, timeout=10)
            if found.returncode == 0 and found.stdout.strip():
                candidates.append(found.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    seen: set[str] = set()
    for candidate in candidates:
        candidate = str(Path(candidate).resolve())
        if candidate in seen:
            continue
        seen.add(candidate)
        with tempfile.TemporaryDirectory(prefix="whosaid-venv-probe-") as probe:
            try:
                result = subprocess.run([candidate, "-m", "venv", "--copies", probe],
                                        capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0 and os.access(Path(probe) / "bin/python3", os.X_OK):
                return candidate
    raise SystemExit(
        "whosaid: no compatible Python could create the dedicated copied interpreter. "
        "Apple Command Line Tools Python cannot create venvs with --copies. Install a "
        "user-local Python (for example `uv python install 3.12`) and rerun, or pass "
        "--interpreter PATH to an executable you manage."
    )


def provision_interpreter(dry: bool) -> str:
    """A private, ad-hoc signed copy of python at a unique path, used ONLY by the
    watcher, so the Full Disk Access grant is scoped to it (TCC grants per path;
    a symlink would resolve back to the shared interpreter)."""
    if AGENT_BIN.is_file() and os.access(AGENT_BIN, os.X_OK):
        return str(AGENT_BIN)
    base = sys.executable if dry else compatible_copying_interpreter()
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
    try:
        subprocess.run([base, "-m", "venv", "--copies", str(AGENT_DIR)], check=True)
        (AGENT_DIR / "bin/python3").rename(AGENT_BIN)
        for stray in (AGENT_DIR / "bin").glob("python*"):
            stray.unlink()
    except (subprocess.CalledProcessError, OSError) as e:
        raise SystemExit(
            f"whosaid: could not provision {AGENT_BIN} from {base} ({e}); "
            f"install a Python with `uv python install 3.12`, pass --interpreter PATH, or set "
            f"WHOSAID_WATCH_AGENT_DIR to a writable location"
        ) from None
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


def is_admin() -> bool:
    """Can the current user satisfy the Full Disk Access toggle, which demands
    an administrator's password? WHOSAID_PRETEND_NON_ADMIN forces False (test
    hook); failed lookups err True so a real admin is never steered away from
    the Voice Memos path by a false negative."""
    if os.environ.get("WHOSAID_PRETEND_NON_ADMIN"):
        return False
    if not is_macos():
        return True
    try:
        user = getpass.getuser()
        try:
            import grp  # POSIX-only; macOS always has it
            return user in grp.getgrnam("admin").gr_mem
        except Exception:  # noqa: BLE001  (no grp module / no admin group: id -Gn)
            pass
        out = subprocess.run(["id", "-Gn"], capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            return True  # uncertain: assume admin
        return "admin" in out.stdout.split()
    except Exception:  # noqa: BLE001
        return True  # uncertain: assume admin


def capture_options() -> list[str]:
    """The three capture setups that land recordings in a plain folder (no Full
    Disk Access anywhere); shared by no_admin_notice and install's no-FDA
    success path."""
    return [
        "  1. iPhone Shortcut \"Record Audio -> Save File\" into a Dropbox folder (e.g. ~/Dropbox/whosaid) on the Action Button — Dropbox, not iCloud Drive, because iCloud lives under ~/Library",
        "  2. QuickTime Player (built in): File > New Audio Recording, stop, save into ~/Recordings",
        "  3. drag recordings out of the Voice Memos app into ~/Recordings",
    ]


def no_admin_notice(source: Path) -> str:
    """The admin-wall explainer printed BEFORE fda_instructions(): the FDA
    toggle needs an administrator's password (MDM may hide the pane), so a
    standard user sees the plain-folder escape hatch first."""
    lines = [
        "",
        "=" * 70,
        "No administrator password? You can skip Full Disk Access entirely",
        "=" * 70,
        f"This agent needs Full Disk Access to read {source}, and the toggle in",
        "System Settings asks for an administrator's password — on a",
        "company-managed Mac the pane may be hidden entirely.",
        "",
        "If you cannot get 5 minutes of admin time, skip FDA: record into a plain",
        "folder outside ~/Library and run",
        "",
        "    whosaid watch install --no-fda",
        "",
        "Three capture options that need no Full Disk Access:",
    ] + capture_options() + [
        "",
        "Details: README section 'No admin rights?'",
        "=" * 70,
    ]
    return "\n".join(lines)


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
    hint_args, hint_err = diarize_hints(cfg.get("watch") or {})
    if hint_err:
        log(f"install: {hint_err}")
        return 1
    source = resolve_source(args.source, cfg)
    label = args.label or default_label(ws)
    interval = int(args.interval if args.interval is not None
                   else (cfg.get("watch") or {}).get("interval_seconds", 900))
    extra_env = parse_env_pairs(args.env)
    dry = args.dry_run
    source_explicit = bool(args.source)
    no_fda_default_source = False
    if args.no_fda:
        if not source_explicit and not str((cfg.get("watch") or {}).get("source") or "").strip():
            # nothing chosen and the admin-gated FDA path is off the table:
            # watch ~/Recordings, a plain folder any capture app can write into
            source = Path.home() / "Recordings"
            source_explicit = True  # pin the path in the plist
            no_fda_default_source = True
        if needs_fda(source):
            log(f"install: --no-fda watches a plain folder, but {source} is under ~/Library")
            log("(TCC guards all of ~/Library; the toggle would demand an admin password).")
            log("Pick a folder outside ~/Library instead, e.g. ~/Recordings or a Dropbox")
            log("folder such as ~/Dropbox/whosaid.")
            return 1

    # The source is now final (including the --no-fda ~/Recordings default): refuse an
    # overlap with the workspace before creating anything, provisioning the dedicated
    # interpreter, writing the plist, or running launchctl -- in --dry-run too.
    problem = overlap_problem(ws, source)
    if problem:
        log(f"install: {problem}")
        return 1
    if no_fda_default_source and not dry:
        source.mkdir(parents=True, exist_ok=True)

    if args.interpreter:
        interpreter = str(Path(args.interpreter).expanduser())
        if not dry and not os.access(interpreter, os.X_OK):
            log(f"install: --interpreter is not executable: {interpreter}")
            return 1
        log(f"using the existing interpreter {interpreter} (grant it Full Disk Access if you have not)")
    elif args.no_fda:
        # A plain folder does not need a dedicated TCC identity.  Reusing the
        # launcher interpreter avoids CLT's unsupported copied-venv path.
        interpreter = sys.executable
        log(f"using {interpreter}; no dedicated interpreter is needed for --no-fda")
    else:
        interpreter = provision_interpreter(dry)

    prior = read_plist(label)
    prior_env = (prior or {}).get("EnvironmentVariables")
    if not isinstance(prior_env, dict):
        prior_env = {}
    data = build_plist(label, interpreter, ws, source, interval, args.offline, extra_env,
                       source_explicit=source_explicit, preserved_env=prior_env)
    xml = plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False).decode()
    target = plist_path(label)
    seed_cmd = [interpreter, str(Path(__file__).resolve()), "run", "--into", str(ws), "--seed"]
    if source_explicit:
        seed_cmd += ["--source", str(source)]

    if dry:
        log("== DRY RUN: nothing written, nothing loaded ==")
        log(f"label     -> {label}")
        log(f"plist     -> {target}")
        log(f"watch     -> {source}")
        log(f"speakers  -> {' '.join(hint_args) if hint_args else 'auto-detect (no [watch] speaker hints)'}")
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
        if needs_fda(source) and not is_admin():
            log("note: you are not an admin; the FDA toggle will ask for an admin password — "
                "see README 'No admin rights?' for the no-FDA path")
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
        if not is_admin():
            print(no_admin_notice(source))  # escape hatch first, so it is not lost below
        print(fda_instructions(interpreter, label, ws, source))
        if not args.no_open:
            subprocess.run(["open", FDA_PANE_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"Watching {source} (outside ~/Library, so no Full Disk Access needed).")
        for line in capture_options():
            print(line)
        print(f"Tail the log with:   tail -f {ws / LOG_NAME}")
    print("Menu bar: install the SwiftBar glyph with:  whosaid watch menubar install")
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
        elif m := re.match(r"state = (.+)", s):
            # launchctl states are not necessarily one word (for example,
            # "not running"). Keep the complete value for CLI users and for
            # consumers that decide whether a PID is expected to be live.
            state = m.group(1).strip()
    return True, pid, last, state


def process_alive(pid: int | None) -> bool:
    """Whether pid currently names a process visible to this user."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_status(args: argparse.Namespace) -> int:
    if not is_macos():
        log("status: launchd LaunchAgents exist only on macOS.")
        return 1
    label, ws = resolve_label(args)
    data = read_plist(label)
    program = list((data or {}).get("ProgramArguments") or [])
    loaded, pid, last, state = launchctl_status(label)
    ws, source = watcher_source(data, ws)
    st = load_state(ws) if ws is not None else {"processed": {}}
    processed = st["processed"]
    log_tail: list[str] = []
    if ws is not None and (ws / LOG_NAME).is_file():
        try:
            log_tail = (ws / LOG_NAME).read_text(errors="replace").splitlines()[-3:]
        except OSError:
            log_tail = []
    pid_alive = process_alive(pid)
    info = {
        "label": label,
        "plist": str(plist_path(label)),
        "installed": data is not None,
        "loaded": loaded,
        "state": state or None,
        "pid": pid,
        "pid_alive": pid_alive,
        "last_exit_status": last,
        "interpreter": program[0] if program else None,
        "workspace": str(ws) if ws is not None else None,
        "source": str(source),
        "source_readable": source_readable(source) if source else False,
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
          + (f"  state={state}" if state else "") + (f"  pid={pid}" if pid else "")
          + (f"  pid-alive={'yes' if pid_alive else 'no'}" if pid else ""))
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


# ---- menubar: the SwiftBar plugin (issue #24) ----------------------------------------

SWIFTBAR_PLUGIN_SRC = Path(__file__).resolve().parent.parent / "contrib" / "swiftbar" / "whosaid.10s.py"
SWIFTBAR_INTERVAL_DEFAULT = "10s"
SWIFTBAR_INTERVAL_RE = re.compile(r"^[0-9]+[smh]$")
MENUBAR_FALLBACK_DIR = Path.home() / ".config/swiftbar"


def swiftbar_app() -> Path | None:
    """The SwiftBar.app bundle, or None when SwiftBar is not installed.
    WHOSAID_SWIFTBAR_APP relocates the check (tests point it at a temp dir)."""
    env = os.environ.get("WHOSAID_SWIFTBAR_APP", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for base in (Path("/Applications"), Path.home() / "Applications"):
        if (base / "SwiftBar.app").is_dir():
            return base / "SwiftBar.app"
    return None


def swiftbar_plugin_dir() -> tuple[Path | None, str]:
    """(dir, how-resolved). WHOSAID_SWIFTBAR_DIR (tests, or installing before
    SwiftBar's first run) > `defaults read com.ameba.SwiftBar PluginDirectory`.
    (None, "unset") when SwiftBar is absent or was never configured."""
    env = os.environ.get("WHOSAID_SWIFTBAR_DIR", "").strip()
    if env:
        return Path(env).expanduser(), "WHOSAID_SWIFTBAR_DIR"
    try:
        out = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None, "unset"
    raw = out.stdout.strip() if out.returncode == 0 else ""
    return (Path(raw).expanduser(), "SwiftBar PluginDirectory") if raw else (None, "unset")


def configure_swiftbar_plugin_dir(pdir: Path) -> bool:
    """Set SwiftBar's existing PluginDirectory preference to pdir.

    This is only called when that preference is unset, so an existing user
    choice is never replaced. A link in an unconfigured fallback directory is
    not presented as a complete install because SwiftBar may not discover it.
    """
    try:
        out = subprocess.run(
            ["defaults", "write", "com.ameba.SwiftBar", "PluginDirectory", "-string", str(pdir)],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if out.returncode != 0:
        return False
    actual, _how = swiftbar_plugin_dir()
    return actual == pdir


def installed_watcher_labels() -> list[str]:
    """All locally installed watcher labels, sorted for deterministic status."""
    try:
        return sorted(p.name[:-len(".plist")] for p in LAUNCH_AGENTS.glob(f"{LABEL_PREFIX}*.plist"))
    except OSError:
        return []


def menubar_links(pdir: Path | None) -> list[tuple[Path, str]]:
    """(symlink, interval) for every whosaid.<interval>.py in pdir that points
    at the repo's plugin source (a stale/broken link still matches: the
    resolved target path is compared, existence is not required)."""
    out: list[tuple[Path, str]] = []
    if pdir is None or not pdir.is_dir():
        return out
    for entry in sorted(pdir.glob("whosaid.*.py")):
        if not entry.is_symlink():
            continue
        try:
            if entry.resolve() == SWIFTBAR_PLUGIN_SRC.resolve():
                out.append((entry, entry.name.split(".")[1]))
        except OSError:
            continue
    return out


def cmd_menubar_install(args: argparse.Namespace) -> int:
    if not SWIFTBAR_PLUGIN_SRC.is_file():
        log(f"menubar install: the plugin source is missing from the checkout: {SWIFTBAR_PLUGIN_SRC}")
        return 1
    if not SWIFTBAR_INTERVAL_RE.match(args.interval or ""):
        log(f"menubar install: --interval must look like 10s / 5m / 1h (got {args.interval!r})")
        return 2
    ws: Path | None = None
    if args.workspace:
        ws = Path(args.workspace).expanduser()
        if not ws.is_dir():
            log(f"menubar install: --workspace not found: {ws}")
            return 1
    pdir, how = swiftbar_plugin_dir()
    if pdir is None:
        if swiftbar_app() is None:
            log("menubar install: SwiftBar is not installed (no SwiftBar.app in /Applications or ~/Applications)")
            log("  install it from https://swiftbar.com, launch it once and set its plugin directory")
            log("  (or rely on the ~/.config/swiftbar default), then re-run:  whosaid watch menubar install")
            return 1
        pdir = MENUBAR_FALLBACK_DIR
        try:
            pdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"menubar install: cannot create the plugin directory {pdir} ({e})")
            return 1
        if not configure_swiftbar_plugin_dir(pdir):
            log("menubar install: SwiftBar PluginDirectory is unset and could not be configured")
            log(f"  choose {pdir} as SwiftBar's plugin directory, then re-run: whosaid watch menubar install")
            return 1
        how = "configured SwiftBar PluginDirectory"
    try:
        pdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"menubar install: cannot create the plugin directory {pdir} ({e})")
        return 1
    target = pdir / f"whosaid.{args.interval}.py"
    if target.exists() and not target.is_symlink():
        log(f"menubar install: refusing to overwrite the non-symlink {target}")
        return 1
    already = False
    if target.is_symlink():
        same = False
        try:
            same = target.resolve() == SWIFTBAR_PLUGIN_SRC.resolve()
        except OSError:
            pass
        already = same and target.exists()
        if already:
            log(f"menubar install: already installed at {target}")
        else:
            target.unlink()
            log(f"menubar install: replaced the link at {target}")
    if not already:
        os.symlink(str(SWIFTBAR_PLUGIN_SRC), target)
        log(f"menubar install: linked {target} -> {SWIFTBAR_PLUGIN_SRC}")
    if not os.access(SWIFTBAR_PLUGIN_SRC, os.X_OK):
        try:
            SWIFTBAR_PLUGIN_SRC.chmod(0o755)
        except OSError:
            pass
    print(f"SwiftBar plugin dir: {pdir}  ({how})")
    print(f"plugin:               {target}  (SwiftBar refreshes it every {args.interval})")
    if how == "configured SwiftBar PluginDirectory":
        print("Plugin directory configured; refresh or relaunch SwiftBar to load the plugin.")
    ws_hint = str(ws) if ws is not None else os.environ.get("WHOSAID_WORKSPACE", "").strip()
    if ws_hint:
        print("The plugin reads WHOSAID_WORKSPACE; make it visible to SwiftBar (a GUI app) with:")
        print(f"    launchctl setenv WHOSAID_WORKSPACE {Path(ws_hint).expanduser()}")
        print("then relaunch SwiftBar (without it the plugin falls back to the single installed watcher label).")
    else:
        print("The plugin reads WHOSAID_WORKSPACE; set it with:")
        print("    launchctl setenv WHOSAID_WORKSPACE <your meeting workspace>")
        print("so SwiftBar (a GUI app) sees it, then relaunch SwiftBar.")
    return 0


def cmd_menubar_uninstall(args: argparse.Namespace) -> int:
    pdir, _how = swiftbar_plugin_dir()
    candidates = [pdir] if pdir is not None else []
    if MENUBAR_FALLBACK_DIR not in candidates:
        candidates.append(MENUBAR_FALLBACK_DIR)
    removed = 0
    for d in candidates:
        for link, _interval in menubar_links(d):
            try:
                link.unlink()
                log(f"menubar uninstall: removed {link}")
                removed += 1
            except OSError as e:
                log(f"menubar uninstall: could not remove {link} ({e})")
    if not removed:
        looked = ", ".join(str(d) for d in candidates if d) or "nowhere"
        log(f"menubar uninstall: no whosaid menu bar plugin found (looked in {looked})")
    else:
        log("menubar uninstall: SwiftBar drops it within one refresh interval")
    return 0


def cmd_menubar_status(args: argparse.Namespace) -> int:
    app = swiftbar_app()
    pdir, how = swiftbar_plugin_dir()
    running: bool | None = None
    try:
        running = subprocess.run(["pgrep", "-x", "SwiftBar"], capture_output=True).returncode == 0
    except OSError:
        pass
    links = menubar_links(pdir)
    if pdir != MENUBAR_FALLBACK_DIR:
        links += [pair for pair in menubar_links(MENUBAR_FALLBACK_DIR) if pair not in links]
    ws_env = os.environ.get("WHOSAID_WORKSPACE", "").strip()
    ws = Path(ws_env).expanduser() if ws_env else None
    if ws is not None:
        data = read_plist(default_label(ws))
    else:
        labels = installed_watcher_labels()
        data = read_plist(labels[0]) if len(labels) == 1 else None
    _ws, source = watcher_source(data, ws)
    source_ok = source_readable(source)
    voice_memos = is_voice_memos_source(source)
    print(f"SwiftBar app:     {'installed (' + str(app) + ')' if app else 'NOT installed'}")
    if pdir is not None:
        print(f"plugin directory: {pdir}  ({how})")
    else:
        print(f"plugin directory: unset ({how}); fallback {MENUBAR_FALLBACK_DIR}")
    if running is None:
        print("SwiftBar running: unknown (pgrep unavailable)")
    else:
        print(f"SwiftBar running: {'yes' if running else 'no'}")
    if links:
        for link, interval in links:
            print(f"plugin:           {link}  (every {interval})")
    else:
        print("plugin:           not installed (run: whosaid watch menubar install)")
    if source_ok:
        print(f"source readable:  yes (this shell reads {source})")
    else:
        print(f"source readable:  NO (this shell cannot read {source})")
        if voice_memos:
            print("                   SwiftBar needs its own Full Disk Access grant for REC detection;")
            print("                   the menu bar shows the live probe and links the pane when it fails.")
    if ws_env:
        print(f"workspace:        $WHOSAID_WORKSPACE={ws_env}")
    else:
        print("workspace:        WHOSAID_WORKSPACE not set; the plugin falls back to the single")
        print("                   installed watcher label, or shows a set-WHOSAID_WORKSPACE line")
    return 0


# ---- CLI --------------------------------------------------------------------------

def add_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default=None,
                   help="folder of recordings to read (default: [watch] source in whosaid.toml, "
                        "else the macOS Voice Memos store)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watch.py",
        description="whosaid watcher: new recordings -> ingest, hands-free (launchd on macOS), "
                    "Voice Memos list/pull/delete helpers, and the SwiftBar menu bar "
                    "plugin installer (issues #14, #24).",
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
    pi.add_argument("--no-fda", action="store_true",
                    help="watch a plain folder outside ~/Library instead of the TCC-protected "
                         "Voice Memos store (no Full Disk Access needed; non-admin friendly)")
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

    pmn = sub.add_parser(
        "menubar",
        help="SwiftBar menu bar plugin: install | uninstall | status (issue #24)")
    mnsub = pmn.add_subparsers(dest="menubar_command", required=True)

    mni = mnsub.add_parser(
        "install",
        help="symlink the menu bar plugin into SwiftBar's plugin directory")
    mni.add_argument("--workspace", default=None,
                     help="meeting workspace (validates it; printed in the launchctl setenv "
                          "WHOSAID_WORKSPACE hint the plugin depends on)")
    mni.add_argument("--interval", default=SWIFTBAR_INTERVAL_DEFAULT,
                     help=f"SwiftBar refresh suffix (default: {SWIFTBAR_INTERVAL_DEFAULT}; "
                          f"a non-default value names the symlink whosaid.<interval>.py)")
    mni.set_defaults(func=cmd_menubar_install)

    mnu = mnsub.add_parser("uninstall",
                           help="remove the plugin symlink(s) from the SwiftBar plugin directory")
    mnu.set_defaults(func=cmd_menubar_uninstall)

    mns = mnsub.add_parser(
        "status",
        help="is SwiftBar installed/running, the plugin linked, and the configured source readable?")
    mns.set_defaults(func=cmd_menubar_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
wsconfig.py: shared helpers for the meeting-workspace search stack (GitHub issue #14).

Used by lib/search.py, lib/graph.py, lib/action_items.py, lib/watch.py and
lib/mcp_server.py so they agree on where a workspace is, how it is configured,
where the search database lives, which folders count as meetings, and how a
speaker-labeled transcript line is parsed. Stdlib only, no network.

Workspace resolution (resolve_workspace):
  1. an explicit path argument, if given
  2. $WHOSAID_WORKSPACE
  3. the current directory, when it holds _workspace.json or whosaid.toml

Per-workspace config is an optional TOML file, <workspace>/whosaid.toml:

  [workspace]
  owner = "Alice_Example"        # whose action items this workspace tracks
  aliases = ["Ali", "Alicia"]    # how the transcript may misspell the owner (default: first name)
  tz = "America/Los_Angeles"     # rendering zone for dates (default: system)

  [groups]                       # optional, ordered; drives action-item sections
  leadership = ["Bob_Example", "Carol_Example"]
  team = ["Dan_Example", "Eve_Example"]

  [summarizer]
  engine = "auto"                # auto | ollama | hook | none
  model = "qwen2.5:14b"
  timeout = 900                  # seconds per model call

  [search]
  ollama = "http://127.0.0.1:11434"
  embed_model = "nomic-embed-text"
  embed = true                   # false: exact search only, no embeddings

  [watch]
  source = ""                    # folder to watch (default: macOS Voice Memos store)

Environment overrides: WHOSAID_WORKSPACE, WHOSAID_OWNER, WHOSAID_OLLAMA,
WHOSAID_SUMMARIZER_MODEL. Unknown keys are kept, so callers can add their own.
"""

from __future__ import annotations

import copy
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "whosaid.toml"
MANIFEST_NAME = "_workspace.json"
SEARCH_DB_NAME = "_search.db"     # search.py owns seg/emb/meta; graph.py owns the entity tables

DEFAULTS: dict = {
    "workspace": {"owner": "", "aliases": [], "tz": ""},
    "groups": {},
    "summarizer": {
        "engine": "auto", "model": "qwen2.5:14b", "num_ctx": 32768,
        "min_chars": 60, "split_chars": 600, "chunk_chars": 8000, "timeout": 900,
        "num_predict": 2048,
    },
    "search": {"ollama": "http://127.0.0.1:11434", "embed_model": "nomic-embed-text", "embed": True},
    "watch": {"source": "", "stable_seconds": 120, "max_wait_seconds": 900, "interval_seconds": 900},
}

# diarize_sherpa.py renders "[HH:MM:SS] Name: text"; "Name (MM:SS): text" is the
# looser hand-made spelling workspace.py also accepts. Timestamps may be M:SS,
# MM:SS, H:MM:SS or HHH:MM:SS.
TURN_BRACKET_RE = re.compile(r"^\[(\d{1,3}:\d{2}(?::\d{2})?)\]\s+([^\n:]+?):\s?(.*)$")
TURN_PAREN_RE = re.compile(r"^([A-Za-z][\w .'/-]*?)\s*\((\d{1,3}:\d{2}(?::\d{2})?)\):\s?(.*)$")


def log(msg: str) -> None:
    print(f"whosaid: {msg}", file=sys.stderr)


# ---- workspace + config -------------------------------------------------------------

def resolve_workspace(arg: str | os.PathLike | None = None) -> Path:
    """Explicit arg > $WHOSAID_WORKSPACE > cwd (if it looks like a workspace)."""
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("WHOSAID_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / MANIFEST_NAME).exists() or (cwd / CONFIG_NAME).exists():
        return cwd.resolve()
    raise SystemExit(
        "whosaid: no workspace given: pass the workspace directory, set WHOSAID_WORKSPACE, "
        f"or run from a directory that holds {MANIFEST_NAME} or {CONFIG_NAME}."
    )


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(ws: Path) -> dict:
    """DEFAULTS merged with <ws>/whosaid.toml (if any) and env overrides."""
    cfg = copy.deepcopy(DEFAULTS)
    path = Path(ws) / CONFIG_NAME
    if path.is_file():
        try:
            import tomllib  # Python 3.11+
        except ImportError:  # pragma: no cover
            log(f"WARN {CONFIG_NAME} needs Python 3.11+ (tomllib); using defaults")
        else:
            try:
                with open(path, "rb") as fh:
                    cfg = _merge(cfg, tomllib.load(fh))
            except Exception as e:  # noqa: BLE001
                log(f"WARN {CONFIG_NAME} unreadable ({e}); using defaults")
    if os.environ.get("WHOSAID_OWNER"):
        cfg["workspace"]["owner"] = os.environ["WHOSAID_OWNER"]
    if os.environ.get("WHOSAID_OLLAMA"):
        cfg["search"]["ollama"] = os.environ["WHOSAID_OLLAMA"]
    if os.environ.get("WHOSAID_SUMMARIZER_MODEL"):
        cfg["summarizer"]["model"] = os.environ["WHOSAID_SUMMARIZER_MODEL"]
    # groups: {name: [labels]} in file order; tolerate a comma string
    groups = {}
    for name, members in (cfg.get("groups") or {}).items():
        if isinstance(members, str):
            members = [m.strip() for m in members.split(",") if m.strip()]
        groups[str(name)] = [str(m) for m in members]
    cfg["groups"] = groups
    return cfg


def search_db(ws: Path) -> Path:
    return Path(ws) / SEARCH_DB_NAME


def ollama_url(cfg: dict) -> str:
    return str(cfg.get("search", {}).get("ollama") or DEFAULTS["search"]["ollama"]).rstrip("/")


def ollama_up(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


# ---- meetings + transcripts --------------------------------------------------------

def iter_meetings(ws: Path) -> list[tuple[str, list[Path]]]:
    """Every immediate subfolder (not starting with '_' or '.') that holds at least
    one *.speakers.txt, as (folder_name, [speakers files sorted]). Dated
    (YYYY-MM-DD-HHMM) and hand-named folders are both included: the search
    layers index whatever is there; the manifest/audit stays strict."""
    out = []
    ws = Path(ws)
    if not ws.is_dir():
        return out
    for entry in sorted(ws.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        files = sorted(p for p in entry.glob("*.speakers.txt") if p.is_file())
        if files:
            out.append((entry.name, files))
    return out


@dataclass
class Turn:
    t_sec: int
    t_str: str          # as written in the transcript, e.g. 00:16:32 or 16:32
    speaker: str
    text: str
    line: int           # 1-based line number of the turn's first line


def tsec(tok: str) -> int:
    parts = [int(x) for x in tok.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def hms(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def parse_turns(text: str) -> list[Turn]:
    """Speaker-labeled transcript -> turns. Lines that start neither form are
    continuation text and are appended to the previous turn; '#' header lines
    and blank lines are skipped."""
    turns: list[Turn] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if m := TURN_BRACKET_RE.match(line):
            t, spk, body = m.groups()
        elif m := TURN_PAREN_RE.match(line):
            spk, t, body = m.groups()
        else:
            if turns:
                turns[-1].text = (turns[-1].text + " " + line.strip()).strip()
            continue
        turns.append(Turn(t_sec=tsec(t), t_str=t, speaker=spk.strip(), text=body.strip(), line=n))
    return turns


def speaker_first_name(label: str) -> str:
    """'Alice_Example' -> 'Alice'; 'SPEAKER_03' stays as is."""
    if label.startswith("SPEAKER_"):
        return label
    return label.split("_")[0].split(" ")[0]

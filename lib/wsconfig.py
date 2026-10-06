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
  engine = "auto"                # auto | ollama | claude | hook | none
                                 # claude is opt-in and sends the transcript to Anthropic
  model = "qwen2.5:14b"
  timeout = 900                  # seconds per model call

  [search]
  ollama = "http://127.0.0.1:11434"
  embed_model = "nomic-embed-text"
  embed = true                   # false: exact search only, no embeddings

  [watch]
  source = ""                    # folder to watch (default: macOS Voice Memos store)

  [diarize]                      # speaker hints for every ingest into this workspace
  max_speakers = 8               # also speakers / min_speakers / expected_speakers;
                                 # skipped when the command passes its own speaker flags

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
        "num_predict": 2048, "think": False,
        "claude_model": "opus", "claude_timeout": 900, "claude_bin": "", "fallback": "ollama",
    },
    "search": {"ollama": "http://127.0.0.1:11434", "embed_model": "nomic-embed-text", "embed": True},
    "watch": {"source": "", "stable_seconds": 120, "max_wait_seconds": 900, "interval_seconds": 900},
    "diarize": {},
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
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]  # backport for < 3.11
            except ImportError:
                tomllib = None
        if tomllib is None:
            log(f"WARN {CONFIG_NAME} IGNORED (needs Python 3.11+ or tomli): "
                "owner/aliases/groups/summarizer fall back to defaults")
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


def diarize_hints(wcfg: dict, section: str = "watch") -> tuple[list[str], str | None]:
    """Extra transcribe/ingest args for a section's speaker hints (`speakers`,
    `min_speakers`, `max_speakers`, `expected_speakers`), or an error message
    naming the bad key. `section` is "watch" (the watcher's own hints) or
    "diarize" (every ingest into the workspace, and MCP transcribe into it).
    Nobody gets asked how many people were in the room, so blind auto-detect
    is the default; these let a workspace pin what it already knows (README
    'Hands-free ingest').

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
            return None, f"[{section}] {key} must be a whole number >= 1 (got {value!r})"
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
        return [], (f"[{section}] min_speakers ({min_speakers}) must be <= "
                     f"max_speakers ({max_speakers})")

    raw_expected = wcfg.get("expected_speakers")
    names: list[str] = []
    if raw_expected:
        if isinstance(raw_expected, str):
            candidates = raw_expected.split(",")
        elif isinstance(raw_expected, list):
            candidates = raw_expected
        else:
            return [], (f"[{section}] expected_speakers must be a list of names or a "
                         f"comma-separated string (got {raw_expected!r})")
        for candidate in candidates:
            name = str(candidate).strip()
            if not name:
                return [], f"[{section}] expected_speakers has an empty name (got {raw_expected!r})"
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


SPEAKER_FLAGS = ("--speakers", "--min-speakers", "--max-speakers", "--expected-speakers")


def find_workspace(path: Path) -> Path | None:
    """The nearest folder at or above `path` holding whosaid.toml or _workspace.json."""
    p = Path(path).expanduser().resolve()
    for cand in (p, *p.parents):
        if (cand / CONFIG_NAME).is_file() or (cand / MANIFEST_NAME).is_file():
            return cand
    return None


def workspace_speaker_args(ws: Path) -> tuple[list[str], str | None]:
    """The workspace's `[diarize]` speaker hints as transcribe args (see diarize_hints)."""
    return diarize_hints(load_config(ws).get("diarize") or {}, "diarize")


def _cli(argv: list[str]) -> int:
    """`wsconfig.py speaker-args <ws>`: one arg per line, or exit 1 with the error."""
    if len(argv) == 2 and argv[0] == "speaker-args":
        args, err = workspace_speaker_args(Path(argv[1]))
        if err:
            log(err)
            return 1
        print("\n".join(args))
        return 0
    log("usage: wsconfig.py speaker-args <workspace>")
    return 2


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


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))

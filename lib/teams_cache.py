#!/usr/bin/env python3
"""
teams_cache.py: read Microsoft Teams chats live from the desktop app's own local cache.

The new Teams desktop app (and Teams in Edge/WebView2) keeps recent chats in a Chromium
IndexedDB store on disk. Reading a copy of that store gives messages seconds after they
arrive, with no network call (nothing to throttle or rate-limit), no Graph permission and
no browser automation. It only holds what the app has synced: recent chats and the ones
you opened, not full history, so `whosaid teams ingest` of a scraper export stays the
backfill path.

  python3 lib/teams_cache.py live  [--chat NAME] [--since 2h|3d|ISO] [--limit N] [--ws WS] [--json]
  python3 lib/teams_cache.py chats [--since 7d] [--ws WS] [--json]
  python3 lib/teams_cache.py sync  WS [--since 14d] [--dry-run] [--json]
  python3 lib/teams_cache.py status [--json]

live reads the newest messages (all chats, or one chat by name, case-insensitive
substring). chats lists conversations with their last message. sync writes the new
messages into a workspace through the same ingest as `whosaid teams ingest` (one folder
per chat-day, source.kind teams-chat, scraper teams-local-cache@1), passing only message
ids the workspace does not have yet, so folders an earlier scrape wrote are left alone
unless something is new. status says where the cache is and how fresh it is.

Cache location: WHOSAID_TEAMS_CACHE (the IndexedDB directory, or one
https_teams.microsoft.com_0.indexeddb.leveldb inside it), else the macOS new-Teams path
~/Library/Containers/com.microsoft.teams2/.../EBWebView/WV2Profile_tfw/IndexedDB.
The store is copied to a temp dir before reading (Teams holds a lock on it and keeps
writing); the decoded result is kept in ~/.cache/whosaid/teams-live.json keyed on the
store's file sizes and mtimes, so repeat reads while nothing changed are instant.

Chat names follow the Teams left rail: the topic when the chat has one, the other
person for a 1:1, otherwise the members' first names. With a workspace, a chat whose
messages are already ingested keeps the name those folders use (matched by message id),
and [teams.chat_names] "<chat id>" = "Name" in whosaid.toml overrides both.

Decoding uses the vendored MIT ccl_chromium_reader (lib/vendor/README.md). Stdlib only.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

_LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(_LIB))

ORIGIN = "https_teams.microsoft.com_0.indexeddb"
DEFAULT_CACHE = (Path.home() / "Library/Containers/com.microsoft.teams2/Data/Library/Application Support"
                 / "Microsoft/MSTeams/EBWebView/WV2Profile_tfw/IndexedDB")
SCRAPER = "teams-local-cache@1"
CACHE_VERSION = 1
TEXT_TYPES = frozenset({"RichText/Html", "Text", "RichText", "RichText/Media_Card"})
CHAT_KINDS = frozenset({"Chat", "Meeting"})
CHANNEL_KINDS = frozenset({"Space", "Thread", "Topic"})
SKIP_IDS = ("48:notes", "48:notifications", "48:calllogs", "48:mentions", "48:drafts")
MENTION = "http://schema.skype.com/Mention"
REPLY = "http://schema.skype.com/Reply"


class CacheError(Exception):
    """The Teams cache is missing, unreadable or in a shape this reader does not know."""


# ---- where and how fresh -------------------------------------------------------------

def cache_dir(path: str | os.PathLike | None = None) -> Path:
    """The IndexedDB directory holding <ORIGIN>.leveldb."""
    p = Path(os.path.expanduser(str(path or os.environ.get("WHOSAID_TEAMS_CACHE") or DEFAULT_CACHE)))
    if p.name.endswith(".indexeddb.leveldb"):
        p = p.parent
    return p


def _leveldb(d: Path) -> Path:
    return d / f"{ORIGIN}.leveldb"


def fingerprint(d: Path) -> str:
    """Changes whenever Teams writes: names, sizes and mtimes of the store's files."""
    h = hashlib.sha256(str(CACHE_VERSION).encode())
    for sub in (_leveldb(d), d / f"{ORIGIN}.blob"):
        if not sub.is_dir():
            continue
        for f in sorted(sub.rglob("*")):
            if f.is_file() and f.name != "LOCK":
                st = f.stat()
                h.update(f"{f.relative_to(d)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def status(path=None) -> dict:
    d = cache_dir(path)
    ldb = _leveldb(d)
    if not ldb.is_dir():
        return {"ok": False, "cache": str(d), "error": f"no Teams cache at {ldb}",
                "hint": "install and sign in to the new Teams desktop app, or set WHOSAID_TEAMS_CACHE"}
    files = [f for f in ldb.iterdir() if f.is_file()]
    newest = max((f.stat().st_mtime for f in files), default=0)
    return {"ok": True, "cache": str(d), "files": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "written_at": datetime.fromtimestamp(newest, timezone.utc).isoformat()[:19] + "Z",
            "written_seconds_ago": int(time.time() - newest)}


# ---- reading the IndexedDB store -------------------------------------------------------

def _plain(v):
    """Decoded IndexedDB values to JSON-safe plain data (undefined -> None, bytes -> str)."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (bytes, bytearray)):
        try:  # V8 writes one-byte strings as Latin-1, two-byte ones as UTF-16
            return bytes(v).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(v).decode("latin-1")
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if type(v).__name__ in ("_Undefined", "Undefined"):
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _snapshot(d: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="whosaid-teams-"))
    for sub in (f"{ORIGIN}.leveldb", f"{ORIGIN}.blob"):
        src = d / sub
        if src.is_dir():
            shutil.copytree(src, tmp / sub, ignore=shutil.ignore_patterns("LOCK"))
    return tmp


def read_store(path=None) -> dict:
    """Raw conversations, reply chains and profiles from a copy of the store."""
    d = cache_dir(path)
    if not _leveldb(d).is_dir():
        raise CacheError(f"no Teams cache at {_leveldb(d)} (set WHOSAID_TEAMS_CACHE)")
    sys.path.insert(0, str(_LIB / "vendor"))
    from ccl_chromium_reader import ccl_chromium_indexeddb as idb  # vendored, MIT
    last: Exception | None = None
    for attempt in range(2):  # Teams may be mid-write when we copy; a second copy settles it
        snap = _snapshot(d)
        try:
            w = idb.WrappedIndexDB(str(snap / f"{ORIGIN}.leveldb"), str(snap / f"{ORIGIN}.blob"))
            dbs: dict[str, int] = {}
            for x in w.database_ids:
                parts = (x.name or "").split(":")
                if len(parts) > 1 and parts[0] == "Teams":
                    dbs.setdefault(parts[1], x.dbid_no)

            def rows(db: str, store: str) -> list:
                if db not in dbs:
                    return []
                h = w[dbs[db]]
                if store not in h.object_store_names:
                    return []
                return [_plain(r.value) for r in h[store].iterate_records() if r.value is not None]

            if "replychain-manager" not in dbs or "conversation-manager" not in dbs:
                raise CacheError("the Teams cache has no replychain-manager / conversation-manager database; "
                                 "this Teams version stores chats differently")
            return {"conversations": rows("conversation-manager", "conversations"),
                    "replychains": rows("replychain-manager", "replychains")
                    + rows("replychain-manager", "replychains-2"),
                    "profiles": rows("profiles", "profiles")}
        except CacheError:
            raise
        except Exception as e:  # noqa: BLE001 - a torn copy surfaces as any decode error
            last = e
            time.sleep(0.5)
        finally:
            shutil.rmtree(snap, ignore_errors=True)
    raise CacheError(f"could not decode the Teams cache: {last}")


# ---- message HTML to text ------------------------------------------------------------

class _Text(HTMLParser):
    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, str]] = []  # (tag, role) for mention / reply spans
        self.href: list[str | None] = []
        self.link_text: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        role = a.get("itemtype") or ""
        if tag == "br":
            self.out.append("\n")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            self.out.append(f"[image: {alt}]" if alt and alt.lower() != "image" else "[image]")
        if tag == "blockquote" and role == REPLY:
            self.out.append("\n> ")
        if role == MENTION:
            self.out.append("@")
        if tag == "a":
            self.href.append(a.get("href"))
            self.link_text.append([])
        self.stack.append((tag, role))

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            href, text = self.href.pop(), "".join(self.link_text.pop()).strip()
            if href and text and href.rstrip("/") != text.rstrip("/") and not href.startswith("mailto:"):
                self.out.append(f" ({href})")
        if tag in self.BLOCK or tag == "blockquote":
            self.out.append("\n")
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break

    def handle_data(self, data):
        self.out.append(data)
        for t in self.link_text:
            t.append(data)


def html_to_text(content: str, message_type: str = "RichText/Html") -> str:
    if not content:
        return ""
    if message_type == "Text" and "<" not in content:
        return html.unescape(content).strip()
    p = _Text()
    try:
        p.feed(content)
        p.close()
        text = "".join(p.out)
    except Exception:  # noqa: BLE001 - malformed markup: keep the visible characters
        text = re.sub(r"<[^>]+>", " ", html.unescape(content))
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# ---- decoding records into chats and messages -----------------------------------------

def _first(*vals):
    for v in vals:
        if v not in (None, ""):
            return v
    return None


def _oid(mri: str) -> str:
    return mri.rsplit(":", 1)[-1] if mri else ""


def _first_name(name: str) -> str:
    return (name or "").split()[0] if (name or "").split() else name


def decode(raw: dict, channels: bool = False) -> dict:
    """{self, chats, messages} from read_store() output. Pure: tests feed it plain dicts.

    messages: {chat_id, author, author_mri, self, epoch_ms, message_id, text, edited}
    chats:    {id: {id, kind, topic, members: [names], name (left-rail guess), last_ms}}
    """
    kinds = CHAT_KINDS | (CHANNEL_KINDS if channels else frozenset())
    convs = {c.get("id"): c for c in raw.get("conversations") or [] if isinstance(c, dict) and c.get("id")}
    names: dict[str, str] = {}
    for p in raw.get("profiles") or []:
        if isinstance(p, dict) and p.get("mri") and p.get("displayName"):
            names[p["mri"]] = p["displayName"]
    msgs: dict[tuple[str, str], dict] = {}
    for chain in raw.get("replychains") or []:
        for m in ((chain or {}).get("messageMap") or {}).values():
            if not isinstance(m, dict):
                continue
            creator = m.get("creator") or ""
            dn = _first(m.get("imDisplayName"), m.get("fromDisplayNameInToken"))
            if creator and dn:
                names.setdefault(creator, dn)
            cid = m.get("conversationId") or chain.get("conversationId")
            mid = str(m.get("id") or "")
            if not cid or not mid or mid.startswith("-"):  # -1: a send still pending; the server copy follows
                continue
            key = (cid, mid)
            prev = msgs.get(key)
            if prev is None or str(m.get("version") or "") >= str(prev.get("version") or ""):
                msgs[key] = m
    # self: the author of notes-to-self, else whoever writes in the most conversations
    notes = [m.get("creator") for (cid, _), m in msgs.items() if cid == "48:notes" and m.get("creator")]
    presence: dict[str, set] = {}
    for (cid, _), m in msgs.items():
        kind = (convs.get(cid) or {}).get("type")
        if m.get("creator") and not cid.startswith("48:") and kind not in CHANNEL_KINDS:
            presence.setdefault(m["creator"], set()).add(cid)
    for cid in convs:  # a 1:1 id names both people: 19:<oid>_<oid>@unq.gbl.spaces
        if cid.endswith("@unq.gbl.spaces"):
            for o in cid.split(":", 1)[-1].split("@", 1)[0].split("_"):
                for k in presence:
                    if _oid(k) == o:
                        presence[k].add("pair:" + cid)
    self_mri = notes[0] if notes else (max(presence, key=lambda k: len(presence[k])) if presence else "")
    me = {"mri": self_mri, "name": names.get(self_mri, "")}

    chats: dict[str, dict] = {}
    out = []
    for (cid, mid), m in msgs.items():
        if cid.startswith(SKIP_IDS):
            continue
        conv = convs.get(cid) or {}
        kind = conv.get("type") or ("Chat" if cid.endswith("@unq.gbl.spaces") else "")
        if kind and kind not in kinds:
            continue
        if (m.get("messageType") or "") not in TEXT_TYPES:
            continue
        props = m.get("properties") or {}
        if m.get("deletionInfo") or props.get("deletetime") or props.get("deletedTime"):
            continue
        text = html_to_text(m.get("content") or "", m.get("messageType") or "")
        if not text:
            continue
        try:
            ms = int(float(_first(m.get("originalArrivalTime"), m.get("clientArrivalTime"), mid) or 0))
        except (TypeError, ValueError):
            continue
        creator = m.get("creator") or ""
        author = names.get(creator) or _first(m.get("imDisplayName"), m.get("fromDisplayNameInToken")) or "Unknown"
        out.append({"chat_id": cid, "author": author, "author_mri": creator, "self": bool(creator) and creator == self_mri,
                    "epoch_ms": ms, "message_id": mid, "text": text,
                    "edited": bool(props.get("edittime") or props.get("editedTime"))})
        c = chats.setdefault(cid, {"id": cid, "kind": kind or "Chat",
                                   "topic": ((conv.get("threadProperties") or {}).get("topic") or "").strip(),
                                   "members": set(), "last_ms": 0})
        if creator and creator != self_mri:
            c["members"].add(creator)
        c["last_ms"] = max(c["last_ms"], ms)
    for cid, c in chats.items():
        if cid.endswith("@unq.gbl.spaces") and ":" in cid:  # 1:1: 19:<oid>_<oid>@unq.gbl.spaces
            pair = cid.split(":", 1)[1].split("@", 1)[0].split("_")
            other = [o for o in pair if o and o != _oid(self_mri)]
            for o in other:
                c["members"].add(next((k for k in names if _oid(k) == o), f"8:orgid:{o}"))
        member_names = sorted(names.get(k) or k for k in c["members"])
        c["members"] = member_names
        c["name"] = rail_name(c)
    out.sort(key=lambda x: (x["epoch_ms"], x["message_id"]))
    return {"self": me, "chats": chats, "messages": out}


def rail_name(chat: dict) -> str:
    """The left-rail label: topic, else the other person (1:1), else members' first names."""
    if chat.get("topic"):
        return chat["topic"]
    members = [m for m in chat.get("members") or [] if not m.startswith("8:")]
    if len(members) == 1:
        return members[0]
    if members:
        return ", ".join(_first_name(m) for m in members)
    return chat.get("id", "")


# ---- cached load ---------------------------------------------------------------------

def _cache_file() -> Path:
    base = os.environ.get("WHOSAID_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "whosaid")
    return Path(base) / "teams-live.json"


def load(path=None, channels: bool = False, use_cache: bool = True) -> dict:
    """decode(read_store()) with a cache keyed on the store's fingerprint."""
    d = cache_dir(path)
    if not _leveldb(d).is_dir():
        raise CacheError(f"no Teams cache at {_leveldb(d)} (set WHOSAID_TEAMS_CACHE)")
    fp = fingerprint(d) + (":channels" if channels else "")
    cf = _cache_file()
    if use_cache:
        try:
            got = json.loads(cf.read_text())
            if got.get("fingerprint") == fp:
                got["data"]["cached"] = True
                return got["data"]
        except (OSError, ValueError, KeyError):
            pass
    t0 = time.time()
    data = decode(read_store(d), channels=channels)
    data["chats"] = {k: v for k, v in data["chats"].items()}
    data["read_seconds"] = round(time.time() - t0, 1)
    data["read_at"] = datetime.now(timezone.utc).isoformat()[:19] + "Z"
    data["cache"] = str(d)
    data["cached"] = False
    if use_cache:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            tmp = cf.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fingerprint": fp, "data": data}, ensure_ascii=False))
            os.chmod(tmp, 0o600)
            tmp.replace(cf)
        except OSError:
            pass
    return data


# ---- naming chats against a workspace --------------------------------------------------

def workspace_names(ws: Path | None) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """(message id -> chat name, chat id -> chat name, [teams.chat_names] overrides)."""
    by_mid: dict[str, str] = {}
    by_cid: dict[str, str] = {}
    overrides: dict[str, str] = {}
    if not ws or not Path(ws).is_dir():
        return by_mid, by_cid, overrides
    try:
        import wsconfig
        t = (wsconfig.load_config(Path(ws)).get("teams") or {})
        overrides = {str(k): str(v) for k, v in (t.get("chat_names") or {}).items()}
    except Exception:  # noqa: BLE001 - a broken toml must not stop a read
        pass
    for sc in Path(ws).glob("*/teams.diarization.json"):
        try:
            data = json.loads(sc.read_text())
        except (OSError, ValueError):
            continue
        src = data.get("source") or {}
        if src.get("kind") != "teams-chat" or not src.get("chat_name"):
            continue
        if src.get("chat_id"):
            by_cid.setdefault(src["chat_id"], src["chat_name"])
        for seg in data.get("segments") or []:
            mid = str(seg.get("mid") or seg.get("message_id") or "")
            if mid:
                by_mid[mid] = src["chat_name"]
    return by_mid, by_cid, overrides


def name_chats(data: dict, ws: Path | None) -> dict[str, str]:
    """chat id -> the name to use: override, then the name the workspace already uses, then the rail guess."""
    by_mid, by_cid, overrides = workspace_names(ws)
    votes: dict[str, dict[str, int]] = {}
    for m in data["messages"]:
        n = by_mid.get(m["message_id"])
        if n:
            v = votes.setdefault(m["chat_id"], {})
            v[n] = v.get(n, 0) + 1
    out: dict[str, str] = {}
    pinned = set()  # names set by an override or a folder that records this chat id
    for cid, c in data["chats"].items():
        if cid in overrides:
            out[cid] = overrides[cid]
            pinned.add(cid)
        elif cid in by_cid:
            out[cid] = by_cid[cid]
            pinned.add(cid)
        elif votes.get(cid):
            out[cid] = max(votes[cid].items(), key=lambda kv: kv[1])[0]
        else:
            out[cid] = c.get("name") or cid
    # Two chats with one name (two people called the same, a meeting chat titled like a 1:1) would
    # land in each other's name-only folders. The chat that owns the name keeps it: pinned, else the
    # one whose messages the workspace already files under it, else the most recent. The others get
    # a short, stable suffix ("Name (chat 1a2b3c)").
    by_name: dict[str, list[str]] = {}
    for cid, n in out.items():
        by_name.setdefault(n, []).append(cid)
    for n, cids in by_name.items():
        if len(cids) < 2:
            continue
        owner = max(cids, key=lambda k: (k in pinned, votes.get(k, {}).get(n, 0),
                                         data["chats"][k].get("last_ms", 0)))
        for cid in cids:
            if cid != owner and cid not in pinned:
                kind = (data["chats"][cid].get("kind") or "chat").lower()
                out[cid] = f"{n} ({kind} {hashlib.sha1(cid.encode()).hexdigest()[:6]})"
    return out


# ---- queries ---------------------------------------------------------------------------

def parse_since(s: str | None, default: str = "2h") -> int:
    """'90m', '2h', '3d', '1w' or an ISO time -> epoch ms."""
    s = (s or default).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw])", s)
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return int((time.time() - float(m.group(1)) * mult) * 1000)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise CacheError(f"--since {s!r}: use 90m, 2h, 3d, 1w or an ISO time") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()[:19] + "Z"


def _meta(data: dict) -> dict:
    return {k: data.get(k) for k in ("cache", "read_at", "read_seconds", "cached")}


def messages(chat: str | None = None, since_ms: int | None = None, limit: int | None = 50,
             ws: Path | None = None, exact: bool = False, path=None, channels: bool = False) -> dict:
    """Newest messages, oldest first, optionally of chats whose name matches `chat`."""
    data = load(path, channels=channels)
    cname = name_chats(data, ws)
    want = (chat or "").strip().lower()
    rows = []
    for m in data["messages"]:
        if since_ms is not None and m["epoch_ms"] < since_ms:
            continue
        n = cname.get(m["chat_id"], m["chat_id"])
        if want and not ((n.lower() == want) if exact else (want in n.lower() or want == m["chat_id"].lower())):
            continue
        rows.append({**m, "chat": n, "at": _iso(m["epoch_ms"])})
    total = len(rows)
    if limit:
        rows = rows[-int(limit):]
    newest = max((m["epoch_ms"] for m in data["messages"]), default=0)
    return {"ok": True, **_meta(data), "self": data["self"].get("name"),
            "newest_message_at": _iso(newest) if newest else None,
            "chats": sorted({r["chat"] for r in rows}), "total": total, "count": len(rows), "messages": rows}


def chats(since_ms: int | None = None, ws: Path | None = None, path=None, channels: bool = False) -> dict:
    data = load(path, channels=channels)
    cname = name_chats(data, ws)
    count: dict[str, int] = {}
    last: dict[str, dict] = {}
    for m in data["messages"]:
        if since_ms is not None and m["epoch_ms"] < since_ms:
            continue
        count[m["chat_id"]] = count.get(m["chat_id"], 0) + 1
        last[m["chat_id"]] = m
    rows = []
    for cid, n in count.items():
        c, lm = data["chats"].get(cid, {}), last[cid]
        rows.append({"chat": cname.get(cid, cid), "chat_id": cid, "kind": c.get("kind"), "messages": n,
                     "members": c.get("members", []), "last_at": _iso(lm["epoch_ms"]),
                     "last_from": "you" if lm["self"] else lm["author"], "last_text": lm["text"][:120],
                     "_ms": lm["epoch_ms"]})
    rows.sort(key=lambda r: r.pop("_ms"), reverse=True)
    return {"ok": True, **_meta(data), "self": data["self"].get("name"), "count": len(rows), "chats": rows}


def sync(ws: str | os.PathLike, since_ms: int | None = None, dry_run: bool = False, path=None,
         channels: bool = False, tz: str | None = None) -> dict:
    """Ingest cache messages the workspace does not have yet (by message id) into chat-day folders."""
    import teams_chat
    ws = Path(ws).expanduser()
    if not ws.is_dir():
        raise CacheError(f"workspace {ws} is not a directory")
    data = load(path, channels=channels)
    cname = name_chats(data, ws)
    have, _, _ = workspace_names(ws)
    new = [m for m in data["messages"] if (since_ms is None or m["epoch_ms"] >= since_ms)
           and m["message_id"] not in have]
    res = {"ok": True, **_meta(data), "workspace": str(ws), "new_messages": len(new), "folders": [],
           "dry_run": dry_run}
    if not new:
        return res
    records = [{"chat": cname.get(m["chat_id"], m["chat_id"]), "chat_id": m["chat_id"], "author": m["author"],
                "epoch_ms": m["epoch_ms"], "message_id": m["message_id"], "text": m["text"]} for m in new]
    src = dataclasses.replace(teams_chat.TEAMS, scraper=SCRAPER)
    fd, tmp = tempfile.mkstemp(prefix="whosaid-teams-sync-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"messages": records}, f, ensure_ascii=False)
        res["folders"] = teams_chat.ingest(tmp, ws, tz_flag=tz, self_name=data["self"].get("name") or None,
                                           dry_run=dry_run, src=src)
    finally:
        os.unlink(tmp)
    return res


# ---- CLI -------------------------------------------------------------------------------

def _print_messages(res: dict) -> None:
    age = "cached" if res.get("cached") else f"read in {res.get('read_seconds')}s"
    print(f"# Teams cache: newest message {res.get('newest_message_at')}, {age}; "
          f"{res['count']} of {res['total']} message(s)")
    by_chat = len(res["chats"]) > 1
    for m in res["messages"]:
        local = datetime.fromtimestamp(m["epoch_ms"] / 1000).strftime("%m-%d %H:%M")
        who = "you" if m["self"] else m["author"]
        head = f"{local}  [{m['chat']}] {who}" if by_chat else f"{local}  {who}"
        text = m["text"].replace("\n", "\n" + " " * 13)
        print(f"{head}: {text}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="whosaid teams", description=__doc__.split("\n\n")[1].strip())
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("live", help="newest messages from the local Teams cache")
    pl.add_argument("--chat", help="chat name (case-insensitive substring) or chat id")
    pl.add_argument("--since", default=None, help="90m, 2h, 3d, 1w or ISO (default 2h; 14d with --chat)")
    pl.add_argument("--limit", type=int, default=50)
    pc = sub.add_parser("chats", help="conversations in the cache with their last message")
    pc.add_argument("--since", default="7d")
    ps = sub.add_parser("sync", help="ingest new cache messages into a workspace")
    ps.add_argument("ws", nargs="?", default=os.environ.get("WHOSAID_WORKSPACE"))
    ps.add_argument("--since", default="14d")
    ps.add_argument("--dry-run", action="store_true")
    ps.add_argument("--tz", default=None)
    sub.add_parser("status", help="where the cache is and when Teams last wrote it")
    for p in sub.choices.values():
        if not any(a.dest == "json" for a in p._actions):
            p.add_argument("--json", action="store_true")
        if p in (pl, pc, ps):
            p.add_argument("--cache", default=None, help="IndexedDB dir (default: WHOSAID_TEAMS_CACHE or the Teams app's)")
            p.add_argument("--channels", action="store_true", help="include team channel posts")
        if p in (pl, pc) and not any(a.dest == "ws" for a in p._actions):
            p.add_argument("--ws", default=os.environ.get("WHOSAID_WORKSPACE"),
                           help="workspace whose ingested chat names to reuse")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            res = status()
            if a.json:
                print(json.dumps(res, indent=2))
            elif res["ok"]:
                print(f"{res['cache']}\n  {res['files']} files, {res['bytes'] // 1024} KB, "
                      f"last written {res['written_at']} ({res['written_seconds_ago']}s ago)")
            else:
                print(f"{res['error']}\n  {res['hint']}", file=sys.stderr)
            return 0 if res["ok"] else 1
        ws = Path(a.ws).expanduser() if getattr(a, "ws", None) else None
        if a.cmd == "live":
            since = parse_since(a.since, "14d" if a.chat else "2h")
            res = messages(a.chat, since, a.limit, ws, path=a.cache, channels=a.channels)
            if a.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            elif a.chat and not res["messages"]:
                print(f"no messages in a chat matching {a.chat!r} since {_iso(since)} "
                      "(see `whosaid teams chats`)", file=sys.stderr)
                return 1
            else:
                _print_messages(res)
            return 0
        if a.cmd == "chats":
            res = chats(parse_since(a.since, "7d"), ws, path=a.cache, channels=a.channels)
            if a.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                for r in res["chats"]:
                    print(f"{r['last_at'][:16]}  {r['chat']}  ({r['messages']} msg, last: {r['last_from']})")
            return 0
        if a.cmd == "sync":
            if not ws:
                print("whosaid teams sync: need a workspace (WS or WHOSAID_WORKSPACE)", file=sys.stderr)
                return 2
            res = sync(ws, parse_since(a.since, "14d"), a.dry_run, path=a.cache, channels=a.channels, tz=a.tz)
            if a.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                verb = "would write" if a.dry_run else "wrote"
                print(f"{res['new_messages']} new message(s); {verb} {len(res['folders'])} chat-day folder(s)")
                for r in res["folders"]:
                    print(f"  {r['folder']}  {r['chat']}  {r['total']} messages ({r['new']} new)")
            return 0
    except CacheError as e:
        print(f"whosaid teams {a.cmd}: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

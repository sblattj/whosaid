#!/usr/bin/env python3
"""
backup.py: remote backup and team share of a meeting workspace (GitHub issue #180).

A workspace is one local folder. This module copies it somewhere else, split by
sensitivity into tiers:

  text       transcripts, *.speakers.txt, sidecars, roll-ups (_ACTION-ITEMS,
             _COMMITMENTS, _WIKI, ...), chat-day folders, whosaid.toml    default
  index      _search.db (rebuildable: `whosaid index` on the receiving side) opt-in
  audio      recordings (.m4a, .wav, ...) and <base>.samples/ clips          opt-in
  state      working state outside the text tier: <ws>/.watch_state.json, the drafts
             outbox (~/.drafts) and the launchd watcher's args/env      opt-in
  biometric  the speaker registry (speakers.json, voiceprints), which lives
             outside the workspace; personal encrypted backup only, never shared
             Without biometric, a personal backup strips voiceprint keys (cluster_emb,
             centroid, ...) from every JSON file, exactly like a share (#234).

Commands (the `whosaid` bash CLI execs this module):

  backup <ws> --to TARGET [--tier text,index,audio,state,biometric] [--dry-run]
         [--recipient AGE_PUBKEY ... | --recipients-file F | --plaintext]
         [--prune-older-than DUR] [--keep N] [--json]
      A full personal copy. Encrypted at rest by default: every file is
      encrypted client-side with `age` before it leaves the machine and stored
      as objects/<HMAC of its sha256>.age, zero-padded to a size bucket first; the
      manifest (file names, hashes, tiers, the names key) is encrypted and
      padded too, and no plaintext recipients file is written. --plaintext
      writes a browsable copy instead (refused for the biometric tier). Incremental: only files whose content hash the
      target lacks are uploaded. Additive: a file deleted locally stays in the
      backup until --prune-older-than drops its meeting. Encrypted runs also
      write manifests/<UTC stamp>.age and keep the newest N (default 30); a run
      never deletes objects, `backup gc` does.

  backup init <ws> --to TARGET [--tier ...] [--interval daily] [--identity F]
         [--recovery-recipient AGE_PUBKEY] [--dry-run]
      age check, daily identity (0600), a recovery identity printed once,
      [backup] with both recipients, dry run, first run, read-back, schedule.

  backup verify <ws> [--to T] [--identity F] [--sample N] [--full] [--query Q] [--json]
      Read objects back and check hashes, report missing objects and counts
      per tier; --full restores into a temp dir and runs a search.

  backup status <ws> [--to T] [--json]   last backup/failure/verify, sizes, alarms
  backup gc <ws> [--to T] --identity F [--keep N] [--dry-run] [--json]
      Delete objects no kept manifest references (all kept manifests must decrypt).

  backup restore TARGET <ws> [--identity F] [--tier ...] [--at STAMP] [--registry-out F]
         [--force] [--dry-run] [--json]
      Pull a backup (or a share) into <ws>, verifying every file's sha256.
      An existing file with different content is a conflict: nothing is
      overwritten without --force. Private files land 0600, restored recordings
      are recorded by content hash in .watch_state.json, and the watcher
      snapshot is re-rooted into <ws>/.backup/restored-watcher.json.

  share <ws> --to TARGET [--meetings A,B] [--since YYYY-MM-DD] [--exclude-chat]
        [--tier text,index,audio] [--yes] [--dry-run] [--prune-older-than DUR] [--json]
      A read-only export for teammates, laid out as a workspace they can point
      WHOSAID_WORKSPACE at (after `whosaid index`). Text tier only by default.
      Voiceprints never leave: the biometric tier is refused, every shared JSON
      file has its voiceprint keys (cluster_emb, embedding, ...) stripped, and
      _voice-groups.json / _reprocess-backups/ are never sent. Skips meetings
      with a `_noshare` or `_retired` marker file, folders under _retired/, and
      folders named in [share] never. Workspace roll-ups are only sent when
      every meeting is shared (a filtered roll-up would leak the rest). The
      share mirrors the selection: files no longer selected are deleted from
      the target. _whosaid-share.json records which meetings were shared.
      The first run to a target prints what would leave and needs --yes.

  share pull TARGET <dir>     alias for `backup restore TARGET <dir>` on a share.

Targets (pluggable adapters):
  /path or file:///path       local disk, NAS mount
  s3://bucket/prefix          S3-compatible. Credentials from AWS_ACCESS_KEY_ID,
                              AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN); region
                              from AWS_REGION / AWS_DEFAULT_REGION (us-east-1);
                              endpoint from WHOSAID_S3_ENDPOINT or AWS_ENDPOINT_URL
                              (path-style, for MinIO and friends). Stdlib SigV4.
  rclone:REMOTE:path          any rclone remote (OneDrive, SharePoint, Drive,
                              Dropbox, ...), via the `rclone` binary.

Workspace config (whosaid.toml):
  [backup]
  to = "s3://bucket/whosaid"          # target for the watcher's scheduled pass
  interval = "daily"                  # hourly | daily | weekly | "<N>h" | "<N>d"; unset = off
  tier = "text"                       # tiers for the scheduled pass
  recipients = ["age1..."]            # age public keys (or WHOSAID_BACKUP_AGE_RECIPIENT)
  identity = "~/.config/whosaid/backup.key"   # for verify/gc (or WHOSAID_BACKUP_AGE_IDENTITY)
  plaintext = false
  keep = 30                           # dated manifests kept
  verify = "weekly"                   # scheduled read-back check
  alarms = true                       # stale backup / stale verify / missing age
  [share]
  never = ["2026-09-16-0703"]         # meeting folders that never leave the machine

Stdlib only. Exit codes: 0 ok, 1 error, 2 usage, 3 needs --yes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wsconfig import load_config  # noqa: E402

SCHEMA = "whosaid.backup/1"
BACKUP_MANIFEST = "_whosaid-backup.json"
SHARE_MANIFEST = "_whosaid-share.json"
STATE_DIR = ".backup"
# legacy: older encrypted backups kept a plaintext sha256 of the recipient set here. The
# fingerprint now lives inside the encrypted manifest; a run deletes this file when it finds it.
RECIPIENTS_KEY = "_whosaid-backup.recipients"
# Encrypted object names are HMAC-SHA256(names key, plaintext sha256). The names key is random,
# lives inside the encrypted manifest (so every recipient can read it, and a future grant
# re-wraps it with the manifest) and in the workspace's local state; the storage host sees
# names it cannot link to content it may already hold.
NAMES_ALG = "hmac-sha256/1"
# Plaintext is zero-padded before encryption to a Padme bucket (at most ~12% overhead, a
# 4 KiB floor), so ciphertext sizes reveal little about the file. `size` in the manifest is
# the true length; restore truncates to it.
PAD_ALG = "padme/1"
PAD_FLOOR = 4096
NOTICE_ACK = Path.home() / ".config" / "whosaid" / "share-notice-acknowledged"

TIERS = ("text", "index", "audio", "state", "biometric")
SHARE_TIERS = ("text", "index", "audio")
AUDIO_EXT = {".m4a", ".wav", ".mp3", ".mp4", ".aac", ".flac", ".ogg", ".opus", ".webm",
             ".caf", ".aif", ".aiff", ".mov", ".m4v", ".mkv", ".wma", ".amr", ".3gp"}
INDEX_NAMES = {"_search.db", "_search.db-wal", "_search.db-shm", "_search.db-journal"}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "__pycache__", BACKUP_MANIFEST, BACKUP_MANIFEST + ".age",
              SHARE_MANIFEST}
MANIFESTS_DIR = "manifests"            # encrypted layout: manifests/<UTC stamp>-<hex>.age, newest N kept
DEFAULT_KEEP = 30
# state tier (#234): working state a wiped-Mac restore needs so nothing is re-ingested. Virtual
# prefixes like biometric/: state/workspace/<f> -> <ws>/<f>, state/drafts/<rel> -> the drafts
# outbox, state/watcher.json -> the launchd watcher's args and env (restored as data, never loaded)
STATE_WS_FILES = (".watch_state.json",)
STATE_WATCHER = "state/watcher.json"
LOCK_FILE_NAMES = {".outbox.lock", ".DS_Store"}
# restored files that can hold keys or voiceprints are written 0600
PRIVATE_SUFFIXES = (".diarization.json",)
PRIVATE_NAMES = {"_voice-groups.json", "whosaid.toml", ".watch_state.json", "speakers.json"}
# workspace-root files a share may carry (only when every meeting is shared)
SHARE_ROOT_ALLOW = re.compile(
    r"^(_ACTION-ITEMS\.md|_action-items\.json|_COMMITMENTS\.md|_commitments\.json|_WIKI\.md"
    r"|_INDEX\.md|_WORKLIST-[^/]+\.md|_tracking\.json|_topics\.json)$")
# JSON keys that hold voiceprints (biometric vectors); stripped from every shared JSON file
VOICEPRINT_KEYS = {"cluster_emb", "embedding", "embeddings", "centroid", "centroids",
                   "voiceprint", "voiceprints"}
VOICEPRINT_NEEDLES = tuple(f'"{k}"'.encode() for k in sorted(VOICEPRINT_KEYS))
DATED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-\d{4}(?:-\d+)?$")
NOTICE = (
    "whosaid share: before you share, check that you may. Recording-consent and chat-export\n"
    "rules differ by organization and jurisdiction; some require every participant's consent\n"
    "before a recording or its transcript is passed on. Voiceprints never leave this machine.\n"
    "Mark a meeting `_noshare` (an empty file in its folder) to keep it out of every share."
)


def log(msg: str) -> None:
    print(f"whosaid: {msg}", file=sys.stderr)


class BackupError(Exception):
    """A user-facing failure: printed as one line, exit 1."""


# ---- files and tiers -----------------------------------------------------------------

def classify(rel: PurePosixPath) -> str | None:
    """Tier of a workspace-relative path, or None for working state that is never copied."""
    parts = rel.parts
    if any(p.startswith(".") for p in parts) or any(p in SKIP_NAMES for p in parts):
        return None
    if rel.name.endswith(".part"):
        return None
    if len(parts) == 1 and rel.name in INDEX_NAMES:
        return "index"
    if rel.name == "_tracking.lock":
        return None
    if any(p.endswith(".samples") for p in parts[:-1]) or rel.suffix.lower() in AUDIO_EXT:
        return "audio"
    return "text"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Entry:
    rel: str                 # posix, workspace-relative
    tier: str
    size: int
    sha256: str
    path: Path | None = None  # source on disk
    data: bytes | None = None  # in-memory payload (a sanitized share file)
    stripped: bool = False     # backup: payload is strip_voiceprints(path), built at upload time


def stripped_body(path: Path) -> tuple[bytes | None, int]:
    """(voiceprint-free JSON bytes, keys removed) for a JSON file; (None, 0) when nothing was
    removed. Raises ValueError when the file is not JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    clean, n = strip_voiceprints(data)
    if not n:
        return None, 0
    return (json.dumps(clean, indent=2) + "\n").encode(), n


def has_voiceprint_bytes(raw: bytes) -> bool:
    return any(k in raw for k in VOICEPRINT_NEEDLES)


class HashCache:
    """sha256 by (size, mtime_ns), in <ws>/.backup/hashes.json, so a 9 GB workspace is
    not re-read on every incremental pass."""

    def __init__(self, ws: Path):
        self.path = ws / STATE_DIR / "hashes.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def sha(self, rel: str, path: Path) -> tuple[int, str]:
        st = path.stat()
        hit = self.data.get(rel)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return st.st_size, hit[2]
        digest = sha256_file(path)
        self.data[rel] = [st.st_size, st.st_mtime_ns, digest]
        self.dirty = True
        return st.st_size, digest

    def stripped(self, rel: str, path: Path) -> tuple[int, str, bool] | None:
        """(size, sha256, stripped) of the voiceprint-free form of a JSON file, cached by size and
        mtime; None when the file is not JSON."""
        st = path.stat()
        key = "strip:" + rel
        hit = self.data.get(key)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return hit[2], hit[3], bool(hit[4])
        try:
            body, _n = stripped_body(path)
        except (OSError, ValueError):
            return None
        if body is None:
            size, sha = self.sha(rel, path)
            out = (size, sha, False)
        else:
            out = (len(body), hashlib.sha256(body).hexdigest(), True)
        self.data[key] = [st.st_size, st.st_mtime_ns, *out]
        self.dirty = True
        return out

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".part")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)
        except OSError as e:
            log(f"WARN hash cache not saved ({e})")


def walk_workspace(ws: Path) -> list[tuple[str, Path, str]]:
    """[(rel, path, tier)] for every regular file worth copying, sorted."""
    out = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_NAMES)
        for name in sorted(files):
            p = Path(root) / name
            if p.is_symlink() or not p.is_file():
                continue
            rel = PurePosixPath(p.relative_to(ws).as_posix())
            tier = classify(rel)
            if tier:
                out.append((str(rel), p, tier))
    out.sort()
    return out


def registry_path() -> Path:
    return Path(os.environ.get("WHOSAID_SPEAKER_DB",
                               Path.home() / ".config" / "whosaid" / "speakers.json")).expanduser()


def parse_tiers(spec: str | None, allowed: tuple[str, ...], default: str = "text") -> list[str]:
    items = [t.strip().lower() for t in (spec or default).split(",") if t.strip()]
    bad = [t for t in items if t not in TIERS]
    if bad:
        raise BackupError(f"unknown tier '{bad[0]}': use {', '.join(allowed)}")
    refused = [t for t in items if t not in allowed]
    if refused:
        if refused[0] == "biometric":
            raise BackupError("the biometric tier (voiceprints) never goes to a shared target; "
                              "it is only in an encrypted personal `whosaid backup`")
        raise BackupError(f"tier '{refused[0]}' is not allowed here: use {', '.join(allowed)}")
    return list(dict.fromkeys(items))


def parse_duration(spec: str) -> dt.timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([hdwmy]?)\s*", spec or "")
    if not m:
        raise BackupError(f"bad duration '{spec}': use e.g. 90d, 12w, 6m, 1y, 36h")
    n, unit = int(m.group(1)), m.group(2) or "d"
    days = {"h": n / 24, "d": n, "w": 7 * n, "m": 30 * n, "y": 365 * n}[unit]
    return dt.timedelta(days=days)


def folder_date(folder: str) -> dt.date | None:
    m = DATED_RE.match(folder)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def meeting_of(rel: str) -> str | None:
    parts = PurePosixPath(rel).parts
    if len(parts) < 2 or parts[0].startswith("_"):
        return None
    return parts[0]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def safe_rel(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts or any(part in ("..", "") for part in p.parts) or "\\" in rel:
        raise BackupError(f"manifest names an unsafe path '{rel}'; refusing to restore it")
    return p


# ---- targets -------------------------------------------------------------------------

class Target:
    """A flat key/value store of files. Keys are posix paths under the target root."""

    kind = "base"

    def describe(self) -> str:
        raise NotImplementedError

    def list_keys(self) -> set[str]:
        raise NotImplementedError

    def put_file(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def put_bytes(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes | None:
        raise NotImplementedError

    def get_to_file(self, key: str, path: Path) -> None:
        data = self.get_bytes(key)
        if data is None:
            raise BackupError(f"{self.describe()}: missing object {key}")
        path.write_bytes(data)

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def local_root(self) -> Path | None:
        return None


class LocalTarget(Target):
    kind = "local"

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def describe(self) -> str:
        return str(self.root)

    def local_root(self) -> Path | None:
        return self.root

    def _p(self, key: str) -> Path:
        return self.root / safe_rel(key)

    def list_keys(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        out = set()
        for root, _dirs, files in os.walk(self.root):
            for name in files:
                if name.endswith(".whosaid-part"):
                    continue
                out.add((Path(root) / name).relative_to(self.root).as_posix())
        return out

    def _atomic(self, key: str, writer) -> None:
        dest = self._p(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".whosaid-part")
        writer(tmp)
        os.replace(tmp, dest)

    def put_file(self, key: str, path: Path) -> None:
        self._atomic(key, lambda tmp: shutil.copyfile(path, tmp))

    def put_bytes(self, key: str, data: bytes) -> None:
        self._atomic(key, lambda tmp: tmp.write_bytes(data))

    def get_bytes(self, key: str) -> bytes | None:
        try:
            return self._p(key).read_bytes()
        except FileNotFoundError:
            return None

    def get_to_file(self, key: str, path: Path) -> None:
        src = self._p(key)
        if not src.is_file():
            raise BackupError(f"{self.describe()}: missing object {key}")
        shutil.copyfile(src, path)

    def delete(self, key: str) -> None:
        p = self._p(key)
        try:
            p.unlink()
        except FileNotFoundError:
            return
        # tidy now-empty folders up to the root
        parent = p.parent
        while parent != self.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


class S3Target(Target):
    """S3-compatible object store over stdlib urllib with AWS Signature V4."""

    kind = "s3"

    def __init__(self, bucket: str, prefix: str, env: dict | None = None, cfg: dict | None = None):
        env = os.environ if env is None else env
        cfg = cfg or {}
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.access = env.get("AWS_ACCESS_KEY_ID", "")
        self.secret = env.get("AWS_SECRET_ACCESS_KEY", "")
        self.token = env.get("AWS_SESSION_TOKEN", "")
        self.region = (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
                       or cfg.get("s3_region") or "us-east-1")
        endpoint = (env.get("WHOSAID_S3_ENDPOINT") or env.get("AWS_ENDPOINT_URL_S3")
                    or env.get("AWS_ENDPOINT_URL") or cfg.get("s3_endpoint") or "")
        if endpoint:
            u = urllib.parse.urlsplit(endpoint.rstrip("/"))
            self.scheme, self.host = u.scheme or "https", u.netloc
            self.base_path = f"{u.path}/{bucket}"          # path-style
        else:
            self.scheme, self.host = "https", f"{bucket}.s3.{self.region}.amazonaws.com"
            self.base_path = ""                             # virtual-hosted
        if not self.access or not self.secret:
            raise BackupError("s3 target needs AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the environment")

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _q(s: str, safe: str = "-_.~") -> str:
        return urllib.parse.quote(s, safe=safe)

    def sign(self, method: str, path: str, query: dict, payload_hash: str,
             now: dt.datetime | None = None, extra: dict | None = None) -> dict:
        """Headers for one SigV4-signed request (path already URI-encoded)."""
        now = now or dt.datetime.now(dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        headers = {"host": self.host, "x-amz-date": amzdate, "x-amz-content-sha256": payload_hash}
        if self.token:
            headers["x-amz-security-token"] = self.token
        for k, v in (extra or {}).items():
            headers[k.lower()] = v
        canonical_query = "&".join(f"{self._q(k)}={self._q(str(v))}" for k, v in sorted(query.items()))
        names = sorted(headers)
        canonical_headers = "".join(f"{n}:{str(headers[n]).strip()}\n" for n in names)
        signed = ";".join(names)
        creq = "\n".join([method, path, canonical_query, canonical_headers, signed, payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode()).hexdigest()])
        k = hmac.new(("AWS4" + self.secret).encode(), datestamp.encode(), hashlib.sha256).digest()
        for part in (self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.access}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={sig}")
        return headers

    def _request(self, method: str, key: str | None, query: dict | None = None,
                 body=None, length: int | None = None, payload_hash: str | None = None,
                 ok_missing: bool = False):
        query = query or {}
        path = self.base_path + ("/" + self._q(self._key(key), safe="/-_.~") if key is not None else "/")
        if payload_hash is None:
            payload_hash = hashlib.sha256(body if isinstance(body, bytes) else b"").hexdigest()
        extra = {}
        if length is not None:
            extra["content-length"] = str(length)
        if body is not None:
            # urllib otherwise sends application/x-www-form-urlencoded, which a
            # werkzeug-based server (moto) parses as a form and stores empty
            extra["content-type"] = "application/octet-stream"
        headers = self.sign(method, path or "/", query, payload_hash, extra=extra)
        headers.pop("host")
        qs = "&".join(f"{self._q(k)}={self._q(str(v))}" for k, v in sorted(query.items()))
        url = f"{self.scheme}://{self.host}{path or '/'}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        last = None
        for attempt in range(3):
            try:
                if hasattr(body, "seek"):
                    body.seek(0)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 404 and ok_missing:
                    return None
                detail = e.read()[:300].decode("utf-8", "replace")
                if e.code < 500:
                    raise BackupError(f"{self.describe()}: {method} {key or ''} -> HTTP {e.code} {detail}")
                last = f"HTTP {e.code} {detail}"
            except (urllib.error.URLError, OSError) as e:
                last = str(e)
            time.sleep(0.5 * (attempt + 1))
        raise BackupError(f"{self.describe()}: {method} {key or ''} failed: {last}")

    def list_keys(self) -> set[str]:
        out, token = set(), None
        pre = f"{self.prefix}/" if self.prefix else ""
        while True:
            q = {"list-type": "2", "prefix": pre}
            if token:
                q["continuation-token"] = token
            raw = self._request("GET", None, q, ok_missing=True)
            if raw is None:
                raise BackupError(f"{self.describe()}: bucket '{self.bucket}' not found")
            root = ET.fromstring(raw)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for c in root.iter(f"{ns}Contents"):
                k = c.findtext(f"{ns}Key") or ""
                out.add(k[len(pre):])
            if (root.findtext(f"{ns}IsTruncated") or "").lower() == "true":
                token = root.findtext(f"{ns}NextContinuationToken")
                if not token:
                    break
            else:
                break
        return out

    def put_file(self, key: str, path: Path) -> None:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            self._request("PUT", key, body=fh, length=size, payload_hash="UNSIGNED-PAYLOAD")

    def put_bytes(self, key: str, data: bytes) -> None:
        self._request("PUT", key, body=data, length=len(data))

    def get_bytes(self, key: str) -> bytes | None:
        return self._request("GET", key, ok_missing=True)

    def delete(self, key: str) -> None:
        self._request("DELETE", key, ok_missing=True)


class RcloneTarget(Target):
    """Any rclone remote (`rclone:REMOTE:path`) through the rclone binary. `runner` is
    injectable so the command construction is unit-tested without a real remote."""

    kind = "rclone"

    def __init__(self, remote: str, runner=None, binary: str | None = None):
        if ":" not in remote:
            raise BackupError(f"rclone target needs REMOTE:path, got '{remote}' (e.g. rclone:onedrive:whosaid)")
        self.remote = remote.rstrip("/")
        self.binary = binary or os.environ.get("WHOSAID_RCLONE") or "rclone"
        self.runner = runner or self._run

    def describe(self) -> str:
        return f"rclone:{self.remote}"

    def path(self, key: str) -> str:
        safe_rel(key)
        return f"{self.remote}{'' if self.remote.endswith(':') else '/'}{key}"

    @staticmethod
    def _run(argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, input=stdin, capture_output=True, timeout=3600)
        except FileNotFoundError as e:
            raise BackupError(f"rclone not found ({e}); install it (brew install rclone) and run `rclone config`")

    def _ok(self, argv: list[str], stdin: bytes | None = None, missing_ok: bool = False):
        proc = self.runner(argv, stdin)
        if proc.returncode != 0:
            err = (proc.stderr or b"")
            err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
            if missing_ok and ("not found" in err.lower() or "doesn't exist" in err.lower()
                               or proc.returncode == 3):
                return None
            raise BackupError(f"{self.describe()}: {' '.join(argv[1:3])} failed: {err.strip()[-300:]}")
        return proc.stdout if proc.stdout is not None else b""

    def list_keys(self) -> set[str]:
        out = self._ok([self.binary, "lsf", "-R", "--files-only", self.remote], missing_ok=True)
        if out is None:
            return set()
        text = out.decode() if isinstance(out, bytes) else out
        return {line.strip() for line in text.splitlines() if line.strip()}

    def put_file(self, key: str, path: Path) -> None:
        self._ok([self.binary, "copyto", str(path), self.path(key)])

    def put_bytes(self, key: str, data: bytes) -> None:
        self._ok([self.binary, "rcat", self.path(key)], stdin=data)

    def get_bytes(self, key: str) -> bytes | None:
        out = self._ok([self.binary, "cat", self.path(key)], missing_ok=True)
        if out is None:
            return None
        return out if isinstance(out, bytes) else out.encode()

    def get_to_file(self, key: str, path: Path) -> None:
        self._ok([self.binary, "copyto", self.path(key), str(path)])

    def delete(self, key: str) -> None:
        self._ok([self.binary, "deletefile", self.path(key)], missing_ok=True)


def open_target(spec: str, cfg: dict | None = None) -> Target:
    spec = (spec or "").strip()
    if not spec:
        raise BackupError("need a target: --to PATH | s3://bucket/prefix | rclone:REMOTE:path")
    if spec.startswith("s3://"):
        rest = spec[5:]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise BackupError(f"bad s3 target '{spec}': use s3://bucket/prefix")
        return S3Target(bucket, prefix, cfg=cfg)
    if spec.startswith("rclone:"):
        return RcloneTarget(spec[len("rclone:"):])
    if spec.startswith("file://"):
        return LocalTarget(Path(urllib.parse.unquote(spec[7:])))
    if re.match(r"^[a-z][a-z0-9+.-]*://", spec):
        raise BackupError(f"unsupported target '{spec}': use a path, s3://..., or rclone:REMOTE:path")
    return LocalTarget(Path(spec))


# ---- age encryption ------------------------------------------------------------------

def age_bin() -> str:
    b = os.environ.get("WHOSAID_AGE") or shutil.which("age")
    if not b:
        raise BackupError("encryption needs the `age` binary (brew install age), or pass --plaintext")
    return b


def age_encrypt(src: Path, dest: Path, recipients: list[str], recipients_file: str | None) -> None:
    argv = [age_bin(), "-e", "-o", str(dest)]
    for r in recipients:
        argv += ["-r", r]
    if recipients_file:
        argv += ["-R", str(Path(recipients_file).expanduser())]
    argv.append(str(src))
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BackupError(f"age encrypt failed: {proc.stderr.strip()}")


def padded_size(n: int) -> int:
    """Padme (Nikitin et al., PETS 2019): round n up so only O(log log n) bits of it show."""
    n = max(n, PAD_FLOOR)
    e = n.bit_length() - 1
    s = e.bit_length()
    mask = (1 << (e - s)) - 1
    return (n + mask) & ~mask


def age_encrypt_padded(src: Path, size: int, dest: Path, recipients: list[str],
                       recipients_file: str | None) -> None:
    """Encrypt src plus zero bytes up to padded_size(size), streamed into age's stdin."""
    argv = [age_bin(), "-e", "-o", str(dest)]
    for r in recipients:
        argv += ["-r", r]
    if recipients_file:
        argv += ["-R", str(Path(recipients_file).expanduser())]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        n = 0
        with open(src, "rb") as fh:
            while chunk := fh.read(1 << 20):
                proc.stdin.write(chunk)
                n += len(chunk)
        if n != size:
            proc.stdin.close()
            proc.wait()
            raise BackupError(f"{src} changed size while the backup ran; run it again")
        left = padded_size(size) - size
        zeros = bytes(min(left, 1 << 20))
        while left > 0:
            proc.stdin.write(zeros[:left])
            left -= min(left, len(zeros))
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise BackupError(f"age encrypt failed: {err.strip()}")


def unpad(plain: Path, fe: dict) -> None:
    """Drop the zero padding of an object written with PAD_ALG (older objects carry none)."""
    if fe.get("pad") and plain.stat().st_size >= int(fe.get("size") or 0):
        os.truncate(plain, int(fe["size"]))


def names_key_new() -> dict:
    import secrets
    return {"alg": NAMES_ALG, "key": secrets.token_hex(32)}


def age_decrypt(src: Path, dest: Path, identity: str) -> None:
    proc = subprocess.run([age_bin(), "-d", "-i", str(Path(identity).expanduser()), "-o", str(dest), str(src)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise BackupError(f"age decrypt failed (wrong --identity?): {proc.stderr.strip()}")


# ---- manifests -----------------------------------------------------------------------

def read_manifest(target: Target, identity: str | None = None, tmpdir: Path | None = None) -> dict | None:
    """The target's manifest (backup plain, backup encrypted, or share), or None."""
    raw = target.get_bytes(BACKUP_MANIFEST)
    if raw is None:
        raw = target.get_bytes(SHARE_MANIFEST)
    if raw is None:
        enc = target.get_bytes(BACKUP_MANIFEST + ".age")
        if enc is None:
            return None
        if not identity:
            raise BackupError(f"{target.describe()} holds an encrypted backup: pass --identity FILE "
                              "(the age key whose public half was the --recipient)")
        with tempfile.TemporaryDirectory(dir=tmpdir) as td:
            a, b = Path(td) / "m.age", Path(td) / "m.json"
            a.write_bytes(enc)
            age_decrypt(a, b, identity)
            raw = b.read_bytes()
    try:
        m = json.loads(raw)
    except ValueError as e:
        raise BackupError(f"{target.describe()}: manifest is not JSON ({e})")
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        raise BackupError(f"{target.describe()}: not a whosaid backup/share manifest")
    return m


def target_kind(target: Target) -> str | None:
    keys = target.list_keys()
    if BACKUP_MANIFEST in keys or BACKUP_MANIFEST + ".age" in keys:
        return "backup"
    if SHARE_MANIFEST in keys:
        return "share"
    return None


def guard_target(ws: Path, target: Target) -> None:
    root = target.local_root()
    if root is None:
        return
    ws = ws.resolve()
    if root == ws or ws in root.parents:
        raise BackupError(f"target {root} is inside the workspace {ws}; pick a path outside it")
    if root in ws.parents:
        raise BackupError(f"target {root} contains the workspace {ws}; pick a separate folder")


def state_file(ws: Path, target: Target) -> Path:
    tid = hashlib.sha256(target.describe().encode()).hexdigest()[:16]
    return ws / STATE_DIR / f"target-{tid}.json"


def load_local_state(ws: Path, target: Target) -> dict:
    try:
        return json.loads(state_file(ws, target).read_text())
    except (OSError, ValueError):
        return {}


def save_local_state(ws: Path, target: Target, state: dict) -> None:
    p = state_file(ws, target)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.touch(mode=0o600)
        os.chmod(tmp, 0o600)                       # the cached manifest holds the object names key
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(p)
    except OSError as e:
        log(f"WARN backup state not saved ({e})")


# ---- backup --------------------------------------------------------------------------

@dataclass
class Plan:
    kind: str
    target: str
    tiers: list[str]
    encrypted: bool
    upload: list[Entry] = field(default_factory=list)
    unchanged: list[Entry] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    meetings: list[str] = field(default_factory=list)
    excluded: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        by_tier: dict = {}
        for e in self.upload + self.unchanged:
            t = by_tier.setdefault(e.tier, {"files": 0, "bytes": 0, "upload_files": 0, "upload_bytes": 0})
            t["files"] += 1
            t["bytes"] += e.size
        for e in self.upload:
            by_tier[e.tier]["upload_files"] += 1
            by_tier[e.tier]["upload_bytes"] += e.size
        return {
            "kind": self.kind, "target": self.target, "tiers": self.tiers, "encrypted": self.encrypted,
            "upload": [{"path": e.rel, "tier": e.tier, "bytes": e.size} for e in self.upload],
            "unchanged": len(self.unchanged), "delete": self.delete,
            "by_tier": by_tier,
            "upload_bytes": sum(e.size for e in self.upload),
            "meetings": len(self.meetings) if self.kind == "share" else None,
            "excluded": self.excluded, "notes": self.notes,
        }


def print_plan(plan: Plan, dry: bool) -> None:
    verb = "would upload" if dry else "upload"
    for e in plan.upload:
        print(f"{verb} {e.rel}  ({e.tier}, {human(e.size)})")
    for k in plan.delete:
        print(f"{'would delete' if dry else 'delete'} {k}")
    s = plan.summary()
    tiers = ", ".join(f"{t} {v['files']} file(s) {human(v['bytes'])}" for t, v in sorted(s["by_tier"].items()))
    log(f"{plan.kind} -> {plan.target}: {len(plan.upload)} to upload ({human(s['upload_bytes'])}), "
        f"{len(plan.unchanged)} unchanged, {len(plan.delete)} to delete"
        f"{'; encrypted with age' if plan.encrypted else '; plaintext'}")
    if tiers:
        log(f"  tiers: {tiers}")
    if plan.kind == "share":
        log(f"  meetings shared: {len(plan.meetings)}")
        for why, names in plan.excluded.items():
            if names:
                log(f"  kept on this machine ({why}): {len(names)}")
    for n in plan.notes:
        log(f"  note: {n}")


def recipients_from(args, cfg: dict) -> tuple[list[str], str | None]:
    bcfg = cfg.get("backup") or {}
    recips = list(getattr(args, "recipient", None) or [])
    rfile = getattr(args, "recipients_file", None) or bcfg.get("recipients_file") or None
    if not recips and not rfile:
        env = os.environ.get("WHOSAID_BACKUP_AGE_RECIPIENT", "").strip()
        if env:
            recips = [r.strip() for r in env.split(",") if r.strip()]
        else:
            cr = bcfg.get("recipients") or bcfg.get("recipient") or []
            recips = [cr] if isinstance(cr, str) else [str(r) for r in cr]
    return recips, rfile


def recipients_fingerprint(recips: list[str], rfile: str | None) -> str:
    """Hash of the age recipient set. Content-addressed objects are only reusable when they were
    encrypted to the same recipients; a changed set re-encrypts everything."""
    parts = sorted(r.strip() for r in recips if r.strip())
    if rfile:
        parts += sorted(ln.strip() for ln in Path(rfile).expanduser().read_text().splitlines()
                        if ln.strip() and not ln.lstrip().startswith("#"))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def drafts_root() -> Path:
    return Path(os.environ.get("WHOSAID_DRAFTS_DIR") or Path.home() / ".drafts").expanduser()


def launch_agents_dir() -> Path:
    return Path(os.environ.get("WHOSAID_LAUNCH_AGENTS_DIR") or Path.home() / "Library" / "LaunchAgents")


def watcher_snapshot(ws: Path) -> dict | None:
    """The launchd agents (com.whosaid.*) whose arguments name this workspace: label, program
    arguments, environment and schedule. Data only; restore never loads it into launchd."""
    import plistlib
    d = launch_agents_dir()
    if not d.is_dir():
        return None
    want = str(ws.resolve())
    agents = []
    aliases: set[str] = set()
    for p in sorted(d.glob("com.whosaid.*.plist")):
        try:
            with open(p, "rb") as fh:
                data = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        args = [str(a) for a in (data.get("ProgramArguments") or [])]
        named = False
        for a in args:
            try:
                if Path(a).expanduser().resolve() == Path(want):
                    named = True
                    if a != want:
                        aliases.add(a)
            except (OSError, ValueError):
                pass
        if not named:
            continue
        agents.append({k: data[k] for k in ("Label", "ProgramArguments", "EnvironmentVariables",
                                            "StartInterval", "WatchPaths", "RunAtLoad") if k in data})
    if not agents:
        return None
    return {"home": str(Path.home()), "workspace": want, "workspace_aliases": sorted(aliases), "agents": agents}


def state_sources(ws: Path) -> list[tuple[str, Path | None, bytes | None]]:
    """[(rel, path, data)] for the state tier: watcher state in the workspace, the drafts outbox,
    and a JSON snapshot of the launchd watcher (data)."""
    out: list[tuple[str, Path | None, bytes | None]] = []
    for name in STATE_WS_FILES:
        f = ws / name
        if f.is_file() and not f.is_symlink():
            out.append((f"state/workspace/{name}", f, None))
    droot = drafts_root()
    if droot.is_dir():
        for root, dirs, files in os.walk(droot):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_NAMES)
            for name in sorted(files):
                f = Path(root) / name
                if name in LOCK_FILE_NAMES or name.endswith((".part", ".tmp")) or f.is_symlink() \
                        or not f.is_file():
                    continue
                out.append((f"state/drafts/{f.relative_to(droot).as_posix()}", f, None))
    snap = watcher_snapshot(ws)
    if snap:
        out.append((STATE_WATCHER, None, (json.dumps(snap, indent=1, sort_keys=True) + "\n").encode()))
    return out


def plan_backup(ws: Path, target: Target, tiers: list[str], encrypted: bool,
                prev: dict | None, keys: set[str], cache: HashCache,
                names: dict | None = None) -> tuple[Plan, dict]:
    plan = Plan("backup", target.describe(), tiers, encrypted)
    files = {}
    strip = "biometric" not in tiers
    stripped = 0
    for rel, path, tier in walk_workspace(ws):
        if tier not in tiers:
            continue
        if strip and path.suffix.lower() == ".json":
            got = cache.stripped(rel, path)
            if got is None:
                try:
                    raw = path.read_bytes()
                except OSError as e:
                    plan.notes.append(f"WARN skipped unreadable {rel} ({e})")
                    continue
                if has_voiceprint_bytes(raw):
                    plan.notes.append(f"WARN skipped {rel}: not valid JSON and it names a voiceprint key, so "
                                      "it cannot be stripped; fix the file or add --tier biometric")
                    continue
                size, sha = cache.sha(rel, path)
                files[rel] = Entry(rel, tier, size, sha, path=path)
                continue
            size, sha, was = got
            stripped += was
            files[rel] = Entry(rel, tier, size, sha, path=path, stripped=was)
            continue
        size, sha = cache.sha(rel, path)
        files[rel] = Entry(rel, tier, size, sha, path=path)
    if stripped:
        plan.notes.append(f"voiceprints stripped from {stripped} JSON file(s) (add --tier biometric to keep them)")
    if "state" in tiers:
        for rel, path, data in state_sources(ws):
            if data is not None:
                files[rel] = Entry(rel, "state", len(data), hashlib.sha256(data).hexdigest(), data=data)
            else:
                files[rel] = Entry(rel, "state", path.stat().st_size, sha256_file(path), path=path)
    if "biometric" in tiers:
        reg = registry_path()
        if reg.is_file():
            files["biometric/speakers.json"] = Entry("biometric/speakers.json", "biometric",
                                                     reg.stat().st_size, sha256_file(reg), path=reg)
        else:
            plan.notes.append(f"no speaker registry at {reg}; biometric tier is empty")
    prev_files = (prev or {}).get("files") or {}
    for rel, e in files.items():
        if encrypted:
            key = object_key(e.sha256, rel, names) if names else f"objects/{e.sha256}.age"  # legacy names
            present = key in keys
        else:
            pe = prev_files.get(rel)
            present = bool(pe) and pe.get("sha256") == e.sha256 and rel in keys
        (plan.unchanged if present else plan.upload).append(e)
    return plan, files


def object_key(e_sha: str, rel: str, names: dict | None) -> str:
    """Encrypted: objects/<HMAC(names key, plaintext sha256)>.age. Plaintext: the path itself."""
    if not names:
        return rel
    if names.get("alg") != NAMES_ALG:
        raise BackupError(f"unknown object naming {names.get('alg')!r}; upgrade whosaid")
    mac = hmac.new(bytes.fromhex(names["key"]), e_sha.encode(), hashlib.sha256).hexdigest()
    return f"objects/{mac}.age"


class BackupLock:
    """<ws>/.backup/lock: one backup, gc or verify per workspace at a time (gc must never run
    while a pass has uploaded objects its manifest does not name yet)."""

    def __init__(self, ws: Path):
        self.path = ws / STATE_DIR / "lock"
        self.fh = None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise BackupError(f"another backup, gc or verify is running on this workspace ({self.path})")
        return self

    def __exit__(self, *exc):
        self.fh.close()


def manifest_stamp(now: dt.datetime | None = None) -> str:
    """UTC stamp to the microsecond plus random hex: sorts by time, and two passes never collide."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + os.urandom(3).hex()


def dated_manifests(keys: set[str]) -> list[str]:
    """manifests/<stamp>.age keys on the target, oldest first."""
    return sorted(k for k in keys if k.startswith(MANIFESTS_DIR + "/") and k.endswith(".age"))


def keep_count(args, bcfg: dict) -> int:
    raw = getattr(args, "keep", None) or bcfg.get("keep") or DEFAULT_KEEP
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise BackupError(f"bad keep '{raw}': use a whole number of manifests (default {DEFAULT_KEEP})")
    if n < 1:
        raise BackupError("keep must be at least 1")
    return n


def decrypt_manifest_key(target: Target, key: str, identity: str) -> dict:
    enc = target.get_bytes(key)
    if enc is None:
        raise BackupError(f"{target.describe()}: missing {key}")
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "m.age", Path(td) / "m.json"
        a.write_bytes(enc)
        age_decrypt(a, b, identity)
        try:
            m = json.loads(b.read_bytes())
        except ValueError as e:
            raise BackupError(f"{key}: not JSON ({e})")
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        raise BackupError(f"{key}: not a whosaid backup manifest")
    return m


def resolve_identity(args, bcfg: dict) -> str | None:
    ident = getattr(args, "identity", None) or os.environ.get("WHOSAID_BACKUP_AGE_IDENTITY") \
        or bcfg.get("identity")
    return str(Path(str(ident)).expanduser()) if ident else None


def referenced_objects(target: Target, keys: set[str], identity: str, keep: int) -> tuple[set[str], list[str]]:
    """Objects any kept manifest names: the newest `keep` dated manifests plus the latest
    _whosaid-backup.json.age. Every one must decrypt; an unreadable manifest aborts (it is never
    read as "references nothing")."""
    kept = dated_manifests(keys)[-keep:]
    names = list(kept)
    if BACKUP_MANIFEST + ".age" in keys:
        names.append(BACKUP_MANIFEST + ".age")
    refs: set[str] = set()
    for k in names:
        try:
            m = decrypt_manifest_key(target, k, identity)
        except BackupError as e:
            raise BackupError(f"cannot read kept manifest {k} ({e}); nothing deleted. Pass an --identity that "
                              "opens every kept manifest, or lower --keep")
        refs |= {fe.get("object") for fe in (m.get("files") or {}).values() if fe.get("object")}
    return refs, kept


def cmd_backup(args) -> int:
    ws = Path(args.ws).expanduser().resolve()
    if not ws.is_dir():
        raise BackupError(f"workspace not found: {ws}")
    with BackupLock(ws):
        return _cmd_backup(args, ws)


def _cmd_backup(args, ws: Path) -> int:
    cfg = getattr(args, "cfg", None) or load_config(ws)
    bcfg = cfg.get("backup") or {}
    target = open_target(args.to or bcfg.get("to") or "", bcfg)
    guard_target(ws, target)
    tiers = parse_tiers(args.tier or bcfg.get("tier"), TIERS)
    plaintext = bool(args.plaintext or (bcfg.get("plaintext") is True and not args.recipient))
    recips, rfile = ([], None) if plaintext else recipients_from(args, cfg)
    encrypted = not plaintext
    if encrypted and not recips and not rfile:
        raise BackupError("backup encrypts with age by default: pass --recipient age1... (from `age-keygen`), "
                          "set [backup] recipients in whosaid.toml (`whosaid backup init` does it), or pass "
                          "--plaintext for an unencrypted copy")
    if plaintext and "biometric" in tiers:
        raise BackupError("the biometric tier (voiceprints) is only backed up encrypted; drop --plaintext")
    if plaintext and "state" in tiers:
        raise BackupError("the state tier (watcher env, drafts) is only backed up encrypted; drop --plaintext")
    if encrypted:
        age_bin()
    keep = keep_count(args, bcfg)
    kind = target_kind(target)
    if kind == "share":
        raise BackupError(f"{target.describe()} holds a share, not a backup; use a separate target")
    keys = target.list_keys()
    if encrypted and BACKUP_MANIFEST in keys:
        raise BackupError(f"{target.describe()} holds a plaintext backup; use a new target for an encrypted one")
    if not encrypted and BACKUP_MANIFEST + ".age" in keys:
        raise BackupError(f"{target.describe()} holds an encrypted backup; drop --plaintext or use a new target")
    ident = resolve_identity(args, bcfg)
    # previous manifest: plaintext from the target, encrypted from the local state cache
    if encrypted:
        prev = load_local_state(ws, target).get("manifest")
        if prev is None and BACKUP_MANIFEST + ".age" in keys:
            if ident:
                prev = read_manifest(target, ident)
            else:
                log("WARN no local record of this encrypted backup (and no --identity): object names are "
                    "keyed by a secret inside its manifest, so every object is uploaded again, and entries "
                    "for files deleted locally since the last backup drop out of the new manifest")
    else:
        prev = read_manifest(target) if BACKUP_MANIFEST in keys else None
    reuse = keys
    if encrypted:
        fp = recipients_fingerprint(recips, rfile)
        old_fp = (prev or {}).get("recipients_fp")
        if old_fp is None and RECIPIENTS_KEY in keys:
            old_fp = (target.get_bytes(RECIPIENTS_KEY) or b"").decode().strip()
        names = (prev or {}).get("names")
        if any(k.startswith("objects/") for k in keys) and old_fp != fp:
            # objects on the target were encrypted to other keys: none can be reused, and a fresh
            # names key keeps a removed recipient from linking the new objects to the old ones
            reuse = {k for k in keys if not k.startswith("objects/")}
            names = None
    else:
        names = None
    new_names = encrypted and not names
    if new_names:
        names = names_key_new()
    cache = HashCache(ws)
    plan, files = plan_backup(ws, target, tiers, encrypted, prev, reuse, cache, names)
    if new_names and prev and not (prev or {}).get("names"):
        plan.notes.append("first run with keyed object names: every object is uploaded again under an opaque "
                          "name; the old ones go with `whosaid backup gc` once no kept manifest names them")
    if reuse is not keys:
        plan.notes.append("age recipients differ from the ones this backup was written with: "
                          "re-encrypting every object (restore needs the new identity)")
    cache.save()

    # additive: keep earlier entries for files no longer on disk
    prev_files = (prev or {}).get("files") or {}
    manifest_files = {}
    rekey: list[str] = []
    for rel, pe in prev_files.items():
        if rel not in files:
            manifest_files[rel] = pe
            if encrypted and pe.get("object") != object_key(pe.get("sha256") or "", rel, names):
                rekey.append(rel)
    if rekey and not ident:
        plan.notes.append(f"WARN {len(rekey)} file(s) no longer on disk keep their old object names (and keys): "
                          "pass --identity to re-encrypt them under the current names key")
        rekey = []
    elif rekey:
        plan.notes.append(f"re-encrypting {len(rekey)} object(s) of files no longer on disk under the current "
                          "names key (read back from the target)")
    for rel, e in files.items():
        fe = {"sha256": e.sha256, "size": e.size, "tier": e.tier,
              "object": object_key(e.sha256, rel, names)}
        if encrypted:
            fe["pad"] = PAD_ALG
        if e.stripped:
            fe["stripped"] = True
        pe = prev_files.get(rel) or {}
        if encrypted and pe.get("csha") and pe.get("object") == fe["object"] and reuse is keys:
            fe["csha"] = pe["csha"]
        manifest_files[rel] = fe
    if args.prune_older_than:
        cutoff = dt.date.today() - parse_duration(args.prune_older_than)
        dropped = [rel for rel in manifest_files
                   if (meeting_of(rel) and folder_date(meeting_of(rel)) and folder_date(meeting_of(rel)) < cutoff)]
        for rel in dropped:
            manifest_files.pop(rel)
        plan.upload = [e for e in plan.upload if e.rel not in dropped]
        plan.unchanged = [e for e in plan.unchanged if e.rel not in dropped]
        live = {v["object"] for v in manifest_files.values()}
        if encrypted:
            # older kept manifests still name these objects (point-in-time restore); `backup gc`
            # deletes them once no kept manifest does
            plan.delete = []
            if dropped:
                plan.notes.append("pruned entries leave the new manifest; their objects go once no kept manifest "
                                  "names them (`whosaid backup gc`, or `gc --keep 1` to drop them now)")
        else:
            candidates = {v.get("object", r) for r, v in prev_files.items()} & keys
            plan.delete = sorted(candidates - live)
        if dropped:
            plan.notes.append(f"pruned {len(dropped)} file(s) in meetings dated before {cutoff}")

    if args.json and args.dry_run:
        print(json.dumps(plan.summary(), indent=1))
        return 0
    print_plan(plan, args.dry_run)
    if args.dry_run:
        return 0

    csha: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="whosaid-backup-") as td:
        seen = set()
        for e in plan.upload:
            key = object_key(e.sha256, e.rel, names)
            if key in seen:
                continue
            seen.add(key)
            src = e.path
            if e.stripped or e.data is not None:
                body = e.data
                if body is None:
                    body, _n = stripped_body(e.path)
                    body = body if body is not None else e.path.read_bytes()
                if hashlib.sha256(body).hexdigest() != e.sha256:
                    raise BackupError(f"{e.rel} changed while the backup ran; run it again")
                src = Path(td) / "payload"
                src.write_bytes(body)
            if encrypted:
                enc = Path(td) / "obj.age"
                age_encrypt_padded(src, e.size, enc, recips, rfile)
                csha[key] = sha256_file(enc)
                target.put_file(key, enc)
                enc.unlink()
            else:
                target.put_file(key, src)
            if src is not e.path:
                src.unlink()
        for rel in rekey:
            pe = manifest_files[rel]
            new = object_key(pe["sha256"], rel, names)
            raw, plain, enc = Path(td) / "rk.age", Path(td) / "rk", Path(td) / "rk.new"
            try:
                target.get_to_file(pe["object"], raw)
                age_decrypt(raw, plain, ident)
                unpad(plain, pe)
                if sha256_file(plain) != pe["sha256"]:
                    raise BackupError("content hash does not match the manifest")
                if new not in seen:
                    age_encrypt_padded(plain, plain.stat().st_size, enc, recips, rfile)
                    csha[new] = sha256_file(enc)
                    target.put_file(new, enc)
                    seen.add(new)
            except BackupError as ex:
                log(f"WARN {rel}: kept under its old object name ({ex})")
                continue
            finally:
                for f in (raw, plain, enc):
                    f.unlink(missing_ok=True)
            manifest_files[rel] = {**{k: v for k, v in pe.items() if k != "csha"}, "object": new, "pad": PAD_ALG,
                                   "size": pe["size"]}
        for fe in manifest_files.values():
            if fe.get("object") in csha:
                fe["csha"] = csha[fe["object"]]
        manifest = {
            "schema": SCHEMA, "kind": "backup", "workspace": ws.name,
            "workspace_path": str(ws), "home": str(Path.home()),
            "created": (prev or {}).get("created") or now_iso(), "updated": now_iso(),
            "encrypted": "age" if encrypted else None,
            "layout": "objects" if encrypted else "tree",
            "tiers": sorted(set(tiers) | set((prev or {}).get("tiers") or []), key=TIERS.index),
            "files": dict(sorted(manifest_files.items())),
        }
        if encrypted:
            manifest["names"] = names
            manifest["recipients_fp"] = fp
        body = json.dumps(manifest, indent=1).encode()
        if encrypted:
            body += b" " * (padded_size(len(body)) - len(body))   # JSON ignores trailing whitespace
        dropped_manifests: list[str] = []
        if encrypted:
            plain, enc = Path(td) / "m.json", Path(td) / "m.age"
            plain.write_bytes(body)
            age_encrypt(plain, enc, recips, rfile)
            stamp_key = f"{MANIFESTS_DIR}/{manifest_stamp()}.age"
            target.put_file(stamp_key, enc)           # the point-in-time copy first, then "latest"
            target.put_file(BACKUP_MANIFEST + ".age", enc)
            if RECIPIENTS_KEY in keys:
                target.delete(RECIPIENTS_KEY)                # legacy plaintext fingerprint
            dated = dated_manifests(keys | {stamp_key})
            dropped_manifests = dated[:-keep]
            for k in dropped_manifests:
                target.delete(k)
        else:
            target.put_bytes(BACKUP_MANIFEST, body)
        for k in plan.delete:
            target.delete(k)
    state = load_local_state(ws, target)
    summ = plan.summary()
    state.update({"last_backup": now_iso(), "manifest": manifest if encrypted else None,
                  "by_tier": {t: {"files": v["files"], "bytes": v["bytes"]} for t, v in summ["by_tier"].items()},
                  "tiers": tiers, "encrypted": encrypted, "target": target.describe()})
    state.setdefault("first_backup", state["last_backup"])
    state.pop("last_failure", None)
    save_local_state(ws, target, state)
    if args.json:
        print(json.dumps({**summ, "ok": True}, indent=1))
    log(f"✓ backup complete: {len(plan.upload)} uploaded, {len(manifest_files)} file(s) in the backup"
        + (f"; {len(dropped_manifests)} manifest(s) past keep={keep} dropped (objects stay until `backup gc`)"
           if dropped_manifests else ""))
    return 0


def cmd_gc(args) -> int:
    """Delete objects/ no kept manifest references. Encrypted layout only."""
    ws = Path(args.ws).expanduser().resolve()
    cfg = load_config(ws)
    bcfg = cfg.get("backup") or {}
    target = open_target(args.to or bcfg.get("to") or "", bcfg)
    keep = keep_count(args, bcfg)
    with BackupLock(ws):
        keys = target.list_keys()
        if BACKUP_MANIFEST in keys:
            raise BackupError("gc is for encrypted backups; a plaintext backup deletes with --prune-older-than")
        if BACKUP_MANIFEST + ".age" not in keys:
            raise BackupError(f"{target.describe()}: no encrypted whosaid backup here")
        identity = resolve_identity(args, bcfg)
        if not identity:
            raise BackupError("gc reads every kept manifest: pass --identity FILE (or set [backup] identity)")
        refs, kept = referenced_objects(target, keys, identity, keep)
        dated = dated_manifests(keys)
        old_manifests = dated[:-keep]
        objects = {k for k in keys if k.startswith("objects/")}
        orphans = sorted(objects - refs)
        missing = sorted(r for r in refs if r not in keys)
        res = {"target": target.describe(), "keep": keep, "manifests_kept": len(kept),
               "manifests_dropped": old_manifests, "objects": len(objects), "referenced": len(refs),
               "delete": orphans, "missing": missing, "dry_run": bool(args.dry_run)}
        if not args.json:
            for k in old_manifests:
                print(f"{'would delete' if args.dry_run else 'delete'} {k}")
            for k in orphans:
                print(f"{'would delete' if args.dry_run else 'delete'} {k}")
            log(f"gc {target.describe()}: {len(objects)} object(s), {len(refs)} referenced by {len(kept)} kept "
                f"manifest(s) + latest; {len(orphans)} unreferenced{' (dry run)' if args.dry_run else ''}")
            if missing:
                log(f"WARN {len(missing)} referenced object(s) are missing from the target: run `whosaid backup verify`")
        if not args.dry_run:
            for k in old_manifests + orphans:
                target.delete(k)
            st = load_local_state(ws, target)
            st["last_gc"] = {"at": now_iso(), "deleted": len(orphans) + len(old_manifests)}
            save_local_state(ws, target, st)
        if args.json:
            print(json.dumps(res, indent=1))
    return 0


# ---- restore -------------------------------------------------------------------------

def is_private(rel: str, tier: str) -> bool:
    name = PurePosixPath(rel).name
    return tier in ("state", "biometric") or name in PRIVATE_NAMES or name.endswith(PRIVATE_SUFFIXES)


def map_roots(obj, pairs: list[tuple[str, str]]):
    """Rewrite path prefixes (old workspace, old home) inside every string of a JSON value."""
    if isinstance(obj, dict):
        return {k: map_roots(v, pairs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [map_roots(v, pairs) for v in obj]
    if isinstance(obj, str):
        for old, new in pairs:
            if old and old != new:
                obj = re.sub(re.escape(old) + r"(?=/|$|[:;,\s])", lambda _m, n=new: n, obj)
        return obj
    return obj


def recording_hashes(ws: Path) -> dict[str, str]:
    """sha256 -> file name for every recording a meeting folder holds (ingest copies the source
    recording byte for byte, so these are the watcher's source files)."""
    out = {}
    for rel, path, tier in walk_workspace(ws):
        parts = PurePosixPath(rel).parts
        if tier != "audio" or len(parts) != 2 or parts[0].startswith("_"):
            continue
        out[sha256_file(path)] = parts[1]
    return out


def reseed_watch_state(ws: Path) -> int:
    """After a restore: record every restored recording in <ws>/.watch_state.json by content hash,
    so the watcher skips a source file with the same bytes whatever its name or size key (#234).
    Additive; the name:size keys a restored state file carries stay. Returns hashes added."""
    p = ws / ".watch_state.json"
    try:
        state = json.loads(p.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state.setdefault("processed", {})
    hashes = state.setdefault("hashes", {})
    added = 0
    for sha, name in recording_hashes(ws).items():
        if sha not in hashes:
            hashes[sha] = {"name": name, "at": now_iso(), "restored": True}
            added += 1
    if added or not p.exists():
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(p)
    return added


def pick_manifest(target: Target, identity: str | None, at: str | None) -> dict | None:
    if not at or at == "latest":
        return read_manifest(target, identity)
    keys = target.list_keys()
    want = at.strip().replace("-", "").replace(":", "")
    hits = [k for k in dated_manifests(keys)
            if PurePosixPath(k).name.replace("-", "").startswith(want)
            or PurePosixPath(k).name.startswith(at.strip())]
    if not hits:
        stamps = ", ".join(PurePosixPath(k).stem for k in dated_manifests(keys)[-5:]) or "none"
        raise BackupError(f"no manifest matches --at {at} (newest: {stamps}); see `whosaid backup status`")
    if not identity:
        raise BackupError("encrypted backup: pass --identity FILE")
    return decrypt_manifest_key(target, hits[-1], identity)


def restore_dest(ws: Path, rel: str, tier: str) -> Path | None:
    """Where a manifest entry lands; None for the registry and the watcher snapshot (handled apart)."""
    if tier == "biometric" or rel == STATE_WATCHER:
        return None
    if rel.startswith("state/workspace/"):
        return ws / safe_rel(rel[len("state/workspace/"):])
    if rel.startswith("state/drafts/"):
        return drafts_root() / safe_rel(rel[len("state/drafts/"):])
    return ws / safe_rel(rel)


def same_content(dest: Path, fe: dict) -> bool:
    if sha256_file(dest) == fe["sha256"]:
        return True
    if fe.get("stripped") and dest.suffix.lower() == ".json":
        # a local file with voiceprints whose stripped form is the backed-up copy: identical, and
        # never overwritten (that would drop the local vectors)
        try:
            body, _n = stripped_body(dest)
        except (OSError, ValueError):
            return False
        return body is not None and hashlib.sha256(body).hexdigest() == fe["sha256"]
    return False


def cmd_restore(args) -> int:
    ws = Path(args.ws).expanduser().resolve()
    # an existing destination's [backup] table (e.g. s3_endpoint) applies to the read too
    target = getattr(args, "target_obj", None) or open_target(args.target, load_config(ws).get("backup") or {})
    if target.local_root() and target.local_root() == ws:
        raise BackupError("restore target and workspace are the same folder")
    identity = args.identity or os.environ.get("WHOSAID_BACKUP_AGE_IDENTITY") or None
    manifest = pick_manifest(target, identity, getattr(args, "at", None))
    if manifest is None:
        raise BackupError(f"{target.describe()}: no whosaid backup or share here")
    encrypted = manifest.get("encrypted") == "age"
    if encrypted and not identity:
        raise BackupError("encrypted backup: pass --identity FILE")
    tiers = parse_tiers(args.tier, TIERS, default=",".join(TIERS)) if args.tier else list(TIERS)
    todo, same, conflicts, bio, watcher = [], 0, [], None, None
    for rel, fe in sorted((manifest.get("files") or {}).items()):
        if fe.get("tier") not in tiers:
            continue
        if fe.get("tier") == "biometric":
            bio = (rel, fe)
            continue
        if rel == STATE_WATCHER:
            watcher = (rel, fe)
            continue
        dest = restore_dest(ws, rel, fe.get("tier"))
        if dest.is_file():
            if same_content(dest, fe):
                same += 1
                continue
            if not args.force:
                conflicts.append(rel)
                continue
        todo.append((rel, fe, dest))
    if conflicts:
        for c in conflicts[:20]:
            log(f"conflict: {c} differs from the backup")
        raise BackupError(f"{len(conflicts)} file(s) in {ws} differ from the backup; pass --force to overwrite")
    bio_dest = None
    if bio:
        bio_dest = Path(args.registry_out).expanduser() if args.registry_out else registry_path()
        if bio_dest.exists() and not args.registry_out:
            log(f"registry {bio_dest} exists; not overwritten. Pass --registry-out FILE, then merge it with "
                "`WHOSAID_SPEAKER_DB=FILE whosaid speakers export --out X.json && "
                "whosaid speakers import X.json --merge`")
            bio_dest = None
        elif bio_dest.exists():
            raise BackupError(f"--registry-out {bio_dest} exists; pick a new path")
    watcher_dest = ws / STATE_DIR / "restored-watcher.json" if watcher else None
    total = sum(fe["size"] for _, fe, _ in todo)
    if args.json and args.dry_run:
        print(json.dumps({"kind": manifest.get("kind"), "restore": [r for r, _, _ in todo], "unchanged": same,
                          "bytes": total, "registry": str(bio_dest) if bio_dest else None,
                          "watcher": str(watcher_dest) if watcher_dest else None}, indent=1))
        return 0
    for rel, fe, dest in todo:
        shown = rel if dest == ws / rel else f"{rel} -> {dest}"
        print(f"{'would restore' if args.dry_run else 'restore'} {shown}  ({fe.get('tier')}, {human(fe['size'])})")
    if bio_dest:
        print(f"{'would restore' if args.dry_run else 'restore'} speaker registry -> {bio_dest}")
    if watcher_dest:
        print(f"{'would restore' if args.dry_run else 'restore'} watcher settings -> {watcher_dest}")
    log(f"restore {manifest.get('kind')} {target.describe()} -> {ws}: {len(todo)} file(s) ({human(total)}), "
        f"{same} already identical")
    if args.dry_run:
        return 0
    ws.mkdir(parents=True, exist_ok=True)
    items = list(todo) + ([(bio[0], bio[1], bio_dest)] if bio_dest else []) \
        + ([(watcher[0], watcher[1], watcher_dest)] if watcher_dest else [])
    pairs = [(str(manifest.get("workspace_path") or ""), str(ws)),
             (str(manifest.get("home") or ""), str(Path.home()))]
    with tempfile.TemporaryDirectory(prefix="whosaid-restore-") as td:
        for rel, fe, dest in items:
            obj = fe.get("object") or rel
            raw = Path(td) / "obj"
            target.get_to_file(obj, raw)
            if encrypted:
                plain = Path(td) / "plain"
                age_decrypt(raw, plain, identity)
                raw.unlink()
                raw = plain
                unpad(raw, fe)
            if sha256_file(raw) != fe["sha256"]:
                raise BackupError(f"{rel}: content hash does not match the manifest (corrupt or tampered object)")
            if rel == STATE_WATCHER:
                snap = json.loads(raw.read_text())
                # longest first, so /old/home/transcripts maps before /old/home
                extra = [(a, str(ws)) for a in snap.get("workspace_aliases") or []]
                both = sorted(extra + pairs, key=lambda pr: -len(pr[0]))
                snap = map_roots(snap, both)
                snap["workspace_aliases"] = []
                raw.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if is_private(rel, fe.get("tier") or ""):
                os.chmod(raw, 0o600)
            shutil.move(str(raw), dest)
    reseeded = reseed_watch_state(ws) if manifest.get("kind") == "backup" else 0
    log(f"✓ restored {len(items)} file(s) into {ws}"
        + ("; run `whosaid index " + str(ws) + "` to rebuild search" if "_search.db" not in
           {r for r, _, _ in todo} else ""))
    if reseeded:
        log(f"  watcher state: {reseeded} restored recording(s) recorded by content hash; the watcher will not "
            "re-ingest them")
    if watcher_dest:
        try:
            snap = json.loads(watcher_dest.read_text())
            for a in snap.get("agents") or []:
                log(f"  watcher {a.get('Label')}: settings saved to {watcher_dest} (not loaded). Reinstall with "
                    f"`whosaid watch install --into {ws}` and the source/env listed there")
        except (OSError, ValueError):
            pass
    return 0


# ---- share ---------------------------------------------------------------------------

def strip_voiceprints(obj):
    """Deep copy of a JSON value without voiceprint keys; returns (clean, removed_count)."""
    removed = 0
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in VOICEPRINT_KEYS:
                removed += 1
                continue
            out[k], n = strip_voiceprints(v)
            removed += n
        return out, removed
    if isinstance(obj, list):
        out = []
        for v in obj:
            c, n = strip_voiceprints(v)
            out.append(c)
            removed += n
        return out, removed
    return obj, 0


def is_chat_folder(folder: Path) -> bool:
    if (folder / "teams.speakers.txt").is_file():
        return True
    for sc in folder.glob("*.diarization.json"):
        try:
            kind = ((json.loads(sc.read_text()).get("source") or {}).get("kind") or "")
        except (OSError, ValueError, AttributeError):
            continue
        if str(kind).startswith("teams"):
            return True
    return False


def select_meetings(ws: Path, cfg: dict, args) -> tuple[list[str], dict]:
    never = set((cfg.get("share") or {}).get("never") or [])
    wanted = None
    if args.meetings:
        wanted = {m.strip().rstrip("/") for m in args.meetings.split(",") if m.strip()}
        missing = sorted(m for m in wanted if not (ws / m).is_dir())
        if missing:
            raise BackupError(f"--meetings names folder(s) not in the workspace: {', '.join(missing)}")
    since = None
    if args.since:
        try:
            since = dt.date.fromisoformat(args.since)
        except ValueError:
            raise BackupError(f"--since needs YYYY-MM-DD, got '{args.since}'")
    excluded = {"noshare": [], "retired": [], "config": [], "chat": [], "filtered": []}
    chosen = []
    for d in sorted(p for p in ws.iterdir() if p.is_dir()):
        name = d.name
        if name.startswith((".", "_")):
            continue
        if (d / "_noshare").exists():
            excluded["noshare"].append(name)
        elif (d / "_retired").exists():
            excluded["retired"].append(name)
        elif name in never:
            excluded["config"].append(name)
        elif wanted is not None and name not in wanted:
            excluded["filtered"].append(name)
        elif since is not None and (folder_date(name) is None or folder_date(name) < since):
            excluded["filtered"].append(name)
        elif args.exclude_chat and is_chat_folder(d):
            excluded["chat"].append(name)
        else:
            chosen.append(name)
    if (ws / "_retired").is_dir():
        excluded["retired"] += sorted(p.name for p in (ws / "_retired").iterdir() if p.is_dir())
    return chosen, excluded


def plan_share(ws: Path, target: Target, tiers: list[str], meetings: list[str], excluded: dict,
               prev: dict | None, keys: set[str], cache: HashCache) -> Plan:
    plan = Plan("share", target.describe(), tiers, False)
    plan.meetings = meetings
    plan.excluded = {k: v for k, v in excluded.items() if v}
    complete = not any(excluded.values())
    chosen = set(meetings)
    selected: dict[str, Entry] = {}
    stripped = 0
    for rel, path, tier in walk_workspace(ws):
        if tier not in tiers:
            continue
        parts = PurePosixPath(rel).parts
        if len(parts) == 1:
            if not (complete and SHARE_ROOT_ALLOW.match(rel)) and not (tier == "index" and complete):
                continue
        elif parts[0] not in chosen or parts[-1] in ("_noshare", "_retired"):
            continue
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                plan.notes.append(f"skipped unreadable JSON {rel} (cannot prove it holds no voiceprints)")
                continue
            clean, n = strip_voiceprints(data)
            if n:
                stripped += 1
                body = (json.dumps(clean, indent=2) + "\n").encode()
                selected[rel] = Entry(rel, tier, len(body), hashlib.sha256(body).hexdigest(), data=body)
                continue
        size, sha = cache.sha(rel, path)
        selected[rel] = Entry(rel, tier, size, sha, path=path)
    if stripped:
        plan.notes.append(f"voiceprints removed from {stripped} JSON file(s)")
    if not complete:
        plan.notes.append("workspace roll-ups and index left out (some meetings stay local); "
                          "the receiver rebuilds them with `whosaid index` / `whosaid roll-up`")
    prev_files = (prev or {}).get("files") or {}
    for rel, e in selected.items():
        pe = prev_files.get(rel)
        ((plan.unchanged if pe and pe.get("sha256") == e.sha256 and rel in keys else plan.upload)
         .append(e))
    plan.delete = sorted(r for r in prev_files if r not in selected and r in keys)
    plan._selected = selected  # type: ignore[attr-defined]
    return plan


def cmd_share(args) -> int:
    ws = Path(args.ws).expanduser().resolve()
    if not ws.is_dir():
        raise BackupError(f"workspace not found: {ws}")
    cfg = load_config(ws)
    target = open_target(args.to, cfg.get("backup") or {})
    guard_target(ws, target)
    tiers = parse_tiers(args.tier, SHARE_TIERS)
    kind = target_kind(target)
    if kind == "backup":
        raise BackupError(f"{target.describe()} holds a personal backup; never share into it")
    prev = read_manifest(target) if kind == "share" else None
    keys = target.list_keys()
    meetings, excluded = select_meetings(ws, cfg, args)
    if args.prune_older_than:
        cutoff = dt.date.today() - parse_duration(args.prune_older_than)
        old = [m for m in meetings if folder_date(m) and folder_date(m) < cutoff]
        meetings = [m for m in meetings if m not in old]
        excluded["pruned"] = old
    cache = HashCache(ws)
    plan = plan_share(ws, target, tiers, meetings, excluded, prev, keys, cache)
    cache.save()
    # last line of defense: nothing biometric or audio-by-accident goes out
    for e in plan.upload + plan.unchanged:
        if e.tier == "biometric" or (e.tier == "audio" and "audio" not in tiers):
            raise BackupError(f"internal: {e.rel} ({e.tier}) selected for a share; refusing")
        if e.rel.endswith(".json"):
            body = e.data if e.data is not None else e.path.read_bytes()
            if any(f'"{k}"'.encode() in body for k in ("cluster_emb",)):
                raise BackupError(f"internal: {e.rel} still holds voiceprints; refusing")

    if args.json and args.dry_run:
        print(json.dumps(plan.summary(), indent=1))
        return 0
    first = prev is None
    if first and not NOTICE_ACK.exists():
        print(NOTICE, file=sys.stderr)
    print_plan(plan, args.dry_run or (first and not args.yes))
    if args.dry_run:
        return 0
    if first and not args.yes:
        log(f"first share to {target.describe()}: the files above would leave this machine. "
            "Re-run with --yes to send them.")
        return 3
    for e in plan.upload:
        if e.data is not None:
            target.put_bytes(e.rel, e.data)
        else:
            target.put_file(e.rel, e.path)
    for k in plan.delete:
        target.delete(k)
    selected = plan._selected  # type: ignore[attr-defined]
    history = list((prev or {}).get("history") or [])
    history.append({"at": now_iso(), "meetings": len(meetings), "uploaded": len(plan.upload),
                    "deleted": len(plan.delete)})
    manifest = {
        "schema": SCHEMA, "kind": "share", "workspace": ws.name,
        "created": (prev or {}).get("created") or now_iso(), "updated": now_iso(),
        "encrypted": None, "layout": "tree", "tiers": tiers,
        "meetings": meetings,
        "excluded_counts": {k: len(v) for k, v in excluded.items() if v},
        "files": {rel: {"sha256": e.sha256, "size": e.size, "tier": e.tier, "object": rel}
                  for rel, e in sorted(selected.items())},
        "history": history[-50:],
    }
    target.put_bytes(SHARE_MANIFEST, json.dumps(manifest, indent=1).encode())
    if first:
        try:
            NOTICE_ACK.parent.mkdir(parents=True, exist_ok=True)
            NOTICE_ACK.write_text(now_iso() + "\n")
        except OSError:
            pass
    st = load_local_state(ws, target)
    st["last_share"] = now_iso()
    save_local_state(ws, target, st)
    if args.json:
        print(json.dumps({**plan.summary(), "ok": True}, indent=1))
    log(f"✓ shared {len(meetings)} meeting(s) to {target.describe()}: {len(plan.upload)} uploaded, "
        f"{len(plan.delete)} removed. Teammates: whosaid share pull {target.describe()} <dir> && "
        "whosaid index <dir> --no-embed")
    return 0


# ---- scheduled pass (the watcher) ----------------------------------------------------

def interval_seconds(spec) -> float | None:
    if spec in (None, "", False, "off", "never"):
        return None
    named = {"hourly": 3600, "daily": 86400, "weekly": 7 * 86400}
    if isinstance(spec, str) and spec.lower() in named:
        return float(named[spec.lower()])
    return parse_duration(str(spec)).total_seconds()


def scheduled_pass(ws: Path, cfg: dict, dry: bool = False, now: float | None = None) -> str:
    """[backup] interval: run one backup when the last one is older than the interval, then a
    verify when [backup] verify is due, then raise alarms (stale backup, stale full verify,
    missing age). Returns status lines; never raises (a backup problem must not stop ingest)."""
    bcfg = cfg.get("backup") or {}
    try:
        every = interval_seconds(bcfg.get("interval"))
    except BackupError as e:
        return f"backup: skipped ({e})"
    if every is None:
        return ""
    if not bcfg.get("to"):
        return "backup: skipped ([backup] interval is set but [backup] to is not)"
    now = time.time() if now is None else now
    lines: list[str] = []
    try:
        target = open_target(str(bcfg["to"]), bcfg)
    except BackupError as e:
        return f"backup: failed ({e})"
    try:
        st = load_local_state(ws, target)
        encrypted = not (bcfg.get("plaintext") is True)
        has_age = bool(os.environ.get("WHOSAID_AGE") or shutil.which("age"))
        last = parse_iso(st.get("last_backup"))
        if encrypted and not has_age:
            # not "will retry": nothing changes until age is installed; the alarm says so once
            if not dry and (st.get("last_failure") or {}).get("error", "").find("age") < 0:
                record_failure(ws, target, "`age` is not installed (brew install age)")
            lines.append("backup: blocked, `age` is not installed (brew install age); see `whosaid backup status`")
        elif last is None or now - last >= every:
            ns = argparse.Namespace(ws=str(ws), to=str(bcfg["to"]), tier=bcfg.get("tier"), dry_run=dry,
                                    recipient=None, recipients_file=None, plaintext=False, identity=None,
                                    prune_older_than=bcfg.get("prune_older_than"), json=False, keep=None,
                                    cfg=cfg)
            try:
                cmd_backup(ns)
                lines.append(f"backup: {'would run' if dry else 'ran'} to {target.describe()}")
            except (BackupError, OSError, subprocess.SubprocessError) as e:
                if not dry:
                    record_failure(ws, target, str(e))
                lines.append(f"backup: failed ({e}); will retry next pass")
        vspec = bcfg.get("verify")
        vevery = interval_seconds(vspec) if vspec not in (None, False, "off") else None
        st = load_local_state(ws, target)
        lv = parse_iso((st.get("last_verify") or {}).get("at"))
        if vevery and not dry and st.get("last_backup") and (encrypted and has_age or not encrypted) \
                and (lv is None or now - lv >= vevery):
            ident = resolve_identity(argparse.Namespace(identity=None), bcfg)
            vns = argparse.Namespace(ws=str(ws), to=str(bcfg["to"]), identity=ident, sample=None,
                                     full=bool(ident) or not encrypted, query=None, tier="text", json=False,
                                     cfg=cfg)
            try:
                with _quiet():
                    rc = cmd_verify(vns)
                lines.append(f"backup verify: {'ok' if rc == 0 else 'FAILED, see `whosaid backup status`'}")
            except (BackupError, OSError, subprocess.SubprocessError) as e:
                lines.append(f"backup verify: failed ({e})")
        if not dry:
            lines += raise_alarms(ws, target, compute_alarms(ws, cfg, load_local_state(ws, target), now), now)
    except (BackupError, OSError, subprocess.SubprocessError) as e:
        lines.append(f"backup: failed ({e}); will retry next pass")
    return "\n".join(lines)


# ---- verify, status, alarms (#234) ---------------------------------------------------

VERIFY_SAMPLE = 20
FULL_VERIFY_MAX_AGE = 8 * 86400


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def configured_target(args, bcfg: dict) -> Target:
    spec = getattr(args, "to", None) or bcfg.get("to") or ""
    if not spec:
        raise BackupError("no target: pass --to TARGET or set [backup] to in whosaid.toml")
    return open_target(str(spec), bcfg)


def known_query(ws: Path) -> str | None:
    """A word from the first transcript turn, for a search smoke test after a full restore."""
    for p in sorted(ws.glob("*/*.speakers.txt")):
        try:
            for line in p.read_text(errors="replace").splitlines():
                m = re.match(r"^\[[^\]]+\]\s+[^:]+:\s+(.*)$", line)
                if not m:
                    continue
                words = sorted({w for w in re.findall(r"[A-Za-z]{5,}", m.group(1))}, key=lambda w: (-len(w), w))
                if words:
                    return words[0].lower()
        except OSError:
            continue
    return None


def search_hits(ws: Path, q: str) -> int:
    import search
    search.build(ws, embed=False)
    _engine, hits, _reason = search.query(ws, q, mode="exact", k=5)
    return len(hits)


def cmd_verify(args) -> int:
    """Read the backup back from the target: the manifest itself (with an identity), every
    object's presence, a sample of objects hash-checked, counts per tier against the workspace,
    and with --full a restore into a temp dir plus a known search."""
    import random
    ws = Path(args.ws).expanduser().resolve()
    cfg = getattr(args, "cfg", None) or load_config(ws)
    bcfg = cfg.get("backup") or {}
    target = configured_target(args, bcfg)
    identity = resolve_identity(args, bcfg)
    problems: list[str] = []
    notes: list[str] = []
    with BackupLock(ws):
        keys = target.list_keys()
        if BACKUP_MANIFEST in keys:
            manifest, encrypted = read_manifest(target), False
        elif BACKUP_MANIFEST + ".age" in keys:
            encrypted = True
            if identity:
                manifest = read_manifest(target, identity)
            else:
                manifest = load_local_state(ws, target).get("manifest")
                notes.append("no --identity: the manifest was not read back from the target, objects are "
                             "checked by ciphertext hash only")
                if manifest is None:
                    raise BackupError("encrypted backup and no local record of it: pass --identity FILE")
        else:
            raise BackupError(f"{target.describe()}: no whosaid backup here")
        files = manifest.get("files") or {}
        by_obj: dict[str, tuple[str, dict]] = {}
        for rel, fe in files.items():
            by_obj.setdefault(fe.get("object") or rel, (rel, fe))
        missing = sorted(o for o in by_obj if o not in keys)
        for o in missing[:20]:
            problems.append(f"missing on the target: {o} ({by_obj[o][0]})")
        if len(missing) > 20:
            problems.append(f"... and {len(missing) - 20} more missing object(s)")
        present = sorted(o for o in by_obj if o in keys)
        n = getattr(args, "sample", None)
        n = VERIFY_SAMPLE if n is None else int(n)
        chosen = present if n <= 0 or n >= len(present) else random.sample(present, n)
        checked = unchecked = 0
        with tempfile.TemporaryDirectory(prefix="whosaid-verify-") as td:
            for o in chosen:
                rel, fe = by_obj[o]
                raw = Path(td) / "obj"
                try:
                    target.get_to_file(o, raw)
                except BackupError as e:
                    problems.append(f"read back failed: {o} ({e})")
                    continue
                ok = None
                if encrypted:
                    if fe.get("csha"):
                        ok = sha256_file(raw) == fe["csha"]
                    if identity:
                        plain = Path(td) / "plain"
                        try:
                            age_decrypt(raw, plain, identity)
                            unpad(plain, fe)
                            ok = (ok is not False) and sha256_file(plain) == fe["sha256"]
                        except BackupError as e:
                            problems.append(f"{o} ({rel}): {e}")
                            ok = False
                        plain.unlink(missing_ok=True)
                else:
                    ok = sha256_file(raw) == fe["sha256"]
                raw.unlink(missing_ok=True)
                if ok is None:
                    unchecked += 1
                elif ok:
                    checked += 1
                else:
                    problems.append(f"hash mismatch on read back: {o} ({rel})")
        if unchecked:
            notes.append(f"{unchecked} object(s) predate ciphertext hashes and need --identity to check")
        # counts per tier: backup vs workspace; files waiting for the next pass are not a problem
        tiers = [t for t in (manifest.get("tiers") or ["text"]) if t in TIERS]
        cache = HashCache(ws)
        plan, local = plan_backup(ws, target, tiers, encrypted, manifest, keys, cache,
                                  (manifest or {}).get("names") if encrypted else None)
        cache.save()
        counts = {}
        for t in tiers:
            counts[t] = {"backup": sum(1 for fe in files.values() if fe.get("tier") == t),
                         "local": sum(1 for e in local.values() if e.tier == t),
                         "pending": sum(1 for e in plan.upload if e.tier == t)}
        full = None
        if getattr(args, "full", False):
            full = {"ok": False}
            with tempfile.TemporaryDirectory(prefix="whosaid-verify-full-") as td:
                dest = Path(td) / "ws"
                old = {k: os.environ.get(k) for k in ("WHOSAID_DRAFTS_DIR",)}
                os.environ["WHOSAID_DRAFTS_DIR"] = str(Path(td) / "drafts")
                try:
                    ns = argparse.Namespace(target=target.describe(), target_obj=target, ws=str(dest), identity=identity,
                                            tier=getattr(args, "tier", None), registry_out=str(Path(td) / "reg.json"),
                                            force=False, dry_run=False, json=False, at=None)
                    with _quiet():
                        cmd_restore(ns)
                    restored = sum(1 for _ in dest.rglob("*") if _.is_file())
                    full.update({"restored_files": restored})
                    q = getattr(args, "query", None) or bcfg.get("verify_query") or known_query(dest)
                    if q:
                        hits = search_hits(dest, q)
                        full.update({"query": q, "hits": hits})
                        if hits < 1:
                            problems.append(f"full restore: search for '{q}' found nothing")
                    else:
                        notes.append("full restore holds no transcript to search")
                    full["ok"] = not problems
                except BackupError as e:
                    problems.append(f"full restore failed: {e}")
                finally:
                    for k, v in old.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
    res = {"target": target.describe(), "ok": not problems, "objects": len(by_obj), "missing": len(missing),
           "sampled": len(chosen), "checked": checked, "counts": counts, "full": full,
           "problems": problems, "notes": notes, "at": now_iso()}
    st = load_local_state(ws, target)
    st["last_verify"] = {k: res[k] for k in ("at", "ok", "sampled", "checked", "missing", "problems")}
    st["last_verify"]["full"] = bool(full)
    if full and res["ok"]:
        st["last_full_verify"] = res["at"]
    save_local_state(ws, target, st)
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        for t, c in counts.items():
            log(f"  {t}: {c['backup']} in the backup, {c['local']} local, {c['pending']} waiting for the next pass")
        for nmsg in notes:
            log(f"  note: {nmsg}")
        for pmsg in problems:
            log(f"  PROBLEM {pmsg}")
        if full and full.get("query"):
            log(f"  full restore: {full.get('restored_files')} file(s); search '{full['query']}': {full['hits']} hit(s)")
        log(f"{'✓' if res['ok'] else '✗'} verify {target.describe()}: {len(by_obj)} object(s), {len(missing)} missing, "
            f"{checked}/{len(chosen)} read back and hash-checked" + ("; full restore ok" if full and res["ok"] else ""))
    return 0 if res["ok"] else 1


class _quiet:
    def __enter__(self):
        import contextlib
        import io
        self._out, self._err = io.StringIO(), io.StringIO()
        self._cms = [contextlib.redirect_stdout(self._out), contextlib.redirect_stderr(self._err)]
        for c in self._cms:
            c.__enter__()
        return self

    def __exit__(self, *exc):
        for c in reversed(self._cms):
            c.__exit__(*exc)


def notify(title: str, message: str) -> None:
    """A macOS notification (never takes focus). WHOSAID_NO_NOTIFY=1 or a non-mac host: log only."""
    if os.environ.get("WHOSAID_NO_NOTIFY") or sys.platform != "darwin" or not shutil.which("osascript"):
        return
    try:
        subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                          "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run",
                          message, title], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def compute_alarms(ws: Path, cfg: dict, state: dict, now: float | None = None) -> list[dict]:
    """What is wrong with this workspace's backup right now: [{key, msg}]."""
    bcfg = cfg.get("backup") or {}
    if bcfg.get("alarms") is False:
        return []
    now = time.time() if now is None else now
    out = []
    encrypted = not (bcfg.get("plaintext") is True)
    if encrypted and not (os.environ.get("WHOSAID_AGE") or shutil.which("age")):
        out.append({"key": "age-missing",
                    "msg": "backup: `age` is not installed, so no encrypted backup can run (brew install age)"})
    try:
        every = interval_seconds(bcfg.get("interval"))
    except BackupError:
        every = None
    if every:
        last = parse_iso(state.get("last_backup"))
        since = last or parse_iso((state.get("last_failure") or {}).get("first_at"))
        if since is not None and now - since > 2 * every:
            age_h = (now - since) / 3600
            what = "the last good backup" if last else "no good backup yet; failing since"
            out.append({"key": "backup-stale",
                        "msg": f"backup: {what} {age_h:.0f}h ago (interval {bcfg.get('interval')}): "
                               "see `whosaid backup status`"})
        first = parse_iso(state.get("first_backup"))
        full = parse_iso(state.get("last_full_verify"))
        if first is not None and now - first > FULL_VERIFY_MAX_AGE and (full is None or now - full > FULL_VERIFY_MAX_AGE):
            out.append({"key": "verify-stale",
                        "msg": "backup: no full verify in 8 days: run `whosaid backup verify <ws> --full` or set "
                               "[backup] verify = \"weekly\""})
    return out


def raise_alarms(ws: Path, target: Target, alarms: list[dict], now: float | None = None) -> list[str]:
    """Record alarms in the local state; notify a new one (age-missing once, others again after
    a day). Returns the lines to log this pass."""
    now = time.time() if now is None else now
    st = load_local_state(ws, target)
    seen = st.get("alarms") or {}
    out, cur = [], {}
    for a in alarms:
        prev = seen.get(a["key"]) or {}
        cur[a["key"]] = {"since": prev.get("since") or now_iso(), "msg": a["msg"],
                         "notified": prev.get("notified")}
        last = parse_iso(prev.get("notified"))
        repeat = a["key"] != "age-missing" and last is not None and now - last > 86400
        if last is None or repeat:
            notify("whosaid backup", a["msg"])
            cur[a["key"]]["notified"] = now_iso()
            out.append("ALARM " + a["msg"])
    if cur != seen:
        st["alarms"] = cur
        save_local_state(ws, target, st)
    return out


def record_failure(ws: Path, target: Target, err: str) -> None:
    st = load_local_state(ws, target)
    prev = st.get("last_failure") or {}
    st["last_failure"] = {"at": now_iso(), "error": err, "first_at": prev.get("first_at") or now_iso()}
    save_local_state(ws, target, st)


def cmd_status(args) -> int:
    ws = Path(args.ws).expanduser().resolve()
    cfg = load_config(ws)
    bcfg = cfg.get("backup") or {}
    target = configured_target(args, bcfg)
    st = load_local_state(ws, target)
    info = {"workspace": str(ws), "target": target.describe(),
            "interval": bcfg.get("interval"), "tiers": st.get("tiers") or bcfg.get("tier") or "text",
            "age": os.environ.get("WHOSAID_AGE") or shutil.which("age"),
            "last_backup": st.get("last_backup"), "last_failure": st.get("last_failure"),
            "by_tier": st.get("by_tier") or {}, "last_verify": st.get("last_verify"),
            "last_full_verify": st.get("last_full_verify"), "last_gc": st.get("last_gc"),
            "alarms": compute_alarms(ws, cfg, st)}
    try:
        keys = target.list_keys()
        dated = dated_manifests(keys)
        info["remote"] = {"manifests": len(dated), "oldest": PurePosixPath(dated[0]).stem if dated else None,
                          "newest": PurePosixPath(dated[-1]).stem if dated else None,
                          "objects": sum(1 for k in keys if k.startswith("objects/")),
                          "legacy_single_manifest": bool(keys & {BACKUP_MANIFEST, BACKUP_MANIFEST + ".age"})
                          and not dated}
    except BackupError as e:
        info["remote"] = {"error": str(e)}
    if args.json:
        print(json.dumps(info, indent=1))
        return 0
    print(f"target:           {info['target']}")
    print(f"interval:         {info['interval'] or 'off (no scheduled pass)'}")
    print(f"age:              {info['age'] or 'NOT INSTALLED'}")
    print(f"last backup:      {info['last_backup'] or 'never'}")
    lf = info["last_failure"]
    print(f"last failure:     {(lf['at'] + ': ' + lf['error']) if lf else 'none'}")
    for t, v in sorted(info["by_tier"].items()):
        print(f"  {t:10s}      {v['files']} file(s), {human(v['bytes'])}")
    lv = info["last_verify"]
    print(f"last verify:      {(lv['at'] + (' ok' if lv['ok'] else ' FAILED') + (' (full)' if lv.get('full') else '')) if lv else 'never'}")
    print(f"last full verify: {info['last_full_verify'] or 'never'}")
    r = info["remote"]
    if "error" in r:
        print(f"remote:           unreachable ({r['error']})")
    else:
        print(f"remote:           {r['objects']} object(s), {r['manifests']} dated manifest(s)"
              + (f" {r['oldest']} .. {r['newest']}" if r["manifests"] else ""))
    for a in info["alarms"]:
        print(f"ALARM {a['msg']}")
    return 0


# ---- init (#234) ----------------------------------------------------------------------

INIT_TIERS = "text,audio,state,biometric"


def keygen_bin() -> str:
    b = os.environ.get("WHOSAID_AGE_KEYGEN") or shutil.which("age-keygen")
    if not b:
        raise BackupError("backup init needs `age-keygen` (brew install age)")
    return b


def public_key_of(identity: Path) -> str:
    proc = subprocess.run([keygen_bin(), "-y", str(identity)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BackupError(f"age-keygen -y {identity} failed: {proc.stderr.strip()}")
    return proc.stdout.strip().splitlines()[0]


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def write_backup_table(ws: Path, values: dict) -> None:
    """Replace the [backup] table of <ws>/whosaid.toml (other tables and comments untouched)."""
    p = ws / "whosaid.toml"
    text = p.read_text() if p.is_file() else ""
    out, skipping = [], False
    for line in text.splitlines():
        head = re.match(r"^\s*\[\[?([^\[\]]+)\]\]?\s*(#.*)?$", line)
        if head:
            skipping = head.group(1).strip() == "backup" and not line.lstrip().startswith("[[")
            if skipping:
                continue
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    body = ["[backup]"] + [f"{k} = {toml_value(v)}" for k, v in values.items() if v not in (None, "", [])]
    new = ("\n".join(out) + "\n\n" if out else "") + "\n".join(body) + "\n"
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(new)
    tmp.replace(p)


def raw_backup_table(ws: Path) -> dict:
    p = ws / "whosaid.toml"
    if not p.is_file():
        return {}
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return dict(load_config(ws).get("backup") or {})
    try:
        return dict(tomllib.loads(p.read_text()).get("backup") or {})
    except (OSError, ValueError) as e:
        raise BackupError(f"{p}: not valid TOML ({e})")


def cmd_init(args) -> int:
    """One command to a trusted backup: age present, a daily identity (0600), a recovery identity
    printed once for offline storage, [backup] with both recipients, a dry run, the first run, a
    read-back check of every object, then the schedule."""
    ws = Path(args.ws).expanduser().resolve()
    if not ws.is_dir():
        raise BackupError(f"workspace not found: {ws}")
    age_bin()
    keygen_bin()
    table = raw_backup_table(ws)
    if any(isinstance(v, dict) for v in table.values()):
        raise BackupError("whosaid.toml has nested [backup.*] tables; set up [backup] by hand (see the README)")
    to = args.to or table.get("to")
    if not to:
        raise BackupError("backup init needs --to TARGET (a path, s3://bucket/prefix or rclone:REMOTE:path)")
    open_target(str(to), table)      # validate the spec before writing anything
    ident = Path(args.identity or table.get("identity") or
                 Path.home() / ".config" / "whosaid" / "backup.key").expanduser()
    tiers = args.tier or table.get("tier") or INIT_TIERS
    parse_tiers(tiers, TIERS)
    if args.dry_run:
        log(f"init (dry run): would {'reuse' if ident.exists() else 'create'} the daily identity {ident} (0600), "
            "create a recovery identity and print it once, write [backup] to whosaid.toml, back up "
            f"{tiers} to {to}, read every object back, then set interval = {args.interval!r}")
        return 0
    if not ident.exists():
        ident.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([keygen_bin(), "-o", str(ident)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise BackupError(f"age-keygen failed: {proc.stderr.strip()}")
        log(f"created the daily identity {ident}")
    os.chmod(ident, 0o600)
    daily = public_key_of(ident)
    existing = table.get("recipients") or []
    existing = [existing] if isinstance(existing, str) else [str(r) for r in existing]
    recovery = args.recovery_recipient
    if not recovery:
        others = [r for r in existing if r != daily]
        if others:
            recovery = others[0]
            log("recovery recipient already in [backup] recipients; keeping it")
    if not recovery:
        proc = subprocess.run([keygen_bin()], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise BackupError(f"age-keygen failed: {proc.stderr.strip()}")
        secret = proc.stdout
        recovery = next(ln.split(": ", 1)[1] for ln in secret.splitlines() if ln.startswith("# public key: "))
        print("=" * 72)
        print("RECOVERY IDENTITY: store this offline (paper, password manager). It is shown once and")
        print("never written to disk. With only this, `whosaid backup restore TARGET <ws> --identity FILE`")
        print("brings the workspace back on a wiped Mac.")
        print("-" * 72)
        print(secret.rstrip())
        print("=" * 72)
    recips = list(dict.fromkeys([daily, recovery] + [r for r in existing if r not in (daily, recovery)]))
    values = {k: v for k, v in table.items() if k not in ("to", "tier", "recipients", "recipient", "identity",
                                                         "interval", "verify")}
    values = {"to": str(to), "tier": tiers, "recipients": recips, "identity": str(ident), **values}
    write_backup_table(ws, values)
    log(f"wrote [backup] to {ws / 'whosaid.toml'}: {len(recips)} recipient(s), tiers {tiers}")
    cfg = load_config(ws)
    base = {"ws": str(ws), "to": "", "tier": None, "recipient": None, "recipients_file": None, "plaintext": False,
            "identity": None, "prune_older_than": None, "json": False, "keep": None, "cfg": cfg}
    if cmd_backup(argparse.Namespace(**{**base, "dry_run": True})) != 0:
        raise BackupError("dry run failed; [backup] written, schedule not turned on")
    cmd_backup(argparse.Namespace(**{**base, "dry_run": False}))
    vns = argparse.Namespace(ws=str(ws), to="", identity=None, sample=0, full=False, query=None, tier=None,
                             json=False, cfg=cfg)
    if cmd_verify(vns) != 0:
        raise BackupError("read-back check failed; schedule not turned on. Fix the target and run "
                          "`whosaid backup verify` again")
    values.update({"interval": args.interval, "verify": "weekly"})
    write_backup_table(ws, values)
    log(f"✓ backup init: first backup read back clean; scheduled {args.interval} (the watcher runs it), "
        "full verify weekly. `whosaid backup status` shows it")
    return 0


# ---- CLI -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whosaid", description="Workspace backup and team share (#180).")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="personal backup of a workspace")
    b.add_argument("ws")
    b.add_argument("--to", default="")
    b.add_argument("--tier", help="comma list of text,index,audio,state,biometric (default text)")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--recipient", action="append", help="age public key (repeatable)")
    b.add_argument("--recipients-file")
    b.add_argument("--identity", help="age identity, to read an existing encrypted manifest")
    b.add_argument("--plaintext", action="store_true", help="no encryption (refused for biometric)")
    b.add_argument("--prune-older-than", help="drop meetings older than DUR (90d, 12w, 1y) from the target")
    b.add_argument("--keep", type=int, help=f"dated manifests to keep on the target (default {DEFAULT_KEEP})")
    b.add_argument("--json", action="store_true")

    v = sub.add_parser("verify", help="read a backup back from its target and check it")
    v.add_argument("ws")
    v.add_argument("--to", default="")
    v.add_argument("--identity")
    v.add_argument("--sample", type=int, help=f"objects to read back (default {VERIFY_SAMPLE}; 0 = all)")
    v.add_argument("--full", action="store_true", help="also restore into a temp dir and run a search")
    v.add_argument("--query", help="search the full restore for this (default: [backup] verify_query or a "
                                   "word from a transcript)")
    v.add_argument("--tier")
    v.add_argument("--json", action="store_true")

    g = sub.add_parser("gc", help="delete objects no kept manifest references")
    g.add_argument("ws")
    g.add_argument("--to", default="")
    g.add_argument("--identity")
    g.add_argument("--keep", type=int)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="last backup, failure, size per tier, verify, alarms")
    st.add_argument("ws")
    st.add_argument("--to", default="")
    st.add_argument("--json", action="store_true")

    i = sub.add_parser("init", help="set up an encrypted, verified, scheduled backup")
    i.add_argument("ws")
    i.add_argument("--to", default="")
    i.add_argument("--tier", help=f"default {INIT_TIERS}")
    i.add_argument("--interval", default="daily")
    i.add_argument("--identity", help="daily age identity (default ~/.config/whosaid/backup.key)")
    i.add_argument("--recovery-recipient", help="age1... public key of an offline recovery identity you already hold")
    i.add_argument("--dry-run", action="store_true")

    r = sub.add_parser("restore", help="restore a backup or pull a share")
    r.add_argument("target")
    r.add_argument("ws")
    r.add_argument("--identity")
    r.add_argument("--tier")
    r.add_argument("--registry-out")
    r.add_argument("--at", help="restore the backup as of a dated manifest (stamp prefix, e.g. 20261101)")
    r.add_argument("--force", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--json", action="store_true")

    s = sub.add_parser("share", help="read-only export for teammates")
    s.add_argument("ws")
    s.add_argument("--to", required=True)
    s.add_argument("--meetings")
    s.add_argument("--since")
    s.add_argument("--exclude-chat", action="store_true")
    s.add_argument("--tier", help="comma list of text,index,audio (default text)")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--prune-older-than")
    s.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `backup restore ...` and `share pull ...` map onto the restore parser
    if argv[:2] == ["backup", "restore"]:
        argv = ["restore", *argv[2:]]
    elif argv[:1] == ["backup"] and argv[1:2] and argv[1] in ("verify", "gc", "status", "init"):
        argv = argv[1:]
    elif argv[:2] == ["share", "pull"]:
        argv = ["restore", *argv[2:]]
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "backup":
            return cmd_backup(args)
        if args.cmd == "restore":
            return cmd_restore(args)
        if args.cmd in ("verify", "gc", "status", "init"):
            return {"verify": cmd_verify, "gc": cmd_gc, "status": cmd_status, "init": cmd_init}[args.cmd](args)
        return cmd_share(args)
    except BackupError as e:
        log(f"{args.cmd}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

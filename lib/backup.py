#!/usr/bin/env python3
"""
backup.py: remote backup and team share of a meeting workspace (GitHub issue #180).

A workspace is one local folder. This module copies it somewhere else, split by
sensitivity into tiers:

  text       transcripts, *.speakers.txt, sidecars, roll-ups (_ACTION-ITEMS,
             _COMMITMENTS, _WIKI, ...), chat-day folders, whosaid.toml    default
  index      _search.db (rebuildable: `whosaid index` on the receiving side) opt-in
  audio      recordings (.m4a, .wav, ...) and <base>.samples/ clips          opt-in
  biometric  the speaker registry (speakers.json, voiceprints), which lives
             outside the workspace; personal encrypted backup only, never shared

Commands (the `whosaid` bash CLI execs this module):

  backup <ws> --to TARGET [--tier text,index,audio,biometric] [--dry-run]
         [--recipient AGE_PUBKEY ... | --recipients-file F | --plaintext]
         [--prune-older-than DUR] [--json]
      A full personal copy. Encrypted at rest by default: every file is
      encrypted client-side with `age` before it leaves the machine and stored
      as objects/<sha256>.age; the manifest (file names, hashes, tiers) is
      encrypted too. --plaintext writes a browsable copy instead (refused for
      the biometric tier). Incremental: only files whose content hash the
      target lacks are uploaded. Additive: a file deleted locally stays in the
      backup until --prune-older-than drops its meeting.

  backup restore TARGET <ws> [--identity F] [--tier ...] [--registry-out F]
         [--force] [--dry-run] [--json]
      Pull a backup (or a share) into <ws>, verifying every file's sha256.
      An existing file with different content is a conflict: nothing is
      overwritten without --force.

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
  identity = "~/.config/whosaid/backup.key"   # for restore (or WHOSAID_BACKUP_AGE_IDENTITY)
  plaintext = false
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
STATE_DIR = ".backup"                       # dot-dir: never itself backed up
NOTICE_ACK = Path.home() / ".config" / "whosaid" / "share-notice-acknowledged"

TIERS = ("text", "index", "audio", "biometric")
SHARE_TIERS = ("text", "index", "audio")
AUDIO_EXT = {".m4a", ".wav", ".mp3", ".mp4", ".aac", ".flac", ".ogg", ".opus", ".webm",
             ".caf", ".aif", ".aiff", ".mov", ".m4v", ".mkv", ".wma", ".amr", ".3gp"}
INDEX_NAMES = {"_search.db", "_search.db-wal", "_search.db-shm", "_search.db-journal"}
SKIP_NAMES = {".DS_Store", "Thumbs.db", "__pycache__", BACKUP_MANIFEST, BACKUP_MANIFEST + ".age",
              SHARE_MANIFEST}
# workspace-root files a share may carry (only when every meeting is shared)
SHARE_ROOT_ALLOW = re.compile(
    r"^(_ACTION-ITEMS\.md|_action-items\.json|_COMMITMENTS\.md|_commitments\.json|_WIKI\.md"
    r"|_INDEX\.md|_WORKLIST-[^/]+\.md|_tracking\.json|_topics\.json)$")
# JSON keys that hold voiceprints (biometric vectors); stripped from every shared JSON file
VOICEPRINT_KEYS = {"cluster_emb", "embedding", "embeddings", "centroid", "centroids",
                   "voiceprint", "voiceprints"}
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


def plan_backup(ws: Path, target: Target, tiers: list[str], encrypted: bool,
                prev: dict | None, keys: set[str], cache: HashCache) -> tuple[Plan, dict]:
    plan = Plan("backup", target.describe(), tiers, encrypted)
    files = {}
    for rel, path, tier in walk_workspace(ws):
        if tier not in tiers:
            continue
        size, sha = cache.sha(rel, path)
        files[rel] = Entry(rel, tier, size, sha, path=path)
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
            present = f"objects/{e.sha256}.age" in keys
        else:
            pe = prev_files.get(rel)
            present = bool(pe) and pe.get("sha256") == e.sha256 and rel in keys
        (plan.unchanged if present else plan.upload).append(e)
    return plan, files


def object_key(e_sha: str, rel: str, encrypted: bool) -> str:
    return f"objects/{e_sha}.age" if encrypted else rel


def cmd_backup(args) -> int:
    ws = Path(args.ws).expanduser().resolve()
    if not ws.is_dir():
        raise BackupError(f"workspace not found: {ws}")
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
                          "set [backup] recipients in whosaid.toml, or pass --plaintext for an unencrypted copy")
    if plaintext and "biometric" in tiers:
        raise BackupError("the biometric tier (voiceprints) is only backed up encrypted; drop --plaintext")
    if encrypted:
        age_bin()
    kind = target_kind(target)
    if kind == "share":
        raise BackupError(f"{target.describe()} holds a share, not a backup; use a separate target")
    keys = target.list_keys()
    if encrypted and BACKUP_MANIFEST in keys:
        raise BackupError(f"{target.describe()} holds a plaintext backup; use a new target for an encrypted one")
    if not encrypted and BACKUP_MANIFEST + ".age" in keys:
        raise BackupError(f"{target.describe()} holds an encrypted backup; drop --plaintext or use a new target")
    # previous manifest: plaintext from the target, encrypted from the local state cache
    if encrypted:
        prev = load_local_state(ws, target).get("manifest")
        if prev is None and BACKUP_MANIFEST + ".age" in keys:
            ident = getattr(args, "identity", None) or os.environ.get("WHOSAID_BACKUP_AGE_IDENTITY") \
                or bcfg.get("identity")
            if ident:
                prev = read_manifest(target, ident)
            else:
                log("WARN no local record of this encrypted backup (and no --identity): entries for files "
                    "deleted locally since the last backup will drop out of the new manifest")
    else:
        prev = read_manifest(target) if BACKUP_MANIFEST in keys else None
    cache = HashCache(ws)
    plan, files = plan_backup(ws, target, tiers, encrypted, prev, keys, cache)
    cache.save()
    if not encrypted and any(r.endswith(".diarization.json") or r == "_voice-groups.json" for r in files):
        plan.notes.append("plaintext: diarization sidecars and _voice-groups.json carry per-meeting "
                          "voiceprints (cluster_emb), unencrypted on this target; encrypt with --recipient "
                          "for anything you do not physically control")

    # additive: keep earlier entries for files no longer on disk
    manifest_files = {}
    for rel, pe in ((prev or {}).get("files") or {}).items():
        if rel not in files:
            manifest_files[rel] = pe
    for rel, e in files.items():
        manifest_files[rel] = {"sha256": e.sha256, "size": e.size, "tier": e.tier,
                               "object": object_key(e.sha256, rel, encrypted)}
    if args.prune_older_than:
        cutoff = dt.date.today() - parse_duration(args.prune_older_than)
        dropped = [rel for rel in manifest_files
                   if (meeting_of(rel) and folder_date(meeting_of(rel)) and folder_date(meeting_of(rel)) < cutoff)]
        for rel in dropped:
            manifest_files.pop(rel)
        plan.upload = [e for e in plan.upload if e.rel not in dropped]
        plan.unchanged = [e for e in plan.unchanged if e.rel not in dropped]
        live = {v["object"] for v in manifest_files.values()}
        candidates = keys - {BACKUP_MANIFEST, BACKUP_MANIFEST + ".age"}
        if encrypted:
            candidates = {k for k in candidates if k.startswith("objects/")}
        else:
            candidates = {v.get("object", r) for r, v in ((prev or {}).get("files") or {}).items()} & keys
        plan.delete = sorted(candidates - live)
        if dropped:
            plan.notes.append(f"pruned {len(dropped)} file(s) in meetings dated before {cutoff}")

    if args.json and args.dry_run:
        print(json.dumps(plan.summary(), indent=1))
        return 0
    print_plan(plan, args.dry_run)
    if args.dry_run:
        return 0

    with tempfile.TemporaryDirectory(prefix="whosaid-backup-") as td:
        seen = set()
        for e in plan.upload:
            key = object_key(e.sha256, e.rel, encrypted)
            if key in seen:
                continue
            seen.add(key)
            if encrypted:
                enc = Path(td) / "obj.age"
                age_encrypt(e.path, enc, recips, rfile)
                target.put_file(key, enc)
                enc.unlink()
            else:
                target.put_file(key, e.path)
        manifest = {
            "schema": SCHEMA, "kind": "backup", "workspace": ws.name,
            "created": (prev or {}).get("created") or now_iso(), "updated": now_iso(),
            "encrypted": "age" if encrypted else None,
            "layout": "objects" if encrypted else "tree",
            "tiers": sorted(set(tiers) | set((prev or {}).get("tiers") or []), key=TIERS.index),
            "files": dict(sorted(manifest_files.items())),
        }
        body = json.dumps(manifest, indent=1).encode()
        if encrypted:
            plain, enc = Path(td) / "m.json", Path(td) / "m.age"
            plain.write_bytes(body)
            age_encrypt(plain, enc, recips, rfile)
            target.put_file(BACKUP_MANIFEST + ".age", enc)
        else:
            target.put_bytes(BACKUP_MANIFEST, body)
        for k in plan.delete:
            target.delete(k)
    state = load_local_state(ws, target)
    state.update({"last_backup": now_iso(), "manifest": manifest if encrypted else None})
    save_local_state(ws, target, state)
    if args.json:
        print(json.dumps({**plan.summary(), "ok": True}, indent=1))
    log(f"✓ backup complete: {len(plan.upload)} uploaded, {len(manifest_files)} file(s) in the backup")
    return 0


# ---- restore -------------------------------------------------------------------------

def cmd_restore(args) -> int:
    ws = Path(args.ws).expanduser().resolve()
    target = open_target(args.target)
    if target.local_root() and target.local_root() == ws:
        raise BackupError("restore target and workspace are the same folder")
    identity = args.identity or os.environ.get("WHOSAID_BACKUP_AGE_IDENTITY") or None
    manifest = read_manifest(target, identity)
    if manifest is None:
        raise BackupError(f"{target.describe()}: no whosaid backup or share here")
    encrypted = manifest.get("encrypted") == "age"
    if encrypted and not identity:
        raise BackupError("encrypted backup: pass --identity FILE")
    tiers = parse_tiers(args.tier, TIERS, default=",".join(TIERS)) if args.tier else list(TIERS)
    todo, same, conflicts, bio = [], 0, [], None
    for rel, fe in sorted((manifest.get("files") or {}).items()):
        if fe.get("tier") not in tiers:
            continue
        if fe.get("tier") == "biometric":
            bio = (rel, fe)
            continue
        dest = ws / safe_rel(rel)
        if dest.is_file():
            if sha256_file(dest) == fe["sha256"]:
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
            log(f"registry {bio_dest} exists; not overwritten. Pass --registry-out FILE, then "
                "`whosaid speakers import FILE --merge`")
            bio_dest = None
        elif bio_dest.exists():
            raise BackupError(f"--registry-out {bio_dest} exists; pick a new path")
    total = sum(fe["size"] for _, fe, _ in todo)
    if args.json and args.dry_run:
        print(json.dumps({"kind": manifest.get("kind"), "restore": [r for r, _, _ in todo], "unchanged": same,
                          "bytes": total, "registry": str(bio_dest) if bio_dest else None}, indent=1))
        return 0
    for rel, fe, _ in todo:
        print(f"{'would restore' if args.dry_run else 'restore'} {rel}  ({fe.get('tier')}, {human(fe['size'])})")
    if bio_dest:
        print(f"{'would restore' if args.dry_run else 'restore'} speaker registry -> {bio_dest}")
    log(f"restore {manifest.get('kind')} {target.describe()} -> {ws}: {len(todo)} file(s) ({human(total)}), "
        f"{same} already identical")
    if args.dry_run:
        return 0
    ws.mkdir(parents=True, exist_ok=True)
    items = list(todo) + ([(bio[0], bio[1], bio_dest)] if bio_dest else [])
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
            if sha256_file(raw) != fe["sha256"]:
                raise BackupError(f"{rel}: content hash does not match the manifest (corrupt or tampered object)")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(raw), dest)
    log(f"✓ restored {len(items)} file(s) into {ws}"
        + ("; run `whosaid index " + str(ws) + "` to rebuild search" if "_search.db" not in
           {r for r, _, _ in todo} else ""))
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
    """[backup] interval: run one backup when the last one is older than the interval.
    Returns a one-line status; never raises (a backup problem must not stop ingest)."""
    bcfg = cfg.get("backup") or {}
    try:
        every = interval_seconds(bcfg.get("interval"))
    except BackupError as e:
        return f"backup: skipped ({e})"
    if every is None:
        return ""
    if not bcfg.get("to"):
        return "backup: skipped ([backup] interval is set but [backup] to is not)"
    try:
        target = open_target(str(bcfg["to"]), bcfg)
        last = load_local_state(ws, target).get("last_backup")
        now = time.time() if now is None else now
        if last:
            age = now - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc).timestamp()
            if age < every:
                return ""
        ns = argparse.Namespace(ws=str(ws), to=str(bcfg["to"]), tier=bcfg.get("tier"), dry_run=dry,
                                recipient=None, recipients_file=None, plaintext=False, identity=None,
                                prune_older_than=bcfg.get("prune_older_than"), json=False, cfg=cfg)
        cmd_backup(ns)
        return f"backup: {'would run' if dry else 'ran'} to {target.describe()}"
    except (BackupError, OSError, subprocess.SubprocessError) as e:
        return f"backup: failed ({e}); will retry next pass"


# ---- CLI -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whosaid", description="Workspace backup and team share (#180).")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="personal backup of a workspace")
    b.add_argument("ws")
    b.add_argument("--to", default="")
    b.add_argument("--tier", help="comma list of text,index,audio,biometric (default text)")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--recipient", action="append", help="age public key (repeatable)")
    b.add_argument("--recipients-file")
    b.add_argument("--identity", help="age identity, to read an existing encrypted manifest")
    b.add_argument("--plaintext", action="store_true", help="no encryption (refused for biometric)")
    b.add_argument("--prune-older-than", help="drop meetings older than DUR (90d, 12w, 1y) from the target")
    b.add_argument("--json", action="store_true")

    r = sub.add_parser("restore", help="restore a backup or pull a share")
    r.add_argument("target")
    r.add_argument("ws")
    r.add_argument("--identity")
    r.add_argument("--tier")
    r.add_argument("--registry-out")
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
    elif argv[:2] == ["share", "pull"]:
        argv = ["restore", *argv[2:]]
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "backup":
            return cmd_backup(args)
        if args.cmd == "restore":
            return cmd_restore(args)
        return cmd_share(args)
    except BackupError as e:
        log(f"{args.cmd}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

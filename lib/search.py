#!/usr/bin/env python3
"""
search.py: full-text and meaning search over a whosaid meeting workspace (issue #14).

Every *.speakers.txt in every immediate subfolder of the workspace (dated or
hand-named, see wsconfig.iter_meetings) is split into speaker turns and indexed
in one SQLite file, <workspace>/_search.db:

  seg   FTS5 (porter unicode61): meeting, source, speaker, t_sec, t_str, line, text
  emb   key -> float32 little-endian unit vector (Ollama embeddings, optional)
  meta  key/value: built_at, embed_model, segments, meetings, speakers, embedded, ...

graph.py owns the entity tables in the same file and reads `seg`; this module
only ever drops or creates seg, emb and meta.

Subcommands (`<ws>` may be omitted anywhere: then $WHOSAID_WORKSPACE, else the
current directory when it holds _workspace.json or whosaid.toml):

  build    <ws> [--no-embed] [--rebuild]
      Rebuild `seg` from the transcripts inside one transaction (readers keep
      seeing the old table until the commit), then embed the segments that
      have no vector yet via Ollama POST /api/embed (search.embed_model,
      default nomic-embed-text). Embedding keys are sha1(meeting|t_str|
      speaker|text), so vectors survive a rebuild and only new turns get
      embedded. Skipped, with a note on stderr and exit 0, when --no-embed,
      search.embed = false in whosaid.toml, or Ollama is not reachable.
      --rebuild drops the vectors too.

  query    <ws> "<query>" [--mode exact|meaning|hybrid] [--speaker S] [--meeting M] [-k N] [--json]
      exact:   FTS5 MATCH over the turn text (phrases in quotes, OR / NOT,
               prefix*), bm25 ranked, snippet with the match between » and «.
      meaning: embed the query, cosine against the stored vectors, top-k.
      hybrid:  reciprocal-rank fusion of both lists; each hit is tagged
               exact, meaning or both.
      meaning/hybrid fall back to exact (stderr: "engine: exact (fallback:
      <reason>)", exit 0) when no vectors exist or Ollama is down.
      --speaker / --meeting are case-insensitive substring filters.

  context  <ws> <meeting-folder> <HH:MM:SS|MM:SS> [--before 60] [--after 120] [--json]
      The verbatim turns of one meeting inside [at-before, at+after], in time
      order, as "[t_str] Speaker: text" lines. A unique substring of the
      folder name is accepted; an unknown meeting exits 1 and lists the
      known folders.

  speakers <ws> [--json]         turns and meetings per speaker
  status   <ws> [--json]         index counts, embed model, built_at, Ollama reachability

Exit codes: 0 ok, 1 runtime problem (message carries the fix, e.g.
"run: whosaid index <ws>" when the index is missing), 2 usage.

Output: human lines on stdout, diagnostics on stderr prefixed "whosaid: ".
--json prints one JSON document on stdout. Stdlib only; numpy is used for
the cosine pass when importable (WHOSAID_SEARCH_PURE=1 forces the pure
Python path). The only network peer is Ollama on the configured localhost URL.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import operator
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wsconfig import (  # noqa: E402
    iter_meetings, load_config, log, ollama_up, ollama_url, parse_turns,
    resolve_workspace, search_db, tsec,
)

try:
    import numpy as np  # optional: vectorized cosine over the embedding matrix
except ImportError:  # pragma: no cover
    np = None
if os.environ.get("WHOSAID_SEARCH_PURE"):
    np = None

SCHEMA_VERSION = "1"
EMBED_BATCH = 64            # segments per /api/embed call
EMBED_TIMEOUT_S = 180       # per batch; nomic on Apple Silicon does 64 turns in a few seconds
QUERY_EMBED_TIMEOUT_S = 30
BUSY_TIMEOUT_MS = 5000      # readers wait this long for a build's commit instead of failing
RRF_K = 60                  # reciprocal-rank fusion constant (the usual 60)
CANDIDATES = 100            # per-engine candidate depth feeding the fusion
SNIPPET_TOKENS = 12
TEXT_PREVIEW = 200          # human-readable meaning/hybrid lines truncate the turn here
MODES = ("exact", "meaning", "hybrid")

SEG_DDL = (
    "CREATE VIRTUAL TABLE seg USING fts5("
    "meeting, source, speaker, t_sec UNINDEXED, t_str UNINDEXED, line UNINDEXED, text, "
    "tokenize='porter unicode61')"
)
EMB_DDL = "CREATE TABLE IF NOT EXISTS emb(key TEXT PRIMARY KEY, model TEXT, dim INT, vec BLOB)"
META_DDL = "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)"
SEG_COLS = "rowid, meeting, source, speaker, t_sec, t_str, line, text"


class SearchError(Exception):
    """Runtime problem the user can fix; the message carries the one-line hint. Exit 1."""


@dataclass
class Hit:
    meeting: str
    t_sec: int
    t_str: str
    speaker: str
    text: str
    score: float
    source: str             # exact | meaning | both
    file: str               # the speakers file the turn came from
    line: int
    snippet: str | None     # FTS5 snippet with »match« markers (exact/both only)
    rowid: int = 0

    def as_json(self) -> dict:
        d = asdict(self)
        d.pop("rowid")
        return d


# ---- db plumbing --------------------------------------------------------------------

def seg_key(meeting: str, t_str: str, speaker: str, text: str) -> str:
    """Stable identity of one turn: the embedding cache key across rebuilds."""
    return hashlib.sha1(f"{meeting}|{t_str}|{speaker}|{text}".encode("utf-8")).hexdigest()


def index_hint(ws: Path) -> str:
    return f"no search index at {search_db(ws)}; run: whosaid index {ws}"


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row is not None


def open_index(ws: Path) -> sqlite3.Connection:
    """Read connection to an existing index; SearchError with the index hint otherwise."""
    db = search_db(ws)
    if not db.is_file():
        raise SearchError(index_hint(ws))
    conn = connect(db)
    if not has_table(conn, "seg"):
        conn.close()
        raise SearchError(index_hint(ws))
    return conn


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    if not has_table(conn, "meta"):
        return {}
    return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}


def set_meta(conn: sqlite3.Connection, **values) -> None:
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     [(k, str(v)) for k, v in values.items()])


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---- vectors ------------------------------------------------------------------------

_sumprod = getattr(math, "sumprod", None)  # Python 3.12+: C-speed dot product


def _dot(a, b) -> float:
    if _sumprod is not None:
        return _sumprod(a, b)
    return sum(map(operator.mul, a, b))


def normalize(vec) -> list[float]:
    n = math.sqrt(_dot(vec, vec)) or 1.0
    return [float(x) / n for x in vec]


def pack_vec(vec) -> bytes:
    a = array("f", vec)
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    return a.tobytes()


def unpack_vec(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    return a


def embed_texts(url: str, model: str, texts: list[str], timeout: float) -> list[list[float]]:
    """POST {url}/api/embed -> unit-normalized vectors, one per text. SearchError with a hint on failure."""
    if not texts:
        return []
    body = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(f"{url}/api/embed", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        hint = f" (run: ollama pull {model})" if "not found" in detail.lower() or e.code == 404 else ""
        raise SearchError(f"Ollama /api/embed failed: HTTP {e.code} {detail}".rstrip() + hint) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SearchError(f"Ollama not reachable at {url} ({e}); start it or set search.embed = false") from None
    except ValueError as e:
        raise SearchError(f"Ollama /api/embed returned no JSON ({e})") from None
    vecs = data.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise SearchError(f"Ollama /api/embed returned {0 if not isinstance(vecs, list) else len(vecs)} "
                          f"vectors for {len(texts)} inputs (model {model})")
    return [normalize(v) for v in vecs]


# ---- build --------------------------------------------------------------------------

def collect_segments(ws: Path) -> list[tuple]:
    """(meeting, source, speaker, t_sec, t_str, line, text) for every non-empty turn."""
    rows = []
    for meeting, files in iter_meetings(ws):
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                log(f"WARN skipping unreadable {path.name} in {meeting}: {e}")
                continue
            for t in parse_turns(text):
                if t.text:
                    rows.append((meeting, path.name, t.speaker, t.t_sec, t.t_str, t.line, t.text))
    return rows


def build(ws: Path, embed: bool = True, rebuild: bool = False, cfg: dict | None = None) -> dict:
    """Rebuild seg (one transaction), prune stale vectors, embed the missing ones. Returns the summary."""
    cfg = cfg or load_config(ws)
    if not ws.is_dir():
        raise SearchError(f"workspace not found: {ws}")
    db = search_db(ws)
    rows = collect_segments(ws)
    if not rows:
        log(f"WARN no *.speakers.txt turns under {ws}; the index will be empty")
    model = str(cfg["search"].get("embed_model") or "nomic-embed-text")
    url = ollama_url(cfg)

    conn = connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS seg")
            conn.execute(SEG_DDL)
            conn.executemany(
                "INSERT INTO seg(meeting, source, speaker, t_sec, t_str, line, text) VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            if rebuild:
                conn.execute("DROP TABLE IF EXISTS emb")
            conn.execute(EMB_DDL)
            conn.execute(META_DDL)
            keys = {seg_key(m, ts, spk, txt) for (m, _src, spk, _sec, ts, _ln, txt) in rows}
            # vectors from another model, or for turns that no longer exist, are dead weight
            other = conn.execute("SELECT COUNT(*) FROM emb WHERE model != ?", (model,)).fetchone()[0]
            if other:
                log(f"dropping {other} vector(s) from a different embed model")
                conn.execute("DELETE FROM emb WHERE model != ?", (model,))
            stale = [(k,) for (k,) in conn.execute("SELECT key FROM emb") if k not in keys]
            if stale:
                conn.executemany("DELETE FROM emb WHERE key = ?", stale)
            meetings = len({r[0] for r in rows})
            speakers = len({r[2] for r in rows})
            files = len({(r[0], r[1]) for r in rows})
            have = conn.execute("SELECT COUNT(*) FROM emb").fetchone()[0]
            set_meta(conn, built_at=now_iso(), embed_model=model, segments=len(rows), meetings=meetings,
                     speakers=speakers, source_files=files, embedded=have, ollama=url,
                     schema=SCHEMA_VERSION)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        summary = {
            "db": str(db), "segments": len(rows), "meetings": meetings, "speakers": speakers,
            "embedded_new": 0, "embedded_total": have, "embed_model": model, "embed": "skipped",
        }
        if not embed:
            summary["embed"] = "skipped (--no-embed)"
            return summary
        if not cfg["search"].get("embed", True):
            summary["embed"] = "skipped (search.embed = false)"
            return summary
        if not ollama_up(url):
            log(f"embeddings skipped: Ollama not reachable at {url} "
                "(start it, or set search.embed = false in whosaid.toml)")
            summary["embed"] = "skipped (Ollama down)"
            return summary

        existing = {k for (k,) in conn.execute("SELECT key FROM emb WHERE model = ?", (model,))}
        todo: dict[str, str] = {}
        for (m, _src, spk, _sec, ts, _ln, txt) in rows:
            k = seg_key(m, ts, spk, txt)
            if k not in existing and k not in todo:
                todo[k] = txt
        items = list(todo.items())
        done = 0
        t0 = time.monotonic()
        tty = sys.stderr.isatty()
        try:
            for i in range(0, len(items), EMBED_BATCH):
                batch = items[i:i + EMBED_BATCH]
                vecs = embed_texts(url, model, [t for _, t in batch], EMBED_TIMEOUT_S)
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "INSERT OR REPLACE INTO emb(key, model, dim, vec) VALUES (?,?,?,?)",
                    [(k, model, len(v), pack_vec(v)) for (k, _t), v in zip(batch, vecs)],
                )
                done += len(batch)
                set_meta(conn, embedded=len(existing) + done, dim=len(vecs[0]) if vecs else "")
                conn.execute("COMMIT")
                if tty and len(items) > EMBED_BATCH:
                    print(f"whosaid: embedding {done}/{len(items)} ...", end="\r", file=sys.stderr, flush=True)
            summary["embed"] = "ok"
        except SearchError as e:
            log(f"embeddings stopped after {done}/{len(items)}: {e}")
            summary["embed"] = f"partial ({e})"
        if tty and len(items) > EMBED_BATCH:
            print(" " * 60, end="\r", file=sys.stderr)
        summary["embedded_new"] = done
        summary["embedded_total"] = conn.execute("SELECT COUNT(*) FROM emb WHERE model = ?", (model,)).fetchone()[0]
        summary["embed_seconds"] = round(time.monotonic() - t0, 1)
        return summary
    finally:
        conn.close()


# ---- query: exact -------------------------------------------------------------------

def _filters(speaker: str | None, meeting: str | None) -> tuple[str, list]:
    where, params = "", []
    if speaker:
        where += " AND speaker LIKE ?"
        params.append(f"%{speaker}%")
    if meeting:
        where += " AND meeting LIKE ?"
        params.append(f"%{meeting}%")
    return where, params


def _quote_tokens(q: str) -> str:
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in q.split())


def exact_hits(conn: sqlite3.Connection, q: str, k: int, speaker: str | None = None,
               meeting: str | None = None) -> list[Hit]:
    """FTS5 MATCH on the text column, bm25 order. Tries, in order: the query scoped to the
    text column; the raw query (so callers can use their own column filters and operators);
    the query with every word quoted (so PR-42 or don't never trip the parser)."""
    q = q.strip()
    if not q:
        return []
    where, params = _filters(speaker, meeting)
    sql = (f"SELECT {SEG_COLS}, snippet(seg, 6, '»', '«', '…', {SNIPPET_TOKENS}), bm25(seg) "
           f"FROM seg WHERE seg MATCH ?{where} ORDER BY rank LIMIT ?")
    attempts = [f"text : ({q})", q, f"text : ({_quote_tokens(q)})"]
    if "{" in q or re.search(r"\w\s*:", q):
        attempts[0], attempts[1] = attempts[1], attempts[0]   # the caller wrote a column filter: raw first
    rows: list = []
    for n, match in enumerate(attempts):
        try:
            rows = conn.execute(sql, [match, *params, k]).fetchall()
        except sqlite3.OperationalError as e:
            # FTS5 parse trouble surfaces as "fts5: syntax error ..." or, for things like
            # PR-42 (a dash reads as a negated column filter), "no such column: 42".
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                raise SearchError(f"exact search failed: {e}; a build may be running, retry") from None
            if n == len(attempts) - 1:
                raise SearchError(f"FTS5 could not parse the query ({e}); quote the phrase") from None
            continue
        if n == len(attempts) - 1:
            log("exact: FTS5 could not parse the query as written; searching for the words themselves")
        break
    return [
        Hit(meeting=m, t_sec=int(sec), t_str=ts, speaker=spk, text=txt, score=round(-float(bm), 4),
            source="exact", file=src, line=int(ln), snippet=snip, rowid=rid)
        for (rid, m, src, spk, sec, ts, ln, txt, snip, bm) in rows or []
    ]


# ---- query: meaning -----------------------------------------------------------------

_CACHE: dict = {}   # one entry: (db, built_at, embedded, model) -> (segs, keys, matrix); reused by long-lived callers


def load_segments(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(f"SELECT {SEG_COLS} FROM seg ORDER BY rowid").fetchall()


def load_vectors(conn: sqlite3.Connection, db: Path, model: str):
    """(segments aligned with vectors, matrix) for the current index; cached per build within a process.
    The matrix is a numpy (n, dim) float32 array, or a list of array('f') rows without numpy."""
    meta = get_meta(conn)
    ck = (str(db), meta.get("built_at"), meta.get("embedded"), model, np is not None)
    hit = _CACHE.get(ck)
    if hit is not None:
        return hit
    segs = load_segments(conn)
    vec_by_key: dict[str, bytes] = {}
    dim = None
    for key, d, blob in conn.execute("SELECT key, dim, vec FROM emb WHERE model = ?", (model,)):
        if dim is None:
            dim = int(d)
        if int(d) == dim:
            vec_by_key[key] = blob
    aligned, blobs = [], []
    for row in segs:
        _rid, m, _src, spk, _sec, ts, _ln, txt = row
        blob = vec_by_key.get(seg_key(m, ts, spk, txt))
        if blob is not None:
            aligned.append(row)
            blobs.append(blob)
    if np is not None and blobs:
        matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), dim)
    else:
        matrix = [unpack_vec(b) for b in blobs]
    _CACHE.clear()
    _CACHE[ck] = (aligned, matrix)
    return _CACHE[ck]


def _top_indices(sims, n: int) -> list[int]:
    if np is not None and not isinstance(sims, list):
        if len(sims) <= n:
            return [int(i) for i in np.argsort(-sims)]
        part = np.argpartition(-sims, n - 1)[:n]
        return [int(i) for i in part[np.argsort(-sims[part])]]
    return heapq.nlargest(n, range(len(sims)), key=sims.__getitem__)


def meaning_hits(conn: sqlite3.Connection, db: Path, cfg: dict, q: str, k: int,
                 speaker: str | None = None, meeting: str | None = None) -> list[Hit]:
    """Cosine top-k over the stored vectors. SearchError (callers fall back to exact) when
    there are no vectors or Ollama cannot embed the query."""
    model = str(cfg["search"].get("embed_model") or "nomic-embed-text")
    url = ollama_url(cfg)
    if not has_table(conn, "emb") or \
            conn.execute("SELECT COUNT(*) FROM emb WHERE model = ?", (model,)).fetchone()[0] == 0:
        raise SearchError(f"no embeddings for {model}; run: whosaid index (with Ollama up)")
    if not ollama_up(url):
        raise SearchError(f"Ollama not reachable at {url}")
    segs, matrix = load_vectors(conn, db, model)
    if not segs:
        raise SearchError("no embeddings match the current segments; run: whosaid index")
    qv = embed_texts(url, model, [q], QUERY_EMBED_TIMEOUT_S)[0]
    spk_f = speaker.lower() if speaker else None
    mtg_f = meeting.lower() if meeting else None
    if np is not None and not isinstance(matrix, list):
        sims = matrix @ np.asarray(qv, dtype=np.float32)
    else:
        sims = [_dot(qv, row) for row in matrix]
    out: list[Hit] = []
    # over-fetch when filtering, then keep the first k that pass
    want = k if not (spk_f or mtg_f) else min(len(segs), max(k * 20, CANDIDATES))
    for i in _top_indices(sims, min(want, len(segs))):
        if sims[i] <= 0:      # descending order: nothing after this is related to the query
            break
        rid, m, src, spk, sec, ts, ln, txt = segs[i]
        if spk_f and spk_f not in spk.lower():
            continue
        if mtg_f and mtg_f not in m.lower():
            continue
        out.append(Hit(meeting=m, t_sec=int(sec), t_str=ts, speaker=spk, text=txt,
                       score=round(float(sims[i]), 4), source="meaning", file=src, line=int(ln),
                       snippet=None, rowid=rid))
        if len(out) >= k:
            break
    return out


def hybrid_hits(conn: sqlite3.Connection, db: Path, cfg: dict, q: str, k: int,
                speaker: str | None = None, meeting: str | None = None) -> list[Hit]:
    """Reciprocal-rank fusion of the exact and meaning candidate lists; tags exact/meaning/both."""
    sem = meaning_hits(conn, db, cfg, q, CANDIDATES, speaker, meeting)   # raises -> caller falls back
    ex = exact_hits(conn, q, CANDIDATES, speaker, meeting)
    sem_rank = {h.rowid: r for r, h in enumerate(sem)}
    ex_rank = {h.rowid: r for r, h in enumerate(ex)}
    by_id: dict[int, Hit] = {h.rowid: h for h in sem}
    for h in ex:
        if h.rowid in by_id:
            by_id[h.rowid].snippet = h.snippet
        else:
            by_id[h.rowid] = h
    fused = []
    for rid, h in by_id.items():
        score = 0.0
        if rid in sem_rank:
            score += 1.0 / (RRF_K + sem_rank[rid])
        if rid in ex_rank:
            score += 1.0 / (RRF_K + ex_rank[rid])
        h.score = round(score, 5)
        h.source = "both" if rid in sem_rank and rid in ex_rank else ("exact" if rid in ex_rank else "meaning")
        fused.append(h)
    fused.sort(key=lambda h: (-h.score, h.meeting, h.t_sec))
    return fused[:k]


def query(ws: Path, q: str, mode: str = "exact", speaker: str | None = None, meeting: str | None = None,
          k: int = 10, cfg: dict | None = None) -> tuple[str, list[Hit], str | None]:
    """(engine actually used, hits, fallback reason or None). meaning/hybrid degrade to exact."""
    if mode not in MODES:
        raise SearchError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    cfg = cfg or load_config(ws)
    conn = open_index(ws)
    db = search_db(ws)
    try:
        if mode == "exact":
            return "exact", exact_hits(conn, q, k, speaker, meeting), None
        try:
            if mode == "meaning":
                return "meaning", meaning_hits(conn, db, cfg, q, k, speaker, meeting), None
            return "hybrid", hybrid_hits(conn, db, cfg, q, k, speaker, meeting), None
        except SearchError as e:
            return "exact", exact_hits(conn, q, k, speaker, meeting), str(e)
    finally:
        conn.close()


# ---- context / speakers / status ----------------------------------------------------

def known_meetings(conn: sqlite3.Connection) -> list[str]:
    return [m for (m,) in conn.execute("SELECT DISTINCT meeting FROM seg ORDER BY meeting")]


def resolve_meeting(conn: sqlite3.Connection, name: str) -> str:
    names = known_meetings(conn)
    if name in names:
        return name
    cands = [n for n in names if name.lower() in n.lower()]
    if len(cands) == 1:
        log(f"context: {name!r} matched meeting {cands[0]}")
        return cands[0]
    listing = ", ".join(names) if names else "(index is empty)"
    if len(cands) > 1:
        raise SearchError(f"meeting {name!r} is ambiguous: {', '.join(cands)}")
    raise SearchError(f"unknown meeting {name!r}; known: {listing}")


def parse_at(tok: str) -> int:
    """'HH:MM:SS', 'MM:SS' or plain seconds -> seconds. ValueError when malformed."""
    tok = tok.strip()
    if not tok or not all(p.isdigit() for p in tok.split(":")) or tok.count(":") > 2:
        raise ValueError(f"expected HH:MM:SS, MM:SS or seconds, got {tok!r}")
    return tsec(tok)


def context(ws: Path, meeting: str, at: int, before: int = 60, after: int = 120) -> tuple[str, list[dict]]:
    conn = open_index(ws)
    try:
        name = resolve_meeting(conn, meeting)
        rows = conn.execute(
            "SELECT t_sec, t_str, speaker, text, line, source FROM seg "
            "WHERE meeting = ? AND t_sec BETWEEN ? AND ? ORDER BY source, t_sec, line",
            (name, max(0, at - before), at + after),
        ).fetchall()
    finally:
        conn.close()
    return name, [{"t_sec": int(s), "t_str": ts, "speaker": spk, "text": txt, "line": int(ln)}
                  for (s, ts, spk, txt, ln, _src) in rows]


def speakers(ws: Path) -> list[dict]:
    conn = open_index(ws)
    try:
        rows = conn.execute(
            "SELECT speaker, COUNT(*) AS n, COUNT(DISTINCT meeting) FROM seg "
            "GROUP BY speaker ORDER BY n DESC, speaker"
        ).fetchall()
    finally:
        conn.close()
    return [{"speaker": s, "turns": int(n), "meetings": int(m)} for (s, n, m) in rows]


def status(ws: Path, cfg: dict | None = None) -> dict:
    """Never raises for a missing index: 'exists' / 'indexed' say so and the CLI exits 1."""
    cfg = cfg or load_config(ws)
    db = search_db(ws)
    model = str(cfg["search"].get("embed_model") or "nomic-embed-text")
    url = ollama_url(cfg)
    out = {
        "workspace": str(ws), "db": str(db), "exists": db.is_file(), "indexed": False,
        "segments": 0, "meetings": 0, "speakers": 0, "embedded": 0, "embed_model": model,
        "embed_enabled": bool(cfg["search"].get("embed", True)), "built_at": None,
        "ollama": url, "ollama_up": ollama_up(url), "dim": None,
    }
    if not out["exists"]:
        return out
    conn = connect(db)
    try:
        if not has_table(conn, "seg"):
            return out
        out["indexed"] = True
        out["segments"] = conn.execute("SELECT COUNT(*) FROM seg").fetchone()[0]
        out["meetings"] = conn.execute("SELECT COUNT(DISTINCT meeting) FROM seg").fetchone()[0]
        out["speakers"] = conn.execute("SELECT COUNT(DISTINCT speaker) FROM seg").fetchone()[0]
        if has_table(conn, "emb"):
            out["embedded"] = conn.execute("SELECT COUNT(*) FROM emb WHERE model = ?", (model,)).fetchone()[0]
            row = conn.execute("SELECT dim FROM emb WHERE model = ? LIMIT 1", (model,)).fetchone()
            out["dim"] = int(row[0]) if row else None
        meta = get_meta(conn)
        out["built_at"] = meta.get("built_at")
        out["indexed_model"] = meta.get("embed_model")
    finally:
        conn.close()
    return out


# ---- CLI ----------------------------------------------------------------------------

def _ws(args: argparse.Namespace) -> Path:
    return resolve_workspace(getattr(args, "ws", None))


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _preview(text: str) -> str:
    return text if len(text) <= TEXT_PREVIEW else text[:TEXT_PREVIEW - 1] + "…"


def cmd_build(args: argparse.Namespace) -> int:
    ws = _ws(args)
    s = build(ws, embed=not args.no_embed, rebuild=args.rebuild)
    line = f"Indexed {s['segments']} segments · {s['meetings']} meetings · {s['speakers']} speakers → {s['db']}"
    if s["embed"] == "ok" or s["embed"].startswith("partial"):
        line += (f"; embedded {s['embedded_new']} new ({s['embedded_total']}/{s['segments']} total, "
                 f"{s['embed_model']}, {s.get('embed_seconds', 0)}s)")
    else:
        line += f"; embeddings {s['embed']}, {s['embedded_total']}/{s['segments']} stored"
    print(line)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    ws = _ws(args)
    if args.k < 1:
        log("query: -k must be at least 1")
        return 2
    engine, hits, reason = query(ws, args.query, mode=args.mode, speaker=args.speaker,
                                 meeting=args.meeting, k=args.k)
    log(f"engine: {engine}" + (f" (fallback: {reason})" if reason else ""))
    if args.json:
        _dump([h.as_json() for h in hits])
        return 0
    for h in hits:
        where = f"[{h.meeting} @ {h.t_str}] {h.speaker}:"
        if engine == "exact":
            print(f"{where} {h.snippet or _preview(h.text)}")
        elif engine == "meaning":
            print(f"[{h.score:.2f}] {where} {_preview(h.text)}")
        else:
            print(f"[{h.source:7s}] {where} {h.snippet or _preview(h.text)}")
    print(f"{len(hits)} hit(s).")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    ws = _ws(args)
    try:
        at = parse_at(args.at)
    except ValueError as e:
        log(f"context: {e}")
        return 2
    if args.before < 0 or args.after < 0:
        log("context: --before and --after must be >= 0")
        return 2
    name, turns = context(ws, args.meeting, at, before=args.before, after=args.after)
    if args.json:
        _dump(turns)
        return 0
    log(f"{name}: {len(turns)} turn(s) between {max(0, at - args.before)}s and {at + args.after}s")
    for t in turns:
        print(f"[{t['t_str']}] {t['speaker']}: {t['text']}")
    return 0


def cmd_speakers(args: argparse.Namespace) -> int:
    rows = speakers(_ws(args))
    if args.json:
        _dump(rows)
        return 0
    print(f"{'turns':>6}  {'meetings':>8}  speaker")
    for r in rows:
        print(f"{r['turns']:6d}  {r['meetings']:8d}  {r['speaker']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = _ws(args)
    s = status(ws)
    if args.json:
        _dump(s)
    else:
        print(f"workspace:  {s['workspace']}")
        print(f"db:         {s['db']} ({'present' if s['exists'] else 'missing'})")
        if s["indexed"]:
            print(f"segments:   {s['segments']}")
            print(f"meetings:   {s['meetings']}")
            print(f"speakers:   {s['speakers']}")
            dim = f", dim {s['dim']}" if s.get("dim") else ""
            print(f"embedded:   {s['embedded']}/{s['segments']} ({s['embed_model']}{dim})")
            print(f"built_at:   {s['built_at'] or 'unknown'}")
        print(f"embed:      {'enabled' if s['embed_enabled'] else 'disabled (search.embed = false)'}")
        print(f"ollama:     {s['ollama']} ({'reachable' if s['ollama_up'] else 'not reachable'})")
    if not s["indexed"]:
        log(index_hint(ws))
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search.py",
        description="whosaid workspace search: FTS5 exact search, Ollama meaning search, "
                    "hybrid fusion, and the turns around a hit (issue #14).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    ws_help = "workspace directory (default: $WHOSAID_WORKSPACE, else the current directory)"

    pb = sub.add_parser("build", help="(re)build _search.db from every *.speakers.txt, then embed new turns")
    pb.add_argument("ws", nargs="?", help=ws_help)
    pb.add_argument("--no-embed", action="store_true", help="skip the Ollama embedding pass")
    pb.add_argument("--rebuild", action="store_true", help="also drop the stored vectors and re-embed everything")
    pb.set_defaults(func=cmd_build)

    pq = sub.add_parser("query", help="search the transcripts")
    pq.add_argument("ws", nargs="?", help=ws_help)
    pq.add_argument("query", help="search text (exact mode takes FTS5 syntax: \"a phrase\", a OR b, NOT c, pre*)")
    pq.add_argument("--mode", choices=MODES, default="exact",
                    help="exact (FTS5), meaning (embeddings), or hybrid (both, fused); default exact")
    pq.add_argument("--speaker", default=None, help="only turns whose speaker label contains this")
    pq.add_argument("--meeting", default=None, help="only turns whose meeting folder contains this")
    pq.add_argument("-k", type=int, default=10, help="max hits (default 10)")
    pq.add_argument("--json", action="store_true", help="JSON array of hits on stdout")
    pq.set_defaults(func=cmd_query)

    pc = sub.add_parser("context", help="verbatim turns around a moment in one meeting")
    pc.add_argument("ws", nargs="?", help=ws_help)
    pc.add_argument("meeting", help="meeting folder name (or a unique substring of it)")
    pc.add_argument("at", help="moment as HH:MM:SS, MM:SS or seconds")
    pc.add_argument("--before", type=int, default=60, help="seconds before the moment (default 60)")
    pc.add_argument("--after", type=int, default=120, help="seconds after the moment (default 120)")
    pc.add_argument("--json", action="store_true", help="JSON array of turns on stdout")
    pc.set_defaults(func=cmd_context)

    ps = sub.add_parser("speakers", help="turns and meetings per speaker")
    ps.add_argument("ws", nargs="?", help=ws_help)
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_speakers)

    pst = sub.add_parser("status", help="index counts, embed model, built_at, Ollama reachability")
    pst.add_argument("ws", nargs="?", help=ws_help)
    pst.add_argument("--json", action="store_true")
    pst.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SearchError as e:
        log(str(e))
        return 1
    except sqlite3.OperationalError as e:
        log(f"database error: {e} (if a build is running, retry; else run: whosaid index)")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
graph.py: entity tables and views over a whosaid meeting workspace (GitHub
issue #14; the per-owner commitments view of issue #13; issue #23 unifies the
commitment table with the CM corpus).

Not a graph database: six small linked tables inside the workspace's shared
<ws>/_search.db (lib/search.py owns seg/emb/meta there; this module owns, and
only ever drops/creates, the tables below). Everything is derived from what
whosaid itself writes, so nothing is invented:

  person        speakers from the seg index (turns, meetings, named?)
  meeting       one row per meeting folder: _workspace.json manifest entries
                plus every folder holding *.speakers.txt (dated or hand-named)
  action_item   the deduplicated corpus in _action-items.json (AI-NNN ids)
  occurrence    where each AI-NNN shows up: corpus occurrences plus any AI-NNN
                reference written in a per-meeting action-items.md
  commitment    the CM-NNN corpus (_commitments.json, rolled up by
                lib/workspace.py) flattened to one row per occurrence: source
                (transcript or action-items), meeting/line/t_sec, owner,
                requester (+role), text, cue, negation, plus the item-level
                status, ai_refs (JSON array of AI-NNN ids) and merged_into
  pr_mention    pull-request numbers, spoken ("pr 42") in the transcripts or
                written ("PR #42") in the action-item documents

Usage (the `whosaid graph ...` / `whosaid wiki` bash front ends shell out here):

  python3 lib/graph.py build    [ws]
  python3 lib/graph.py items    [ws] [--owner X] [--requester R] [--status S] [--type T] [--json]
  python3 lib/graph.py item     [ws] AI-NNN [--json]
  python3 lib/graph.py commitments [ws] [--owner X] [--source transcript|action-items] [--status S] [--json]
  python3 lib/graph.py person   [ws] [Name] [--json]      (alias: speakers)
  python3 lib/graph.py prs      [ws] [--json]
  python3 lib/graph.py meetings [ws] [--json]
  python3 lib/graph.py wiki     [ws] [--stdout] [-o FILE]

[ws] is optional (nargs="?"): when omitted, $WHOSAID_WORKSPACE or the current
directory is used (wsconfig.resolve_workspace). `person Name` with no
workspace works too: a first argument that is not an existing directory is
taken as the name.

The commitment table is the CM corpus verbatim (issue #23): it is NOT a
re-parse of per-meeting action-items.md bullets. lib/workspace.py's roll-up
owns extraction, dedup and the stable CM-NNN ids; `graph build` only flattens
what it wrote — one table row per corpus occurrence. Corpus fields are read
defensively (missing keys become empty/null), so corpora written before a
field existed still load; a missing _commitments.json leaves the table empty.
ai_refs is the corpus-computed link to AI-NNN action items.

Exit codes: 0 ok, 1 runtime problem (missing index, unknown id; stderr says what
to run), 2 usage. Stdlib only, offline, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wsconfig  # noqa: E402
from wsconfig import log  # noqa: E402

BUSY_TIMEOUT_S = 30.0
CORPUS_NAME = "_action-items.json"
CM_CORPUS_NAME = "_commitments.json"
CORPUS_MD_NAME = "_ACTION-ITEMS.md"
WIKI_NAME = "_WIKI.md"
MEETING_MD_NAME = "action-items.md"

TABLES = ("person", "meeting", "action_item", "occurrence", "commitment", "pr_mention")
SCHEMA = (
    "CREATE TABLE person(name TEXT PRIMARY KEY, turns INT, meetings INT, named INT)",
    "CREATE TABLE meeting(folder TEXT PRIMARY KEY, source_name TEXT, created TEXT, "
    "duration_s REAL, minutes INT, dated INT, has_action_items INT, segments INT)",
    "CREATE TABLE action_item(id TEXT PRIMARY KEY, text TEXT, owner TEXT, status TEXT, "
    "type TEXT, first_seen TEXT, last_seen TEXT, merged_into TEXT, occurrences INT)",
    "CREATE TABLE occurrence(id TEXT, meeting TEXT, line INT)",
    "CREATE TABLE commitment(id TEXT, source TEXT, meeting TEXT, line INT, t_sec INT, "
    "owner TEXT, requester TEXT, requester_role TEXT, text TEXT, cue TEXT, negative INT, "
    "status TEXT, ai_refs TEXT, merged_into TEXT)",
    "CREATE TABLE pr_mention(pr INT, source TEXT, meeting TEXT, detail TEXT)",
)

# Mirrors workspace.DATE_DIR_RE (test/graph_test.py guards against drift) so this
# module does not import the roll-up at runtime.
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(?:-\d+)?$")
AI_RE = re.compile(r"\bAI-\d{3,}\b")
PR_SPOKEN_RE = re.compile(r"\b(\d{2,5})\s*pr\b|\bpr\s*#?(\d{2,5})\b", re.IGNORECASE)
PR_DOC_RE = re.compile(r"(?:PR\s*#|#)(\d{2,5})\b")
STATUS_ORDER = ("open", "ongoing", "contingent", "resolved")


class GraphError(Exception):
    """A runtime problem with a one-line fix hint; cmd_* turn it into exit 1."""


def fmt_t(t_sec) -> str:
    """Seconds -> 'H:MM:SS' (or 'MM:SS' under an hour); '' for no timestamp.
    The commitment table stores t_sec only; every human view formats through
    here so the corpus timestamps and the display never drift apart."""
    if not isinstance(t_sec, (int, float)):
        return ""
    s = int(t_sec)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def iso_utc(raw) -> str | None:
    """Sidecar creation_time (ISO 8601, 'Z' or offset) -> 'YYYY-MM-DDTHH:MM:SSZ';
    unparseable strings are kept as written."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    t = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    t = t.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes_of(duration_s: float | None) -> int | None:
    return None if duration_s is None else int(round(duration_s / 60.0))


def same_person(a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a.lower() == b.lower():
        return True
    fa, fb = wsconfig.speaker_first_name(a).lower(), wsconfig.speaker_first_name(b).lower()
    return len(fa) >= 2 and fa == fb


def name_match(field: str, query: str) -> bool:
    """Case-insensitive prefix/substring on the label, or the same first name
    ('Alice' matches 'Alice_Example' and the other way round)."""
    f, q = (field or "").strip().lower(), (query or "").strip().lower()
    if not f or not q:
        return False
    return q in f or same_person(field, query)


# ---- build --------------------------------------------------------------------------

def _load_json(path: Path, what: str) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log(f"WARN {path.name} unreadable ({e}); {what} skipped")
        return None
    if not isinstance(data, dict):
        log(f"WARN {path.name} is not a JSON object; {what} skipped")
        return None
    return data


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    row = c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row is not None


def sidecar_source(folder: Path) -> dict:
    """{'created', 'duration_s', 'source_name'} from the first *.diarization.json
    sidecar's 'source' block (all None when absent)."""
    out = {"created": None, "duration_s": None, "source_name": None}
    for side in sorted(folder.glob("*.diarization.json")):
        data = _load_json(side, "sidecar")
        src = data.get("source") if data else None
        if not isinstance(src, dict):
            continue
        out["created"] = iso_utc(src.get("creation_time"))
        dur = src.get("duration_seconds")
        out["duration_s"] = float(dur) if isinstance(dur, (int, float)) else None
        out["source_name"] = Path(str(src["path"])).name if src.get("path") else None
        break
    return out


def meeting_rows(ws: Path, seg_counts: dict[str, int]) -> list[tuple]:
    rows: dict[str, tuple] = {}
    manifest = _load_json(ws / wsconfig.MANIFEST_NAME, "manifest") or {}
    for entry in manifest.get("meetings") or []:
        if not isinstance(entry, dict) or not entry.get("folder"):
            continue
        folder = str(entry["folder"])
        dur = entry.get("duration_s")
        dur = float(dur) if isinstance(dur, (int, float)) else None
        has_ai = bool(entry.get("has_action_items")) or (ws / folder / MEETING_MD_NAME).is_file()
        rows[folder] = (folder, entry.get("source_name"), entry.get("created"), dur,
                        minutes_of(dur), int(bool(DATE_DIR_RE.match(folder))), int(has_ai),
                        seg_counts.get(folder, 0))
    for folder, _files in wsconfig.iter_meetings(ws):
        if folder in rows:
            continue
        side = sidecar_source(ws / folder)
        rows[folder] = (folder, side["source_name"], side["created"], side["duration_s"],
                        minutes_of(side["duration_s"]), int(bool(DATE_DIR_RE.match(folder))),
                        int((ws / folder / MEETING_MD_NAME).is_file()), seg_counts.get(folder, 0))
    return [rows[k] for k in sorted(rows)]


def doc_folders(ws: Path) -> list[Path]:
    """Every immediate subfolder (not '_'/'.'-prefixed) holding an action-items.md."""
    if not ws.is_dir():
        return []
    return [p for p in sorted(ws.iterdir())
            if p.is_dir() and not p.name.startswith(("_", "."))
            and (p / MEETING_MD_NAME).is_file()]


def build(ws: Path) -> int:
    db = wsconfig.search_db(ws)
    hint = f"run: whosaid index {ws}"
    if not db.is_file():
        log(f"graph build: no search index at {db}; {hint}")
        return 1
    c = sqlite3.connect(db, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        if not _has_table(c, "seg"):
            log(f"graph build: {db.name} has no seg table yet; {hint}")
            return 1
        # Everything below runs in one write transaction, so a concurrent reader
        # (MCP server, watcher) sees either the old tables or the new ones.
        c.execute("BEGIN IMMEDIATE")
        try:
            for t in TABLES:
                c.execute(f"DROP TABLE IF EXISTS {t}")
            for stmt in SCHEMA:
                c.execute(stmt)
            _fill(c, ws)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        c.close()
    print("built graph: " + " · ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


def cm_rows(ws: Path) -> list[tuple]:
    """_commitments.json (the CM-NNN corpus lib/workspace.py rolls up) -> one
    `commitment` table row per occurrence: the item-level id/status/ai_refs/
    merged_into stamped onto every sighting with its source, meeting, line,
    t_sec, owner, requester (+role), text, cue and negation.

    Read straight from the JSON rather than workspace.load_commitments_corpus:
    that loader rehydrates occurrences into dataclasses, which drops (or, pre
    #23, rejects with TypeError) the occurrence fields this table needs, and
    graph must not import the roll-up at runtime anyway. Every field goes
    through .get(), so both corpus generations load: a pre-#23 occurrence
    (meeting + line only) yields a row with empty source/owner/cue and no
    timestamp instead of failing the build. A missing file means no rows
    (logged once); unreadable JSON is warned about by _load_json."""
    path = ws / CM_CORPUS_NAME
    data = _load_json(path, "commitment corpus")
    if data is None:
        if not path.is_file():
            log(f"graph build: no {CM_CORPUS_NAME} in {ws.name}; commitment table is empty")
        return []
    rows: list[tuple] = []
    for it in data.get("items") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        cid, status = str(it["id"]), str(it.get("status") or "open")
        merged = str(it.get("merged_into") or "")
        refs = it.get("ai_refs") or []
        if isinstance(refs, str):
            refs = refs.split(",")
        refs_json = json.dumps(sorted({str(a) for a in refs if str(a)}))
        occs = it.get("occurrences") or []
        if not isinstance(occs, list):
            continue
        for o in occs:
            if not isinstance(o, dict) or not o.get("meeting"):
                continue
            t = o.get("t_sec")
            t = int(t) if isinstance(t, (int, float)) else None
            try:
                line = int(o.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            rows.append((cid, str(o.get("source") or ""), str(o["meeting"]), line, t,
                         str(o.get("owner") or ""), str(o.get("requester") or ""),
                         str(o.get("requester_role") or ""),
                         str(o.get("text") or it.get("text") or ""),
                         str(o.get("cue") or ""), 1 if o.get("negative") else 0,
                         status, refs_json, merged))
    return sorted(rows, key=lambda r: (r[0], r[2], r[4] is None, r[4] or 0, r[3]))


def _fill(c: sqlite3.Connection, ws: Path) -> None:
    # people from the seg index
    for name, turns, mtgs in c.execute(
            "SELECT speaker, COUNT(*), COUNT(DISTINCT meeting) FROM seg GROUP BY speaker").fetchall():
        name = str(name or "")
        c.execute("INSERT OR REPLACE INTO person VALUES (?,?,?,?)",
                  (name, turns, mtgs, 0 if name.startswith("SPEAKER_") else 1))

    # meetings: manifest entries plus every folder holding *.speakers.txt
    seg_counts = {str(m): n for m, n in
                  c.execute("SELECT meeting, COUNT(*) FROM seg GROUP BY meeting").fetchall()}
    c.executemany("INSERT OR REPLACE INTO meeting VALUES (?,?,?,?,?,?,?,?)",
                  meeting_rows(ws, seg_counts))

    # action items + their corpus occurrences
    occ: set[tuple[str, str, int]] = set()
    corpus = _load_json(ws / CORPUS_NAME, "corpus") or {}
    for it in corpus.get("items") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        aid = str(it["id"])
        occs = [o for o in (it.get("occurrences") or []) if isinstance(o, dict) and o.get("meeting")]
        c.execute("INSERT OR REPLACE INTO action_item VALUES (?,?,?,?,?,?,?,?,?)", (
            aid, str(it.get("text") or ""), str(it.get("owner") or ""),
            str(it.get("status") or "open"), str(it.get("type") or ""),
            str(it.get("first_seen") or ""), str(it.get("last_seen") or ""),
            str(it.get("merged_into") or ""), len(occs)))
        for o in occs:
            try:
                line = int(o.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            occ.add((aid, str(o["meeting"]), line))

    # per-meeting action-items.md: AI-NNN references and doc PR mentions
    # (commitments themselves come from the CM corpus below, not a re-parse)
    for folder in doc_folders(ws):
        md_path = folder / MEETING_MD_NAME
        try:
            text = md_path.read_text()
        except OSError as e:
            log(f"WARN {md_path} unreadable ({e}); skipped")
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for aid in set(AI_RE.findall(line)):
                occ.add((aid, folder.name, n))
        for pr in sorted({int(p) for p in PR_DOC_RE.findall(text)}):
            c.execute("INSERT INTO pr_mention VALUES (?,?,?,?)", (pr, "doc", folder.name, MEETING_MD_NAME))
    c.executemany("INSERT INTO occurrence VALUES (?,?,?)", sorted(occ))

    # commitments: the CM corpus flattened, one row per occurrence (issue #23)
    c.executemany("INSERT INTO commitment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cm_rows(ws))

    corpus_md = ws / CORPUS_MD_NAME
    if corpus_md.is_file():
        try:
            for pr in sorted({int(p) for p in PR_DOC_RE.findall(corpus_md.read_text())}):
                c.execute("INSERT INTO pr_mention VALUES (?,?,?,?)", (pr, "doc", "(corpus)", CORPUS_MD_NAME))
        except OSError as e:
            log(f"WARN {corpus_md} unreadable ({e}); skipped")

    # spoken PR mentions straight from the transcripts
    for mtg, tstr, txt in c.execute("SELECT meeting, t_str, text FROM seg").fetchall():
        txt = str(txt or "")
        for pr in sorted({int(a or b) for a, b in PR_SPOKEN_RE.findall(txt)}):
            c.execute("INSERT INTO pr_mention VALUES (?,?,?,?)",
                      (pr, "spoken", mtg, f"@{tstr}: {txt[:120]}"))


# ---- reading --------------------------------------------------------------------------

def open_graph(ws: Path) -> sqlite3.Connection:
    db = wsconfig.search_db(ws)
    hint = f"run: whosaid index {ws}"
    if not db.is_file():
        raise GraphError(f"no search index at {db}; {hint}")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    if not all(_has_table(c, t) for t in TABLES):
        c.close()
        raise GraphError(f"{db.name} has no graph tables yet; {hint}")
    return c


def _span(first: str, last: str) -> str:
    if not first and not last:
        return ""
    return first if first == last or not last else f"{first} → {last}"


def _item_dict(r: sqlite3.Row, requesters: dict[str, set[str]]) -> dict:
    return {
        "id": r["id"], "text": r["text"], "owner": r["owner"], "status": r["status"],
        "type": r["type"], "first_seen": r["first_seen"], "last_seen": r["last_seen"],
        "span": _span(r["first_seen"], r["last_seen"]), "merged_into": r["merged_into"],
        "occurrences": r["occurrences"], "requesters": sorted(requesters.get(r["id"], set())),
    }


def _commitment_dict(r: sqlite3.Row) -> dict:
    try:
        refs = json.loads(r["ai_refs"] or "[]")
    except ValueError:
        refs = []
    return {
        "id": r["id"], "source": r["source"], "meeting": r["meeting"], "line": r["line"],
        "t_sec": r["t_sec"], "owner": r["owner"], "requester": r["requester"],
        "requester_role": r["requester_role"], "text": r["text"], "cue": r["cue"],
        "negative": bool(r["negative"]), "status": r["status"],
        "ai_refs": [str(a) for a in refs], "merged_into": r["merged_into"],
    }


def load_commitments(c: sqlite3.Connection) -> list[dict]:
    """Every commitment occurrence, id then meeting then time (nulls last)."""
    return [_commitment_dict(r) for r in
            c.execute("SELECT * FROM commitment ORDER BY id, meeting, t_sec IS NULL, t_sec, line")]


def dedupe_commitments(cms: list[dict]) -> list[dict]:
    """Collapse occurrence rows to one entry per CM id (person views would
    otherwise repeat the same commitment once per sighting). The entry keeps
    the first row's fields plus `occ` (sighting count), `sources`, `meetings`
    and `at` (the [meeting, t_sec] pairs, meeting then time)."""
    out: dict[str, dict] = {}
    for cm in cms:
        d = out.get(cm["id"])
        if d is None:
            d = out[cm["id"]] = {k: cm[k] for k in
                                 ("id", "owner", "requester", "requester_role",
                                  "text", "cue", "negative", "status", "ai_refs",
                                  "merged_into")}
            d["occ"] = 0
            d["sources"] = set()
            d["meetings"] = set()
            d["at"] = []
        d["occ"] += 1
        d["sources"].add(cm["source"])
        d["meetings"].add(cm["meeting"])
        d["at"].append([cm["meeting"], cm["t_sec"]])
    for d in out.values():
        d["sources"] = sorted(s for s in d["sources"] if s)
        d["meetings"] = sorted(d["meetings"])
        d["at"].sort(key=lambda p: (p[0], p[1] is None, p[1] or 0))
    return [out[k] for k in sorted(out)]


def requesters_by_item(commitments: list[dict]) -> dict[str, set[str]]:
    """AI-NNN -> people who asked for it (a commitment citing it whose requester
    is not the owner)."""
    out: dict[str, set[str]] = {}
    for cm in commitments:
        if not cm["requester"] or same_person(cm["requester"], cm["owner"]):
            continue
        for aid in cm["ai_refs"]:
            out.setdefault(aid, set()).add(cm["requester"])
    return out


def meeting_order(c: sqlite3.Connection) -> dict[str, str]:
    """folder -> sort key (created timestamp, else the folder name)."""
    return {r["folder"]: (r["created"] or r["folder"])
            for r in c.execute("SELECT folder, created FROM meeting")}


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# ---- views ----------------------------------------------------------------------------

def view_items(c: sqlite3.Connection, owner: str | None, requester: str | None,
               status: str | None, itype: str | None) -> list[dict]:
    where, params = [], []
    for col, val in (("owner", owner), ("status", status), ("type", itype)):
        if val:
            where.append(f"{col} LIKE ?")
            params.append(f"%{val}%")
    sql = "SELECT * FROM action_item"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    reqs = requesters_by_item(load_commitments(c))
    items = [_item_dict(r, reqs) for r in c.execute(sql, params)]
    if requester:
        items = [it for it in items if any(name_match(r, requester) for r in it["requesters"])]
    return items


def print_items(items: list[dict]) -> None:
    if items:
        w_st = max(len(it["status"]) for it in items)
        w_ty = max([len(it["type"]) for it in items] + [1])
        w_ow = max([len(it["owner"]) for it in items] + [1])
        w_sp = max([len(it["span"]) for it in items] + [1])
        for it in items:
            print(f"{it['id']}  {it['status']:<{w_st}}  {it['type'] or '-':<{w_ty}}  "
                  f"{it['owner'] or '-':<{w_ow}}  {it['span'] or '-':<{w_sp}}  {it['text']}")
        print()
    print(f"{len(items)} item(s).")


def view_item(c: sqlite3.Connection, aid: str) -> dict:
    r = c.execute("SELECT * FROM action_item WHERE id=?", (aid,)).fetchone()
    if r is None:
        raise GraphError(f"{aid} not found in the action-item corpus; see: whosaid graph items")
    commitments = load_commitments(c)
    d = _item_dict(r, requesters_by_item(commitments))
    d["occurrences"] = [{"meeting": o["meeting"], "line": o["line"]} for o in
                        c.execute("SELECT meeting, line FROM occurrence WHERE id=? ORDER BY meeting, line", (aid,))]
    d["commitments"] = [cm for cm in commitments if aid in cm["ai_refs"]]
    return d


def print_item(d: dict) -> None:
    print(f"{d['id']}  [{d['status']}]  type={d['type'] or '-'}  owner={d['owner'] or '-'}  "
          f"span={d['span'] or '-'}" + (f"  merged into {d['merged_into']}" if d["merged_into"] else ""))
    print(f"  {d['text']}")
    print()
    if d["requesters"]:
        print("  asked by: " + ", ".join(d["requesters"]))
    occ = ", ".join(f"{o['meeting']}:{o['line']}" for o in d["occurrences"])
    print("  appears in: " + (occ or "(no recorded occurrence)"))
    if d["commitments"]:
        print("  commitment occurrences:")
        for cm in d["commitments"]:
            tag = (f"  (asked by {cm['requester']})"
                   if cm["requester"] and not same_person(cm["requester"], cm["owner"]) else "")
            print(f"    {cm['id']} [{cm['status']}] [{cm['meeting']} @ "
                  f"{fmt_t(cm['t_sec']) or 'no time'}] {cm['owner'] or '-'}: {cm['text']}{tag}")


def view_commitments(c: sqlite3.Connection, owner: str | None, source: str | None,
                     status: str | None) -> list[dict]:
    """The unified commitment table, one row per CM-NNN occurrence, optionally
    filtered. --owner is a name_match substring; --source matches transcript /
    action-items exactly ('_' and '-' are equivalent); --status is a
    case-insensitive substring."""
    cms = load_commitments(c)
    if owner:
        cms = [cm for cm in cms if name_match(cm["owner"], owner)]
    if source:
        want = source.strip().lower().replace("_", "-")
        cms = [cm for cm in cms
               if (cm["source"] or "").strip().lower().replace("_", "-") == want]
    if status:
        cms = [cm for cm in cms if status.strip().lower() in (cm["status"] or "").lower()]
    return cms


def print_commitments(cms: list[dict]) -> None:
    if cms:
        w_id = max(len(cm["id"]) for cm in cms)
        w_st = max([len(cm["status"] or "") for cm in cms] + [1])
        w_ow = max([len(cm["owner"] or "") for cm in cms] + [1])
        w_src = max([len(cm["source"] or "") for cm in cms] + [1])
        for cm in cms:
            print(f"{cm['id']:<{w_id}}  {cm['status'] or '-':<{w_st}}  {cm['owner'] or '-':<{w_ow}}  "
                  f"{cm['source'] or '-':<{w_src}}  {cm['meeting']}@{fmt_t(cm['t_sec']) or '-'}  "
                  f"{cm['text']}")
        print()
    print(f"{len(cms)} commitment occurrence(s).")


def view_people(c: sqlite3.Connection) -> list[dict]:
    return [{"name": r["name"], "turns": r["turns"], "meetings": r["meetings"],
             "named": bool(r["named"])}
            for r in c.execute("SELECT * FROM person ORDER BY turns DESC, name")]


def print_people(people: list[dict]) -> None:
    for p in people:
        print(f"{p['name']:22s} {p['turns']:4d} turns · {p['meetings']} meeting(s)"
              + ("" if p["named"] else "  (unlabeled cluster)"))
    if not people:
        print("no speakers indexed yet.")


def view_person(c: sqlite3.Connection, name: str) -> dict:
    people = [p for p in view_people(c) if name_match(p["name"], name)]
    commitments = load_commitments(c)
    order = meeting_order(c)

    def newest_first(entries: list[dict]) -> list[dict]:
        # by the newest meeting the entry occurs in; ties keep id order
        return sorted(entries, reverse=True,
                      key=lambda e: max((order.get(m, m) for m in e["meetings"]), default=""))

    requested = newest_first(dedupe_commitments([
        cm for cm in commitments
        if name_match(cm["requester"], name) and not same_person(cm["requester"], cm["owner"])]))
    owned = newest_first(dedupe_commitments(
        [cm for cm in commitments if name_match(cm["owner"], name)]))
    reqs = requesters_by_item(commitments)
    items = {r["id"]: _item_dict(r, reqs) for r in c.execute("SELECT * FROM action_item ORDER BY id")}
    cited = {aid for e in requested for aid in e["ai_refs"]}
    return {
        "query": name,
        "people": people,
        "requested": requested,
        "requested_items": [items[a] for a in sorted(cited) if a in items],
        "owned": owned,
        "owned_items": [it for it in items.values() if name_match(it["owner"], name)],
    }


def _print_person_commitments(entries: list[dict], items: dict[str, dict], who: str) -> None:
    """One line per CM id (already newest first): status, sighting count when
    the item occurs more than once, where it was said, text, AI refs, and the
    other party."""
    for e in entries:
        where = " · ".join(f"{m}@{fmt_t(t) or 'no time'}" for m, t in e["at"]) or "(no occurrence)"
        refs = " ".join(f"{a} [{items[a]['status']}]" if a in items else a for a in e["ai_refs"])
        other = e["requester"] if who == "owner" else e["owner"]
        tag = ""
        if other and not same_person(e["requester"], e["owner"]):
            tag = f"  (asked by {other})" if who == "owner" else f"  (owner: {other})"
        n = f" ({e['occ']}x)" if e["occ"] > 1 else ""
        print(f"    {e['id']} [{e['status']}]{n}  {where}  {e['text']}"
              + (f"  {refs}" if refs else "") + tag)


def print_person(d: dict) -> None:
    label = d["people"][0]["name"] if d["people"] else d["query"]
    if d["people"]:
        print_people(d["people"])
    else:
        print(f"{d['query']}: no speaker in the index matches (checking owners and requesters)")
    n_meet = len({m for e in d["owned"] for m in e["meetings"]})
    print()
    print(f"Requested by {label} (asks made of others): {len(d['requested'])} commitment(s)"
          + (", items " + ", ".join(it["id"] for it in d["requested_items"]) if d["requested_items"] else ""))
    items = {it["id"]: it for it in d["requested_items"] + d["owned_items"]}
    _print_person_commitments(d["requested"], items, "requester")
    print()
    print(f"Owned by {label} (what they signed up for): {len(d['owned'])} commitment(s) "
          f"across {n_meet} meeting(s)")
    _print_person_commitments(d["owned"], items, "owner")
    if d["owned_items"]:
        print("  corpus items owned: " + ", ".join(f"{it['id']} [{it['status']}]" for it in d["owned_items"]))


def view_prs(c: sqlite3.Connection) -> list[dict]:
    out: dict[int, dict] = {}
    for r in c.execute("SELECT pr, source, meeting FROM pr_mention ORDER BY pr, meeting, rowid"):
        d = out.setdefault(r["pr"], {"pr": r["pr"], "refs": 0, "spoken": 0, "doc": 0, "meetings": []})
        d["refs"] += 1
        d[r["source"] if r["source"] in ("spoken", "doc") else "doc"] += 1
        if r["meeting"] not in d["meetings"]:
            d["meetings"].append(r["meeting"])
    return [out[k] for k in sorted(out)]


def print_prs(prs: list[dict]) -> None:
    for d in prs:
        tag = f"{d['spoken']} spoken" if d["spoken"] else "docs only"
        print(f"PR #{d['pr']:<6} {d['refs']} ref(s) ({tag})  {', '.join(d['meetings'])}")
    if not prs:
        print("no PR references found.")


def view_meetings(c: sqlite3.Connection) -> list[dict]:
    return [{"folder": r["folder"], "source_name": r["source_name"], "created": r["created"],
             "duration_s": r["duration_s"], "minutes": r["minutes"], "dated": bool(r["dated"]),
             "has_action_items": bool(r["has_action_items"]), "segments": r["segments"]}
            for r in c.execute("SELECT * FROM meeting ORDER BY folder")]


def print_meetings(meetings: list[dict]) -> None:
    for m in meetings:
        mins = f"{m['minutes']}m" if m["minutes"] is not None else "-"
        print(f"{m['folder']:32s} {m['created'] or '-':20s} {mins:>6s}  "
              f"{'dated' if m['dated'] else 'hand-named':10s} {m['segments']:5d} seg  "
              f"{'action items' if m['has_action_items'] else '-'}")
    if not meetings:
        print("no meetings indexed yet.")


# ---- wiki -----------------------------------------------------------------------------

def _now_iso(cfg: dict) -> str:
    tz = str(cfg.get("workspace", {}).get("tz") or "")
    now = datetime.now(timezone.utc)
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
    return now.astimezone().isoformat(timespec="seconds")


def render_wiki(c: sqlite3.Connection, ws: Path, cfg: dict) -> str:
    L: list[str] = []
    w = L.append
    n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    n["seg"] = c.execute("SELECT COUNT(*) FROM seg").fetchone()[0] if _has_table(c, "seg") else 0
    commitments = load_commitments(c)
    reqs = requesters_by_item(commitments)
    cites: dict[str, list[str]] = {}
    for cm in commitments:
        for aid in cm["ai_refs"]:
            cites.setdefault(aid, []).append(f"{cm['id']} {cm['meeting']}@{fmt_t(cm['t_sec']) or 'no-time'}")

    w("# Workspace wiki (generated)")
    w("")
    w(f"_Generated from `{wsconfig.SEARCH_DB_NAME}` on {_now_iso(cfg)}. Do not hand-edit: "
      f"re-run `whosaid index` to regenerate. The transcripts are the source of truth; "
      f"every action item below cites the CM id and `meeting@time` it was committed at._")
    w("")
    w(f"**Corpus:** {n['seg']} segments · {n['meeting']} meetings · {n['person']} speakers "
      f"· {n['action_item']} action items · {n['commitment']} commitment occurrences "
      f"· {n['pr_mention']} PR refs.")
    w("")

    w("## Coverage")
    w("")
    meetings = view_meetings(c)
    if meetings:
        w("| Meeting | Created | Length | Dated | Segments | Action items |")
        w("|---|---|---|---|---|---|")
        for m in meetings:
            mins = f"{m['minutes']}m" if m["minutes"] is not None else "-"
            w(f"| `{m['folder']}` | {m['created'] or '-'} | {mins} | {'yes' if m['dated'] else 'no'} | "
              f"{m['segments']} | {'yes' if m['has_action_items'] else '-'} |")
    else:
        w("_No meetings indexed yet._")
    w("")

    w("## Action items")
    w("")
    items = [_item_dict(r, reqs) for r in c.execute("SELECT * FROM action_item ORDER BY id")]
    if items:
        seen = set(STATUS_ORDER) | {"merged"}
        extra = sorted({it["status"] for it in items} - seen)
        for st in STATUS_ORDER + tuple(extra) + ("merged",):
            rows = [it for it in items if it["status"] == st]
            if not rows:
                continue
            w(f"### {st.title()} ({len(rows)})")
            w("")
            for it in rows:
                meta = [it["type"] or "-", f"owner: {it['owner'] or '-'}"]
                if it["requesters"]:
                    meta.append("asked by: " + ", ".join(it["requesters"]))
                if it["status"] == "merged" and it["merged_into"]:
                    meta.append(f"merged into {it['merged_into']}")
                elif it["span"]:
                    meta.append(it["span"])
                w(f"- **{it['id']}** ({', '.join(meta)}) {it['text']}")
                ev = "  ".join(f"`{x}`" for x in cites.get(it["id"], [])[:4])
                if ev:
                    w(f"    cited: {ev}")
            w("")
    else:
        w("_No action items yet._")
        w("")

    w("## Speakers")
    w("")
    named = [p for p in view_people(c) if p["named"]]
    if named:
        w("| Speaker | Turns | Meetings | Items requested |")
        w("|---|---|---|---|")
        for p in named:
            asked = sorted({aid for aid, who in reqs.items() if any(name_match(r, p["name"]) for r in who)})
            w(f"| {p['name']} | {p['turns']} | {p['meetings']} | {', '.join(asked) if asked else '-'} |")
    else:
        w("_No named speakers yet (relabel SPEAKER_NN clusters with `whosaid relabel`)._")
    w("")

    w("## PR references")
    w("")
    prs = view_prs(c)
    if prs:
        w("| PR | Refs | Spoken | Where |")
        w("|---|---|---|---|")
        for d in prs:
            w(f"| #{d['pr']} | {d['refs']} | {d['spoken']} | {', '.join(d['meetings'])} |")
    else:
        w("_No PR references yet._")
    w("")
    return "\n".join(L)


# ---- CLI --------------------------------------------------------------------------------

def _ws(args: argparse.Namespace) -> Path:
    return wsconfig.resolve_workspace(getattr(args, "ws", None))


def cmd_build(args: argparse.Namespace) -> int:
    return build(_ws(args))


def cmd_items(args: argparse.Namespace) -> int:
    with open_graph(_ws(args)) as c:
        items = view_items(c, args.owner, args.requester, args.status, args.type)
    if args.json:
        _print_json(items)
    else:
        print_items(items)
    return 0


def cmd_item(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"AI-\d{3,}", args.id):
        log(f"item: expected an action-item id like AI-001, got {args.id!r}")
        return 2
    with open_graph(_ws(args)) as c:
        d = view_item(c, args.id)
    if args.json:
        _print_json(d)
    else:
        print_item(d)
    return 0


def cmd_commitments(args: argparse.Namespace) -> int:
    with open_graph(_ws(args)) as c:
        cms = view_commitments(c, args.owner, args.source, args.status)
    if args.json:
        _print_json(cms)
    else:
        print_commitments(cms)
    return 0


def cmd_person(args: argparse.Namespace) -> int:
    ws_arg, name = getattr(args, "ws", None), getattr(args, "name", None)
    if ws_arg and name is None and not Path(ws_arg).expanduser().is_dir():
        ws_arg, name = None, ws_arg          # `person Alice` with the workspace from env/cwd
    ws = wsconfig.resolve_workspace(ws_arg)
    with open_graph(ws) as c:
        if name:
            d = view_person(c, name)
            if args.json:
                _print_json(d)
            else:
                print_person(d)
            if not (d["people"] or d["requested"] or d["owned"] or d["owned_items"]):
                log(f"person: nothing in {ws.name} matches {name!r}; see: whosaid graph person")
                return 1
            return 0
        people = view_people(c)
    if args.json:
        _print_json(people)
    else:
        print_people(people)
    return 0


def cmd_prs(args: argparse.Namespace) -> int:
    with open_graph(_ws(args)) as c:
        prs = view_prs(c)
    if args.json:
        _print_json(prs)
    else:
        print_prs(prs)
    return 0


def cmd_meetings(args: argparse.Namespace) -> int:
    with open_graph(_ws(args)) as c:
        meetings = view_meetings(c)
    if args.json:
        _print_json(meetings)
    else:
        print_meetings(meetings)
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    ws = _ws(args)
    with open_graph(ws) as c:
        md = render_wiki(c, ws, wsconfig.load_config(ws))
    if args.stdout:
        print(md)
        return 0
    out = Path(args.out) if args.out else ws / WIKI_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    log(f"wiki -> {out} ({len(md)} chars)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="graph.py",
        description="whosaid entity graph over a meeting workspace: people, meetings, "
                    "action items, timestamped commitments, PR mentions, and a generated wiki.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    ws_help = "workspace directory (default: $WHOSAID_WORKSPACE, else the current directory)"

    pb = sub.add_parser("build", help="(re)build the graph tables inside <ws>/_search.db")
    pb.add_argument("ws", nargs="?", help=ws_help)
    pb.set_defaults(func=cmd_build)

    pi = sub.add_parser("items", help="list corpus action items (filters are case-insensitive substrings)")
    pi.add_argument("ws", nargs="?", help=ws_help)
    pi.add_argument("--owner", help="who owns the item")
    pi.add_argument("--requester", help="who asked for it (from timestamped commitments)")
    pi.add_argument("--status", help="open, ongoing, contingent, resolved, merged, ...")
    pi.add_argument("--type", help="free-form type, e.g. 'leadership ask'")
    pi.add_argument("--json", action="store_true", help="print a JSON array")
    pi.set_defaults(func=cmd_items)

    pit = sub.add_parser("item", help="one action item with its occurrences and commitments")
    pit.add_argument("ws", nargs="?", help=ws_help)
    pit.add_argument("id", help="action-item id, e.g. AI-001")
    pit.add_argument("--json", action="store_true", help="print a JSON object")
    pit.set_defaults(func=cmd_item)

    pcm = sub.add_parser("commitments", help="the unified commitment corpus: one row per "
                                            "CM-NNN occurrence (transcript or action-items)")
    pcm.add_argument("ws", nargs="?", help=ws_help)
    pcm.add_argument("--owner", help="who committed (case-insensitive substring)")
    pcm.add_argument("--source", help="transcript or action-items")
    pcm.add_argument("--status", help="open, ongoing, done, merged, ... (case-insensitive substring)")
    pcm.add_argument("--json", action="store_true", help="print a JSON array")
    pcm.set_defaults(func=cmd_commitments)

    for cmd, help_text in (("person", "speakers, or one person's asks and commitments"),
                           ("speakers", "alias of `person` with no name: every speaker")):
        pp = sub.add_parser(cmd, help=help_text)
        pp.add_argument("ws", nargs="?", help=ws_help)
        if cmd == "person":
            pp.add_argument("name", nargs="?", help="speaker/owner/requester (case-insensitive substring)")
        pp.add_argument("--json", action="store_true", help="print JSON")
        pp.set_defaults(func=cmd_person)

    ppr = sub.add_parser("prs", help="pull-request numbers mentioned, spoken or written")
    ppr.add_argument("ws", nargs="?", help=ws_help)
    ppr.add_argument("--json", action="store_true", help="print a JSON array")
    ppr.set_defaults(func=cmd_prs)

    pm = sub.add_parser("meetings", help="every meeting folder with coverage figures")
    pm.add_argument("ws", nargs="?", help=ws_help)
    pm.add_argument("--json", action="store_true", help="print a JSON array")
    pm.set_defaults(func=cmd_meetings)

    pw = sub.add_parser("wiki", help=f"render the generated wiki (default: <ws>/{WIKI_NAME})")
    pw.add_argument("ws", nargs="?", help=ws_help)
    pw.add_argument("--stdout", action="store_true", help="print the markdown instead of writing it")
    pw.add_argument("-o", "--out", default=None, help="write to this file instead")
    pw.set_defaults(func=cmd_wiki)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GraphError as e:
        log(f"{args.command}: {e}")
        return 1
    except sqlite3.OperationalError as e:
        log(f"{args.command}: database problem ({e}); run: whosaid index")
        return 1


if __name__ == "__main__":
    sys.exit(main())

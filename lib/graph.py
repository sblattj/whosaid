#!/usr/bin/env python3
"""
graph.py: entity tables and views over a whosaid meeting workspace (GitHub
issue #14; the per-owner commitments view of issue #13).

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
  commitment    timestamped bullets from per-meeting action-items.md: who owns
                it, who asked for it, and when it was said (joins to seg by
                meeting + t_sec, so every commitment points back at the audio)
  pr_mention    pull-request numbers, spoken ("pr 42") in the transcripts or
                written ("PR #42") in the action-item documents

Usage (the `whosaid graph ...` / `whosaid wiki` bash front ends shell out here):

  python3 lib/graph.py build    [ws]
  python3 lib/graph.py items    [ws] [--owner X] [--requester R] [--status S] [--type T] [--json]
  python3 lib/graph.py item     [ws] AI-NNN [--json]
  python3 lib/graph.py person   [ws] [Name] [--json]      (alias: speakers)
  python3 lib/graph.py prs      [ws] [--json]
  python3 lib/graph.py meetings [ws] [--json]
  python3 lib/graph.py wiki     [ws] [--stdout] [-o FILE]

[ws] is optional (nargs="?"): when omitted, $WHOSAID_WORKSPACE or the current
directory is used (wsconfig.resolve_workspace). `person Name` with no
workspace works too: a first argument that is not an existing directory is
taken as the name.

Commitment bullet shapes accepted in action-items.md (times are MM:SS or
H:MM:SS; a bullet may list several, "[Bob 12:01 / 14:30]" or a range
"[Bob 46:36-47:35]", one row per time):

  - **Owner** [Requester HH:MM:SS] text     requester asked owner for it
  - **Owner** [HH:MM:SS] text               owner's own commitment
  - **Owner** [inferred] text               no timestamp (t_sec NULL, t_str "inferred")
  - **[Requester HH:MM:SS] title** rest     legacy: the bracketed name is who asked;
  - **[HH:MM:SS] title** rest               the owner is [workspace].owner from
                                            whosaid.toml (else ""), and a bare
                                            time means owner asked it of themselves

Legacy brackets are messy ("[Bob ~47-48 / 52:xx]", "[Bob, implicit]",
"[Bob 51:26 -> carries the earlier ask]"): the requester is the first word-ish
token before the first time or comma, every MM:SS/H:MM:SS token becomes a row,
and a bracket with no parsable time still yields one row (t_sec NULL, t_str "").

A commitment links to action items two ways, both folded into ai_refs: AI-NNN
ids written on the bullet line, and occurrence rows (corpus or converter)
whose (meeting, line) is the bullet's own line.

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
    "CREATE TABLE commitment(meeting TEXT, owner TEXT, requester TEXT, t_sec INT, "
    "t_str TEXT, text TEXT, ai_refs TEXT, line INT)",
    "CREATE TABLE pr_mention(pr INT, source TEXT, meeting TEXT, detail TEXT)",
)

# Mirrors workspace.DATE_DIR_RE (test/graph_test.py guards against drift) so this
# module does not import the roll-up at runtime.
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(?:-\d+)?$")
AI_RE = re.compile(r"\bAI-\d{3,}\b")
# Times inside a commitment bracket: MM:SS or H:MM:SS (ranges and "/" lists
# just yield several matches).
TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
# The requester inside a bracket: the first word-ish token. Legacy brackets also
# carry notes ("[Bob, implicit]", "[implicit]"), so there the token has to start
# with a capital (SPEAKER_NN included) to count as a name.
TOKEN_RE = re.compile(r"[A-Za-z][\w.'-]*")
STRICT_TOKEN_RE = re.compile(r"[A-Z][\w.'-]*")
BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<body>\S.*)$")
NEW_RE = re.compile(r"^\*\*(?P<owner>[^*\[\]]+?)\s*:?\*\*\s*:?\s*\[(?P<br>[^\]]*)\]\s*(?P<text>.*)$")
LEGACY_RE = re.compile(r"^\*\*\[(?P<br>[^\]]*)\]\s*(?P<title>[^*]*?)\s*\*\*(?P<rest>.*)$")
PR_SPOKEN_RE = re.compile(r"\b(\d{2,5})\s*pr\b|\bpr\s*#?(\d{2,5})\b", re.IGNORECASE)
PR_DOC_RE = re.compile(r"(?:PR\s*#|#)(\d{2,5})\b")
STATUS_ORDER = ("open", "ongoing", "contingent", "resolved")


class GraphError(Exception):
    """A runtime problem with a one-line fix hint; cmd_* turn it into exit 1."""


def clean(s: str) -> str:
    return re.sub(r"\*\*|`", "", s).strip()


# ---- shared parsing ------------------------------------------------------------------

def parse_bracket(br: str, strict: bool = False) -> tuple[str, list[str]]:
    """Bracket content -> (requester, times).

    '[Bob 12:01 / 14:30]' -> ('Bob', ['12:01', '14:30']); '[Bob 46:36-47:35]'
    takes both ends; '[12:01]' -> ('', ['12:01']); '[inferred]' -> ('', ['inferred']);
    a bracket with no parsable time ('[Bob, implicit]', '[Bob ~47-48 / 52:xx]')
    -> ('Bob', ['']), one row with no timestamp. The requester is the first
    word-ish token before the first time or comma; with strict=True (legacy
    brackets) it must start with a capital so notes like '[implicit]' are not
    mistaken for a person."""
    s = br.strip()
    if s.lower() == "inferred":
        return "", ["inferred"]
    times = TIME_RE.findall(s)
    cut = len(s)
    if m := TIME_RE.search(s):
        cut = m.start()
    comma = s.find(",")
    if 0 <= comma < cut:
        cut = comma
    head = s[:cut].strip(" ~\t")
    tok = (STRICT_TOKEN_RE if strict else TOKEN_RE).match(head)
    return (tok.group(0) if tok else ""), (times or [""])


def parse_commitments(md_text: str, meeting: str, default_owner: str) -> list[dict]:
    """Commitment bullets of one action-items.md -> rows (dicts with meeting,
    line, owner, requester, t_sec, t_str, text, ai_refs). ai_refs here holds the
    AI-NNN ids written on the line; build() adds the ids whose occurrence points
    at the same (meeting, line). See the module docstring for the shapes."""
    out: list[dict] = []
    for n, raw in enumerate(md_text.splitlines(), 1):
        bm = BULLET_RE.match(raw)
        if not bm:
            continue
        body = bm.group("body").strip()
        if m := NEW_RE.match(body):
            owner = clean(m.group("owner"))
            who, times = parse_bracket(m.group("br"))
            requester, text = (who or owner), clean(m.group("text"))
        elif m := LEGACY_RE.match(body):
            who, times = parse_bracket(m.group("br"), strict=True)
            owner = default_owner
            requester = who or default_owner
            text = clean(m.group("title")) or clean(m.group("rest"))
        else:
            continue
        refs = ",".join(sorted(set(AI_RE.findall(raw))))
        for tok in times:
            out.append({
                "meeting": meeting, "line": n, "owner": owner, "requester": requester,
                "t_sec": wsconfig.tsec(tok) if tok and tok != "inferred" else None,
                "t_str": tok, "text": text, "ai_refs": refs,
            })
    return out


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
    cfg = wsconfig.load_config(ws)
    cfg_owner = str(cfg.get("workspace", {}).get("owner") or "")
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
            _fill(c, ws, cfg_owner)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        c.close()
    print("built graph: " + " · ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


def _fill(c: sqlite3.Connection, ws: Path, cfg_owner: str) -> None:
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

    # per-meeting action-items.md: AI-NNN references, commitments, doc PR mentions
    commitments: list[dict] = []
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
        commitments += parse_commitments(text, folder.name, cfg_owner)
        for pr in sorted({int(p) for p in PR_DOC_RE.findall(text)}):
            c.execute("INSERT INTO pr_mention VALUES (?,?,?,?)", (pr, "doc", folder.name, MEETING_MD_NAME))
    c.executemany("INSERT INTO occurrence VALUES (?,?,?)", sorted(occ))

    # link commitments to items: ids on the line, plus occurrences at that line
    at_line: dict[tuple[str, int], set[str]] = {}
    for aid, meeting, line in occ:
        at_line.setdefault((meeting, line), set()).add(aid)
    for cm in commitments:
        refs = {a for a in cm["ai_refs"].split(",") if a} | at_line.get((cm["meeting"], cm["line"]), set())
        c.execute("INSERT INTO commitment VALUES (?,?,?,?,?,?,?,?)",
                  (cm["meeting"], cm["owner"], cm["requester"], cm["t_sec"],
                   cm["t_str"], cm["text"], ",".join(sorted(refs)), cm["line"]))

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
    return {
        "meeting": r["meeting"], "line": r["line"], "owner": r["owner"],
        "requester": r["requester"], "t_sec": r["t_sec"], "t_str": r["t_str"],
        "text": r["text"], "ai_refs": [a for a in (r["ai_refs"] or "").split(",") if a],
    }


def load_commitments(c: sqlite3.Connection) -> list[dict]:
    return [_commitment_dict(r) for r in
            c.execute("SELECT * FROM commitment ORDER BY meeting, t_sec IS NULL, t_sec, line")]


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
        print("  timestamped commitments:")
        for cm in d["commitments"]:
            print(f"    [{cm['meeting']} @ {cm['t_str'] or 'no time'}] {cm['owner'] or '-'}: {cm['text']}"
                  + (f"  (asked by {cm['requester']})"
                     if cm["requester"] and not same_person(cm["requester"], cm["owner"]) else ""))


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

    def newest_first(cms: list[dict]) -> list[dict]:
        return sorted(cms, key=lambda cm: (order.get(cm["meeting"], cm["meeting"]), cm["meeting"]),
                      reverse=True)

    requested = newest_first([cm for cm in commitments
                              if name_match(cm["requester"], name)
                              and not same_person(cm["requester"], cm["owner"])])
    owned = newest_first([cm for cm in commitments if name_match(cm["owner"], name)])
    reqs = requesters_by_item(commitments)
    items = {r["id"]: _item_dict(r, reqs) for r in c.execute("SELECT * FROM action_item ORDER BY id")}
    cited = {aid for cm in requested for aid in cm["ai_refs"]}
    return {
        "query": name,
        "people": people,
        "requested": requested,
        "requested_items": [items[a] for a in sorted(cited) if a in items],
        "owned": owned,
        "owned_items": [it for it in items.values() if name_match(it["owner"], name)],
    }


def _print_grouped(cms: list[dict], items: dict[str, dict], who: str) -> None:
    """Commitments grouped by meeting (already newest first), times ascending."""
    current = None
    for cm in cms:
        if cm["meeting"] != current:
            current = cm["meeting"]
            print(f"  {current}")
        refs = " ".join(f"{a} [{items[a]['status']}]" if a in items else a for a in cm["ai_refs"])
        other = cm["requester"] if who == "owner" else cm["owner"]
        tag = ""
        if other and not same_person(cm["requester"], cm["owner"]):
            tag = f"  (asked by {other})" if who == "owner" else f"  (owner: {other})"
        print(f"    {cm['t_str'] or '(no time)':>9}  {cm['text']}" + (f"  {refs}" if refs else "") + tag)


def print_person(d: dict) -> None:
    label = d["people"][0]["name"] if d["people"] else d["query"]
    if d["people"]:
        print_people(d["people"])
    else:
        print(f"{d['query']}: no speaker in the index matches (checking owners and requesters)")
    n_meet = len({cm["meeting"] for cm in d["owned"]})
    print()
    print(f"Requested by {label} (asks made of others): {len(d['requested'])} commitment(s)"
          + (", items " + ", ".join(it["id"] for it in d["requested_items"]) if d["requested_items"] else ""))
    items = {it["id"]: it for it in d["requested_items"] + d["owned_items"]}
    _print_grouped(d["requested"], items, "requester")
    print()
    print(f"Owned by {label} (what they signed up for): {len(d['owned'])} commitment(s) "
          f"across {n_meet} meeting(s)")
    _print_grouped(d["owned"], items, "owner")
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
            cites.setdefault(aid, []).append(f"{cm['meeting']}@{cm['t_str'] or 'no-time'}")

    w("# Workspace wiki (generated)")
    w("")
    w(f"_Generated from `{wsconfig.SEARCH_DB_NAME}` on {_now_iso(cfg)}. Do not hand-edit: "
      f"re-run `whosaid index` to regenerate. The transcripts are the source of truth; "
      f"every action item below cites the `meeting@time` it was committed at._")
    w("")
    w(f"**Corpus:** {n['seg']} segments · {n['meeting']} meetings · {n['person']} speakers "
      f"· {n['action_item']} action items · {n['commitment']} timestamped commitments "
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

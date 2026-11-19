#!/usr/bin/env python3
"""
Drafts outbox (GitHub issue #165): chat drafts that wait offline, carry the agent session
that wrote them, get their claims checked before the prose is written, and reach Teams only
when the user stages them.

    whosaid drafts new --to "First Last" --topic <slug> [--page /abs/page.html ...] [--supersedes ID] [--new-chat]
    whosaid drafts text ID (--file draft.md | -)          write the message
    whosaid drafts claims ID --file claims.json [--ws WS] write {claims, stakeholders, docs}; prints said turns
    whosaid drafts attach|detach ID /abs/page.html
    whosaid drafts verify ID [--voice warn|off|enforce|clear --reason R] [--ws WS] [--recap-days N]
                                                          check the gates and voice lint, stamp a content hash
    whosaid drafts lint [ID|--all] [--ws WS] [--json]     the voice lint alone, read-only
    whosaid drafts list [--all] [--mine] [--json]
    whosaid drafts show ID [--json]
    whosaid drafts stakeholders [WS] --topic T [--topic T2] [--repo DIR --path P ...] [--json]
    whosaid drafts stale-check [WS] [ID] [--json]         new statements since each draft was verified
    whosaid drafts stage ID|--all [--dry-run] [--tab TARGET] [--ws WS]
    whosaid drafts merge INTO FROM [FROM ...] [--text-file F] [--dry-run] [--force]   fold rival drafts into one
    whosaid drafts mark-sent ID [--ws WS] | mark-sent --check [WS] [--backfill] | drop ID [--reason R]
    whosaid drafts show ID --rev N | diff ID [--from N --to M]
    whosaid drafts html [--out PATH] [--all]              one review page of the open drafts

One open draft per recipient: `new` to someone who already has an open draft joins that draft
(the caller becomes a contributor and its text a new revision) unless --new. Every text write
keeps a revision (revisions/NNN.md) with the session that wrote it. Changes by a live session
that is neither the owner nor a contributor are refused without --force (ask that session
instead); a draft whose session is gone is adopted by whoever changes it. Closing is a soft delete.

History (#166): stage reads the newest ingested messages of the target chat (teams ingest) and
reports them, skipping a draft that already appears there as your message; mark-sent --check
marks staged drafts sent once they show up in the ingested chat.

A draft is a folder, ~/.drafts/<id>/ (WHOSAID_DRAFTS_DIR overrides): draft.md, claims.json,
draft.json. Pages stay where they were written and are referenced by absolute path.

Verify gates: draft.md exists with no em dash; every claim has a verdict (confirmed, refuted,
unverified); a refuted claim is marked dropped; a confirmed claim has evidence and a tech claim
cites a URL; at least one stakeholder, each with the topics searched. Verify hashes the message,
claims and pages; any later edit reads as stale until verified again.

Said claims (#214): kind "said" (who said, agreed or decided what) needs evidence that is a list of
turn refs, {meeting, t, quote} or {message_id, quote} (the index id or the raw Teams mid). Verify
resolves each ref through the workspace index (--ws, else WHOSAID_WORKSPACE), the exact turn at that
time, and fails when it does not resolve, when the quote (3+ words, fuzzy so ASR noise survives) is
not in that turn, or when the claim cites only a paraphrase or a summary (commitments.md, a recap,
items). `drafts claims ID --file F --ws WS` prints the resolved turns, so the real words come first.

Recap replies (#215): when the draft's chat thread id is known and the recipient's newest messages
(since our own last one before the draft) hold a recap ("I heard: A, B, C, D. Does that match your
understanding?", "to recap", a bullet list), verify splits it into points, searches each one over the
meetings the recipient spoke in during the last --recap-days (14; the chat itself never counts), and
requires a claim per point with `point: N` and `stance` agree|disagree|skip (skip is refused when the
draft text names the point or shares its words). Agree and disagree are
said claims; a disagree fails without a resolved turn, and the recap message is never a turn behind
its own point. The per-point turns come back from verify (and on failure in DraftsError.detail, which
the MCP tool returns), so the author fixes the draft against what was actually said.

Voice lint (lib/voice_lint.py, DEFAULT_PROFILE): verify also checks the message shape. Group B
(more than one paragraph, a greeting opener like "Hi Sam", or an implied one like "heads up", "can you review" or "FYI", an "attached page" or "Thanks!"
closer) fails verify when the workspace's whosaid.toml sets [drafts] voice_lint = "enforce";
group A (over the word limit, "want me to" asks, banned words and characters) only warns.
"warn" (the default, also with no workspace) reports every hit as a warning; "off" skips it.
[drafts.voice] in whosaid.toml merges over the default profile. The workspace is --ws, else
WHOSAID_WORKSPACE (never the cwd). A real exception gets a per-draft override with a reason:
`verify ID --voice warn --reason R` (kept in draft.json and its history; --voice clear drops it).

Staging is the only step that drives a browser. It holds ~/.drafts/.outbox.lock, refuses stale
drafts and rival open drafts to the same recipient, then runs the stager command from
WHOSAID_DRAFTS_STAGER, or the built-in Teams web stager (lib/drafts_stager.py, a browser on the
DevTools port) when that is unset (argv: --chat NAME --md FILE [--attach PAGE ...] [--mention NAME ...]
[--replace --only-these-attachments] [--new-chat] [--tab ID]) with TEAMS_OUTBOX_LOCK set.
Turning --mention names into real @mentions is the stager's job. Nothing is sent.

Stdlib only; stakeholders and stale-check read the workspace search index (search.py).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html as htmllib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPEN = ("drafting", "verified", "staged")
VERDICTS = ("confirmed", "refuted", "unverified")
KINDS = ("code", "tech", "people", "status", "said")
# evidence that names one of these is a summary of what was said, never the words themselves (#214)
SUMMARY_REF = re.compile(r"commitments\.md|_wiki\.md|action[-_ ]?items?|items\.json|\brecap\b|summary|insights|"
                         r"worklist|obligations", re.I)
QUOTE_MIN_WORDS = 3
QUOTE_RATIO = 0.8
UNNAMED = re.compile(r"^SPEAKER_\d+$")
EM_DASH = "\u2014"
VOICE_MODES = ("enforce", "warn", "off")


class DraftsError(Exception):
    """A refused request: bad id, failed gate, held lock, missing stager. detail: structured data
    for the caller (verify's per-point recap turns), merged into the MCP error payload."""

    def __init__(self, msg: str = "", detail: dict | None = None):
        super().__init__(msg)
        self.detail = detail or {}


# ---- storage -----------------------------------------------------------------------

def root() -> Path:
    r = Path(os.environ.get("WHOSAID_DRAFTS_DIR") or Path.home() / ".drafts").expanduser()
    r.mkdir(parents=True, exist_ok=True)
    return r


def ddir(draft_id: str) -> Path:
    if not draft_id or "/" in draft_id or draft_id.startswith("."):
        raise DraftsError(f"bad draft id {draft_id!r}")
    return root() / draft_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_json(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


def load(draft_id: str) -> dict:
    m = _read_json(ddir(draft_id) / "draft.json")
    if not m:
        raise DraftsError(f"no draft {draft_id}")
    return m


def save(m: dict) -> None:
    p = ddir(m["id"]) / "draft.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=2) + "\n")
    tmp.replace(p)


def all_drafts() -> list[dict]:
    return [load(d.name) for d in sorted(root().iterdir()) if (d / "draft.json").exists()]


def slug(s: str) -> str:
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", s.lower()))[:40]


def alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


# ---- owner -------------------------------------------------------------------------

def owner(session_id: str | None = None) -> dict:
    """The agent session that is writing: env plus the Claude Code session registry."""
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    rec: dict = {}
    sd = Path(os.environ.get("WHOSAID_SESSIONS_DIR") or Path.home() / ".claude" / "sessions")
    if sid and sd.is_dir():
        for f in sd.glob("*.json"):
            try:
                r = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(r, dict) and r.get("sessionId") == sid:
                rec = r
                break
    cwd = rec.get("cwd") or os.getcwd()
    pid = rec.get("pid") or (os.environ.get("CLAUDE_PID") if not session_id else None)
    return {
        "sessionId": sid, "name": rec.get("name"), "pid": int(pid) if pid else None, "cwd": cwd,
        "jobId": rec.get("jobId"), "agent": os.environ.get("AI_AGENT"),
        "resume": f"cd {json.dumps(cwd)} && claude --resume {sid}" if sid else None,
    }


def owner_line(o: dict) -> str:
    sid = (o.get("sessionId") or "no-session")[:8]
    return f"{o.get('name') or '(unnamed)'} {sid} {'live' if alive(o.get('pid')) else 'gone'}"


# ---- hash, state, gates ------------------------------------------------------------

def content_hash(m: dict) -> str:
    """sha256 over (path, bytes) of draft.md, claims.json and each page; same as the prototype."""
    h = hashlib.sha256()
    d = ddir(m["id"])
    for f in [str(d / "draft.md"), str(d / "claims.json"), *m.get("pages", [])]:
        p = Path(f)
        h.update(f.encode() + b"\0" + (p.read_bytes() if p.exists() else b"MISSING") + b"\0")
    # Mentions are staged too, so they count; drafts without any hash exactly as before.
    if m.get("mentions"):
        h.update(b"mentions\0" + json.dumps(sorted(m["mentions"])).encode() + b"\0")
    return h.hexdigest()[:16]


def state(m: dict) -> str:
    if m["status"] == "sent":
        return edit_state(m)
    if m["status"] not in OPEN:
        return m["status"]
    h = content_hash(m)
    if m["status"] == "staged" and m.get("staged_hash") == h:
        return "staged"
    if m.get("verified_hash") == h:
        return "verified, changed since staged" if m["status"] == "staged" else "verified"
    return "stale, edited after verify" if m.get("verified_hash") else "drafting"


# ---- edits of a sent message (#209) -------------------------------------------------
# A sent draft takes a revised text (and its claims), is verified again, and `stage --edit` loads it
# into Teams' Edit box on the sent message; the user saves. sent_hash is the content as it went out
# (or as the last edit that landed left it); anything else is a pending edit. Pages are not part of
# an edit (Teams can't change a sent message's attachments), so the edit hash leaves them out.

def edit_hash(m: dict) -> str:
    h = hashlib.sha256(b"edit\0")
    d = ddir(m["id"])
    for f in (d / "draft.md", d / "claims.json"):
        h.update(f.name.encode() + b"\0" + (f.read_bytes() if f.exists() else b"MISSING") + b"\0")
    h.update(json.dumps(sorted(m.get("mentions") or [])).encode())
    return h.hexdigest()[:16]


def _changed_since_sent(m: dict, h: str) -> bool:
    if m.get("sent_edit_hash"):
        return h != m["sent_edit_hash"]
    # sent before #209: pending when the text has a revision newer than the sent event
    sent_at = max((e.get("at") or "" for e in m.get("history") or [] if e.get("event") in ("sent", "edit-staged")),
                  default="")
    revs = m.get("revisions") or []
    return bool(sent_at and revs and (revs[-1].get("at") or "") > sent_at)


def edit_state(m: dict) -> str:
    """sent, or for a sent draft whose text or claims moved on: sent, edit drafting | edit verified | edit staged."""
    h = edit_hash(m)
    if not _changed_since_sent(m, h):
        return "sent"
    if m.get("edit_staged_hash") == h:
        return "sent, edit staged"
    if m.get("edit_verified_hash") == h:
        return "sent, edit verified"
    return "sent, edit drafting"


def edit_pending(m: dict) -> bool:
    return m["status"] == "sent" and edit_state(m) != "sent"


def _actor(by: str | None = None) -> str | None:
    """Who closes or edits a draft (#228): the given name (e.g. "watcher"), else the agent session."""
    return by or os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def _mark_sent(m: dict, hit: dict | None, how: str, by: str | None = None) -> None:
    """status sent, with the message it went out as and the baseline later edits compare against."""
    m["status"] = "sent"
    if hit:
        m["sent_message"] = {k: hit.get(k) for k in ("at", "mid", "folder")}
    m["sent_edit_hash"] = edit_hash(m)
    m["sent_text"] = _draft_text(m)
    _event(m, "sent", how=how, message=m.get("sent_message"), by=_actor(by))


def _plain_links(s: str) -> str:
    """_plain plus the link targets, so an edit that only adds or fixes a link counts as a change."""
    urls = set(re.findall(r"\]\((\S+?)\)", s or "")) | set(re.findall(r"https?://[^\s)\]>]+", s or ""))
    return _plain(s) + " " + " ".join(sorted(urls))


def claims_of(m: dict) -> dict:
    return _read_json(ddir(m["id"]) / "claims.json", {"claims": [], "stakeholders": [], "docs": []})


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (s or "").lower())


def quote_score(quote: str, text: str) -> float:
    """How well QUOTE appears in TEXT, 0..1: 1.0 for the words in order, else the best ratio of the
    quote against any window of the turn about its length (survives ASR noise: a dropped or misheard
    word), so a short quote inside a long turn is not diluted by the rest of the turn."""
    q, t = _words(quote), _words(text)
    if not q or not t:
        return 0.0
    qs = " ".join(q)
    if f" {qs} " in f" {' '.join(t)} ":
        return 1.0
    best = 0.0
    for size in range(max(1, len(q) - 2), len(q) + 3):
        for i in range(0, max(1, len(t) - size + 1)):
            best = max(best, difflib.SequenceMatcher(None, qs, " ".join(t[i:i + size]), autojunk=False).ratio())
    return round(best, 3)


def _hms(sec: int) -> str:
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _turn_ref(r) -> bool:
    return isinstance(r, dict) and bool(r.get("message_id") or r.get("mid") or (r.get("meeting") and
                                        (r.get("t") is not None or r.get("t_sec") is not None)))


def _summary_label(r) -> str | None:
    """The summary a non-turn evidence item names (commitments.md, a live recap, items), else None."""
    if isinstance(r, dict):
        r = " ".join(str(v) for k, v in r.items() if k in ("file", "path", "source", "summary", "ref", "kind"))
    r = str(r or "").strip()
    return r if r and SUMMARY_REF.search(r) else None


def _sidecar_message(ws: Path, mid: str) -> dict | None:
    """A chat or mail message by its raw source id (the Teams mid) from the ingested sidecars."""
    from message_identity import TEXT_KINDS
    try:
        folders = sorted((p for p in Path(ws).iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return None
    for f in folders:
        for sc in f.glob("*.diarization.json"):
            try:
                data = json.loads(sc.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or (data.get("source") or {}).get("kind") not in TEXT_KINDS:
                continue
            for seg in data.get("segments") or []:
                if isinstance(seg, dict) and mid in (str(seg.get("mid") or ""), str(seg.get("message_id") or "")):
                    sec = int(seg.get("start") or 0)
                    return {"meeting": f.name, "t_sec": sec, "t_str": _hms(sec), "speaker": seg.get("speaker") or "",
                            "text": seg.get("text") or "", "message_id": mid,
                            "attribution_uncertain": bool(seg.get("attribution_uncertain"))}
    return None


def _uncertain(conn, rowid: int) -> bool:
    """#216: the index row's attribution_uncertain flag, the same source search hits read
    (search.annotate_hit): a short reply the diarizer split off at a turn boundary but could not place."""
    import search
    if not search.has_table(conn, "seg_message"):
        return False
    row = conn.execute("SELECT attribution FROM seg_message WHERE rowid = ?", (rowid,)).fetchone()
    try:
        return bool(row and json.loads(row[0] or "{}").get("attribution_uncertain"))
    except (ValueError, AttributeError):
        return False


def _resolve_ref(conn, ws: Path, ref: dict) -> tuple[list[dict], str | None]:
    """The candidate turns REF points at (by meeting + time, or by message id), or an error. Each turn
    carries attribution_uncertain (#216) from the index, so a said claim never rests on one alone."""
    import search
    mid = str(ref.get("message_id") or ref.get("mid") or "")
    if mid:
        rows = []
        if search.has_table(conn, "seg_message"):
            rows = conn.execute("SELECT s.meeting, s.t_sec, s.t_str, s.speaker, s.text, s.rowid FROM seg_message m "
                                "JOIN seg s ON s.rowid = m.rowid WHERE m.message_id = ?", (mid,)).fetchall()
        turns = [{"meeting": a, "t_sec": int(b), "t_str": c, "speaker": d, "text": e, "message_id": mid,
                  "attribution_uncertain": _uncertain(conn, rid)} for a, b, c, d, e, rid in rows]
        if not turns:
            hit = _sidecar_message(ws, mid)
            turns = [hit] if hit else []
        return (turns, None) if turns else ([], f"message {mid} not found in the workspace (ingested chats or mail)")
    try:
        name = search.resolve_meeting(conn, str(ref["meeting"]))
    except search.SearchError as e:
        return [], str(e).split("; known:")[0]
    try:
        sec = int(ref["t_sec"]) if ref.get("t_sec") is not None else search.parse_at(str(ref["t"]))
    except (ValueError, TypeError) as e:
        return [], f"bad time in {ref}: {e}"
    rows = conn.execute("SELECT meeting, t_sec, t_str, speaker, text, rowid FROM seg WHERE meeting = ? AND t_sec = ?",
                        (name, sec)).fetchall()
    if not rows:
        return [], f"no turn at {_hms(sec)} in {name}"
    return [{"meeting": a, "t_sec": int(b), "t_str": c, "speaker": d, "text": e,
             "attribution_uncertain": _uncertain(conn, rid)} for a, b, c, d, e, rid in rows], None


def said_check(claims: dict, ws=None) -> dict:
    """#214: every confirmed said claim cites real turns. Each turn ref ({meeting, t, quote} or
    {message_id, quote}) must resolve through the workspace index, and its quote (or the claim's) must
    appear in that turn, fuzzily. A claim citing only a summary (commitments.md, a recap, items) or a
    paraphrase fails. Returns {errors: ["said claim K: ..."], said: [{key, text, turns, errors}]}."""
    out = {"errors": [], "said": []}
    todo = [k for k in claims.get("claims") or [] if k.get("kind") == "said" and k.get("verdict") == "confirmed"]
    if not todo:
        return out
    wsp = _voice_ws(ws)
    conn, why = None, None
    if wsp is None:
        why = "needs a workspace (--ws or WHOSAID_WORKSPACE) to resolve its turns"
    else:
        import search
        try:
            conn = search.open_index(wsp)
        except search.SearchError as e:
            why = str(e)
    try:
        for k in todo:
            kid = k.get("key") or (k.get("text") or "")[:40]
            row = {"key": kid, "text": k.get("text"), "point": k.get("point"), "turns": [], "errors": []}
            out["said"].append(row)
            ev = k.get("evidence")
            ev = ev if isinstance(ev, list) else [ev] if ev else []
            refs = [r for r in ev if _turn_ref(r)]
            if not refs:
                labels = [x for x in (_summary_label(r) for r in ev) if x]
                row["errors"].append(f"cites only a summary ({', '.join(labels)}), not a turn: cite "
                                     "{meeting, t, quote} or {message_id, quote}" if labels else
                                     "evidence must cite transcript turns [{meeting, t, quote}] or Teams message ids "
                                     "[{message_id, quote}], not a paraphrase")
            elif why:
                row["errors"].append(why)
            for r in refs if not why else []:
                where = r.get("message_id") or r.get("mid") or f"{r.get('meeting')} @ {r.get('t', r.get('t_sec'))}"
                quote = (r.get("quote") or k.get("quote") or "").strip()
                if not quote:
                    row["errors"].append(f"{where}: needs the quoted words (quote), so verify can find them in the turn")
                    continue
                if len(_words(quote)) < QUOTE_MIN_WORDS:
                    row["errors"].append(f"{where}: quote too short ({QUOTE_MIN_WORDS}+ words), it could match anything")
                    continue
                turns, err = _resolve_ref(conn, wsp, r)
                if err:
                    row["errors"].append(f"{where}: {err}")
                    continue
                best = max(turns, key=lambda t: quote_score(quote, t["text"]))
                score = quote_score(quote, best["text"])
                said = f"[{best['meeting']} @ {best['t_str']}] {best['speaker']}: {best['text']}"
                if score < QUOTE_RATIO:
                    row["errors"].append(f"{where}: quote not found in that turn ({score:.2f} < {QUOTE_RATIO}); "
                                         f"it says {said[:300]}")
                    continue
                if r.get("speaker") and _norm(str(r["speaker"])) != _norm(best["speaker"] or ""):
                    row["errors"].append(f"{where}: spoken by {best['speaker']}, not {r['speaker']}")
                    continue
                row["turns"].append({**best, "quote": quote, "score": score})
            if row["turns"] and all(t.get("attribution_uncertain") for t in row["turns"]):
                row["errors"].append("only cites turns whose speaker is uncertain (attribution_uncertain: a short "
                                     "reply split off at a turn boundary, " + ", ".join(
                                         f"{t['meeting']} @ {t['t_str']}" for t in row["turns"]) +
                                     "); a yes nobody can place is not their agreement: cite a turn whose "
                                     "speaker is certain, or mark the claim unverified")
            out["errors"] += [f"said claim {kid}: {e}" for e in row["errors"]]
    finally:
        if conn is not None:
            conn.close()
    return out


# ---- recap replies (#215): answer the recipient's own recap point by point ------------------

RECAP_RE = re.compile(r"\bI heard\b|\b(?:does|did) (?:that|this|it) (?:match|sound right)|\bmatch(?:es)? your "
                      r"understanding|\bto recap\b|^\s*recap\b|\bmy understanding is\b|\bdid I get (?:that|this|it) "
                      r"right|\bmy takeaways?\b", re.I | re.M)
_RECAP_LEAD = re.compile(r"^.*?(?:\bI heard|\bto recap|^\s*recap|\bmy understanding is|\bmy takeaways?(?: (?:are|is|were))?)"
                         r"(?:\s+(?:that|was|is))?\s*[:,-]?\s*", re.I | re.S)
_RECAP_ASK = re.compile(r"\b(?:does|did) (?:that|this|it) (?:match|sound right)|\bmatch(?:es)? your understanding|"
                        r"\bdid I get (?:that|this|it) right|\banything I missed\b|\bcorrect me\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\(?\d+[.)])\s+")
STANCES = ("agree", "disagree", "skip")
STOP = set("a an and are as at be been but by did do does for from had has have i if in is it its me my of "
           "on or our so that the their them then there they this to was we were what when which who will "
           "with you your heard recap understanding match".split())


def _drop_asks(s: str) -> str:
    """The recap without its question ("Does that match your understanding?"), sentence by sentence."""
    parts = re.split(r"(?<=[.?!;])\s+", s.strip())
    keep = []
    for p in parts:
        m = _RECAP_ASK.search(p)
        if m:
            p = p[:m.start()]
        keep.append(p)
    return " ".join(x for x in keep if x.strip()).strip()


def recap_points(text: str) -> list[str]:
    """The points of a recap message: its bullet or numbered lines, else inline 1) 2) numbering, else
    the list after "I heard" / "to recap" split on semicolons or commas (and a final "and")."""
    lines = [x for x in (text or "").splitlines() if x.strip()]
    items = [_BULLET.sub("", x).strip() for x in lines if _BULLET.match(x)]
    if len(items) < 2:
        flat = _drop_asks(_RECAP_LEAD.sub("", " ".join(x.strip() for x in lines), count=1))
        nums = re.split(r"(?:^|\s)\(?\d+[.)]\s+", flat)
        if len(nums) >= 3:
            items = nums[1:]
        else:
            body = flat
            items = re.split(r"\s*;\s*", body) if ";" in body else re.split(r",\s*", body)
            if len(items) >= 2:
                items[-1] = re.sub(r"^and\s+", "", items[-1].strip())
            elif len(items) == 1:
                items = re.split(r"\s+and\s+", items[0])
    out = []
    for x in items:
        x = _drop_asks(x).strip().rstrip(".;,:!?").strip()
        x = re.sub(r"^(?:and|also|then)\s+", "", x, flags=re.I)
        if len(_words(x)) >= 2:
            out.append(x)
    return out


def find_recap(ws: Path, m: dict) -> dict | None:
    """The recipient's newest recap message in the draft's chat, when the thread id is known and it
    came after our own last message before the draft (an answered recap is not asked again)."""
    if m.get("new_chat") or not chat_thread_id(m, ws):
        return None
    msgs = draft_chat_messages(ws, m)
    created = _ms(m.get("created_at")) or int(time.time() * 1000)
    ours = [x["epoch_ms"] for x in msgs if x["self"] and x["epoch_ms"] < created]
    since = max(ours) if ours else -1
    theirs = [x for x in msgs if not x["self"] and x["epoch_ms"] > since and RECAP_RE.search(x["text"] or "")]
    for x in reversed(theirs):
        pts = recap_points(x["text"])
        if len(pts) >= 2:
            return {**{k: x[k] for k in ("at", "speaker", "mid", "folder", "text", "epoch_ms")}, "points": pts,
                    "chat_folders": sorted({y["folder"] for y in msgs})}
    return None


def _is_recap(text: str, recap: dict) -> bool:
    a, b = _plain(text), _plain(recap.get("text") or "")
    return bool(a) and (a == b or (len(a) >= 20 and a in b))


def _point_query(point: str) -> str:
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(_words(point)) if w not in STOP and len(w) > 2)


def _attended(conn, speaker: str, days: int) -> set[str]:
    """Meetings in the last DAYS where SPEAKER has a turn (the index knows who spoke, not who listened)."""
    floor = datetime.now().astimezone() - timedelta(days=days)
    rows = conn.execute("SELECT DISTINCT meeting FROM seg WHERE lower(speaker) = lower(?)", (speaker,)).fetchall()
    return {mt for (mt,) in rows if (_meeting_time(mt) or floor) >= floor}


def _point_turns(wsp: Path, q: str, meetings: set[str], rc: dict, k: int) -> list[dict]:
    """The top K turns for one point, searched inside each attended meeting (FTS5 OR over the point's
    content words) and merged best first: a global search filtered afterwards loses the supporting
    turn once older or unattended meetings fill its result list."""
    if not q or not meetings:
        return []
    import search
    try:
        conn = search.open_index(wsp)
    except search.SearchError:
        return []
    hits = []
    try:
        for mt in sorted(meetings):
            try:
                hits += [h for h in search.exact_hits(conn, q, k * 3, meeting=mt)
                         if h.meeting == mt and not _is_recap(h.text, rc)]  # meeting= is a substring filter
            except search.SearchError:
                continue
    finally:
        conn.close()
    hits.sort(key=lambda h: (-h.score, h.meeting, h.t_sec))
    return [{"meeting": h.meeting, "t_str": h.t_str, "speaker": h.speaker, "text": h.text} for h in hits[:k]]


_NUM_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth"}


def _mentions_point(draft: str, n: int, point: str) -> bool:
    """The draft text refers to recap point N: by number ("point 1", "points 3 and 4", "#1", "the
    first point") or by sharing two or more of its content words."""
    d = (draft or "").lower()
    if re.search(rf"\bpoints?\s+(?:\d+\s*(?:,|and|&|or)\s*)*{n}\b", d) or re.search(rf"(?:^|\s)#{n}\b", d):
        return True
    if n in _NUM_WORDS and re.search(rf"\b{_NUM_WORDS[n]} (?:point|one)\b", d):
        return True
    words = {w for w in _words(point) if w not in STOP and len(w) > 3}
    return len(words & set(_words(draft))) >= 2


def recap_check(ws, m: dict, said: dict, days: int = 14, k: int = 3) -> dict:
    """#215: the draft replies in a chat whose newest message from the recipient is a recap ("I heard:
    A, B, C, D. Does that match?"). Each point gets the top turns from meetings the recipient spoke in
    over the last DAYS, and needs a claim {point: N, stance: agree|disagree|skip}: agree and disagree
    are said claims, a disagree must cite a resolved turn, and the recap message never counts as one.
    Returns {recap: None | {...points: [{n, text, turns, claims, stance}]}, errors}."""
    wsp = _voice_ws(ws)
    if wsp is None:
        return {"recap": None, "errors": []}
    try:
        rc = find_recap(wsp, m)
    except Exception:  # noqa: BLE001 - an unreadable chat is no recap, never a crash
        rc = None
    if not rc:
        return {"recap": None, "errors": []}
    import search
    try:
        conn = search.open_index(wsp)
    except search.SearchError:
        conn = None
    try:
        # the chat being answered is not where its points were made: its days never count as meetings
        meetings = (_attended(conn, rc["speaker"], days) - set(rc["chat_folders"])) if conn else set()
    finally:
        if conn is not None:
            conn.close()
    claims = [x for x in claims_of(m).get("claims") or [] if not x.get("dropped")]
    resolved = {x["key"]: x for x in said.get("said") or []}
    draft_text = _draft_text(m)
    points, errs = [], []
    for n, text in enumerate(rc["points"], 1):
        turns = _point_turns(wsp, _point_query(text), meetings, rc, k)
        mine = [x for x in claims if str(x.get("point")) == str(n)]
        stance = next((x.get("stance") for x in mine if x.get("stance")), None)
        row = {"n": n, "text": text, "turns": turns, "claims": [x.get("key") for x in mine], "stance": stance}
        points.append(row)
        why = []
        if not mine:
            why.append(f"no claim answers it: add a said claim with point {n} and stance agree|disagree "
                       "(stance skip when the draft leaves it alone)")
        for x in mine:
            kid = x.get("key") or (x.get("text") or "")[:40]
            st = x.get("stance")
            if st not in STANCES:
                why.append(f"claim {kid}: stance must be agree, disagree or skip")
                continue
            if st == "skip":
                if _mentions_point(draft_text, n, text):
                    why.append(f"claim {kid}: skip, but the draft talks about point {n}: agree or disagree with a said claim")
                continue
            if x.get("kind") != "said":
                why.append(f"claim {kid}: {st} needs a said claim (kind said) citing the turn")
                continue
            got = resolved.get(kid) or {}
            if any(_is_recap(t["text"], rc) for t in got.get("turns") or []):
                why.append(f"claim {kid}: cites the recipient's own recap, not a turn behind it")
            elif st == "disagree" and (x.get("verdict") != "confirmed" or not got.get("turns") or got.get("errors")):
                why.append(f"claim {kid}: disagrees without a cited turn")
        if why:
            shown = "; turns behind the point: " + " | ".join(
                f"[{t['meeting']} @ {t['t_str']}] {t['speaker']}: {t['text'][:200]}" for t in turns) if turns else \
                "; no turn found for it in the recipient's meetings of the last {days} days".format(days=days)
            errs.append(f'recap point {n} "{text}": ' + "; ".join(why) + shown)
    recap = {"from": rc["speaker"], "at": rc["at"], "mid": rc["mid"], "folder": rc["folder"], "text": rc["text"],
             "days": days, "points": points}
    return {"recap": recap, "errors": errs}


def _cites_uncertain(evidence) -> bool:
    """True when claim evidence includes a whosaid hit flagged attribution_uncertain (#216):
    a "yes" whose speaker the transcript could not place is nobody's agreement."""
    if isinstance(evidence, dict):
        return evidence.get("attribution_uncertain") is True or any(_cites_uncertain(v) for v in evidence.values())
    if isinstance(evidence, list):
        return any(_cites_uncertain(v) for v in evidence)
    return False


def gate(m: dict, ws=None, said: bool = True) -> list[str]:
    """Every open gate. said=False skips resolving said claims' turns (stage and edit without a
    workspace: the verify stamp already covers them)."""
    return _gate(m, ws, said)[0]


def _gate(m: dict, ws=None, check_said: bool = True) -> tuple[list[str], dict]:
    d = ddir(m["id"])
    errs: list[str] = []
    md = d / "draft.md"
    if not md.exists():
        errs.append("draft.md missing")
    elif EM_DASH in md.read_text():
        errs.append("draft.md has an em dash")
    for p in m.get("pages", []) if m["status"] != "sent" else []:  # an edit can't change attachments
        if not Path(p).exists():
            errs.append(f"page missing: {p}")
    c = _read_json(d / "claims.json")
    if c is None:
        return errs + ["claims.json missing"], {"errors": [], "said": []}
    claims = c.get("claims") or []
    if not claims:
        errs.append("claims.json has no claims")
    for k in claims:
        kid = k.get("key") or (k.get("text") or "")[:40]
        v = k.get("verdict")
        if v not in VERDICTS:
            errs.append(f"claim {kid}: verdict must be confirmed|refuted|unverified")
        if v == "refuted" and not k.get("dropped"):
            errs.append(f"claim {kid}: refuted but not marked dropped from the draft")
        if v == "confirmed" and not k.get("evidence"):
            errs.append(f"claim {kid}: confirmed without evidence")
        if k.get("kind") == "tech" and v == "confirmed" and not re.search(r"https?://", json.dumps(k.get("evidence"))):
            errs.append(f"claim {kid}: tech claim needs a doc URL in evidence")
        if v == "confirmed" and _cites_uncertain(k.get("evidence")):
            errs.append(f"claim {kid}: confirmed on a turn whose speaker is uncertain (attribution_uncertain, "
                        f"e.g. a short reply split off at a turn boundary); cite another turn or mark it unverified")
    sh = c.get("stakeholders") or []
    if not sh:
        errs.append("stakeholders[] empty: who has opinions on this, and what did whosaid say")
    for s in sh:
        if not s.get("searched"):
            errs.append(f"stakeholder {s.get('name')}: no whosaid topics searched")
    said = said_check(c, ws) if check_said else {"errors": [], "said": []}
    return errs + said["errors"], said


def _event(m: dict, event: str, **kw) -> None:
    m.setdefault("history", []).append({"at": now(), "event": event, **kw})
    m["updated_at"] = now()


# ---- operations (CLI and MCP share these) ------------------------------------------

def _who(o: dict) -> dict:
    return {"sessionId": o.get("sessionId"), "name": o.get("name")}


def new(to: str, topic: str, pages: list[str] | None = None, supersedes: str | None = None,
        new_chat: bool = False, owner_session: str | None = None, mentions: list[str] | None = None,
        separate: bool = False, thread_id: str | None = None) -> dict:
    """Start a draft, or join the open draft to the same recipient (one compose box per chat).

    separate=True (CLI --new) or supersedes makes a second draft instead of joining. thread_id = the
    chat's Teams thread id; without one (and not new_chat) it is filled from the Teams desktop cache
    when exactly one cached chat carries the recipient's name."""
    to, topic = (to or "").strip(), (topic or "").strip()
    if not to:
        raise DraftsError('need the recipient, exactly as the chat list shows the name ("First Last")')
    if not topic:
        raise DraftsError("need a topic")
    pages = list(pages or [])
    for p in pages:
        if not os.path.isabs(p):
            raise DraftsError(f"page needs an absolute path: {p}")
    mentions = [x.strip() for x in mentions or [] if x and x.strip()]
    thread_id = check_thread_id(thread_id)
    if supersedes:
        load(supersedes)
    me = owner(owner_session)
    clash = [d for d in all_drafts() if d["status"] in OPEN and d["to"] == to and d["id"] != supersedes]
    closed, check_errors = _close_sent_rivals(clash)
    if closed:  # #228: a staged draft that already went out is closed, never built on
        clash = [d for d in clash if d["id"] not in closed]
    if clash and not separate and not supersedes:
        joined = _join(max(clash, key=lambda d: d.get("updated_at") or ""), me, topic, pages, mentions, new_chat,
                       thread_id)
        if closed:
            joined["closed_sent"] = closed
        if check_errors:
            joined["sent_check_errors"] = check_errors
        return joined
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    draft_id = f"{ts}-{slug(to)}-{slug(topic)}"
    d = ddir(draft_id)
    if (d / "draft.json").exists():
        raise DraftsError(f"{draft_id} exists; edit it instead")
    d.mkdir(parents=True, exist_ok=True)
    m = {"id": draft_id, "to": to, "topic": topic, "status": "drafting", "new_chat": bool(new_chat),
         "pages": pages, "mentions": mentions, "supersedes": supersedes, "owner": me, "contributors": [],
         "revisions": [], "created_at": now(), "updated_at": now(),
         "verified_hash": None, "staged_hash": None, "history": [{"at": now(), "event": "new", "by": me["sessionId"]}]}
    if thread_id:
        m["thread_id"], m["thread_id_source"] = thread_id, "given"
    elif not new_chat:
        found = chat_thread_id(m, _voice_ws())
        if found:
            m["thread_id"], m["thread_id_source"] = found, "teams cache"
    save(m)
    (d / "claims.json").write_text(json.dumps({"claims": [], "stakeholders": [], "docs": []}, indent=2) + "\n")
    out = {"id": draft_id, "path": str(d), "joined": False, "thread_id": m.get("thread_id"),
           "next": "write claims and stakeholders, verify them, then the text"}
    if closed:
        out["closed_sent"] = closed
    if check_errors:
        out["sent_check_errors"] = check_errors
    if clash:
        out["note"] = (f"open draft(s) to {to} already: " + ", ".join(f"{c['id']} [{owner_line(c['owner'])}]" for c in clash)
                       + ". Teams has one compose box per chat: stage refuses both until one is dropped or merged (drafts merge)"
                       + (", or this one supersedes it." if not supersedes else "."))
    return out


def _close_sent_rivals(clash: list[dict]) -> tuple[list[str], list[dict]]:
    """(ids of the staged drafts in CLASH that the chat (WHOSAID_WORKSPACE) shows already went out, now marked
    sent; the checks that failed). No workspace: nothing checked, and new joins as before. A failed check is
    reported in new's result and on stderr, never raised."""
    ws = _voice_ws()
    staged = [d for d in clash if d["status"] == "staged"]
    if ws is None or not staged:
        return [], []
    closed, errors = [], []
    for d in staged:
        try:
            if sent_check(ws, d["id"])["sent"]:
                closed.append(d["id"])
        except Exception as e:  # noqa: BLE001 - reported below, never a reason new fails
            errors.append({"id": d["id"], "error": f"{type(e).__name__}: {e}"})
            print(f"whosaid drafts: sent check of {d['id']} failed ({e}); it may already be sent: "
                  "run mark-sent --check before staging", file=sys.stderr)
    return closed, errors


def check_thread_id(tid: str | None) -> str | None:
    """A Teams thread id ("19:...@thread.v2", "48:notes"): one token with a colon. None/"" is none."""
    tid = (tid or "").strip()
    if not tid:
        return None
    if re.search(r"\s", tid) or ":" not in tid:
        raise DraftsError(f"not a Teams thread id: {tid!r} (they look like 19:...@thread.v2)")
    return tid


def _join(m: dict, me: dict, topic: str, pages: list[str], mentions: list[str], new_chat: bool,
          thread_id: str | None = None) -> dict:
    """A second session's `new` to the same recipient: add it as a contributor of the open draft."""
    sid = me.get("sessionId")
    o = m.get("owner") or {}
    contributors = m.setdefault("contributors", [])
    if sid and sid != o.get("sessionId") and all(c.get("sessionId") != sid for c in contributors):
        contributors.append({**me, "joined_at": now()})
    for p in pages:
        if p not in m["pages"]:
            m["pages"].append(p)
    for x in mentions:
        if x not in m.setdefault("mentions", []):
            m["mentions"].append(x)
    m["new_chat"] = bool(m.get("new_chat") or new_chat)
    if thread_id and thread_id != m.get("thread_id"):
        if m.get("thread_id") and m.get("thread_id_source") not in ("teams cache", "staged"):
            raise DraftsError(f"{m['id']} already goes to thread {m['thread_id']}, not {thread_id}; "
                              "change it with write/thread-id if that one is wrong")
        m["thread_id"], m["thread_id_source"] = thread_id, "given"
    _event(m, "joined", by=sid, topic=topic)
    save(m)
    return {"id": m["id"], "path": str(ddir(m["id"])), "joined": True, "owner_line": owner_line(o),
            "note": (f"{m['to']} already has open draft {m['id']} (owner {owner_line(o)}, topic {m.get('topic')}); "
                     "you joined it. Read it (show), then write: your text becomes a new revision. "
                     "Pass separate/--new for a second draft instead."),
            "next": "show the draft, merge your points into its text and claims, write, verify"}


def _contributor(m: dict, sid: str) -> bool:
    return any(c.get("sessionId") == sid for c in m.get("contributors") or [])


def guard(m: dict, force: bool = False) -> None:
    """Refuse a change by a live session that is neither the owner nor a contributor, unless forced.

    No session in the environment means the user at a terminal: allowed. A draft whose owner is gone
    is adopted by the session changing it, so `show` points at the session that can still be resumed.
    """
    me = os.environ.get("CLAUDE_CODE_SESSION_ID")
    o = m.get("owner") or {}
    if not me or o.get("sessionId") in (None, me):
        return
    if _contributor(m, me) and alive(o.get("pid")):
        return
    if alive(o.get("pid")) and not force:
        raise DraftsError(f"{m['id']} belongs to live session {owner_line(o)}; ask it to make the change "
                          f"(SendMessage to {o.get('name') or o.get('sessionId')}), or pass force")
    if not alive(o.get("pid")):
        _event(m, "adopted", by=me, previous=o.get("sessionId"))
        m["owner"] = owner()


def _rev_path(draft_id: str, n: int) -> Path:
    return ddir(draft_id) / "revisions" / f"{n:03d}.md"


def revisions(m: dict) -> list[dict]:
    """Revision records, oldest first. A draft written before revisions existed has its current
    draft.md as revision 1 (virtual until the next write saves it)."""
    revs = list(m.get("revisions") or [])
    if not revs and (ddir(m["id"]) / "draft.md").exists():
        revs = [{"n": 1, "at": m.get("updated_at"), "by": _who(m.get("owner") or {}), "virtual": True}]
    return revs


def revision_text(m: dict, n: int) -> str:
    revs = revisions(m)
    if not 1 <= n <= len(revs):
        raise DraftsError(f"{m['id']} has revisions 1..{len(revs)}" if revs else f"{m['id']} has no text yet")
    if revs[n - 1].get("virtual"):
        return (ddir(m["id"]) / "draft.md").read_text()
    return _rev_path(m["id"], n).read_text()


def _save_revision(m: dict, text: str, by: dict, at: str | None = None) -> None:
    revs = m.setdefault("revisions", [])
    n = len(revs) + 1
    p = _rev_path(m["id"], n)
    p.parent.mkdir(exist_ok=True)
    p.write_text(text)
    revs.append({"n": n, "at": at or now(), "by": by, "chars": len(text)})


def diff(draft_id: str, a: int | None = None, b: int | None = None) -> dict:
    """Unified diff between two revisions; default the previous one against the newest."""
    m = load(draft_id)
    revs = revisions(m)
    if not revs:
        raise DraftsError(f"{draft_id} has no text yet")
    b = b or len(revs)
    a = a or max(1, b - 1)
    ta, tb = revision_text(m, a), revision_text(m, b)
    ra, rb = revs[a - 1], revs[b - 1]

    def label(n, r):
        by = r.get("by") or {}
        return f"rev {n} {(r.get('at') or '')[:16]} {by.get('name') or (by.get('sessionId') or 'user')[:8]}"
    lines = list(difflib.unified_diff(ta.splitlines(keepends=True), tb.splitlines(keepends=True),
                                      label(a, ra), label(b, rb)))
    return {"id": draft_id, "from": a, "to": b, "from_by": ra.get("by"), "to_by": rb.get("by"),
            "diff": "".join(lines), "same": ta == tb}


def write(draft_id: str, text: str | None = None, claims: list | None = None, stakeholders: list | None = None,
          docs: list | None = None, pages: list[str] | None = None, remove_pages: list[str] | None = None,
          force: bool = False, mentions: list[str] | None = None, thread_id: str | None = None) -> dict:
    """Replace the message text, mentions and/or the claims parts that are passed; add or remove pages.
    A changed text is kept as a new revision, tagged with the writing session. thread_id sets the
    chat's Teams thread id ("" clears it)."""
    m = load(draft_id)
    # a sent draft takes a corrected text and its claims (for stage --edit, #209); Teams can't change a
    # sent message's attachments or mentions, so nothing else
    edit_only = mentions is None and not pages and not remove_pages and thread_id is None
    if m["status"] not in OPEN and not (m["status"] == "sent" and edit_only):
        raise DraftsError(f"{draft_id} is {m['status']}" + (
            " (a sent draft takes only a corrected text, claims, stakeholders and docs, for stage --edit)"
            if m["status"] == "sent" else ""))
    guard(m, force)
    d = ddir(draft_id)
    for p in remove_pages or []:
        if p not in m["pages"]:
            raise DraftsError(f"not attached: {p} (attached: {', '.join(m['pages']) or 'none'})")
        m["pages"].remove(p)
    if mentions is not None:
        m["mentions"] = [x.strip() for x in mentions if x and x.strip()]
    if thread_id is not None:
        tid = check_thread_id(thread_id)
        if tid:
            m["thread_id"], m["thread_id_source"] = tid, "given"
        else:
            m.pop("thread_id", None)
            m.pop("thread_id_source", None)
    if text is not None:
        text = text if text.endswith("\n") else text + "\n"
        md = d / "draft.md"
        old = md.read_text() if md.exists() else None
        if old != text:
            if old is not None and not m.get("revisions"):
                _save_revision(m, old, _who(m.get("owner") or {}), m.get("updated_at"))
            md.write_text(text)
            _save_revision(m, text, _who(owner()) if os.environ.get("CLAUDE_CODE_SESSION_ID")
                           else {"sessionId": None, "name": "user"})
            m.pop("merge_text_joined", None)
    if claims is not None or stakeholders is not None or docs is not None:
        c = claims_of(m)
        for key, val in (("claims", claims), ("stakeholders", stakeholders), ("docs", docs)):
            if val is not None:
                if not isinstance(val, list):
                    raise DraftsError(f"{key} must be a list")
                c[key] = val
        (d / "claims.json").write_text(json.dumps(c, indent=2) + "\n")
    for p in pages or []:
        if not os.path.isabs(p) or not Path(p).exists():
            raise DraftsError(f"not an existing absolute path: {p}")
        if p not in m["pages"]:
            m["pages"].append(p)
    _event(m, "written")
    save(m)
    return {"id": draft_id, "state": state(m), "gate": gate(m)}


# ---- voice lint: the message shape, from lib/voice_lint.py ----------------------------

def _voice_ws(ws=None) -> Path | None:
    """--ws, else WHOSAID_WORKSPACE; never the cwd. None: no workspace, the defaults apply."""
    raw = str(ws or "").strip() or os.environ.get("WHOSAID_WORKSPACE", "").strip()
    return Path(raw).expanduser() if raw else None


def voice_settings(ws=None) -> dict:
    """{mode, profile, source}: [drafts] voice_lint and [drafts.voice] of the workspace's whosaid.toml."""
    import voice_lint
    wsp = _voice_ws(ws)
    mode, over, source = "warn", {}, "default"
    if wsp is not None and (wsp / "whosaid.toml").is_file():
        from wsconfig import load_config
        d = load_config(wsp).get("drafts") or {}
        mode = str(d.get("voice_lint") or "warn").strip().lower()
        over = d.get("voice") or {}
        source = str(wsp / "whosaid.toml")
    if mode not in VOICE_MODES:
        raise DraftsError(f"{source}: [drafts] voice_lint = {mode!r}; use one of {', '.join(VOICE_MODES)}")
    if not isinstance(over, dict):
        raise DraftsError(f"{source}: [drafts.voice] must be a table")
    unknown = set(over) - set(voice_lint.RULES) - {"groups"}
    if unknown:
        raise DraftsError(f"{source}: unknown [drafts.voice] keys: {', '.join(sorted(unknown))}")
    profile = {**voice_lint.DEFAULT_PROFILE, **over,
               "groups": {**voice_lint.DEFAULT_PROFILE["groups"], **(over.get("groups") or {})}}
    return {"mode": mode, "profile": profile, "source": source}


def _hit_text(hit) -> str:
    return ", ".join(f'"{x}"' for x in hit) if isinstance(hit, list) else str(hit)


def voice_check(m: dict, ws=None) -> dict:
    """Lint draft.md. errors: group B hits under enforce (verify refuses); warnings: the rest."""
    import voice_lint
    cfg = voice_settings(ws)
    ov = m.get("voice_lint") or {}
    mode = ov.get("mode") or cfg["mode"]
    out = {"mode": mode, "source": "draft override" if ov.get("mode") else cfg["source"],
           "override": ov or None, "errors": [], "warnings": [], "hits": {}}
    md = ddir(m["id"]) / "draft.md"
    if mode == "off" or not md.exists():
        return out
    r = voice_lint.lint(md.read_text(), cfg["profile"])
    out["hits"] = r["hits"]
    groups = cfg["profile"].get("groups") or {}
    for rule, hit in r["hits"].items():
        g = groups.get(rule) or "A"
        line = r["where"].get(rule)
        what = {"greeting": "opens with a greeting", "closers": f"closer {_hit_text(hit)}"}.get(rule, _hit_text(hit))
        at = "second paragraph starts" if rule == "max_paragraphs" else "at"
        msg = f"voice {g} {rule}: {what}" + (f', {at} "{line}"' if line else "")
        (out["errors"] if g == "B" and mode == "enforce" else out["warnings"]).append(msg)
    return out


def lint(draft_id: str | None = None, all_open: bool = False, ws=None) -> dict:
    """The voice lint alone, for one draft or every open one; reads only, stamps nothing."""
    if not draft_id and not all_open:
        raise DraftsError("lint needs an id or --all")
    rows = [load(draft_id)] if draft_id else [m for m in all_drafts() if m["status"] in OPEN]
    out = []
    for m in rows:
        v = voice_check(m, ws)
        out.append({"id": m["id"], "to": m["to"], "state": state(m), "would_fail": bool(v["errors"]),
                    **{k: v[k] for k in ("mode", "source", "override", "errors", "warnings")}})
    return {"drafts": out, "would_fail": sum(1 for r in out if r["would_fail"])}


def set_voice_override(m: dict, mode: str, reason: str | None) -> None:
    """Per-draft voice lint mode for a real exception; a reason is required, clear drops it."""
    if mode == "clear":
        if m.pop("voice_lint", None) is not None:
            _event(m, "voice_lint", mode="clear", by=os.environ.get("CLAUDE_CODE_SESSION_ID"))
        return
    if mode not in VOICE_MODES:
        raise DraftsError(f"--voice {mode!r}: use one of {', '.join(VOICE_MODES)} or clear")
    if not (reason or "").strip():
        raise DraftsError("a voice lint override needs --reason: why this draft is an exception")
    m["voice_lint"] = {"mode": mode, "reason": reason.strip(), "at": now(),
                       "by": os.environ.get("CLAUDE_CODE_SESSION_ID")}
    _event(m, "voice_lint", mode=mode, reason=reason.strip(), by=os.environ.get("CLAUDE_CODE_SESSION_ID"))


def verify(draft_id: str, force: bool = False, voice: str | None = None, reason: str | None = None,
           ws=None, recap_days: int = 14) -> dict:
    m = load(draft_id)
    editing = m["status"] == "sent"
    if editing and not edit_pending(m):
        raise DraftsError(f"{draft_id} is sent and unchanged; to correct the sent message write the new text "
                          "first (drafts text), then verify and stage --edit")
    if m["status"] not in OPEN and not editing:
        raise DraftsError(f"{draft_id} is {m['status']}")
    guard(m, force)
    if voice:
        set_voice_override(m, voice, reason)
        save(m)
    errs, said = _gate(m, ws)
    rc = recap_check(ws, m, said, days=recap_days)
    errs += rc["errors"]
    v = voice_check(m, ws)
    if m.get("merge_text_joined"):
        v["warnings"].append("merged text is still the joined drafts: rewrite it as one message (drafts text)")
    if errs or v["errors"]:
        msg = "not verified:\n  - " + "\n  - ".join(errs + v["errors"])
        if v["errors"]:
            msg += (f"\n  voice lint is enforced ({v['source']}): rewrite as one short paragraph with no "
                    f"greeting or closer line, or for a real exception: whosaid drafts verify {draft_id} "
                    "--voice warn --reason R")
        raise DraftsError(msg, detail={"recap": rc["recap"], "said": said["said"]})
    if editing:
        m["edit_verified_hash"], m["edit_verified_at"] = edit_hash(m), now()
    else:
        m["verified_hash"] = content_hash(m)
        m["verified_at"] = now()
    if m["status"] == "drafting":
        m["status"] = "verified"
    _event(m, "verified", hash=m.get("edit_verified_hash") if editing else m["verified_hash"], edit=editing or None,
           by=os.environ.get("CLAUDE_CODE_SESSION_ID"), voice_lint=v["mode"], voice_warnings=len(v["warnings"]))
    save(m)
    c = claims_of(m)
    n = {v_: sum(1 for k in c["claims"] if k.get("verdict") == v_) for v_ in VERDICTS}
    return {"id": draft_id, "state": state(m) if editing else "verified", **n, "stakeholders": len(c.get("stakeholders") or []),
            "voice_lint": v["mode"], "voice_warnings": v["warnings"], "said": said["said"], "recap": rc["recap"]}


def summary(m: dict) -> dict:
    return {"id": m["id"], "to": m["to"], "topic": m.get("topic"), "state": state(m), "owner": m["owner"],
            "owner_line": owner_line(m["owner"]), "owner_live": alive(m["owner"].get("pid")),
            "updated_at": m["updated_at"], "supersedes": m.get("supersedes"), "pages": m.get("pages", []),
            "mentions": m.get("mentions") or [], "revisions": len(revisions(m)),
            "contributors": [_who(c) for c in m.get("contributors") or []],
            "sent_message": m.get("sent_message"), "merged_from": m.get("merged_from") or [],
            "merged_into": m.get("merged_into"),
            "edits": [{k: e.get(k) for k in ("n", "staged_at", "edited_at", "mid", "rev")} for e in m.get("edits") or []]}


def listing(include_closed: bool = False, mine: bool = False, session_id: str | None = None) -> list[dict]:
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if mine and not sid:
        raise DraftsError("mine needs a Claude Code session (CLAUDE_CODE_SESSION_ID)")
    # a sent draft with a pending edit is open work too
    rows = [m for m in all_drafts() if include_closed or m["status"] in OPEN or edit_pending(m)]
    if mine:
        rows = [m for m in rows if m["owner"].get("sessionId") == sid]
    open_to: dict[str, int] = {}
    for m in all_drafts():
        if m["status"] in OPEN:
            open_to[m["to"]] = open_to.get(m["to"], 0) + 1
    out = []
    for m in rows:
        r = summary(m)
        r["rivals"] = open_to.get(m["to"], 1) - 1 if m["status"] in OPEN else 0
        out.append(r)
    return out


def show(draft_id: str, rev: int | None = None) -> dict:
    """The draft as it is now; rev=N swaps in the text of revision N (oldest is 1)."""
    m = load(draft_id)
    md = ddir(draft_id) / "draft.md"
    text = revision_text(m, rev) if rev else (md.read_text() if md.exists() else None)
    return {**summary(m), "text": text, "rev": rev or len(revisions(m)) or None,
            "revision_log": revisions(m), "claims": claims_of(m), "gate": gate(m), "history": m.get("history", []),
            "edits": m.get("edits") or []}


def close(draft_id: str, status: str, reason: str | None = None, force: bool = False,
          ws: Path | str | None = None) -> dict:
    """Soft delete: sent or dropped. The folder and its history stay; list --all still shows it.
    Sent by hand still looks for the message in the chat (WS, else WHOSAID_WORKSPACE) and keeps it (#228)."""
    if status not in ("sent", "dropped"):
        raise DraftsError("status must be sent or dropped")
    m = load(draft_id)
    if m["status"] not in OPEN:
        raise DraftsError(f"{draft_id} is already {m['status']}")
    guard(m, force)
    out: dict = {"id": draft_id, "status": status}
    if status == "sent":
        hit, err = _find_quietly(_voice_ws(ws), m)
        _mark_sent(m, hit, "by hand")
        m["history"][-1]["reason"] = reason
        out["sent_message"] = m.get("sent_message")
        if err:  # closed anyway; the result and history say the lookup failed, so --backfill can fix it
            out["lookup_error"] = m["history"][-1]["lookup_error"] = err
    else:
        m["status"] = status
        _event(m, status, reason=reason, by=_actor())
    save(m)
    return out


def _find_quietly(ws: Path | None, m: dict) -> tuple[dict | None, str | None]:
    """(find_sent's message or None, the read error or None). No workspace is (None, None). A lookup never
    blocks a close: the caller reports the error instead of raising it."""
    if ws is None:
        return None, None
    try:
        return find_sent(ws, m)[0], None
    except Exception as e:  # noqa: BLE001 - reported to the caller, never raised
        return None, f"{type(e).__name__}: {e}"


def unstage(draft_id: str, reason: str | None = None) -> dict:
    """Back from staged to verified, for a compose box someone cleared by hand: the next stage starts
    fresh (no --replace). Only the bookkeeping changes; the chat is not touched."""
    m = load(draft_id)
    if m["status"] != "staged":
        raise DraftsError(f"{draft_id} is {state(m)}, not staged")
    m["status"] = "verified"
    m.pop("staged_hash", None)
    m.pop("staged_at", None)
    _event(m, "unstaged", why=reason, by=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    save(m)
    return {"id": draft_id, "status": m["status"], "state": state(m)}


def _claim_id(k: dict) -> str:
    return k.get("key") or (k.get("text") or "")[:40]


def _same_json(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _merge_parts(parts: list[dict]) -> tuple[dict, list[dict]]:
    """Union the claims.json parts of several drafts (first draft wins on fields). Returns the merged
    {claims, stakeholders, docs} and the claim clashes (same key, different verdict or evidence)."""
    claims: dict[str, dict] = {}
    clashes: list[dict] = []
    stakeholders: dict[str, dict] = {}
    docs: list = []
    for part in parts:
        for k in part.get("claims") or []:
            kid = _claim_id(k)
            have = claims.get(kid)
            if have is None:
                claims[kid] = k
            elif have.get("verdict") != k.get("verdict") or not _same_json(have.get("evidence"), k.get("evidence")):
                clashes.append({"key": kid, "kept": {"verdict": have.get("verdict"), "evidence": have.get("evidence")},
                                "other": {"verdict": k.get("verdict"), "evidence": k.get("evidence")}})
        for s in part.get("stakeholders") or []:
            sid = s.get("speaker") or s.get("name") or ""
            have = stakeholders.get(sid)
            if have is None:
                stakeholders[sid] = {**s, "searched": list(s.get("searched") or [])}
                continue
            for t in s.get("searched") or []:
                if t not in have["searched"]:
                    have["searched"].append(t)
            for f, v in s.items():
                if f != "searched" and not have.get(f) and v:
                    have[f] = v
        for d in part.get("docs") or []:
            if not any(_same_json(d, x) for x in docs):
                docs.append(d)
    return {"claims": list(claims.values()), "stakeholders": list(stakeholders.values()), "docs": docs}, clashes


def merge(into: str, froms: list[str], text: str | None = None, force: bool = False,
          dry_run: bool = False) -> dict:
    """Fold open drafts to the same recipient into INTO (one compose box per chat).

    Claims union by key (a different verdict or evidence on one key refuses), stakeholders by speaker
    with their searched topics merged, pages, mentions and docs union, the FROM owners become INTO
    contributors. text is the merged message; without it the texts are joined with a blank line and
    verify flags the extra paragraphs. Each FROM is closed as dropped with merged_into; INTO gets
    merged_from and goes stale. dry_run computes the same result and writes nothing."""
    froms = list(dict.fromkeys(froms or []))
    if not froms:
        raise DraftsError("merge needs at least one FROM draft id")
    if into in froms:
        raise DraftsError(f"{into} cannot be merged into itself")
    m = load(into)
    srcs = [load(f) for f in froms]
    for d in [m, *srcs]:
        if d["status"] not in OPEN:
            raise DraftsError(f"{d['id']} is {d['status']}; only open drafts merge")
    odd = [d["id"] + " -> " + d["to"] for d in srcs if d["to"] != m["to"]]
    if odd:
        raise DraftsError(f"all drafts must go to the same recipient ({into} -> {m['to']}); differs: "
                          + ", ".join(odd))
    guard(m, force)
    for d in srcs:
        guard(d, force)
    parts = [claims_of(d) for d in [m, *srcs]]
    merged, clashes = _merge_parts(parts)
    pages = list(m.get("pages") or [])
    mentions = list(m.get("mentions") or [])
    for d in srcs:
        pages += [p for p in d.get("pages") or [] if p not in pages]
        mentions += [x for x in d.get("mentions") or [] if x not in mentions]
    texts = []
    for d in [m, *srcs]:
        f = ddir(d["id"]) / "draft.md"
        if f.exists() and f.read_text().strip():
            texts.append(f.read_text().strip())
    joined = text is None
    new_text = (text if text is not None else "\n\n".join(texts)).rstrip("\n") + "\n" if (text or texts) else None
    plan = {"into": into, "from": froms, "to": m["to"], "claims": merged["claims"],
            "stakeholders": merged["stakeholders"], "docs": merged["docs"], "pages": pages,
            "mentions": mentions, "clashes": clashes, "text": new_text, "text_joined": joined and len(texts) > 1}
    if dry_run:
        return {**plan, "dry_run": True}
    if clashes:
        raise DraftsError("claim clashes (same key, different verdict or evidence); fix one draft first:\n  - "
                          + "\n  - ".join(f"{c['key']}: {c['kept']} vs {c['other']}" for c in clashes))
    d = ddir(into)
    was_verified = bool(m.get("verified_hash"))
    (d / "claims.json").write_text(json.dumps(merged, indent=2) + "\n")
    m["pages"], m["mentions"] = pages, mentions
    m["new_chat"] = bool(m.get("new_chat") or any(s.get("new_chat") for s in srcs))
    if new_text is not None:
        md = d / "draft.md"
        old = md.read_text() if md.exists() else None
        if old != new_text:
            if old is not None and not m.get("revisions"):
                _save_revision(m, old, _who(m.get("owner") or {}), m.get("updated_at"))
            md.write_text(new_text)
            _save_revision(m, new_text, _who(owner()) if os.environ.get("CLAUDE_CODE_SESSION_ID")
                           else {"sessionId": None, "name": "user"})
    if plan["text_joined"]:
        m["merge_text_joined"] = True
    else:
        m.pop("merge_text_joined", None)
    me = os.environ.get("CLAUDE_CODE_SESSION_ID")
    o = m.get("owner") or {}
    contributors = m.setdefault("contributors", [])
    for s in srcs:
        so = s.get("owner") or {}
        sid = so.get("sessionId")
        if sid and sid != o.get("sessionId") and all(c.get("sessionId") != sid for c in contributors):
            contributors.append({**so, "joined_at": now()})
    m["merged_from"] = list(dict.fromkeys([*(m.get("merged_from") or []), *froms]))
    # A staged FROM left its text in the compose box and is dropped below: remember it so stage replaces.
    staged_from = [s["id"] for s in srcs if s["status"] == "staged" or s.get("merged_from_staged")]
    if staged_from:
        m["merged_from_staged"] = True
    _event(m, "merged", by=me, merged_from=froms, staged_from=staged_from)
    if was_verified and content_hash(m) == m["verified_hash"]:
        m["verified_hash"] = "merged"  # nothing changed in the bytes, but the merge must be verified again
    save(m)
    for s in srcs:
        s["status"] = "dropped"
        s["merged_into"] = into
        _event(s, "dropped", reason=f"merged into {into}")
        _event(s, "merged", by=me, merged_into=into)
        save(s)
    return {**plan, "dry_run": False, "state": state(load(into)), "gate": gate(load(into))}


# ---- chat history: what is already in the chat, and was it sent (#166) -----------------

CHAT_SIDECAR = "teams.diarization.json"
CHAT_KIND = "teams-chat"


def _plain(s: str) -> str:
    """Markdown or chat text to comparable plain text: link labels kept, markup and case dropped."""
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s or "")
    s = re.sub(r"^\s*(?:[-+*]|\d+\.)\s+", "", s, flags=re.M)
    s = re.sub(r"[*_`>#~]", "", s)
    s = re.sub(r"https?://\S+", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def same_message(draft_text: str, chat_text: str) -> bool:
    """The chat message is this draft as sent (the user may have fixed a typo or two)."""
    a, b = _plain(draft_text), _plain(chat_text)
    if not a or not b:
        return False
    if a == b or (len(a) >= 40 and (a[:80] in b or b[:80] in a)):
        return True
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.85


def _self_speakers(sidecar: dict) -> set[str]:
    roles = sidecar.get("roles") or {}
    mine = {s for s, r in roles.items() if r == "self"}
    if not mine:
        try:
            import teams_chat
            mine = {s for s, r in teams_chat.registry_roles().items() if r == "self"}
        except Exception:  # noqa: BLE001 - no registry: nothing counts as ours
            mine = set()
    return mine


_CHAT_SUFFIX = re.compile(r"\s+\(chat [0-9a-f]{4,}\)$")


def same_chat(name: str, chat: str) -> bool:
    """name is the chat, or the chat with the short suffix teams_cache gives a second chat of that
    name ("Name (chat 1a2b3c)", e.g. a group chat titled like the 1:1)."""
    n, want = (name or "").strip().lower(), (chat or "").strip().lower()
    return bool(want) and (n == want or _CHAT_SUFFIX.sub("", n) == want)


def chat_messages(ws: Path, chat: str, since_ms: int | None = None, days: int = 14,
                  thread_id: str | None = None) -> list[dict]:
    """Ingested messages of one chat (chat name case-insensitive, or that name with a "(chat 1a2b3c)"
    suffix, or, given THREAD_ID, any folder recorded with that chat id: ingest can name an untitled
    group by its raw id or by the one member who had posted, #219), oldest first.

    Folders are YYYY-MM-DD-HHMM chat-days, so only the last `days` (or since since_ms) are read."""
    floor_ms = since_ms if since_ms is not None else int((time.time() - days * 86400) * 1000)
    floor_day = datetime.fromtimestamp(floor_ms / 1000).strftime("%Y-%m-%d")
    out: list[dict] = []
    try:
        folders = sorted((p for p in Path(ws).iterdir() if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", p.name)),
                         reverse=True)
    except OSError:
        return []
    for f in folders:
        if f.name[:10] < floor_day:
            break
        sc = f / CHAT_SIDECAR
        if not sc.is_file():
            continue
        try:
            data = json.loads(sc.read_text())
        except (OSError, ValueError):
            continue
        src = data.get("source") or {}
        if src.get("kind") != CHAT_KIND:
            continue
        # a folder that records its chat id counts by id alone once the draft's id is known: a 1:1 and a
        # group named alike ("Name" / "Name (chat 1a2b3c)") must not read each other's messages
        if thread_id and src.get("chat_id"):
            if src["chat_id"] != thread_id:
                continue
        elif not same_chat(src.get("chat_name") or "", chat):
            continue
        mine = _self_speakers(data)
        for seg in data.get("segments") or []:
            ms = seg.get("epoch_ms")
            if not isinstance(ms, int) or ms < floor_ms:
                continue
            out.append({"epoch_ms": ms, "at": datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()[:19] + "Z",
                        "speaker": seg.get("speaker"), "self": seg.get("speaker") in mine,
                        "text": seg.get("text") or "", "mid": seg.get("mid") or seg.get("message_id"),
                        "folder": f.name, "edited_ms": seg.get("edited_ms")})
    by_mid = {str(m["mid"]): m for m in out if m.get("mid")}
    for x in _live_chat_messages(ws, chat, floor_ms, thread_id):
        old = by_mid.get(str(x["mid"]))
        if old is None:
            out.append(x)
        elif x.get("edited_ms") and (x["edited_ms"] > (old.get("edited_ms") or 0)):
            old.update(text=x["text"], edited_ms=x["edited_ms"])  # edited after the last ingest
    return sorted(out, key=lambda x: x["epoch_ms"])


def _live_chat_messages(ws: Path, chat: str, floor_ms: int, thread_id: str | None = None) -> list[dict]:
    """Messages of the chat still only in the Teams desktop app's local cache (not ingested yet).
    Empty when there is no cache; WHOSAID_TEAMS_CACHE=off turns it off."""
    if (os.environ.get("WHOSAID_TEAMS_CACHE") or "").strip().lower() in ("off", "0", "none"):
        return []
    try:
        import teams_cache
        if not teams_cache._leveldb(teams_cache.cache_dir()).is_dir():
            return []
        if thread_id:  # the chat by its id alone, whatever ingest calls it
            rows = [m for m in teams_cache.messages(thread_id, floor_ms, None, Path(ws)).get("messages") or []
                    if m.get("chat_id") == thread_id]
        else:
            rows = [m for m in teams_cache.messages(chat, floor_ms, None, Path(ws)).get("messages") or []
                    if same_chat(m.get("chat", ""), chat)]
    except Exception:  # noqa: BLE001 - the cache is a bonus, never a reason to fail
        return []
    return [{"epoch_ms": m["epoch_ms"], "at": m["at"], "speaker": m["author"].replace(" ", "_"),
             "self": m["self"], "text": m["text"], "mid": m["message_id"], "folder": "(teams cache)",
             "edited_ms": m.get("edited_ms")} for m in sorted(rows, key=lambda m: m["epoch_ms"])]


def draft_chat_messages(ws: Path, m: dict, since_ms: int | None = None) -> list[dict]:
    """chat_messages for a draft's chat: by its name and, when known, its thread id."""
    tid = None if m.get("new_chat") else chat_thread_id(m, ws)
    return chat_messages(ws, m["to"], since_ms=since_ms, thread_id=tid)


def _is_group(to: str) -> bool:
    """TO lists two or more people ("A, B", "A and B", "A, B and C"), the stager's split_members rule."""
    return len([w for w in re.split(r",\s*(?:and\s+)?|\s+and\s+", to or "") if w.strip()]) >= 2


def chat_thread_id(m: dict, ws: Path | None) -> str | None:
    """The Teams thread id of the draft's chat: the draft's own thread_id, else the one chat the Teams
    desktop app's cache names like the draft's recipient. None when there is no cache or the name is
    not exactly one chat (the stager then checks the title instead)."""
    if m.get("thread_id"):
        return str(m["thread_id"])
    if (os.environ.get("WHOSAID_TEAMS_CACHE") or "").strip().lower() in ("off", "0", "none"):
        return _thread_id_from_other_drafts(m)
    try:
        import teams_cache
        if not teams_cache._leveldb(teams_cache.cache_dir()).is_dir():
            return None
        data = teams_cache.load()
        names = teams_cache.name_chats(data, Path(ws) if ws else None)
    except Exception:  # noqa: BLE001 - the cache is a bonus, never a reason to fail
        return _thread_id_from_other_drafts(m)
    return resolve_chat_id(m.get("to") or "", names, data.get("rosters") or {}) or _thread_id_from_other_drafts(m)


def _thread_id_from_other_drafts(m: dict) -> str | None:
    """The thread id other drafts to the same recipient were given or staged into, when they all agree."""
    want = (m.get("to") or "").strip().lower()
    if not want:
        return None
    try:
        ids = {str(x["thread_id"]) for x in all_drafts() if x.get("id") != m.get("id") and not x.get("new_chat")
               and (x.get("to") or "").strip().lower() == want and x.get("thread_id")
               and x.get("thread_id_source") in ("given", "staged")}
    except Exception:  # noqa: BLE001
        return None
    return ids.pop() if len(ids) == 1 else None


def resolve_chat_id(to: str, names: dict[str, str], rosters: dict[str, dict]) -> str | None:
    """The one chat id TO means, else None (the stager then checks the title instead).
    1. A chat ingest names TO that its roster fits, or the only one ingest names TO when no roster fits
       TO at all (an alias of the user's own). A group where one person posted is named like the 1:1
       with them, so a 1:1 draft goes to the chat whose roster fits, never to that group.
    2. Else the one chat whose topic, Teams title or member set TO describes (members in any order,
       full or unique first names): an untitled group is never named by a member list anywhere else.
    Several candidates: None, never a guess."""
    import teams_cache
    want = (to or "").strip().lower()
    if not want:
        return None
    ids = [cid for cid, n in names.items() if (n or "").strip().lower() == want]
    hits = [cid for cid, r in rosters.items() if teams_cache.roster_fits(r, to)]
    fit = [cid for cid in ids if cid in hits]
    if len(fit) == 1:
        return fit[0]
    if len(ids) == 1 and not hits:
        return ids[0]  # a name of the user's own ([teams.chat_names]) that no roster contradicts
    if len(ids) > 1:
        return None
    return hits[0] if len(hits) == 1 else None


def _ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _draft_text(m: dict) -> str:
    md = ddir(m["id"]) / "draft.md"
    return md.read_text() if md.exists() else ""


def chat_context(ws: Path, m: dict, n: int = 5) -> dict:
    """What the target chat already holds: the newest n ingested messages, our last one, whether
    they replied since, and whether this draft already appears there as ours (already sent)."""
    msgs = draft_chat_messages(ws, m)
    created = _ms(m.get("created_at")) or 0
    sent = next((x for x in reversed(msgs) if x["self"] and x["epoch_ms"] >= created - 300_000
                 and same_message(_draft_text(m), x["text"])), None)
    ours = next((x for x in reversed(msgs) if x["self"]), None)
    return {"chat": m["to"], "ingested": len(msgs), "newest_at": msgs[-1]["at"] if msgs else None,
            "recent": [{k: x[k] for k in ("at", "speaker", "self")} | {"text": x["text"][:200]} for x in msgs[-n:]],
            "ours_last_at": ours["at"] if ours else None,
            "reply_since_ours": bool(ours and any(not x["self"] for x in msgs if x["epoch_ms"] > ours["epoch_ms"])),
            "already_sent": {k: sent[k] for k in ("at", "mid", "folder")} if sent else None}


def sent_check(ws: Path, draft_id: str | None = None, backfill: bool = False, by: str | None = None) -> dict:
    """Mark staged drafts sent once the ingested chat shows them as our message after staging, and staged
    edits saved once the sent message (by id) holds the new text or Teams marks it edited.
    backfill: also give sent drafts that have no recorded message (closed by hand) the one the chat holds,
    changing nothing else (#228). by: who runs the check ("watcher"), else the agent session."""
    def wanted(m: dict) -> bool:
        return m["status"] == "staged" or _edit_open(m) or (backfill and _needs_backfill(m))
    todo = [load(draft_id)] if draft_id else [m for m in all_drafts() if wanted(m)]
    res: dict = {"sent": [], "still_staged": [], "edited": [], "edit_not_seen": [],
                 "note": "a draft stays staged until its chat is ingested again (whosaid teams ingest)"}
    if backfill:
        res["backfilled"], res["not_found"] = [], []
    for m in todo:
        if _edit_open(m):
            _check_edit(ws, m, res, by)
            continue
        if backfill and _needs_backfill(m):
            _backfill(ws, m, res, by)
            continue
        if m["status"] != "staged":
            res["still_staged"].append({"id": m["id"], "why": f"is {m['status']}, not staged"})
            continue
        since = (_ms(m.get("staged_at")) or _ms(m.get("created_at")) or 0) - 300_000
        msgs = draft_chat_messages(ws, m, since_ms=since)
        hit = next((x for x in msgs if x["self"] and same_message(_draft_text(m), x["text"])), None)
        if not hit:
            res["still_staged"].append({"id": m["id"], "to": m["to"], "why": f"not in the {len(msgs)} ingested "
                                        f"message(s) of {m['to']} since staging"})
            continue
        _mark_sent(m, hit, "check", by)
        save(m)
        res["sent"].append({"id": m["id"], "to": m["to"], **m["sent_message"]})
    return res


def _needs_backfill(m: dict) -> bool:
    return m["status"] == "sent" and not (m.get("sent_message") or {}).get("mid")


def _backfill(ws: Path, m: dict, res: dict, by: str | None) -> None:
    """The message a draft closed by hand went out as, recorded; status, text and edit baseline untouched."""
    hit, err = _find_quietly(ws, m)
    if not hit or not hit.get("mid"):
        res["not_found"].append({"id": m["id"], "to": m["to"], **({"error": err} if err else {})})
        return
    m["sent_message"] = {k: hit.get(k) for k in ("at", "mid", "folder")}
    _event(m, "backfilled", message=m["sent_message"], by=_actor(by))
    save(m)
    res["backfilled"].append({"id": m["id"], "to": m["to"], **m["sent_message"]})


def _edit_open(m: dict) -> bool:
    """A staged edit the chat hasn't shown saved yet."""
    e = (m.get("edits") or [None])[-1]
    return m["status"] == "sent" and bool(e) and not e.get("edited_at")


def _check_edit(ws: Path, m: dict, res: dict, by: str | None = None) -> None:
    e = m["edits"][-1]
    since = (_ms((m.get("sent_message") or {}).get("at")) or _ms(e.get("staged_at")) or 0) - 300_000
    x = next((x for x in draft_chat_messages(ws, m, since_ms=since) if e.get("mid") and str(x.get("mid")) == str(e["mid"])),
             None)
    staged_ms = _ms(e.get("staged_at")) or 0
    if x is None:
        res["edit_not_seen"].append({"id": m["id"], "to": m["to"], "why": f"message {e.get('mid')} is not in the "
                                     f"ingested {m['to']} chat"})
        return
    # Teams' edit time is the proof; without one, the new text (and not the old) in that message
    flagged = bool(x.get("edited_ms")) and x["edited_ms"] >= staged_ms - 60_000
    def near(a: str) -> float:
        return difflib.SequenceMatcher(None, _plain(a), _plain(x["text"]), autojunk=False).ratio()
    by_text = (_plain(e["to_text"]) != _plain(e["from_text"]) and same_message(e["to_text"], x["text"])
               and near(e["to_text"]) > near(e["from_text"]))
    if not (flagged or by_text):
        res["edit_not_seen"].append({"id": m["id"], "to": m["to"], "why": "the message still holds the old text "
                                     "(not saved in Teams yet, or the chat not ingested since)"})
        return
    # Teams' own edit time when it flagged this edit; seen by its text only, the time it was first seen here
    # (the message's own "at" is when it was first SENT, before the edit was even staged)
    e["edited_at"] = (datetime.fromtimestamp(x["edited_ms"] / 1000, timezone.utc).isoformat()[:19] + "Z"
                      if flagged else now())
    e["edited_at_source"] = "teams" if flagged else "seen"
    e["seen_text"] = x["text"]
    if e.get("hash") == edit_hash(m):  # the outbox still holds what was saved: that is the new baseline
        m["sent_edit_hash"], m["sent_text"] = e["hash"], e["to_text"]
    _event(m, "edited", n=e["n"], mid=e.get("mid"), edited_at=e["edited_at"], by=_actor(by))
    save(m)
    res["edited"].append({"id": m["id"], "to": m["to"], "n": e["n"], "mid": e.get("mid"), "edited_at": e["edited_at"]})


# ---- review page ------------------------------------------------------------------------

def html_page(out: str | None = None, include_closed: bool = False) -> dict:
    """One self-contained page listing the drafts: recipient, state, owner, last update, text."""
    rows = [m for m in all_drafts() if include_closed or m["status"] in OPEN]
    esc = htmllib.escape
    cards = []
    for m in sorted(rows, key=lambda d: d.get("updated_at") or "", reverse=True):
        s = summary(m)
        pages = "".join(f'<li><a href="file://{esc(p)}">{esc(Path(p).name)}</a></li>' for p in s["pages"])
        extra = []
        if s["mentions"]:
            extra.append("mentions: " + esc(", ".join(s["mentions"])))
        if s["contributors"]:
            extra.append("also written by: " + esc(", ".join(c.get("name") or (c.get("sessionId") or "?")[:8]
                                                              for c in s["contributors"])))
        cards.append(
            f'<section class="d"><h2>{esc(s["to"])} <span class="st">{esc(s["state"])}</span></h2>'
            f'<p class="meta">{esc(s["id"])} &middot; owner {esc(s["owner_line"])} &middot; updated '
            f'{esc((s["updated_at"] or "")[:16].replace("T", " "))} &middot; rev {s["revisions"]}'
            + "".join(f" &middot; {x}" for x in extra) + "</p>"
            f'<pre>{esc(_draft_text(m)) or "(no text yet)"}</pre>'
            + (f"<ul>{pages}</ul>" if pages else "")
            + f'<p class="cmd">whosaid drafts show {esc(s["id"])}</p></section>')
    body = "".join(cards) or "<p>The outbox is empty.</p>"
    doc = ("<!doctype html><html><head><meta charset=utf-8><title>Drafts outbox: open drafts</title><style>"
           "body{font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:2em auto;padding:0 1em;color:#222}"
           ".d{border:1px solid #ddd;border-radius:8px;padding:.6em 1em;margin:1em 0}"
           "h2{font-size:1.1em;margin:.2em 0}.st{font-weight:normal;font-size:.85em;background:#eef;"
           "border-radius:4px;padding:1px 6px}.meta,.cmd{color:#666;font-size:.85em}.cmd{font-family:monospace}"
           "pre{white-space:pre-wrap;background:#fafafa;padding:.6em;border-radius:6px}"
           f"</style></head><body><h1>Drafts outbox</h1><p class=meta>{len(rows)} draft(s), "
           f"written {esc(now()[:16].replace('T', ' '))} UTC</p>{body}</body></html>\n")
    path = Path(out).expanduser() if out else root() / "outbox.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc)
    return {"path": str(path), "drafts": len(rows)}


# ---- stakeholders and staleness (the whosaid part) ----------------------------------

def _meeting_time(folder: str) -> datetime | None:
    """Meeting folders are YYYY-MM-DD-HHMM[-n] in local time."""
    mt = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:-(\d{2})(\d{2}))?", folder or "")
    if not mt:
        return None
    y, mo, d, hh, mm = mt.groups()
    return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0)).astimezone()


def _search(ws: Path, q: str, k: int, speaker: str | None = None):
    import search
    engine, hits, _fallback = search.query(ws, q, mode="hybrid", speaker=speaker, k=k)
    return engine, hits


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.replace("_", " ").lower()).strip()


def git_authors(repo: str, paths: list[str], since: str = "6 months ago") -> dict[str, int]:
    try:
        out = subprocess.run(["git", "-C", repo, "log", f"--since={since}", "--format=%an", "--", *(paths or ["."])],
                             capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise DraftsError(f"git log in {repo} took over 30s; narrow --path or leave out --repo") from None
    except OSError as exc:
        raise DraftsError(f"git log in {repo} could not run: {exc}") from None
    if out.returncode != 0:
        raise DraftsError(f"git log failed in {repo}: {out.stderr.strip()[:200]}")
    counts: dict[str, int] = {}
    for a in out.stdout.splitlines():
        if a.strip():
            counts[a.strip()] = counts.get(a.strip(), 0) + 1
    return counts


def stakeholders(ws: Path, topics: list[str], repo: str | None = None, paths: list[str] | None = None,
                 k: int = 25, exclude: list[str] | None = None, half_life_days: float = 90.0) -> dict:
    """Rank who has spoken about these topics (search rank x recency), plus who changed the files."""
    topics = [t.strip() for t in topics if t and t.strip()]
    if not topics:
        raise DraftsError("need at least one topic")
    skip = {_norm(x) for x in exclude or []}
    now_dt = datetime.now().astimezone()
    people: dict[str, dict] = {}
    engines = set()
    for t in topics:
        engine, hits = _search(ws, t, k)
        engines.add(engine)
        # Reciprocal rank, not h.score: exact, meaning and hybrid hits score on different
        # scales (exact can be 0.0), but every engine returns best first.
        for rank, h in enumerate(hits):
            spk = h.speaker or ""
            if not spk or UNNAMED.match(spk) or _norm(spk) in skip:
                continue
            when = _meeting_time(h.meeting)
            age = max(0.0, (now_dt - when).total_seconds() / 86400) if when else 365.0
            w = 1.0 / (rank + 1) * 0.5 ** (age / half_life_days)
            p = people.setdefault(spk, {"speaker": spk, "weight": 0.0, "hits": 0, "topics": [], "newest": None,
                                        "git_commits": 0})
            p["weight"] += w
            p["hits"] += 1
            if t not in p["topics"]:
                p["topics"].append(t)
            if p["newest"] is None or h.meeting > p["newest"]["meeting"]:
                p["newest"] = {"meeting": h.meeting, "t_str": h.t_str, "text": (h.text or "")[:200]}
    authors: dict[str, int] = {}
    if repo:
        authors = git_authors(repo, paths or [])
        known = {_norm(s): s for s in people}
        import search
        try:
            conn = search.open_index(ws)
            try:
                for (s,) in conn.execute("SELECT DISTINCT speaker FROM seg"):
                    known.setdefault(_norm(s or ""), s)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - an unreadable index only loses the git match
            pass
        for a, n in authors.items():
            na = _norm(a)
            if not na or na in skip:
                continue
            spk = known.get(na) or next((s for kn, s in known.items() if kn and (kn.startswith(na) or na.startswith(kn))), None)
            key = spk or a
            p = people.setdefault(key, {"speaker": spk, "weight": 0.0, "hits": 0, "topics": [], "newest": None,
                                        "git_commits": 0})
            p["git_commits"] += n
            p.setdefault("git_author", a)
    total_commits = sum(authors.values()) or 1
    for p in people.values():
        p["score"] = round(p["weight"] + 2.0 * p["git_commits"] / total_commits, 4)
        p["weight"] = round(p["weight"], 4)
        name = p.get("speaker") or p.get("git_author")
        why = []
        if p["hits"]:
            why.append(f"{p['hits']} hit(s) on {', '.join(p['topics'])}")
        if p["git_commits"]:
            why.append(f"{p['git_commits']} of {total_commits} commits on the paths")
        p["name"] = (name or "").replace("_", " ")
        p["why"] = "; ".join(why)
    ranked = sorted(people.values(), key=lambda p: -p["score"])
    return {"topics": topics, "engine": "/".join(sorted(engines)), "stakeholders": ranked,
            "next": "add the people who matter to the draft's stakeholders[] with the topics you searched"}


def stale_check(ws: Path, draft_id: str | None = None, k: int = 10) -> dict:
    """Open verified drafts with statements, by a stakeholder on a searched topic, newer than the verify."""
    drafts = [load(draft_id)] if draft_id else [m for m in all_drafts() if m["status"] in OPEN]
    out = []
    for m in drafts:
        if not m.get("verified_at"):
            continue
        since = datetime.fromisoformat(m["verified_at"].replace("Z", "+00:00"))
        c = claims_of(m)
        found = []
        for s in c.get("stakeholders") or []:
            spk = s.get("speaker") or (s.get("name") or "").replace(" ", "_")
            for t in s.get("searched") or []:
                _engine, hits = _search(ws, t, k, speaker=spk or None)
                for h in hits:
                    when = _meeting_time(h.meeting)
                    if when and when > since:
                        found.append({"stakeholder": s.get("name"), "topic": t, "meeting": h.meeting,
                                      "t_str": h.t_str, "speaker": h.speaker, "text": (h.text or "")[:200]})
        if found:
            out.append({"id": m["id"], "to": m["to"], "owner_line": owner_line(m["owner"]),
                        "verified_at": m["verified_at"], "new_statements": found})
    return {"maybe_stale": out, "checked": len(drafts),
            "next": "for each, have the owning session read the new statements (whosaid_context) and re-verify"}


# ---- staging -----------------------------------------------------------------------

def _lock_path() -> Path:
    return root() / ".outbox.lock"


def acquire_lock(what: str) -> Path:
    """mkdir is the lock. A stale one (dead pid) is moved aside with one rename, so two processes
    reclaiming it at once can't both win: the loser's rename or mkdir fails and it reports the lock."""
    lk = _lock_path()
    try:
        lk.mkdir()
    except FileExistsError:
        holder = _held_by(lk)
        if holder is not None:
            raise DraftsError(f"outbox locked by pid {holder.get('pid')} ({holder.get('what')}) since {holder.get('at')}")
        aside = lk.with_name(f".outbox.lock.stale-{os.getpid()}-{os.urandom(3).hex()}")
        try:
            lk.rename(aside)
            lk.mkdir()
        except (FileNotFoundError, FileExistsError, OSError):
            raise DraftsError("outbox lock was just taken by another stage; try again") from None
        shutil.rmtree(aside, ignore_errors=True)
    (lk / "owner.json").write_text(json.dumps({"pid": os.getpid(), "at": now(), "what": what}))
    return lk


def _held_by(lk: Path) -> dict | None:
    """The live holder of an existing lock, or None when it is stale. A lock without owner.json is
    taken as held for 10 seconds: its creator may not have written the file yet."""
    try:
        holder = _read_json(lk / "owner.json", None)
    except (OSError, ValueError):
        holder = None
    if holder is None:
        try:
            young = time.time() - lk.stat().st_mtime < 10
        except FileNotFoundError:
            return None
        return {"pid": "?", "what": "starting", "at": "just now"} if young else None
    return holder if alive(holder.get("pid")) else None


def release_lock() -> None:
    """Remove the lock only if this process holds it."""
    lk = _lock_path()
    try:
        holder = _read_json(lk / "owner.json", {}) or {}
    except (OSError, ValueError):
        return
    if holder.get("pid") == os.getpid():
        shutil.rmtree(lk, ignore_errors=True)


def _stager_msg(r: subprocess.CompletedProcess) -> str:
    """The stager's error text for a failure record: its stderr and stdout, minus the hands-off heads-up
    (#221), which was news while it ran and would only push the real error out of the 500 chars."""
    lines = [x for x in ((r.stderr or "") + (r.stdout or "")).splitlines()
             if not x.startswith("drafts_stager: hands off Teams")]
    return "\n".join(lines).strip()[:500]


def stager() -> list[str]:
    """WHOSAID_DRAFTS_STAGER when set (any chat app), else the built-in Teams web stager."""
    cmd = os.environ.get("WHOSAID_DRAFTS_STAGER", "").strip()
    if not cmd:
        return ["uv", "run", "--quiet", "--with", "websockets", "python",
                str(Path(__file__).resolve().parent / "drafts_stager.py")]
    return shlex.split(cmd)


def _devtools_browser(port: int, timeout: float = 1.0) -> str | None:
    """The "Browser" string /json/version reports on port, or None when nothing answers."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return str(json.load(r).get("Browser", ""))
    except Exception:
        return None


def stager_info() -> dict:
    """Which stager stage runs and where it lands, the way the built-in stager picks its port: an
    explicit WHOSAID_CDP_PORT is a browser; else the Teams app when its port (WHOSAID_TEAMS_CDP_PORT,
    default 9333) answers as Edg/; else the browser on 9222. An override that is the browser-only
    stage.ts while the app answers gets a warning: its drafts would go to the browser, not the app."""
    cmd = os.environ.get("WHOSAID_DRAFTS_STAGER", "").strip()
    raw_app = os.environ.get("WHOSAID_TEAMS_CDP_PORT") or "9333"
    try:
        app_port = int(raw_app)
    except ValueError:
        app_port = None
    explicit = (os.environ.get("WHOSAID_CDP_PORT") or "").strip()
    app_up = False
    if not explicit and app_port is not None:
        app_up = (_devtools_browser(app_port) or "").startswith("Edg/")
    if cmd:
        info = {"kind": "override", "stager": f"WHOSAID_DRAFTS_STAGER={cmd}", "target": "chosen by the override"}
        if "stage.ts" in cmd and app_up:
            info["warning"] = (f"WHOSAID_DRAFTS_STAGER points at stage.ts (browser only) while the Teams app answers "
                               f"on port {app_port}; drafts go to the browser, not the app. Unset it to use the "
                               f"built-in stager: env -u WHOSAID_DRAFTS_STAGER whosaid drafts stage ...")
        return info
    if explicit:
        target = f"browser on port {explicit} (WHOSAID_CDP_PORT)"
    elif app_up:
        target = f"Teams app on port {app_port}"
    else:
        target = "browser on port 9222" + (f" (no Teams app on port {app_port})" if app_port is not None else "")
    return {"kind": "built-in", "stager": "built-in drafts_stager.py", "target": target}


def _stage_ws(ws) -> Path | None:
    """The workspace whose ingested chats stage reads; None (history not checked) when there is none."""
    if ws:
        return Path(ws).expanduser()
    if os.environ.get("WHOSAID_DRAFTS_NO_HISTORY"):
        return None
    try:
        return _ws(None)
    except SystemExit:
        return None


def stage(ids: list[str] | None = None, all_ready: bool = False, dry_run: bool = False,
          tab: str | None = None, ws: str | os.PathLike | None = None, replace: bool = False,
          queue: bool = False, edit: bool = False, allow_newer: bool = False, allow_longer: bool = False) -> dict:
    """Stage drafts into their chats; never sends. One per run unless queue: each lands in its own chat's
    compose box, so queue stages them all, oldest first, and stops at the first stager failure.
    edit: load a sent draft's verified correction into Teams' Edit box on that message instead (one id;
    never saves). Returns the usual shape, with the edit in staged[0] (sent_message, warnings)."""
    if edit:
        if all_ready or len(ids or []) != 1:
            raise DraftsError("stage --edit takes one draft id")
        try:
            r = _stage_edit(ids[0], dry_run, tab, _stage_ws(ws), None, allow_longer, allow_newer)
        except DraftsError as exc:
            return {"staged": [], "skipped": [], "failed": [{"id": ids[0], "why": str(exc)}], "edit": True}
        out = {"staged": [], "skipped": [], "failed": [], "edit": True, "warnings": r.get("warnings") or []}
        if dry_run:
            out["would_run"] = [{"id": ids[0], "argv": r["would_run"], "sent_message": r["sent_message"]}]
        else:
            out["staged"] = [{k: r[k] for k in ("id", "to", "stager", "sent_message")}]
            out["note"] = r["note"]
        return out
    if all_ready:
        ready = [m for m in all_drafts() if m["status"] in OPEN and state(m).startswith("verified")]
        ids = [m["id"] for m in sorted(ready, key=lambda m: m.get("updated_at") or "")]
    ids = ids or []
    if not ids:
        return {"staged": [], "skipped": [], "failed": [], "note": "nothing verified and unstaged"}
    cmd = stager()
    wsp = _stage_ws(ws)
    if not dry_run:
        acquire_lock(" ".join(["stage", *ids]))
    res: dict = {"staged": [], "skipped": [], "failed": [], "would_run": [], "context": {},
                 "stager_info": stager_info()}
    if wsp is None:
        res["history_note"] = "no workspace (WHOSAID_WORKSPACE or --ws): the chat history was not checked"
    try:
        for draft_id in ids:
            m = load(draft_id)
            st, errs = state(m), gate(m, wsp, said=wsp is not None)
            if m["status"] not in OPEN:
                res["failed"].append({"id": draft_id, "why": f"is {m['status']}"})
                continue
            if st == "staged":
                res["skipped"].append({"id": draft_id, "why": "already staged with this content"})
                continue
            if not st.startswith("verified"):
                res["failed"].append({"id": draft_id, "why": f"{st}; owner {owner_line(m['owner'])} must re-verify"})
                continue
            if errs:
                res["failed"].append({"id": draft_id, "why": "gate fails: " + "; ".join(errs)})
                continue
            rivals = [d["id"] for d in all_drafts() if d["id"] != draft_id and d["to"] == m["to"]
                      and d["status"] in OPEN and d["id"] != m.get("supersedes")]
            if rivals:
                res["failed"].append({"id": draft_id, "why": f"other open draft(s) to {m['to']}: {', '.join(rivals)}; "
                                       f"fold them into one: whosaid drafts merge {draft_id} {' '.join(rivals)}"})
                continue
            if wsp is not None:
                ctx = chat_context(wsp, m)
                res["context"][draft_id] = ctx
                if ctx["already_sent"]:
                    res["skipped"].append({"id": draft_id, "why": f"already in {m['to']} as your message at "
                                           f"{ctx['already_sent']['at']}; run mark-sent --check, or edit "
                                           "that message in the chat instead"})
                    continue
            sup = load(m["supersedes"]) if m.get("supersedes") else None
            prev = (replace or m["status"] == "staged" or bool(sup and sup["status"] == "staged")
                    or bool(m.get("merged_from_staged")))
            argv = ["--chat", m["to"], "--md", str(ddir(draft_id) / "draft.md")]
            for p in m.get("pages", []):
                argv += ["--attach", p]
            for x in m.get("mentions") or []:
                argv += ["--mention", x]
            if prev:
                argv += ["--replace", "--only-these-attachments"]
            if m.get("new_chat"):
                argv += ["--new-chat"]
            if tab:
                argv += ["--tab", tab]
            # a new-chat draft to a group passes the id of the chat those members already have, when the cache
            # knows exactly one, so the stager opens it instead of starting a duplicate (#223); a 1:1 doesn't
            tid = chat_thread_id(m, wsp) if (not m.get("new_chat") or _is_group(m["to"])) else None
            if tid:
                argv += ["--thread-id", tid]
            if dry_run:
                res["would_run"].append({"id": draft_id, "argv": cmd + argv})
                continue
            env = {**os.environ, "TEAMS_OUTBOX_LOCK": str(_lock_path())}
            # The stager's own stderr is captured until it exits, so the heads-up goes out from here (#221).
            print(f"{draft_id}: staging into {m['to']}; hands off the chat app (no Enter, no Send) until it "
                  "finishes", file=sys.stderr, flush=True)
            try:
                r = subprocess.run(cmd + argv, capture_output=True, text=True, env=env, timeout=300)
            except (OSError, subprocess.TimeoutExpired) as exc:
                r = subprocess.CompletedProcess(cmd, 1, "", str(exc))
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            if r.returncode != 0:
                msg = _stager_msg(r)
                _event(m, "stage-failed", err=msg)
                save(m)
                res["failed"].append({"id": draft_id, "why": f"stager exit {r.returncode}: {msg}"})
                break
            m["status"], m["staged_hash"], m["staged_at"] = "staged", content_hash(m), now()
            try:  # the stager proved this id is the chat it typed into: keep it for the next stage (#219)
                got = (json.loads(tail[0]) or {}).get("thread_id") if tail[0].startswith("{") else None
            except ValueError:
                got = None
            if got and not m.get("new_chat") and m.get("thread_id_source") != "given":
                m["thread_id"], m["thread_id_source"] = str(got), "staged"
            _event(m, "staged", out=(r.stdout or "")[-500:])
            save(m)
            if sup and sup["status"] in OPEN:
                sup["status"] = "superseded"
                _event(sup, "superseded", by=draft_id)
                save(sup)
            res["staged"].append({"id": draft_id, "to": m["to"], "stager": tail[0]})
            # The stager focuses the chat it staged: one per run, so the user sends or edits it first.
            if len(ids) > 1 and not queue:
                res["note"] = f"staged 1 of {len(ids)}; run stage --all again for the next (or --queue for all)"
                break
    finally:
        if not dry_run:
            release_lock()
    if not dry_run:
        res.pop("would_run")
    return res


def edit_match(text: str, chat_text: str, ratio: float = 0.75) -> bool:
    """Looser than same_message, for a corrected text: the chat message is the draft, or one paragraph
    of it, give or take the correction."""
    if same_message(text, chat_text):
        return True
    b = _plain(chat_text)
    if len(b) < 20:
        return False
    parts = [text] + [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    return any(difflib.SequenceMatcher(None, _plain(p), b, autojunk=False).ratio() >= ratio for p in parts)


def sent_text(ws: Path | None, m: dict) -> dict | None:
    """Our message in the chat that is this draft as sent: matched against every revision, newest message first."""
    if ws is None:
        return None
    since = (_ms(m.get("staged_at")) or _ms(m.get("created_at")) or 0) - 300_000
    texts = [revision_text(m, r["n"]) for r in revisions(m)] or [_draft_text(m)]
    for x in reversed(draft_chat_messages(ws, m, since_ms=since)):
        if x["self"] and any(edit_match(t, x["text"]) for t in texts if t.strip()):
            return x
    return None


def find_sent(ws: Path | None, m: dict) -> tuple[dict | None, list[dict]]:
    """(the message this draft went out as, the chat messages after it). By the recorded message id; a
    draft sent before ids were recorded (or marked sent by hand) falls back to the text match."""
    if ws is None:
        return None, []
    sm = m.get("sent_message") or {}
    since = (_ms(sm.get("at")) or _ms(m.get("staged_at")) or _ms(m.get("created_at")) or 0) - 300_000
    msgs = draft_chat_messages(ws, m, since_ms=since)
    if sm.get("mid"):
        hit = next((x for x in msgs if str(x.get("mid")) == str(sm["mid"])), None)
    else:
        hit = sent_text(ws, m)
        hit = next((x for x in msgs if x.get("mid") and x.get("mid") == (hit or {}).get("mid")), hit)
    if hit is None:
        return None, []
    return hit, [x for x in msgs if x["epoch_ms"] > hit["epoch_ms"]]


def _stage_edit(draft_id: str, dry_run: bool, tab: str | None, wsp: Path | None, match_file: str | None,
                allow_longer: bool, allow_newer: bool) -> dict:
    """Load a verified correction of a sent draft into Teams' Edit box on that message (#176, #209).
    The stager never saves the edit: the user checks it and saves it in Teams."""
    m = load(draft_id)
    if m["status"] in OPEN:
        hit = sent_text(wsp, m)  # sent by hand without mark-sent: found in the chat, so it went out
        if not hit:
            raise DraftsError(f"{draft_id} is {state(m)}, not sent; stage it the normal way")
        # it went out as that message; the baseline is what the chat holds, so a correction already in
        # draft.md is a pending edit (and needs its own verify)
        unchanged = _plain_links(_draft_text(m)) == _plain_links(hit["text"])
        _mark_sent(m, hit, "found in chat by stage --edit")
        if not unchanged:
            m["sent_edit_hash"], m["sent_text"] = "unknown", hit["text"]
        if not dry_run:  # a dry run reports, it records nothing
            save(m)
    if m["status"] != "sent":
        raise DraftsError(f"{draft_id} is {m['status']}")
    st = edit_state(m)
    if st == "sent":
        raise DraftsError(f"{draft_id} is sent and unchanged; write the corrected text first (drafts text)")
    if st == "sent, edit drafting":
        raise DraftsError(f"{draft_id}: the correction is not verified; owner {owner_line(m['owner'])} must "
                          "verify it (drafts verify)")
    errs = gate(m, wsp, said=wsp is not None)
    if errs:
        raise DraftsError("gate fails: " + "; ".join(errs))
    new = _draft_text(m).strip()
    warnings: list[str] = []
    if match_file:  # the chat isn't ingested: the user hands over the sent text, so nothing else can be checked
        old, where, after = Path(match_file).expanduser().read_text(), {"from": "--match-file"}, []
        warnings.append("--match-file: the message's id, author and later messages were not checked")
    else:
        if wsp is None:
            raise DraftsError("no workspace (WHOSAID_WORKSPACE or --ws) to find the sent message in")
        hit, after = find_sent(wsp, m)
        if hit is None:
            sm = m.get("sent_message") or {}
            raise DraftsError(f"can't find the sent message in the ingested {m['to']} chat"
                              + (f" (id {sm['mid']})" if sm.get("mid") else "")
                              + "; ingest the chat again (whosaid teams sync), or pass --match-file with the sent text")
        if not hit["self"]:
            raise DraftsError(f"message {hit.get('mid')} in {m['to']} is by {hit.get('speaker')}, not you: "
                              "only your own messages can be edited")
        old, where = hit["text"], {k: hit.get(k) for k in ("at", "mid", "folder")}
        others = [x for x in after if not x["self"]]
        if others and not allow_newer:
            last = others[-1]
            raise DraftsError(f"{len(others)} newer message(s) in {m['to']} since yours, the last by "
                              f"{last.get('speaker')} at {last['at']}: \"{last['text'][:80]}\". Editing a message "
                              "people already answered can confuse the thread; send a follow-up instead, or pass "
                              "--allow-newer (allow_newer=true) to edit anyway")
        if others:
            warnings.append(f"{len(others)} newer message(s) from others since yours (--allow-newer)")
        mine = [x for x in after if x["self"]]
        if mine:
            warnings.append(f"you posted {len(mine)} message(s) after this one; the edit changes the older message")
    base = m.get("sent_text") or old
    if _plain_links(base) == _plain_links(new) or _plain_links(old) == _plain_links(new):
        raise DraftsError("draft.md is the same as the sent message; write the corrected text first")
    # The edit replaces the whole message: a draft of which only a part was sent would add the rest.
    if not allow_longer and len(_plain(new)) > 1.5 * len(_plain(old)) + 40:
        raise DraftsError(f"draft.md ({len(_plain(new))} chars) is much longer than the sent message "
                          f"({len(_plain(old))} chars), and the edit replaces the whole message; write only the "
                          "corrected message (drafts text), or pass --allow-longer")
    matchf = ddir(draft_id) / ".edit-match.txt"
    argv = [*stager_builtin(), "--chat", m["to"], "--md", str(ddir(draft_id) / "draft.md"),
            "--edit-sent", "--match-file", str(matchf)]
    if allow_newer:
        argv += ["--allow-newer"]
    tid = chat_thread_id(m, wsp)
    if tid:
        argv += ["--thread-id", tid]
    if tab:
        argv += ["--tab", tab]
    res: dict = {"id": draft_id, "to": m["to"], "sent_message": where, "warnings": warnings}
    if dry_run:
        return res | {"would_run": argv}
    matchf.write_text(old)
    acquire_lock(f"stage --edit {draft_id}")
    try:
        env = {**os.environ, "TEAMS_OUTBOX_LOCK": str(_lock_path())}
        print(f"{draft_id}: loading the edit in {m['to']}; hands off Teams (no Enter, no Done) until it finishes",
              file=sys.stderr, flush=True)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            r = subprocess.CompletedProcess(argv, 1, "", str(exc))
    finally:
        release_lock()
    if r.returncode != 0:
        msg = _stager_msg(r)
        _event(m, "edit-failed", err=msg)
        save(m)
        raise DraftsError(f"stager exit {r.returncode}: {msg}")
    h = edit_hash(m)
    # sent before #209 (no baseline hash): pin one now, or the edit-staged event would read as "unchanged"
    m.setdefault("sent_edit_hash", "unknown")
    edits = m.setdefault("edits", [])
    edits.append({"n": len(edits) + 1, "staged_at": now(),
                  "mid": where.get("mid") or (m.get("sent_message") or {}).get("mid"), "rev": len(revisions(m)),
                  "from_text": old, "to_text": _draft_text(m), "hash": h, "edited_at": None})
    m["edit_staged_hash"] = h
    _event(m, "edit-staged", n=len(edits), message=where, out=(r.stdout or "")[-300:])
    save(m)
    return res | {"stager": ((r.stdout or "").strip().splitlines() or [""])[-1],
                  "note": "the new text is in Teams' Edit box on your message; check it and save it in Teams"}


def edit_sent(draft_id: str, dry_run: bool = False, tab: str | None = None, ws: str | os.PathLike | None = None,
              match_file: str | None = None, allow_longer: bool = False, allow_newer: bool = False) -> dict:
    """`drafts edit-sent ID`: an alias of `drafts stage ID --edit` (same gates, #209)."""
    return _stage_edit(draft_id, dry_run, tab, _stage_ws(ws), match_file, allow_longer, allow_newer)


def stager_builtin() -> list[str]:
    """Edit-sent uses the built-in Teams web stager (WHOSAID_DRAFTS_STAGER has no edit mode);
    WHOSAID_DRAFTS_EDIT_STAGER overrides it (tests, other chat apps)."""
    cmd = os.environ.get("WHOSAID_DRAFTS_EDIT_STAGER", "").strip()
    if cmd:
        return shlex.split(cmd)
    return ["uv", "run", "--quiet", "--with", "websockets", "python",
            str(Path(__file__).resolve().parent / "drafts_stager.py")]


# ---- CLI ---------------------------------------------------------------------------

def _ws(arg: str | None) -> Path:
    from wsconfig import resolve_workspace
    return resolve_workspace(arg)


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("outbox empty")
    for r in rows:
        extra = f"   supersedes: {r['supersedes']}" if r.get("supersedes") else ""
        if r.get("rivals"):
            extra += f"   rivals: {r['rivals']}"
        if r.get("merged_into"):
            extra += f"   merged_into: {r['merged_into']}"
        if r.get("merged_from"):
            extra += f"   merged_from: {', '.join(r['merged_from'])}"
        print(f"{r['id']}\n   to: {r['to']}   state: {r['state']}   owner: {r['owner_line']}   "
              f"updated: {r['updated_at'][5:16]}{extra}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whosaid drafts", description="Offline outbox for verified chat drafts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("new")
    a.add_argument("--to", required=True)
    a.add_argument("--topic", required=True)
    a.add_argument("--page", action="append", default=[])
    a.add_argument("--supersedes")
    a.add_argument("--new-chat", action="store_true")
    a.add_argument("--thread-id", help="the chat's Teams thread id (19:...@thread.v2); default: from the "
                   "Teams cache when exactly one chat has the recipient's name")
    a.add_argument("--mention", action="append", default=[], help="a name to @mention when staged")
    a.add_argument("--new", action="store_true", dest="separate",
                   help="a second draft even if the recipient has an open one (default: join it)")
    force = "change a draft another live session owns"
    a = sub.add_parser("text")
    a.add_argument("id")
    a.add_argument("--file", required=True, help="markdown file, or - for stdin")
    a.add_argument("--force", action="store_true", help=force)
    a = sub.add_parser("claims")
    a.add_argument("id")
    a.add_argument("--file", required=True, help="JSON {claims, stakeholders, docs}, or - for stdin")
    a.add_argument("--force", action="store_true", help=force)
    a.add_argument("--ws", help="workspace whose index resolves said claims' turns (default WHOSAID_WORKSPACE)")
    for name in ("attach", "detach"):
        a = sub.add_parser(name)
        a.add_argument("id")
        a.add_argument("page")
        a.add_argument("--force", action="store_true", help=force)
    a = sub.add_parser("mentions", help="set the names to @mention (none clears them)")
    a.add_argument("id")
    a.add_argument("names", nargs="*")
    a.add_argument("--force", action="store_true", help=force)
    a = sub.add_parser("thread-id", help="set the chat's Teams thread id (none clears it)")
    a.add_argument("id")
    a.add_argument("thread_id", nargs="?", default="")
    a.add_argument("--force", action="store_true", help=force)
    a = sub.add_parser("verify")
    a.add_argument("id")
    a.add_argument("--force", action="store_true", help=force)
    a.add_argument("--voice", choices=(*VOICE_MODES, "clear"),
                   help="per-draft voice lint mode for a real exception (needs --reason); clear drops it")
    a.add_argument("--reason", help="why this draft is an exception to the voice lint")
    a.add_argument("--ws", help="workspace whose whosaid.toml [drafts] voice_lint applies and whose index and "
                   "chats said claims and recap replies are checked against (default WHOSAID_WORKSPACE)")
    a.add_argument("--recap-days", type=int, default=14,
                   help="a recap reply's points are searched in the recipient's meetings of the last N days (14)")
    a = sub.add_parser("lint", help="the voice lint alone, read-only")
    a.add_argument("id", nargs="?")
    a.add_argument("--all", action="store_true", help="every open draft")
    a.add_argument("--ws", help="workspace whose whosaid.toml [drafts] voice_lint applies (default WHOSAID_WORKSPACE)")
    a.add_argument("--json", action="store_true")
    sub.add_parser("path").add_argument("id")
    a = sub.add_parser("list")
    a.add_argument("--all", action="store_true")
    a.add_argument("--mine", action="store_true")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("mine")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("show")
    a.add_argument("id")
    a.add_argument("--rev", type=int, help="the text of revision N (1 is the oldest)")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("diff")
    a.add_argument("id")
    a.add_argument("--from", type=int, dest="rev_from")
    a.add_argument("--to", type=int, dest="rev_to")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("html")
    a.add_argument("--out", help="default: <drafts dir>/outbox.html")
    a.add_argument("--all", action="store_true", help="include sent, dropped and superseded")
    a = sub.add_parser("stakeholders")
    a.add_argument("ws", nargs="?")
    a.add_argument("--topic", action="append", required=True)
    a.add_argument("--repo")
    a.add_argument("--path", action="append", default=[])
    a.add_argument("--exclude", action="append", default=[], help="a name to leave out (yourself)")
    a.add_argument("-k", type=int, default=25)
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("stale-check")
    a.add_argument("ws", nargs="?")
    a.add_argument("id", nargs="?")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("stage")
    a.add_argument("id", nargs="?")
    a.add_argument("--all", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--tab")
    a.add_argument("--replace", action="store_true",
                   help="overwrite text already in the compose box and drop its old attachments")
    a.add_argument("--ws", help="workspace with the ingested chats (default WHOSAID_WORKSPACE)")
    a.add_argument("--queue", action="store_true", help="with --all: stage every verified draft, oldest first")
    a.add_argument("--edit", action="store_true",
                   help="a sent draft: load its verified correction into Teams' Edit box on the sent message "
                        "(never saves; you save it in Teams)")
    a.add_argument("--allow-newer", action="store_true",
                   help="with --edit: edit even though others posted after your message")
    a.add_argument("--allow-longer", action="store_true",
                   help="with --edit: the new text may be much longer than the sent one")
    a = sub.add_parser("edit-sent", help="alias of stage ID --edit (plus --match-file for a chat not ingested)")
    a.add_argument("id")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--tab")
    a.add_argument("--ws", help="workspace with the ingested chats (default WHOSAID_WORKSPACE)")
    a.add_argument("--match-file", help="text of the sent message, when it isn't ingested yet")
    a.add_argument("--allow-longer", action="store_true", help="the new text may be much longer than the sent one")
    a.add_argument("--allow-newer", action="store_true", help="edit even though others posted after your message")
    a = sub.add_parser("mark-sent")
    a.add_argument("id", nargs="?", help="a draft id, or with --check an optional workspace")
    a.add_argument("--check", action="store_true", help="mark staged drafts sent that show up in the ingested chat")
    a.add_argument("--backfill", action="store_true",
                   help="with --check: also record the message for sent drafts closed by hand without one")
    a.add_argument("--ws")
    a.add_argument("--reason")
    a.add_argument("--force", action="store_true", help=force)
    a = sub.add_parser("merge", help="fold open drafts to the same recipient into one")
    a.add_argument("into")
    a.add_argument("from_ids", nargs="+", metavar="from")
    a.add_argument("--text-file", help="the merged message (markdown, or - for stdin); default: the texts joined")
    a.add_argument("--dry-run", action="store_true", help="print the merged result and clashes, write nothing")
    a.add_argument("--force", action="store_true", help=force)
    a = sub.add_parser("unstage", help="staged back to verified, after the compose box was cleared by hand")
    a.add_argument("id")
    a.add_argument("--reason")
    a = sub.add_parser("drop")
    a.add_argument("id")
    a.add_argument("--reason")
    a.add_argument("--force", action="store_true", help=force)
    return p


def _read_arg_file(f: str) -> str:
    return sys.stdin.read() if f == "-" else Path(f).read_text()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        c = args.cmd
        if c == "new":
            r = new(args.to, args.topic, args.page, args.supersedes, args.new_chat,
                    mentions=args.mention, separate=args.separate, thread_id=args.thread_id)
            print(json.dumps({k: v for k, v in r.items() if k != "note"}, indent=2))
            if r.get("note"):
                print(f"note: {r['note']}", file=sys.stderr)
        elif c == "text":
            r = write(args.id, text=_read_arg_file(args.file), force=args.force)
            print(f"{r['id']}: {r['state']}")
        elif c == "claims":
            data = json.loads(_read_arg_file(args.file))
            r = write(args.id, claims=data.get("claims", []), stakeholders=data.get("stakeholders", []),
                      docs=data.get("docs", []), force=args.force)
            print(f"{r['id']}: {r['state']}; gate: {len(gate(load(args.id), args.ws))} open")
            for k in said_check(claims_of(load(args.id)), args.ws)["said"]:  # the real words, before the prose
                for t in k["turns"]:
                    print(f"  said {k['key']}: [{t['meeting']} @ {t['t_str']}] {t['speaker']}: {t['text']}")
                for e in k["errors"]:
                    print(f"  said {k['key']}: {e}")
        elif c in ("attach", "detach"):
            page = os.path.abspath(os.path.expanduser(args.page))
            key = "pages" if c == "attach" else "remove_pages"
            write(args.id, force=args.force, **{key: [page]})
            print("\n".join(load(args.id)["pages"]) or "(no pages)")
        elif c == "mentions":
            write(args.id, mentions=args.names, force=args.force)
            print(", ".join(load(args.id).get("mentions") or []) or "(no mentions)")
        elif c == "thread-id":
            write(args.id, thread_id=args.thread_id, force=args.force)
            print(load(args.id).get("thread_id") or "(no thread id: the stager checks the chat title)")
        elif c == "verify":
            r = verify(args.id, force=args.force, voice=args.voice, reason=args.reason, ws=args.ws,
                       recap_days=args.recap_days)
            print(f"verified {r['id']}: {r['confirmed']} confirmed, {r['unverified']} unverified, "
                  f"{r['refuted']} refuted and dropped, {r['stakeholders']} stakeholder(s)")
            for p in (r["recap"] or {}).get("points") or []:
                print(f"  recap point {p['n']} ({p['stance']}): {p['text']}")
                for t in p["turns"]:
                    print(f"    [{t['meeting']} @ {t['t_str']}] {t['speaker']}: {t['text'][:200]}")
            for w in r["voice_warnings"]:
                print(f"warning: {w}", file=sys.stderr)
        elif c == "lint":
            r = lint(args.id, all_open=args.all, ws=args.ws)
            if args.json:
                print(json.dumps(r, indent=2))
            for d in [] if args.json else r["drafts"]:
                verdict = "FAIL" if d["would_fail"] else "ok"
                print(f"{d['id']}  {verdict}  voice lint {d['mode']} ({d['source']})")
                for e in d["errors"]:
                    print(f"   error: {e}")
                for w in d["warnings"]:
                    print(f"   warning: {w}")
            return 1 if r["would_fail"] else 0
        elif c == "path":
            print(ddir(load(args.id)["id"]))
        elif c in ("list", "mine"):
            rows = listing(include_closed=getattr(args, "all", False), mine=c == "mine" or args.mine)
            print(json.dumps(rows, indent=2)) if args.json else _print_rows(rows)
        elif c == "show":
            s = show(args.id, args.rev)
            if args.json:
                print(json.dumps(s, indent=2))
            else:
                o = s["owner"]
                print(f"# {s['id']}\nto: {s['to']}\nstate: {s['state']}\npages: {', '.join(s['pages']) or '(none)'}")
                if s["mentions"]:
                    print(f"mentions: {', '.join(s['mentions'])}")
                if s["merged_from"]:
                    print(f"merged_from: {', '.join(s['merged_from'])}")
                if s["merged_into"]:
                    print(f"merged_into: {s['merged_into']}")
                for k in s["contributors"]:
                    print(f"contributor: {k.get('name') or '(unnamed)'} {(k.get('sessionId') or '')[:8]}")
                if s["revisions"]:
                    by = (s["revision_log"][(s["rev"] or 1) - 1].get("by") or {})
                    print(f"revision: {s['rev']} of {s['revisions']} (by {by.get('name') or (by.get('sessionId') or 'user')[:8]})")
                print(f"owner: {s['owner_line']}\n  cwd: {o.get('cwd')}\n  resume: {o.get('resume') or '(not made from Claude Code)'}")
                if o.get("name"):
                    print(f'  from another Claude session: SendMessage to "{o["name"]}"')
                print(f"\n--- draft.md\n{s['text'] or '(missing)'}--- claims")
                for k in s["claims"].get("claims", []):
                    print(f"  [{k.get('verdict') or '?'}{', dropped' if k.get('dropped') else ''}] ({k.get('kind') or '?'}) {k.get('text')}")
                for st in s["claims"].get("stakeholders", []):
                    print(f"  stakeholder {st.get('name')}: {st.get('why') or ''}; searched: {' | '.join(st.get('searched') or [])}")
                if s["gate"]:
                    print("--- gate\n  - " + "\n  - ".join(s["gate"]))
        elif c == "diff":
            r = diff(args.id, args.rev_from, args.rev_to)
            if args.json:
                print(json.dumps(r, indent=2))
            else:
                print(r["diff"] or f"rev {r['from']} and rev {r['to']} are the same")
        elif c == "html":
            r = html_page(args.out, args.all)
            print(f"{r['path']} ({r['drafts']} draft(s))")
        elif c == "stakeholders":
            r = stakeholders(_ws(args.ws), args.topic, args.repo, args.path, args.k, args.exclude)
            if args.json:
                print(json.dumps(r, indent=2))
            else:
                for p in r["stakeholders"][:15]:
                    nw = p["newest"]
                    print(f"{p['score']:>7.3f}  {p['name']}  ({p['why']})"
                          + (f"\n         newest: {nw['meeting']} {nw['t_str']} {nw['text'][:90]}" if nw else ""))
        elif c == "stale-check":
            r = stale_check(_ws(args.ws), args.id)
            if args.json:
                print(json.dumps(r, indent=2))
            elif not r["maybe_stale"]:
                print(f"no new statements since verify ({r['checked']} draft(s) checked)")
            else:
                for d in r["maybe_stale"]:
                    print(f"{d['id']} (to {d['to']}, owner {d['owner_line']}), verified {d['verified_at'][:16]}")
                    for f in d["new_statements"]:
                        print(f"   {f['meeting']} {f['t_str']} {f['speaker']} on '{f['topic']}': {f['text'][:100]}")
        elif c == "edit-sent":
            r = edit_sent(args.id, args.dry_run, args.tab, args.ws, args.match_file, args.allow_longer,
                          args.allow_newer)
            for w in r.get("warnings") or []:
                print(f"warning: {w}", file=sys.stderr)
            if "would_run" in r:
                print(f"{r['id']}: would edit your message in {r['to']} ({r['sent_message']}): "
                      + " ".join(shlex.quote(x) for x in r["would_run"]))
            else:
                print(f"{r['id']}: {r['note']}. {r['stager']}")
        elif c == "stage":
            if not args.all and not args.id:
                raise DraftsError("need an id or --all")
            if args.queue and not args.all:
                raise DraftsError("--queue needs --all")
            if (args.allow_newer or args.allow_longer) and not args.edit:
                raise DraftsError("--allow-newer/--allow-longer go with --edit")
            if args.edit and (args.all or args.replace):
                raise DraftsError("--edit takes one draft id (no --all, --replace)")
            r = stage([args.id] if args.id else None, args.all, args.dry_run, args.tab, args.ws, args.replace,
                      queue=args.queue, edit=args.edit, allow_newer=args.allow_newer, allow_longer=args.allow_longer)
            for w in r.get("warnings") or []:
                print(f"warning: {w}", file=sys.stderr)
            si = r.get("stager_info")
            if si:
                print(f"stager: {si['stager']}" + (f", {si['target']}" if si["kind"] == "built-in" else ""),
                      file=sys.stderr)
                if si.get("warning"):
                    print(f"warning: {si['warning']}", file=sys.stderr)
            for did, ctx in r.get("context", {}).items():
                print(f"{did}: {ctx['chat']} has {ctx['ingested']} ingested message(s) in 14 days"
                      + (f", newest {ctx['newest_at'][:16]}" if ctx["newest_at"] else ""))
                for x in ctx["recent"]:
                    print(f"   {x['at'][5:16]} {'you' if x['self'] else x['speaker']}: {x['text'][:100]}")
                if ctx["reply_since_ours"]:
                    print("   they replied after your last message")
            if r.get("history_note"):
                print(r["history_note"], file=sys.stderr)
            for s in r.get("would_run", []):
                print(f"{s['id']}: would run {' '.join(shlex.quote(x) for x in s['argv'])}")
            for s in r["staged"]:
                print(f"{s['id']}: {'edit staged on your message' if r.get('edit') else 'staged'} in {s['to']}. "
                      f"{s['stager']}")
            for s in r["skipped"]:
                print(f"{s['id']}: skip, {s['why']}")
            for s in r["failed"]:
                print(f"{s['id']}: {s['why']}", file=sys.stderr)
            if r.get("note"):
                print(r["note"])
            return 1 if r["failed"] else 0
        elif c == "mark-sent" and args.check:
            r = sent_check(_ws(args.ws or args.id), backfill=args.backfill)
            for s in r.get("backfilled", []):
                print(f"{s['id']}: backfilled, went out at {s['at']} (found in {s['folder']})")
            for s in r.get("not_found", []):
                print(f"{s['id']}: sent, its message not found in the chat"
                      + (f" (lookup failed: {s['error']})" if s.get("error") else ""))
            for s in r["sent"]:
                print(f"{s['id']}: sent at {s['at']} (found in {s['folder']})")
            for s in r["still_staged"]:
                print(f"{s['id']}: still staged, {s['why']}")
            for s in r["edited"]:
                print(f"{s['id']}: edit {s['n']} saved in Teams at {s['edited_at']}")
            for s in r["edit_not_seen"]:
                print(f"{s['id']}: edit staged, not seen saved yet: {s['why']}")
            if not any(r.get(k) for k in ("sent", "still_staged", "edited", "edit_not_seen", "backfilled", "not_found")):
                print("no staged drafts")
            elif r["still_staged"]:
                print(r["note"])
        elif c == "merge":
            r = merge(args.into, args.from_ids, _read_arg_file(args.text_file) if args.text_file else None,
                      force=args.force, dry_run=args.dry_run)
            verb = "would merge" if r["dry_run"] else "merged"
            print(f"{verb} {', '.join(r['from'])} into {r['into']} (to {r['to']}): {len(r['claims'])} claim(s), "
                  f"{len(r['stakeholders'])} stakeholder(s), {len(r['pages'])} page(s), "
                  f"{len(r['mentions'])} mention(s), {len(r['docs'])} doc(s)")
            if r["dry_run"]:
                for k in r["claims"]:
                    print(f"  claim [{k.get('verdict') or '?'}] {_claim_id(k)}")
                for st in r["stakeholders"]:
                    print(f"  stakeholder {st.get('name')}; searched: {' | '.join(st.get('searched') or [])}")
                for pg in r["pages"]:
                    print(f"  page {pg}")
                for cl in r["clashes"]:
                    print(f"  CLASH {cl['key']}: {cl['kept']} vs {cl['other']}", file=sys.stderr)
                return 1 if r["clashes"] else 0
            if r["text_joined"]:
                print("text: the drafts' texts are joined with blank lines; rewrite them as one message "
                      "(drafts text), then verify")
            print(f"{r['into']}: {r['state']}; verify it again before stage")
        elif c == "unstage":
            r = unstage(args.id, args.reason)
            print(f"{r['id']}: {r['state']}")
        elif c in ("mark-sent", "drop"):
            if not args.id:
                raise DraftsError("need a draft id (or mark-sent --check)")
            if getattr(args, "backfill", False):  # mark-sent only; drop has no --backfill
                raise DraftsError("--backfill goes with --check")
            r = close(args.id, "sent" if c == "mark-sent" else "dropped", args.reason, force=args.force,
                      ws=getattr(args, "ws", None))
            sm = r.get("sent_message") or {}
            print(f"{r['id']}: {r['status']}" + (f" (message at {sm['at']})" if sm.get("at") else ""))
            if r.get("lookup_error"):
                print(f"whosaid drafts: closed, but the chat lookup failed ({r['lookup_error']}); "
                      "run mark-sent --check --backfill later to record the message", file=sys.stderr)
    except DraftsError as e:
        print(f"whosaid drafts: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())

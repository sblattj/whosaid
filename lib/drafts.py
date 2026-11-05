#!/usr/bin/env python3
"""
Drafts outbox (GitHub issue #165): chat drafts that wait offline, carry the agent session
that wrote them, get their claims checked before the prose is written, and reach Teams only
when the user stages them.

    whosaid drafts new --to "First Last" --topic <slug> [--page /abs/page.html ...] [--supersedes ID] [--new-chat]
    whosaid drafts text ID (--file draft.md | -)          write the message
    whosaid drafts claims ID --file claims.json           write {claims, stakeholders, docs}
    whosaid drafts attach ID /abs/page.html
    whosaid drafts verify ID                              check the gates, stamp a content hash
    whosaid drafts list [--all] [--mine] [--json]
    whosaid drafts show ID [--json]
    whosaid drafts stakeholders [WS] --topic T [--topic T2] [--repo DIR --path P ...] [--json]
    whosaid drafts stale-check [WS] [ID] [--json]         new statements since each draft was verified
    whosaid drafts stage ID|--all [--dry-run] [--tab TARGET]
    whosaid drafts mark-sent ID | drop ID [--reason R]

A draft is a folder, ~/.drafts/<id>/ (WHOSAID_DRAFTS_DIR overrides): draft.md, claims.json,
draft.json. Pages stay where they were written and are referenced by absolute path.

Verify gates: draft.md exists with no em dash; every claim has a verdict (confirmed, refuted,
unverified); a refuted claim is marked dropped; a confirmed claim has evidence and a tech claim
cites a URL; at least one stakeholder, each with the topics searched. Verify hashes the message,
claims and pages; any later edit reads as stale until verified again.

Staging is the only step that drives a browser. It holds ~/.drafts/.outbox.lock, refuses stale
drafts and rival open drafts to the same recipient, then runs the stager command from
WHOSAID_DRAFTS_STAGER (argv: --chat NAME --md FILE [--attach PAGE ...] [--replace
--only-these-attachments] [--new-chat] [--tab ID]) with TEAMS_OUTBOX_LOCK set. Nothing is sent.

Stdlib only; stakeholders and stale-check read the workspace search index (search.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

OPEN = ("drafting", "verified", "staged")
VERDICTS = ("confirmed", "refuted", "unverified")
KINDS = ("code", "tech", "people", "status")
UNNAMED = re.compile(r"^SPEAKER_\d+$")
EM_DASH = "\u2014"


class DraftsError(Exception):
    """A refused request: bad id, failed gate, held lock, missing stager."""


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
    return h.hexdigest()[:16]


def state(m: dict) -> str:
    if m["status"] not in OPEN:
        return m["status"]
    h = content_hash(m)
    if m["status"] == "staged" and m.get("staged_hash") == h:
        return "staged"
    if m.get("verified_hash") == h:
        return "verified, changed since staged" if m["status"] == "staged" else "verified"
    return "stale, edited after verify" if m.get("verified_hash") else "drafting"


def claims_of(m: dict) -> dict:
    return _read_json(ddir(m["id"]) / "claims.json", {"claims": [], "stakeholders": [], "docs": []})


def gate(m: dict) -> list[str]:
    d = ddir(m["id"])
    errs: list[str] = []
    md = d / "draft.md"
    if not md.exists():
        errs.append("draft.md missing")
    elif EM_DASH in md.read_text():
        errs.append("draft.md has an em dash")
    for p in m.get("pages", []):
        if not Path(p).exists():
            errs.append(f"page missing: {p}")
    c = _read_json(d / "claims.json")
    if c is None:
        return errs + ["claims.json missing"]
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
    sh = c.get("stakeholders") or []
    if not sh:
        errs.append("stakeholders[] empty: who has opinions on this, and what did whosaid say")
    for s in sh:
        if not s.get("searched"):
            errs.append(f"stakeholder {s.get('name')}: no whosaid topics searched")
    return errs


def _event(m: dict, event: str, **kw) -> None:
    m.setdefault("history", []).append({"at": now(), "event": event, **kw})
    m["updated_at"] = now()


# ---- operations (CLI and MCP share these) ------------------------------------------

def new(to: str, topic: str, pages: list[str] | None = None, supersedes: str | None = None,
        new_chat: bool = False, owner_session: str | None = None) -> dict:
    to, topic = (to or "").strip(), (topic or "").strip()
    if not to:
        raise DraftsError('need the recipient, exactly as the chat list shows the name ("First Last")')
    if not topic:
        raise DraftsError("need a topic")
    pages = list(pages or [])
    for p in pages:
        if not os.path.isabs(p):
            raise DraftsError(f"page needs an absolute path: {p}")
    if supersedes:
        load(supersedes)
    me = owner(owner_session)
    clash = [d for d in all_drafts() if d["status"] in OPEN and d["to"] == to and d["id"] != supersedes]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    draft_id = f"{ts}-{slug(to)}-{slug(topic)}"
    d = ddir(draft_id)
    if (d / "draft.json").exists():
        raise DraftsError(f"{draft_id} exists; edit it instead")
    d.mkdir(parents=True, exist_ok=True)
    m = {"id": draft_id, "to": to, "topic": topic, "status": "drafting", "new_chat": bool(new_chat),
         "pages": pages, "supersedes": supersedes, "owner": me, "created_at": now(), "updated_at": now(),
         "verified_hash": None, "staged_hash": None, "history": [{"at": now(), "event": "new", "by": me["sessionId"]}]}
    save(m)
    (d / "claims.json").write_text(json.dumps({"claims": [], "stakeholders": [], "docs": []}, indent=2) + "\n")
    out = {"id": draft_id, "path": str(d), "next": "write claims and stakeholders, verify them, then the text"}
    if clash:
        out["note"] = (f"open draft(s) to {to} already: " + ", ".join(f"{c['id']} [{owner_line(c['owner'])}]" for c in clash)
                       + ". Teams has one compose box per chat: edit that draft, or make this one with supersedes.")
    return out


def write(draft_id: str, text: str | None = None, claims: list | None = None, stakeholders: list | None = None,
          docs: list | None = None, pages: list[str] | None = None) -> dict:
    """Replace the message text and/or the claims parts that are passed; add pages."""
    m = load(draft_id)
    if m["status"] not in OPEN:
        raise DraftsError(f"{draft_id} is {m['status']}")
    d = ddir(draft_id)
    if text is not None:
        (d / "draft.md").write_text(text if text.endswith("\n") else text + "\n")
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


def verify(draft_id: str) -> dict:
    m = load(draft_id)
    if m["status"] not in OPEN:
        raise DraftsError(f"{draft_id} is {m['status']}")
    errs = gate(m)
    if errs:
        raise DraftsError("not verified:\n  - " + "\n  - ".join(errs))
    m["verified_hash"] = content_hash(m)
    m["verified_at"] = now()
    if m["status"] == "drafting":
        m["status"] = "verified"
    _event(m, "verified", hash=m["verified_hash"], by=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    save(m)
    c = claims_of(m)
    n = {v: sum(1 for k in c["claims"] if k.get("verdict") == v) for v in VERDICTS}
    return {"id": draft_id, "state": "verified", **n, "stakeholders": len(c.get("stakeholders") or [])}


def summary(m: dict) -> dict:
    return {"id": m["id"], "to": m["to"], "topic": m.get("topic"), "state": state(m), "owner": m["owner"],
            "owner_line": owner_line(m["owner"]), "owner_live": alive(m["owner"].get("pid")),
            "updated_at": m["updated_at"], "supersedes": m.get("supersedes"), "pages": m.get("pages", [])}


def listing(include_closed: bool = False, mine: bool = False, session_id: str | None = None) -> list[dict]:
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if mine and not sid:
        raise DraftsError("mine needs a Claude Code session (CLAUDE_CODE_SESSION_ID)")
    rows = [m for m in all_drafts() if include_closed or m["status"] in OPEN]
    if mine:
        rows = [m for m in rows if m["owner"].get("sessionId") == sid]
    return [summary(m) for m in rows]


def show(draft_id: str) -> dict:
    m = load(draft_id)
    md = ddir(draft_id) / "draft.md"
    return {**summary(m), "text": md.read_text() if md.exists() else None, "claims": claims_of(m),
            "gate": gate(m), "history": m.get("history", [])}


def close(draft_id: str, status: str, reason: str | None = None) -> dict:
    m = load(draft_id)
    m["status"] = status
    _event(m, status, reason=reason)
    save(m)
    return {"id": draft_id, "status": status}


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
    out = subprocess.run(["git", "-C", repo, "log", f"--since={since}", "--format=%an", "--", *(paths or ["."])],
                         capture_output=True, text=True, timeout=30)
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
    lk = _lock_path()
    try:
        lk.mkdir()
    except FileExistsError:
        holder = _read_json(lk / "owner.json", {}) or {}
        if alive(holder.get("pid")):
            raise DraftsError(f"outbox locked by pid {holder.get('pid')} ({holder.get('what')}) since {holder.get('at')}")
        for f in lk.iterdir():
            f.unlink()
        lk.rmdir()
        lk.mkdir()
    (lk / "owner.json").write_text(json.dumps({"pid": os.getpid(), "at": now(), "what": what}))
    return lk


def release_lock() -> None:
    lk = _lock_path()
    if lk.exists():
        for f in lk.iterdir():
            f.unlink()
        lk.rmdir()


def stager() -> list[str]:
    cmd = os.environ.get("WHOSAID_DRAFTS_STAGER", "").strip()
    if not cmd:
        raise DraftsError("set WHOSAID_DRAFTS_STAGER to the command that pastes a draft into a Teams chat "
                          "(it gets --chat NAME --md FILE [--attach PAGE ...])")
    return shlex.split(cmd)


def stage(ids: list[str] | None = None, all_ready: bool = False, dry_run: bool = False,
          tab: str | None = None) -> dict:
    if all_ready:
        ids = [m["id"] for m in all_drafts() if m["status"] in OPEN and state(m).startswith("verified")]
    ids = ids or []
    if not ids:
        return {"staged": [], "skipped": [], "failed": [], "note": "nothing verified and unstaged"}
    cmd = stager() if not dry_run else (shlex.split(os.environ.get("WHOSAID_DRAFTS_STAGER", "")) or ["<stager>"])
    if not dry_run:
        acquire_lock(" ".join(["stage", *ids]))
    res: dict = {"staged": [], "skipped": [], "failed": [], "would_run": []}
    try:
        for draft_id in ids:
            m = load(draft_id)
            st, errs = state(m), gate(m)
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
                res["failed"].append({"id": draft_id, "why": f"other open draft(s) to {m['to']}: {', '.join(rivals)}"})
                continue
            sup = load(m["supersedes"]) if m.get("supersedes") else None
            prev = m["status"] == "staged" or bool(sup and sup["status"] == "staged")
            argv = ["--chat", m["to"], "--md", str(ddir(draft_id) / "draft.md")]
            for p in m.get("pages", []):
                argv += ["--attach", p]
            if prev:
                argv += ["--replace", "--only-these-attachments"]
            if m.get("new_chat"):
                argv += ["--new-chat"]
            if tab:
                argv += ["--tab", tab]
            if dry_run:
                res["would_run"].append({"id": draft_id, "argv": cmd + argv})
                continue
            env = {**os.environ, "TEAMS_OUTBOX_LOCK": str(_lock_path())}
            try:
                r = subprocess.run(cmd + argv, capture_output=True, text=True, env=env, timeout=300)
            except (OSError, subprocess.TimeoutExpired) as exc:
                r = subprocess.CompletedProcess(cmd, 1, "", str(exc))
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            if r.returncode != 0:
                msg = ((r.stderr or "") + (r.stdout or "")).strip()[:500]
                _event(m, "stage-failed", err=msg)
                save(m)
                res["failed"].append({"id": draft_id, "why": f"stager exit {r.returncode}: {msg}"})
                break
            m["status"], m["staged_hash"], m["staged_at"] = "staged", content_hash(m), now()
            _event(m, "staged", out=(r.stdout or "")[-500:])
            save(m)
            if sup and sup["status"] in OPEN:
                sup["status"] = "superseded"
                _event(sup, "superseded", by=draft_id)
                save(sup)
            res["staged"].append({"id": draft_id, "to": m["to"], "stager": tail[0]})
            # The stager focuses the chat it staged: one per run, so the user sends or edits it first.
            if len(ids) > 1:
                res["note"] = f"staged 1 of {len(ids)}; run stage --all again for the next"
                break
    finally:
        if not dry_run:
            release_lock()
    if not dry_run:
        res.pop("would_run")
    return res


# ---- CLI ---------------------------------------------------------------------------

def _ws(arg: str | None) -> Path:
    from wsconfig import resolve_workspace
    return resolve_workspace(arg)


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("outbox empty")
    for r in rows:
        extra = f"   supersedes: {r['supersedes']}" if r.get("supersedes") else ""
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
    a = sub.add_parser("text")
    a.add_argument("id")
    a.add_argument("--file", required=True, help="markdown file, or - for stdin")
    a = sub.add_parser("claims")
    a.add_argument("id")
    a.add_argument("--file", required=True, help="JSON {claims, stakeholders, docs}, or - for stdin")
    a = sub.add_parser("attach")
    a.add_argument("id")
    a.add_argument("page")
    for name in ("verify", "path"):
        sub.add_parser(name).add_argument("id")
    a = sub.add_parser("list")
    a.add_argument("--all", action="store_true")
    a.add_argument("--mine", action="store_true")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("mine")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("show")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
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
    for name in ("mark-sent", "drop"):
        a = sub.add_parser(name)
        a.add_argument("id")
        a.add_argument("--reason")
    return p


def _read_arg_file(f: str) -> str:
    return sys.stdin.read() if f == "-" else Path(f).read_text()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        c = args.cmd
        if c == "new":
            r = new(args.to, args.topic, args.page, args.supersedes, args.new_chat)
            print(json.dumps({k: v for k, v in r.items() if k != "note"}, indent=2))
            if r.get("note"):
                print(f"note: {r['note']}", file=sys.stderr)
        elif c == "text":
            r = write(args.id, text=_read_arg_file(args.file))
            print(f"{r['id']}: {r['state']}")
        elif c == "claims":
            data = json.loads(_read_arg_file(args.file))
            r = write(args.id, claims=data.get("claims", []), stakeholders=data.get("stakeholders", []),
                      docs=data.get("docs", []))
            print(f"{r['id']}: {r['state']}; gate: {len(r['gate'])} open")
        elif c == "attach":
            print("\n".join(write(args.id, pages=[args.page]) and load(args.id)["pages"]))
        elif c == "verify":
            r = verify(args.id)
            print(f"verified {r['id']}: {r['confirmed']} confirmed, {r['unverified']} unverified, "
                  f"{r['refuted']} refuted and dropped, {r['stakeholders']} stakeholder(s)")
        elif c == "path":
            print(ddir(load(args.id)["id"]))
        elif c in ("list", "mine"):
            rows = listing(include_closed=getattr(args, "all", False), mine=c == "mine" or args.mine)
            print(json.dumps(rows, indent=2)) if args.json else _print_rows(rows)
        elif c == "show":
            s = show(args.id)
            if args.json:
                print(json.dumps(s, indent=2))
            else:
                o = s["owner"]
                print(f"# {s['id']}\nto: {s['to']}\nstate: {s['state']}\npages: {', '.join(s['pages']) or '(none)'}")
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
        elif c == "stage":
            if not args.all and not args.id:
                raise DraftsError("need an id or --all")
            r = stage([args.id] if args.id else None, args.all, args.dry_run, args.tab)
            for s in r.get("would_run", []):
                print(f"{s['id']}: would run {' '.join(shlex.quote(x) for x in s['argv'])}")
            for s in r["staged"]:
                print(f"{s['id']}: staged in {s['to']}. {s['stager']}")
            for s in r["skipped"]:
                print(f"{s['id']}: skip, {s['why']}")
            for s in r["failed"]:
                print(f"{s['id']}: {s['why']}", file=sys.stderr)
            if r.get("note"):
                print(r["note"])
            return 1 if r["failed"] else 0
        elif c in ("mark-sent", "drop"):
            r = close(args.id, "sent" if c == "mark-sent" else "dropped", args.reason)
            print(f"{r['id']}: {r['status']}")
    except DraftsError as e:
        print(f"whosaid drafts: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())

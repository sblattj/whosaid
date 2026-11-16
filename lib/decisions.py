#!/usr/bin/env python3
"""
decisions.py: one ledger per call that joins every turn on a subject, across
revisits (`whosaid decisions`, the whosaid_decisions MCP tool; issue #213).

A call often settles one subject in two places far apart ("the date parser stays
in the service lib for now" at minute 11, "the auth client moves to a platform
lib" at minute 39). Anything that reads the call one line at a time tends to merge
the two and drop the first. The ledger keeps one row per decision, each with every
turn that supports it, and marks the topic when its visits say different things.

  ledger(turns, meeting)   the reusable entry point (lane #217's live `decision`
                           events build on it); turns are dicts {t, speaker, text}
                           (+ optional clock), from turns_from_transcript() or
                           turns_from_live(). Pure and deterministic.
  changes(prev, cur)       what is new between two ledgers of one growing call:
                           decision / status / flag events.

Local engine (the default; no model, no network):

  1. TOPICS    the live insights clustering (lib/live_insights.py: ~45 s chunks
               joined by tf-idf cosine); a topic back after >= REVISIT_GAP (120 s)
               away is one more visit, numbered from 1.
  2. PROPOSALS a sentence with a decision cue ("let's", "we'll", "we should",
               "go with", "stays in", "moves to", "decided" ...) that is not a
               hedge ("could", "maybe"), meeting navigation ("let's move on",
               "keep it moving") or a question (except "should we ...?").
  3. REPLIES   the next REPLY_TURNS turns within REPLY_SECONDS of the proposal:
               another speaker's explicit yes ("agreed", "works for me", "yes")
               is an `agree` turn, push-back ("I don't think", "wait", "yes, but")
               a `contest` turn; the proposer saying it again (or anyone, with a
               decision cue and the same terms) a `restate` turn; a turn on the
               same topic sharing >= 2 content terms a `discuss` turn. A later
               proposal in the same visit with the same terms joins the row too.
  4. STATUS    contested when someone's last stance is push-back, else agreed
               when someone other than the proposer said yes, else proposed.
  5. VISITS    decisions of one topic on different visits are compared by their
               content terms (overlap coefficient): below SAME_SUBJECT they are a
               `split` (different things decided), else a `reversal` when the later
               one undoes it ("actually", "instead", "let's not") or a
               `refinement`. The topic gets "split across visits", "refined across
               visits" or "reversed across visits" so a summary cannot keep one.

Every cited turn is the transcript's own text: [meeting, t, speaker, verbatim].

Claude engine (OPT-IN, CLOUD: the transcript goes to Anthropic): `--engine claude`,
or `[summarizer] engine = "claude"` / `[decisions] engine = "claude"` in the
workspace's whosaid.toml (the action-items opt-in, issue #44). One isolated
`claude -p` call (lib/claude_engine.py ClaudeCLI) reads the numbered transcript and
answers {"decisions": [{subject, text, turns: [n, ...], status, relation}]}. The
script keeps only turn numbers that exist and quotes those turns itself; visits are
cut by REVISIT_GAP between decisions of one subject. Any failure (no CLI, a bad
reply) falls back to the local ledger with `engine_error` set. The MCP tool and
--follow are always local.

CLI:
  whosaid decisions [WS] <meeting> [--json] [--engine auto|local|claude] [--out F]
      <meeting>: a folder name in WS (default $WHOSAID_WORKSPACE; a unique
      substring is enough), a meeting folder, or a *.speakers.txt path.
  whosaid decisions --live [--into DIR] [--json] [--ws WS]
      the newest live session (<into>/.live/*.jsonl).
  whosaid decisions --live --follow [--into DIR] [--once] [--poll S] [--json]
      keep <stem>.decisions.json next to the session current and print each new
      decision, status change and flag as it happens (--json: one event per line;
      --once: exit after the ended call).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402
import live_insights as li  # noqa: E402
import wsconfig  # noqa: E402

VERSION = 1
REPLY_TURNS = 4          # turns after a proposal that can answer it
REPLY_SECONDS = 90.0     # ... and only this soon after it
SAME_SUBJECT = 0.5       # overlap coefficient of two decisions' terms: the same thing decided
RESTATE_MIN = 0.6        # a later proposal in the same visit this close joins the row
DISCUSS_TERMS = 2        # shared content terms for a neutral reply to count as support
FLAGS = {"split": "split across visits", "refinement": "refined across visits",
         "reversal": "reversed across visits"}
STATUSES = ("agreed", "proposed", "contested")
RELATIONS = ("split", "refinement", "reversal")
REL_WORDS = {"split": "split from", "refinement": "refines", "reversal": "reverses"}

PROPOSE_RE = re.compile(
    r"\b(let's|let us|we'll|we will|we're going to|we are going to|we should|should we|we decided|"
    r"decided to|the decision is|the plan is|we agreed|go with|going with|so we \w+|"
    r"(?:should|will|can) (?:live|stay|move|go|sit|be kept|be moved)|stays (?:in|with|on|where)|"
    r"moves (?:to|into|out)|goes (?:to|into))\b", re.I)
HEDGE_RE = re.compile(r"\b(could|might|maybe|perhaps|what if|how about|not sure|i wonder)\b", re.I)
MEETING_RE = re.compile(r"\b(keep (?:it|this|things) (?:moving|short|brief|going)|get started|wrap (?:up|it up)|"
                        r"take a break|move on|hard stop|see you|talk later|we'll see|let's see|"
                        r"let's (?:start|begin|go around|recap)|we'll (?:start|begin))\b", re.I)
AGREE_RE = re.compile(r"(^\W*(yes|yeah|yep|yup|sure|ok|okay|correct|exactly|perfect|deal)\b)|"
                      r"\b(agreed|i agree|agree with|sounds good|sounds great|sounds right|makes sense|"
                      r"works for me|that works|fine by me|good call|let's do (?:it|that)|\+1|on board)\b", re.I)
CONTEST_RE = re.compile(r"(^\W*(no|nope|wait|hold on|hang on)\b)|(^\W*\w+,? but\b)|"
                        r"\b(i don't think|don't agree|disagree|not sure|i'd rather|i would rather|shouldn't|"
                        r"push back|won't work|doesn't work|not convinced|bad idea|i object|concerned)\b", re.I)
REVERSE_RE = re.compile(r"\b(actually|instead|scratch that|change of plan|no longer|not anymore|after all|"
                        r"reverse|undo|rather than|let's not|we won't|we shouldn't|not (?:use|keep|move|put|go with))\b",
                        re.I)
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


# ---------------------------------------------------------------- turns in

def turns_from_transcript(text: str, meeting: str = "") -> list[dict]:
    """A speaker-labeled transcript (*.speakers.txt) -> turn dicts, in time order."""
    out = [{"meeting": meeting, "t": float(t.t_sec), "t_str": wsconfig.hms(t.t_sec), "speaker": t.speaker,
            "text": t.text.strip()} for t in wsconfig.parse_turns(text) if t.text.strip()]
    return sorted(out, key=lambda t: t["t"])


def live_speaker(e: dict) -> str:
    """"me", the live name a voice matched, else "them (v3)" so two voices stay two people."""
    who = live.speaker(e)
    if who == e.get("who") and who != "me" and e.get("voice"):
        return f"{who} ({e['voice']})"
    return who


def turns_from_live(entries: list[dict], meeting: str = "") -> list[dict]:
    """Live .jsonl entries (me/them lines) -> turn dicts, in time order."""
    out = [{"meeting": meeting, "t": float(e["t"]), "t_str": wsconfig.hms(float(e["t"])), "clock": e.get("clock") or "",
            "speaker": live_speaker(e), "text": (e.get("text") or "").strip()}
           for e in entries if (e.get("text") or "").strip()]
    return sorted(out, key=lambda t: t["t"])


# ---------------------------------------------------------------- local engine

def terms(text: str) -> set[str]:
    return set(li.content(text))


def overlap(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def proposal_sentence(text: str) -> str | None:
    """The first sentence of a turn that reads as a decision being made, else None."""
    for s in li.sentences(text):
        if len(li.words(s)) < 4 or not PROPOSE_RE.search(s) or MEETING_RE.search(s) or li.NAV_RE.search(s):
            continue
        if HEDGE_RE.search(s):
            continue
        if s.rstrip().endswith("?") and not re.search(r"\bshould we\b", s, re.I):
            continue
        return s.strip()
    return None


def stance(text: str) -> str | None:
    """'contest', 'agree' or None for a reply (push-back wins over a yes in the same turn)."""
    if CONTEST_RE.search(text):
        return "contest"
    if AGREE_RE.search(text):
        return "agree"
    return None


def topic_visits(turns: list[dict]) -> tuple[list[dict], list[int], list[int]]:
    """Live insights topics (finished: label, visits) plus, per turn, its topic index and
    the 1-based visit of that topic the turn falls in."""
    entries = [{"t": t["t"], "text": t["text"], "who": t["speaker"]} for t in turns]
    if not entries:
        return [], [], []
    chunks = li.chunk(entries)
    topics, of_chunk = li.assign(entries, chunks)
    li.finish(entries, chunks, topics, li.durations(entries))
    topic_of = [0] * len(turns)
    visit_of = [1] * len(turns)
    seen: dict[int, tuple[int, float]] = {}     # topic -> (visit, last t)
    for n, idx in enumerate(chunks):
        k = of_chunk[n]
        t0, t1 = float(entries[idx[0]]["t"]), float(entries[idx[-1]]["t"])
        visit, last = seen.get(k, (0, None))
        if last is None or t0 - last >= li.REVISIT_GAP:
            visit += 1
        seen[k] = (visit, t1)
        for i in idx:
            topic_of[i], visit_of[i] = k, visit
    return topics, topic_of, visit_of


def cite(turn: dict, role: str) -> dict:
    row = {"meeting": turn.get("meeting", ""), "t": turn["t"], "t_str": turn.get("t_str") or wsconfig.hms(turn["t"]),
           "speaker": turn["speaker"], "text": turn["text"], "role": role}
    if turn.get("clock"):
        row["clock"] = turn["clock"]
    return row


def status_of(d: dict) -> str:
    last: dict[str, str] = {}
    for tr in d["turns"]:
        if tr["role"] in ("agree", "contest") and tr["speaker"] != d["speaker"]:
            last[tr["speaker"]] = tr["role"]
    if "contest" in last.values():
        return "contested"
    return "agreed" if "agree" in last.values() else "proposed"


def relation(a: dict, b: dict) -> str:
    """How a later visit's decision b stands to an earlier one a."""
    if overlap(terms(a["text"]), terms(b["text"])) < SAME_SUBJECT:
        return "split"
    return "reversal" if REVERSE_RE.search(b["text"]) else "refinement"


def new_id(t: float, used: set) -> str:
    base = f"d-{int(round(t))}"
    did, n = base, 2
    while did in used:
        did, n = f"{base}-{n}", n + 1
    used.add(did)
    return did


def local_decisions(turns: list[dict]) -> tuple[list[dict], dict]:
    """(decision rows without relations, {topic id: topic info}) for the local engine."""
    topics, topic_of, visit_of = topic_visits(turns)
    rows: list[dict] = []
    open_: list[tuple[int, dict]] = []          # (proposal turn index, row) still taking replies
    used: set = set()
    for i, turn in enumerate(turns):
        k, visit = topic_of[i], visit_of[i]
        open_ = [(j, d) for j, d in open_ if i - j <= REPLY_TURNS and turn["t"] - turns[j]["t"] <= REPLY_SECONDS]
        sent = proposal_sentence(turn["text"])
        attached = False
        for j, d in reversed(open_):                # a reply answers the latest proposal
            if turn["speaker"] != d["speaker"] and (st := stance(turn["text"])):
                d["turns"].append(cite(turn, st))
                attached = True
                break
        if attached and not (sent and stance(turn["text"]) == "contest"):
            continue
        if sent:
            same = [d for d in rows if d["_k"] == k and d["visit"] == visit
                    and overlap(terms(d["text"]), terms(sent)) >= RESTATE_MIN]
            if same and not attached:
                same[-1]["turns"].append(cite(turn, "restate"))
                continue
            if not same:
                d = {"id": new_id(turn["t"], used), "_k": k, "topic": topics[k]["id"], "topicLabel": topics[k]["label"],
                     "visit": visit, "t": turn["t"], "t_str": turn.get("t_str") or wsconfig.hms(turn["t"]),
                     "speaker": turn["speaker"], "text": sent, "turns": [cite(turn, "proposal")], "related": []}
                if turn.get("clock"):
                    d["clock"] = turn["clock"]
                rows.append(d)
                open_.append((i, d))
            continue
        if attached:
            continue
        mine = terms(turn["text"])
        for _j, d in reversed(open_):
            if d["_k"] == k and len(mine & terms(" ".join(t["text"] for t in d["turns"]))) >= DISCUSS_TERMS:
                d["turns"].append(cite(turn, "discuss"))
                break
    info = {tp["id"]: {"label": tp["label"], "visits": tp["visits"]} for tp in topics}
    return rows, info


# ---------------------------------------------------------------- shared assembly

def assemble(rows: list[dict], info: dict, meeting: str, turns: list[dict], engine: str,
             hints: dict | None = None) -> dict:
    """Statuses (unless given), cross-visit relations, topic rows and flags."""
    hints = hints or {}
    for d in rows:
        d.pop("_k", None)
        if d.get("status") not in STATUSES:
            d["status"] = status_of(d)
    by_topic: dict[str, list[dict]] = {}
    for d in sorted(rows, key=lambda d: (d["t"], d["id"])):
        by_topic.setdefault(d["topic"], []).append(d)
    topic_rows = []
    for tid, ds in by_topic.items():
        rels = []
        for bi, b in enumerate(ds):
            earlier = [a for a in ds[:bi] if a["visit"] < b["visit"]]
            for a in earlier:
                kind = hints.get(b["id"]) if a is earlier[-1] and hints.get(b["id"]) in RELATIONS else relation(a, b)
                rels.append({"from": a["id"], "to": b["id"], "kind": kind})
                a["related"].append({"id": b["id"], "kind": kind})
                b["related"].append({"id": a["id"], "kind": kind})
        kinds = {r["kind"] for r in rels}
        topic_rows.append({"id": tid, "label": info.get(tid, {}).get("label", tid),
                           "visits": max([info.get(tid, {}).get("visits", 1)] + [d["visit"] for d in ds]),
                           "decisions": [d["id"] for d in ds], "decisionVisits": sorted({d["visit"] for d in ds}),
                           "flags": [FLAGS[k] for k in RELATIONS if k in kinds], "relations": rels})
    decisions = sorted(rows, key=lambda d: (d["t"], d["id"]))
    return {"version": VERSION, "meeting": meeting, "engine": engine, "lines": len(turns),
            "t": turns[-1]["t"] if turns else 0.0, "decisions": decisions, "topics": topic_rows}


# ---------------------------------------------------------------- claude engine

CLAUDE_SYSTEM = (
    "You list the decisions made in a meeting transcript. A decision is something the group settles or "
    "proposes to settle: where something lives, what to do, what not to do. It is not an errand one person "
    "takes on. Keep separate decisions separate even when they share a subject, and never merge two "
    "decisions made at different times into one. Reply with JSON only, no prose: "
    '{"decisions": [{"subject": "<short noun phrase; the SAME string for every decision on one subject>", '
    '"text": "<the decision in one sentence>", "turns": [<turn numbers: the proposal first, then every turn '
    'that restates, agrees with or pushes back on it>], "status": "agreed|proposed|contested", '
    '"relation": "split|refinement|reversal (only when an earlier decision on the same subject exists: '
    'split = a different thing decided, refinement = the same decision with more detail, reversal = it '
    'undoes the earlier one)"}]}. agreed = someone other than the proposer said yes; contested = '
    "push-back nobody resolved; proposed = no reply."
)


def _claude_client(model: str, timeout: int, binary: str = ""):
    """The Claude CLI client; tests pass their own via ledger(client=...)."""
    from claude_engine import ClaudeCLI
    return ClaudeCLI(model, binary=binary, timeout=timeout)


def parse_reply(reply: str) -> list[dict]:
    s = FENCE_RE.sub("", str(reply).strip())
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("the reply holds no JSON object")
    obj = json.loads(s[a:b + 1])
    items = obj.get("decisions") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise ValueError("the reply has no decisions list")
    return [x for x in items if isinstance(x, dict)]


def claude_decisions(turns: list[dict], meeting: str, client) -> tuple[list[dict], dict, dict]:
    rows_in = "\n".join(f"#{n} [{t.get('t_str') or wsconfig.hms(t['t'])}] {t['speaker']}: {t['text']}"
                        for n, t in enumerate(turns))
    reply = client.chat(CLAUDE_SYSTEM, f"Meeting: {meeting or '(unnamed)'}\nTranscript:\n{rows_in}")
    items = parse_reply(reply)
    rows: list[dict] = []
    hints: dict = {}
    used: set = set()
    for x in items:
        nums: list[int] = []
        for n in x.get("turns") or []:
            if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(turns) and n not in nums:
                nums.append(n)
        if not nums:
            continue
        first, rest = turns[nums[0]], sorted((turns[n] for n in nums[1:]), key=lambda t: t["t"])
        subject = str(x.get("subject") or "").strip() or "Decisions"
        cited = [cite(first, "proposal")]
        for tr in rest:
            st = stance(tr["text"]) if tr["speaker"] != first["speaker"] else None
            cited.append(cite(tr, st or ("restate" if tr["speaker"] == first["speaker"] else "discuss")))
        d = {"id": new_id(first["t"], used), "topic": "s-" + li.slug(subject), "topicLabel": subject, "visit": 1,
             "t": first["t"], "t_str": first.get("t_str") or wsconfig.hms(first["t"]), "speaker": first["speaker"],
             "text": str(x.get("text") or "").strip() or first["text"], "turns": cited, "related": []}
        if first.get("clock"):
            d["clock"] = first["clock"]
        if x.get("status") in STATUSES:
            d["status"] = x["status"]
        if x.get("relation") in RELATIONS:
            hints[d["id"]] = x["relation"]
        rows.append(d)
    if not rows and items:
        raise ValueError("no decision in the reply cites a turn that exists")
    info: dict = {}
    last: dict[str, tuple[int, float]] = {}
    for d in sorted(rows, key=lambda d: d["t"]):
        visit, end = last.get(d["topic"], (0, None))
        if end is None or d["t"] - end >= li.REVISIT_GAP:
            visit += 1
        d["visit"] = visit
        last[d["topic"]] = (visit, max(t["t"] for t in d["turns"]))
        info[d["topic"]] = {"label": d["topicLabel"], "visits": visit}
    return rows, info, hints


# ---------------------------------------------------------------- the entry points

def ledger(turns: list[dict], meeting: str = "", engine: str = "local", client=None,
           cfg: dict | None = None) -> dict:
    """The decisions ledger for one call. engine "claude" asks Claude once (client, or a
    ClaudeCLI from cfg's [summarizer] claude_* keys) and falls back to local on any failure."""
    turns = sorted((dict(t) for t in turns), key=lambda t: float(t["t"]))   # never the caller's dicts
    for t in turns:
        t["t"] = float(t["t"])
        t.setdefault("meeting", meeting)
        t.setdefault("t_str", wsconfig.hms(t["t"]))
    err = ""
    if engine == "claude" and turns:
        try:
            if client is None:
                summ = (cfg or {}).get("summarizer") or {}
                client = _claude_client(str(summ.get("claude_model") or "opus"),
                                        int(summ.get("claude_timeout") or 900), str(summ.get("claude_bin") or ""))
            rows, info, hints = claude_decisions(turns, meeting, client)
            return assemble(rows, info, meeting, turns, "claude", hints)
        except Exception as e:  # noqa: BLE001  any failure: the local ledger stands
            err = str(e) or type(e).__name__
    rows, info = local_decisions(turns)
    res = assemble(rows, info, meeting, turns, "local")
    if err:
        res["engine_error"] = err
    return res


def changes(prev: dict | None, cur: dict) -> list[dict]:
    """Events between two ledgers of one growing call: a new decision, a status change,
    a new topic flag. Empty when nothing changed."""
    prev = prev or {"decisions": [], "topics": []}
    old = {d["id"]: d for d in prev.get("decisions", [])}
    old_flags = {t["id"]: set(t["flags"]) for t in prev.get("topics", [])}
    out: list[dict] = []
    for d in cur.get("decisions", []):
        o = old.get(d["id"])
        if o is None:
            out.append({"event": "decision", "id": d["id"], "topic": d["topic"], "topicLabel": d["topicLabel"],
                        "visit": d["visit"], "t": d["t"], "t_str": d["t_str"], **({"clock": d["clock"]} if d.get("clock") else {}),
                        "speaker": d["speaker"], "status": d["status"], "text": d["text"]})
        elif o["status"] != d["status"]:
            out.append({"event": "status", "id": d["id"], "from": o["status"], "to": d["status"], "text": d["text"]})
    for t in cur.get("topics", []):
        for f in t["flags"]:
            if f not in old_flags.get(t["id"], set()):
                out.append({"event": "flag", "topic": t["id"], "label": t["label"], "flag": f,
                            "decisions": list(t["decisions"])})
    return out


def engine_for(ws: Path | None, explicit: str = "") -> str:
    """--engine wins; else [decisions] engine, then [summarizer] engine, of an explicit
    workspace; never claude without one."""
    if explicit in ("local", "claude"):
        return explicit
    if ws is None:
        return "local"
    cfg = wsconfig.load_config(Path(ws))
    chosen = str((cfg.get("decisions") or {}).get("engine") or (cfg.get("summarizer") or {}).get("engine") or "")
    return "claude" if chosen == "claude" else "local"


# ---------------------------------------------------------------- rendering

def render(res: dict) -> str:
    ds = {d["id"]: d for d in res["decisions"]}
    num = {d["id"]: f"D{n}" for n, d in enumerate(res["decisions"], 1)}
    lines = [f"{res['meeting'] or 'call'}: {len(ds)} decision(s) on {len(res['topics'])} topic(s), "
             f"{res['engine']} engine" + (f" (claude failed: {res['engine_error']})" if res.get("engine_error") else "")]
    for tp in sorted(res["topics"], key=lambda t: ds[t["decisions"][0]]["t"]):
        visits = "visit" if tp["visits"] == 1 else "visits"
        head = f"\n{tp['label']} ({tp['visits']} {visits})"
        if tp["flags"]:
            head += f": {', '.join(tp['flags'])}; a summary must keep each decision below"
        lines.append(head)
        for did in tp["decisions"]:
            d = ds[did]
            rel = "".join(f", {REL_WORDS[r['kind']]} {num[r['id']]}" for r in d["related"] if ds[r["id"]]["t"] < d["t"])
            when = d.get("clock") or d["t_str"]
            lines.append(f"  {num[did]} visit {d['visit']} [{when}] {d['status']}{rel}: {d['text']}")
            for tr in d["turns"]:
                lines.append(f"     [{tr.get('clock') or tr['t_str']}] {tr['speaker']} ({tr['role']}): {tr['text']}")
    if not ds:
        lines.append("no decisions found")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- meetings and live sessions

class DecisionsError(RuntimeError):
    pass


def meeting_transcript(folder: Path) -> Path:
    files = sorted(p for p in folder.glob("*.speakers.txt") if p.is_file())
    if not files:
        raise DecisionsError(f"no *.speakers.txt in {folder}")
    return max(files, key=lambda p: (p.stat().st_size, p.name))


def find_meeting(ws: Path, name: str) -> Path:
    """A meeting folder of ws by exact name, else by a unique substring."""
    names = [f for f, _files in wsconfig.iter_meetings(ws)]
    if name in names:
        return ws / name
    cands = [n for n in names if name.lower() in n.lower()]
    if len(cands) == 1:
        return ws / cands[0]
    if len(cands) > 1:
        raise DecisionsError(f"meeting {name!r} is ambiguous: {', '.join(cands)}")
    raise DecisionsError(f"unknown meeting {name!r} in {ws}; known: {', '.join(names) or '(none)'}")


def load_meeting(target: list[str]) -> tuple[Path, Path, str]:
    """[WS] MEETING | PATH -> (transcript, workspace, meeting name)."""
    if len(target) == 2:
        ws = Path(target[0]).expanduser()
        p = Path(target[1]).expanduser()
        folder = p if p.is_dir() else (p.parent if p.is_file() else find_meeting(ws, target[1]))
        tr = p if p.is_file() else meeting_transcript(folder)
        return tr, ws, folder.name
    p = Path(target[0]).expanduser()
    if p.is_file():
        return p, p.resolve().parent.parent, p.resolve().parent.name
    if p.is_dir() and list(p.glob("*.speakers.txt")):
        return meeting_transcript(p), p.resolve().parent, p.resolve().name
    ws = wsconfig.resolve_workspace(None)
    folder = find_meeting(ws, target[0])
    return meeting_transcript(folder), ws, folder.name


def sidecar(jl: Path) -> Path:
    stem = jl.name[: -len(".jsonl")] if jl.name.endswith(".jsonl") else jl.stem
    return jl.with_name(stem + ".decisions.json")


def live_ledger(jl: Path, engine: str = "local", cfg: dict | None = None) -> tuple[dict, dict]:
    meta, entries = live.read_entries(jl)
    stem = sidecar(jl).name[: -len(".decisions.json")]
    res = ledger(turns_from_live(entries, stem), meeting=stem, engine=engine, cfg=cfg)
    res["ended"] = bool(meta.get("ended"))
    return meta, res


def event_line(e: dict) -> str:
    if e["event"] == "decision":
        return f"[{e.get('clock') or e['t_str']}] decision {e['id']} ({e['status']}, {e['topicLabel']}, visit {e['visit']}) {e['speaker']}: {e['text']}"
    if e["event"] == "status":
        return f"status {e['id']}: {e['from']} -> {e['to']}: {e['text']}"
    return f"flag {e['label']}: {e['flag']} ({', '.join(e['decisions'])})"


def follow(into: Path, poll: float = 2.0, once: bool = False, max_hours: float = 12.0, as_json: bool = False,
           stop=lambda: False) -> int:
    """Keep the newest live session's <stem>.decisions.json current (local engine) and print
    each change event once."""
    deadline = time.monotonic() + max_hours * 3600
    seen: dict[Path, tuple] = {}
    prev: dict[Path, dict] = {}
    while not stop() and time.monotonic() < deadline:
        found = live.sessions(into)
        if found:
            jl = found[-1]
            try:
                st = jl.stat()
                sig = (st.st_mtime_ns, st.st_size)
                if seen.get(jl) != sig:
                    _meta, res = live_ledger(jl)
                    res["updated"] = time.time()
                    li.write_json(sidecar(jl), res)
                    for e in changes(prev.get(jl), res):
                        print(json.dumps(e) if as_json else event_line(e), flush=True)
                    seen[jl], prev[jl] = sig, res
                    if once and res["ended"]:
                        return 0
            except (OSError, ValueError) as e:      # a half-written file: next poll
                print(f"decisions: {jl.name}: {e}", file=sys.stderr, flush=True)
        time.sleep(poll)
    return 0


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid decisions",
                                description="One decisions ledger per call, joined across topic revisits.")
    p.add_argument("target", nargs="*", help="[WS] MEETING (folder name or substring), a meeting folder, "
                   "or a *.speakers.txt path")
    p.add_argument("--live", action="store_true", help="the newest live session instead of a meeting")
    p.add_argument("--into", default=str(live.DEFAULT_INTO), help="recordings dir for --live")
    p.add_argument("--follow", action="store_true", help="with --live: keep <stem>.decisions.json current")
    p.add_argument("--once", action="store_true", help="with --follow: exit after the ended call")
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--max-hours", type=float, default=12.0)
    p.add_argument("--engine", choices=("auto", "local", "claude"), default="auto",
                   help="auto: claude only when the workspace's whosaid.toml opts in")
    p.add_argument("--ws", help="workspace whose whosaid.toml picks the engine (--live; default: the meeting's)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="also write the ledger JSON here")
    args = p.parse_args(argv)
    explicit = "" if args.engine == "auto" else args.engine

    try:
        if args.live:
            into = Path(args.into).expanduser()
            if args.follow:
                state = {"stop": False}

                def on_signal(_s, _f):
                    state["stop"] = True
                signal.signal(signal.SIGINT, on_signal)
                signal.signal(signal.SIGTERM, on_signal)
                return follow(into, args.poll, args.once, args.max_hours, args.json, stop=lambda: state["stop"])
            found = live.sessions(into)
            if not found:
                raise DecisionsError(f"no live session under {into / live.LIVE_DIR}")
            ws = Path(args.ws).expanduser() if args.ws else None
            engine = engine_for(ws, explicit)
            _meta, res = live_ledger(found[-1], engine, wsconfig.load_config(ws) if ws else None)
        else:
            if not args.target or len(args.target) > 2:
                p.error("pass [WS] MEETING, a meeting folder, or a *.speakers.txt path (or --live)")
            tr, ws, name = load_meeting(args.target)
            if args.ws:
                ws = Path(args.ws).expanduser()
            engine = engine_for(ws, explicit)
            turns = turns_from_transcript(tr.read_text(errors="replace"), meeting=name)
            res = ledger(turns, meeting=name, engine=engine, cfg=wsconfig.load_config(ws) if engine == "claude" else None)
    except DecisionsError as e:
        print(f"decisions: {e}", file=sys.stderr)
        return 1
    if args.out:
        li.write_json(Path(args.out).expanduser(), res)
    print(json.dumps(res, indent=1) if args.json else render(res), end="" if not args.json else "\n")
    if res.get("engine_error") and not args.json:
        print(f"decisions: claude failed, local ledger shown: {res['engine_error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

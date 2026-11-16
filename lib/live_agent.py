#!/usr/bin/env python3
"""
live_agent.py: `whosaid live follow --agent` (issue #217), events for an AI agent's
background monitor, read from the newest live session (<into>/.live/<start>.jsonl).

Plain `live follow` prints one line per Whisper segment. An agent acting as a live
copilot wants less noise and more structure, so --agent prints events instead:

  call_started   {session, started, clock, voices}: a session was first seen.
  utterance      {t, end, clock, speaker, voice, confidence, mixed, text, n, topic,
                 topicLabel, visit}: consecutive segments of one voice joined while the
                 gap stays under JOIN_GAP (8 s, live_insights.utterances' rule) and the
                 span under UTTER_MAX. It is printed once it is finished: another voice
                 (or a later line JOIN_GAP on) follows it in the file, the call ended,
                 or the file has not been written for --flush-seconds. topic / visit are
                 the live insights clustering as of that moment.
  topic_revisit  {topic, label, visit, t, speaker, text, first, previous, gap}: a topic
                 came back after >= REVISIT_GAP (120 s) away. `first` is the first turn
                 of its first visit, `previous` the first turn of the visit before.
                 Printed once the ~45 s chunk that brought it back is complete (so it
                 cannot be a passing guess), before that chunk's utterance when it can.
  for_me         {why, speaker, text, before}: a turn from someone else that names you
                 (`named`), or, right after you spoke, puts a question to "you"
                 (`question`) or asks you to do something (`request`: "can you take
                 that"). `before` is the 2 utterances before it. Local regexes.
  decision       {id, status, text, speaker, proposal, yes, turns}: a decisions ledger
                 row (lib/decisions.py, the local engine, the same ids as the
                 <start>.decisions.json sidecar) the first time it is agreed: a proposal
                 with an explicit yes from another voice. `decision_status` if it later
                 turns contested.
  call_ended     {session, voices, utterances, stale, superseded}: meta ended, no write
                 for --stale-seconds, or a newer session started (the old file is read
                 once more first, so its last lines come before call_ended). Then the
                 next call's call_started, unless --once.
  alive          {cursor, at}: with --heartbeat S, after S seconds with no event.

--cursor-file F keeps the follower's place: after each event it writes F (tmp +
os.replace) with the session, the entry numbers ("n") already emitted, and the keys
of the derived events. A restarted follower resumes from F: every line ends up in
exactly one utterance, and each derived event is printed once. Lines are keyed by the
writer's sequence number, so the .jsonl being rewritten (names added to earlier
lines, a late line sorted in) moves nothing. A for_me or topic_revisit is saved
before its utterance, so a kill between them never loses one.

Stdlib only (lib/live.py, lib/live_insights.py, lib/decisions.py).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402
import live_insights as li  # noqa: E402
import decisions as dc  # noqa: E402

JOIN_GAP = li.JOIN_GAP          # same voice, next line this soon: one utterance
UTTER_MAX = 90.0                # an utterance spanning this long is cut there
FLUSH_SECONDS = 45.0            # the file not written for this long: the last utterance is finished
CONTEXT_TURNS = 2               # utterances before a for_me
CURSOR_VERSION = 1

YOU_RE = re.compile(r"\b(you|your|yours|you're|you'll|you've)\b", re.I)
REQUEST_RE = re.compile(
    r"\b((can|could|would|will) you\b|would you mind|are you able|do you want to|can i ask you|"
    r"(you|u) take (that|this|it)|take (that|this) one|on your plate|over to you|"
    r"assign(ed)? (it|that|this) to you|you own (that|this|it))", re.I)


def log(msg: str) -> None:
    print(f"live follow: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the cursor file

class Cursor:
    """Where an agent follower is: one session, the line keys already emitted and the
    keys of the derived events already printed."""

    def __init__(self, session: str | None = None):
        self.session = session
        self.consumed: set = set()
        self.revisits: set[str] = set()
        self.for_me: set[str] = set()
        self.decisions: dict[str, str] = {}
        self.started = False
        self.ended = False

    def consumed_set(self) -> set:
        return set(self.consumed)

    def position(self) -> str:
        """`<stem>:n<N>`: every line up to N emitted (`+K` more past it). Informational:
        resume with the cursor file, not this string."""
        ints = sorted(k for k in self.consumed if isinstance(k, int))
        w = -1
        for k in ints:
            if k != w + 1:
                break
            w = k
        extra = len(self.consumed) - (w + 1)
        return f"{self.session}:n{w}" + (f"+{extra}" if extra else "")

    def to_json(self) -> dict:
        ints = sorted(k for k in self.consumed if isinstance(k, int))
        ranges: list[list[int]] = []
        for k in ints:
            if ranges and k == ranges[-1][1] + 1:
                ranges[-1][1] = k
            else:
                ranges.append([k, k])
        return {"version": CURSOR_VERSION, "session": self.session, "lines": ranges,
                "keys": sorted(str(k) for k in self.consumed if not isinstance(k, int)),
                "revisits": sorted(self.revisits), "forMe": sorted(self.for_me),
                "decisions": dict(sorted(self.decisions.items())), "started": self.started, "ended": self.ended}

    @classmethod
    def from_json(cls, obj) -> "Cursor":
        if not isinstance(obj, dict) or obj.get("version") != CURSOR_VERSION:
            raise ValueError("not a whosaid agent cursor (want version 1)")
        c = cls(obj.get("session") or None)
        try:
            for a, b in obj.get("lines") or []:
                c.consumed.update(range(int(a), int(b) + 1))
            c.consumed.update(str(k) for k in obj.get("keys") or [])
            c.revisits = {str(k) for k in obj.get("revisits") or []}
            c.for_me = {str(k) for k in obj.get("forMe") or []}
            c.decisions = {str(k): str(v) for k, v in (obj.get("decisions") or {}).items()}
        except (TypeError, ValueError, AttributeError) as err:
            raise ValueError(f"bad agent cursor: {err}") from None
        c.started, c.ended = bool(obj.get("started")), bool(obj.get("ended"))
        return c


def load_cursor(path: Path | None) -> Cursor:
    """The saved cursor, a fresh one when the file does not exist; ValueError when it
    is not a cursor (never a silent replay)."""
    if path is None or not Path(path).exists() or not Path(path).read_text().strip():
        return Cursor()     # none yet, or an empty file (`mktemp`): start fresh
    try:
        return Cursor.from_json(json.loads(Path(path).read_text()))
    except json.JSONDecodeError as err:
        raise ValueError(f"cursor file {path} is not JSON: {err}") from None


def save_cursor(path: Path, cur: Cursor) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cur.to_json()) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- self, for_me

def self_names(explicit=None) -> list[str]:
    """--self NAME(s), else the registry's self-roled voice (WHOSAID_SPEAKER_DB)."""
    if explicit:
        return [n for n in explicit if n and n.strip()]
    path = Path(os.environ.get("WHOSAID_SPEAKER_DB") or Path.home() / ".config" / "whosaid" / "speakers.json")
    try:
        reg = json.loads(path.read_text())
        return [str(s["name"]) for s in reg.get("speakers", []) if s.get("role") == "self" and s.get("name")]
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return []


def is_mine(e: dict, names_lc: set[str]) -> bool:
    return e.get("who") == "me" or e.get("role") == "self" or str(e.get("name") or "").lower() in names_lc


class ForMe:
    """Whether a turn asks something of you."""

    def __init__(self, names):
        self.names_lc = {n.lower() for n in names}
        alts = set()
        for n in names:
            alts.add(n.replace("_", " "))
            first = n.split("_")[0].split(" ")[0]
            if len(first) > 1:
                alts.add(first)
        alts = sorted((a for a in alts if a.strip()), key=len, reverse=True)
        self.name_re = re.compile(r"\b(" + "|".join(re.escape(a) for a in alts) + r")\b", re.I) if alts else None

    def why(self, u: dict, before: list[dict]) -> list[str] | None:
        """Reasons (named / question / request) the utterance `u` is for you, else None.
        `before` holds the utterances before it (dicts with who/role/speaker/text)."""
        if is_mine(u, self.names_lc):
            return None
        text = u.get("text") or ""
        named = bool(self.name_re and self.name_re.search(text))
        question = any(s.rstrip().endswith("?") for s in li.sentences(text))
        request = bool(REQUEST_RE.search(text))
        after_me = bool(before) and is_mine(before[-1], self.names_lc)
        out = []
        if named:
            out.append("named")
        if question and (named or (after_me and YOU_RE.search(text))):
            out.append("question")
        if request and (named or after_me):
            out.append("request")
        return out or None


# ---------------------------------------------------------------- utterances

def keys_of(order: list[dict]) -> list:
    """A stable key per line: the writer's sequence number, else t plus a same-t ordinal."""
    if order and all(isinstance(e.get("n"), int) for e in order):
        return [e["n"] for e in order]
    out, same = [], {}
    for e in order:
        t = float(e["t"])
        same[t] = same.get(t, 0) + 1
        out.append(f"t{t!r}#{same[t]}")
    return out


def voice_of(e: dict) -> str:
    return "me" if e.get("who") == "me" else (e.get("voice") or str(e.get("who") or "them"))


def joins(prev: dict, e: dict, first: dict) -> bool:
    return prev.get("who") == e.get("who") and prev.get("voice") == e.get("voice") \
        and float(e["t"]) - float(prev["t"]) <= JOIN_GAP and float(e["t"]) - float(first["t"]) <= UTTER_MAX


def groups(order: list[dict], keys: list, skip=frozenset()) -> list[list[int]]:
    """Runs of consecutive indexes into `order` that make one utterance; a line in
    `skip` (already emitted) breaks a run and is left out."""
    out: list[list[int]] = []
    for i, e in enumerate(order):
        if keys[i] in skip:
            out.append([])     # a break
            continue
        if out and out[-1] and joins(order[out[-1][-1]], e, order[out[-1][0]]):
            out[-1].append(i)
        else:
            out.append([i])
    return [g for g in out if g]


def finished(g: list[int], order: list[dict], final: bool, flush: bool) -> bool:
    """A run no later line can extend any more."""
    if final or flush:
        return True
    last = g[-1]
    return last + 1 < len(order) and not joins(order[last], order[last + 1], order[g[0]])


def utterance(g: list[int], order: list[dict], keys: list) -> dict:
    es = [order[i] for i in g]
    confs = [float(e["confidence"]) for e in es if e.get("confidence") is not None]
    last = es[-1]
    return {"t": float(es[0]["t"]), "end": float(last["t"]), "clock": es[0].get("clock") or "",
            "who": last.get("who"), "role": last.get("role") or "", "speaker": dc.live_speaker(last),
            "voice": voice_of(last), "confidence": round(min(confs), 3) if confs else None,
            "mixed": any(bool(e.get("mixed")) for e in es),
            "text": " ".join((e.get("text") or "").strip() for e in es), "n": [keys[i] for i in g]}


def voices(order: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for e in order:
        v = voice_of(e)
        row = seen.setdefault(v, {"voice": v, "speaker": "", "lines": 0})
        row["speaker"] = dc.live_speaker(e)
        row["lines"] += 1
    return list(seen.values())


def turn_cite(e: dict, t_only: bool = False) -> dict:
    return {"t": float(e["t"]), "clock": e.get("clock") or "", "speaker": dc.live_speaker(e),
            "text": (e.get("text") or "").strip()}


# ---------------------------------------------------------------- the follower

class AgentFollower:
    def __init__(self, into: Path, fmt: str = "jsonl", out=None, cursor_file: Path | None = None,
                 names=None, stale_seconds: float = live.STALE_SECONDS, flush_seconds: float = FLUSH_SECONDS,
                 heartbeat: float | None = None, now=time.time):
        self.into, self.fmt, self.out = Path(into), fmt, out or sys.stdout
        self.cursor_file = Path(cursor_file) if cursor_file else None
        self.cur = load_cursor(self.cursor_file)
        self.names = self_names(names)
        self.forme = ForMe(self.names)
        self.stale_seconds, self.flush_seconds = stale_seconds, flush_seconds
        self.heartbeat, self.now = heartbeat, now
        self.last_emit = now()

    # -- output
    def emit(self, obj: dict) -> None:
        obj = {"event": obj.pop("event"), "session": self.cur.session, "cursor": self.cur.position(), **obj}
        print(json.dumps(obj) if self.fmt == "jsonl" else text_line(obj), file=self.out, flush=True)
        if self.cursor_file:
            save_cursor(self.cursor_file, self.cur)
        self.last_emit = self.now()

    def reset(self, stem: str) -> None:
        self.cur = Cursor(stem)

    # -- one read of one session file
    def process(self, jl: Path, superseded: bool = False) -> bool:
        """Emit what is new in `jl`; True when the call is over (call_ended emitted)."""
        try:
            meta, entries = live.read_entries(jl)
            mtime = jl.stat().st_mtime
        except (OSError, ValueError):       # replaced or removed between listing and reading
            if not superseded:
                return False
            meta, entries, mtime = {"ended": True}, [], 0.0
        order = sorted((e for e in entries if (e.get("text") or "").strip()),
                       key=lambda e: (float(e["t"]), e.get("n") if isinstance(e.get("n"), int) else 0))
        keys = keys_of(order)
        cur = self.cur
        if not cur.started:
            cur.started = True
            self.emit({"event": "call_started", "started": meta.get("started"),
                       "clock": live._clock_at(meta.get("started"), 0) if meta.get("started") else "",
                       "voices": voices(order)})
        idle = self.now() - max(float(meta.get("updated") or 0), mtime)
        stale = not meta.get("ended") and not superseded and idle >= self.stale_seconds
        final = superseded or bool(meta.get("ended")) or stale
        flush = idle >= self.flush_seconds

        # topics over every line so far; a chunk is settled once a later one started
        turns = [{"t": float(e["t"]), "speaker": dc.live_speaker(e), "text": e["text"].strip()} for e in order]
        topics, topic_of, visit_of = dc.topic_visits(turns) if turns else ([], [], [])
        chunk_list = li.chunk([{"t": t["t"], "text": t["text"], "who": t["speaker"]} for t in turns]) if turns else []
        settled = chunk_list if final else chunk_list[:-1]
        first_of: dict[tuple[int, int], int] = {}
        for i in range(len(order)):
            first_of.setdefault((topic_of[i], visit_of[i]), i)
        revisit_at = {}                       # entry index -> (topic index, visit) of a settled revisit
        for idx in settled:
            i = idx[0]
            words = {w for j in idx for w in li.content(turns[j]["text"])}
            if visit_of[i] > 1 and first_of.get((topic_of[i], visit_of[i])) == i and len(words) >= li.MIN_CHUNK_TERMS:
                revisit_at[i] = (topic_of[i], visit_of[i])   # a chunk too thin to pick a topic stays put: no event

        def revisit(i: int) -> None:
            k, v = revisit_at[i]
            key = f"{topics[k]['id']}:{v}"
            if key in cur.revisits:
                return
            cur.revisits.add(key)
            first, prev = order[first_of[(k, 1)]], order[first_of[(k, v - 1)]]
            prev_last = max(float(order[j]["t"]) for j in range(i) if topic_of[j] == k and visit_of[j] == v - 1)
            self.emit({"event": "topic_revisit", "topic": topics[k]["id"], "label": topics[k]["label"], "visit": v,
                       **turn_cite(order[i]), "first": dict(turn_cite(first), visit=1),
                       "previous": dict(turn_cite(prev), visit=v - 1), "gap": round(float(order[i]["t"]) - prev_last, 1)})

        all_groups = groups(order, keys)
        emitted = False
        for g in groups(order, keys, skip=cur.consumed):
            if not finished(g, order, final, flush):
                continue
            for i in g:
                if i in revisit_at:
                    revisit(i)
            u = utterance(g, order, keys)
            k, v = topic_of[g[0]], visit_of[g[0]]
            u.update(topic=topics[k]["id"], topicLabel=topics[k]["label"], visit=v)
            before = [utterance(bg, order, keys) for bg in all_groups if bg[-1] < g[0]][-CONTEXT_TURNS:]
            why = self.forme.why(u, before)
            ukey = str(u["n"][0])
            if why and ukey not in cur.for_me:
                cur.for_me.add(ukey)
                self.emit({"event": "for_me", "why": why, **{x: u[x] for x in ("t", "end", "clock", "speaker", "voice", "text")},
                           "topic": u["topic"], "visit": v,
                           "before": [{x: b[x] for x in ("t", "clock", "speaker", "text")} for b in before]})
            cur.consumed.update(u["n"])
            u.pop("who")
            u.pop("role")
            self.emit({"event": "utterance", **u})
            emitted = True
        for i in sorted(revisit_at):          # settled after their utterance was printed
            if keys[i] in cur.consumed:
                revisit(i)
        if emitted or final:
            self.decisions(order, keys)
        if final:
            cur.ended = True
            n_utt = len(groups(order, keys))
            self.emit({"event": "call_ended", "voices": voices(order), "utterances": n_utt,
                       "stale": stale, "superseded": superseded})
        return final

    def decisions(self, order: list[dict], keys: list) -> None:
        """decision events from the local ledger over the lines already emitted."""
        cur = self.cur
        done = [e for e, k in zip(order, keys) if k in cur.consumed]
        if not done:
            return
        res = dc.ledger(dc.turns_from_live(done, cur.session or ""), meeting=cur.session or "")
        for d in res["decisions"]:
            old = cur.decisions.get(d["id"])
            if old is None and d["status"] == "agreed":
                yes = next(t for t in d["turns"] if t["role"] == "agree" and t["speaker"] != d["speaker"])
                cur.decisions[d["id"]] = d["status"]
                cite = lambda t: {x: t[x] for x in ("t", "clock", "speaker", "text", "role") if x in t}  # noqa: E731
                self.emit({"event": "decision", "id": d["id"], "status": d["status"], "topic": d["topic"],
                           "topicLabel": d["topicLabel"], "visit": d["visit"], "t": d["t"], "clock": d.get("clock", ""),
                           "speaker": d["speaker"], "text": d["text"], "proposal": cite(d["turns"][0]),
                           "yes": cite(yes), "turns": [cite(t) for t in d["turns"]]})
            elif old is not None and old != d["status"]:
                cur.decisions[d["id"]] = d["status"]
                self.emit({"event": "decision_status", "id": d["id"], "from": old, "to": d["status"], "text": d["text"]})

    # -- the loop
    def run(self, once: bool = False, poll: float = 5.0, deadline: float | None = None, sleep=time.sleep,
            max_polls: int | None = None) -> int:
        polls = 0
        while (deadline is None or time.monotonic() < deadline) and (max_polls is None or polls < max_polls):
            polls += 1
            found = live.sessions(self.into)
            newest = found[-1] if found else None
            if newest is not None:
                if self.cur.session is None:
                    self.reset(newest.stem)
                elif self.cur.session != newest.stem:
                    if not self.cur.ended:    # the old call never said it ended: read it once more
                        self.process(newest.with_name(f"{self.cur.session}.jsonl"), superseded=True)
                        if once:
                            return 0
                    self.reset(newest.stem)
                    log(f"following {newest}")
                if not self.cur.ended:
                    if self.process(newest) and once:
                        return 0
                elif once:
                    return 0
            if self.heartbeat and self.now() - self.last_emit >= self.heartbeat:
                self.emit({"event": "alive", "at": round(self.now(), 3)})
            sleep(poll)
        return 0


def text_line(o: dict) -> str:
    ev = o["event"]
    if ev == "utterance":
        return f"[{o['clock']}] {o['speaker']}: {o['text']}"
    if ev == "for_me":
        return f">> for you ({', '.join(o['why'])}): [{o['clock']}] {o['speaker']}: {o['text']}"
    if ev == "topic_revisit":
        return (f"# back to {o['label']} (visit {o['visit']}, {o['gap'] / 60:.0f} min later; first time: "
                f"[{o['first']['clock']}] {o['first']['speaker']}: {o['first']['text']})")
    if ev == "decision":
        return f"# decision ({o['status']}): {o['speaker']}: {o['text']} / yes: {o['yes']['speaker']}"
    if ev == "decision_status":
        return f"# decision {o['id']}: {o['from']} -> {o['to']}: {o['text']}"
    if ev == "call_started":
        return f"call started {o['session']} ({', '.join(v['speaker'] for v in o['voices']) or 'nobody yet'})"
    if ev == "call_ended":
        why = " (a newer call started)" if o.get("superseded") else (" (stale)" if o.get("stale") else "")
        return f"ended {o['session']}{why}"
    return f"# alive {o['cursor']}"


def follow_agent(into: Path, fmt: str = "jsonl", once: bool = False, poll: float = 5.0,
                 stale_seconds: float = live.STALE_SECONDS, deadline: float | None = None, out=None,
                 sleep=time.sleep, cursor_file: Path | None = None, heartbeat: float | None = None,
                 self_names=None, flush_seconds: float = FLUSH_SECONDS, now=time.time,
                 max_polls: int | None = None) -> int:
    """`live follow --agent`. Raises ValueError for a cursor file that is not one."""
    f = AgentFollower(into, fmt, out, cursor_file, self_names, stale_seconds, flush_seconds, heartbeat, now)
    return f.run(once, poll, deadline, sleep, max_polls)

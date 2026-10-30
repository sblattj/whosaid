#!/usr/bin/env python3
"""
live_insights.py: topics, a wordgraph, summaries and next steps for the live
call (`whosaid live insights`, `whosaid live ask`; the macOS app reads it).

  build   One shot: <stem>.jsonl -> <stem>.insights.json (tmp + os.replace).
  follow  Follow the newest <into>/.live/*.jsonl and rewrite its .insights.json
          whenever the transcript changes (--once: exit after the ended call).
  ask     Answer a question from the newest live session: lexical search by
          default; only `[summarizer] engine = "claude"` in <ws>/whosaid.toml
          sends the transcript to Claude (any failure falls back to search).

The engine is local and deterministic (no model, no network): the same entries
give the same JSON apart from `updated`. Entries are cut into ~45 s chunks of
consecutive turns (by transcript `t`, never wall clock); each chunk joins the
topic whose term vector it matches best (cosine over tf-idf, idf from the chunks
seen so far, so a chunk's topic depends only on what came before it) or opens a
new topic. A topic id is the slug of its first chunk's dominant term plus an
ordinal and is never reassigned. A topic back after >= 120 s away counts one
more visit. A turn lasts until the next one, capped at 20 s (the last turn:
words / 2.5 s); speakingMs, ranks and shares use that. Wordgraph: topic ->
top terms (depth 1) -> bigrams holding the term, then co-occurring terms
(depth 2), at most 6 children each. Summaries are extractive, one per 5
transcript minutes. Next steps come from cue phrases (agreed vs suggested).

Schema: ~/.artifacts/whosaid-macos-app/logs/p7-contract.md. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402

VERSION = 1
CHUNK_SECONDS = 45.0
REVISIT_GAP = 120.0      # away this long (of t) and back = one more visit
NEW_WINDOW = 60.0        # firstT this close to the latest t = new
SUMMARY_SECONDS = 300.0
MATCH_MIN = 0.12         # cosine a chunk needs to join an existing topic
STICKY_MIN = 0.08        # the topic of the previous chunk keeps it from this
MIN_CHUNK_TERMS = 3      # fewer content terms: the chunk stays with the previous topic
TOPIC_TERMS = 6          # depth-1 children per topic
MAX_CHILDREN = 6
MAX_NODES = 800
TURN_CAP = 20.0
ASK_TOP = 5

STOPWORDS = frozenset("""
a about above after again against all almost also am an and any are aren't around as at
back be because been before being below between both but by can can't cannot could couldn't
did didn't do does doesn't doing don't done down during each either else even ever every few
for from further get gets getting go goes going gone got gotta had hadn't has hasn't have
haven't having he he'd he'll he's her here here's hers herself him himself his how how's i
i'd i'll i'm i've if in into is isn't it it'd it'll it's its itself just keep kind know let
let's like little lot lots made make makes making may me maybe mean might mine more most much
must my myself need needs never new no nor not now of off oh ok okay on once one only or other
ought our ours ourselves out over own pretty probably put quite rather really right said same
say says see seem seems she she'd she'll she's should shouldn't so some something sort still
such sure take than thank thanks that that's the their theirs them themselves then there
there's these they they'd they'll they're they've thing things think this those though through
to too totally try two under until up upon us very want wanna was wasn't way we we'd we'll
we're we've well were weren't what what's whatever when when's where where's whether which
while who who's whom why why's will with won't would wouldn't yeah yes yet you you'd you'll
you're you've your yours yourself yourselves actually anyway basically guess hmm huh mm mhm
um uh alright cool great good fine nice yep nope sounds look looks looking gonna today
tomorrow tonight week next last first time bit day days able call talk talked talking said
wait sorry hey hello hi stuff whole part already agree agreed send sent come came give
guys folks everyone everybody whoa wow bye soon forgot forget remember exactly definitely
absolutely correct awesome damn gosh anyways honestly literally question questions side using
feel feeling coming another ago assuming appreciate seen saw happen happened point minute
minutes second seconds hour hours morning afternoon evening work working works
""".split())

WORD_RE = re.compile(r"[a-z][a-z0-9']*[a-z0-9]|[a-z]")
SENT_RE = re.compile(r"[^.!?]+[.!?]*")
AGREED_RE = re.compile(r"\b(we'll|let's|i will|i'll|will do|we will)\b", re.I)
AGREED_SOFT_RE = re.compile(r"\b(agreed|sounds good|deal|works for me)\b", re.I)
SUGGESTED_RE = re.compile(r"\b(could|maybe|should we|might|what if|how about|we should)\b", re.I)
# meeting navigation, not a next step
NAV_RE = re.compile(r"\b(let's|we'll) (move on|switch|talk about|go back|start with|get into|turn to|"
                    r"circle back|pick up where|see where)\b", re.I)
ACTION_RE = re.compile(r"\b(send|write|draft|fix|ship|add|update|file|open|book|schedule|check|"
                       r"review|merge|move|cut|post|share|set up|test|run|rerun|ping|loop|"
                       r"tag|publish|record|pull|split|cap|track|own|take|land|bump|clean|set|"
                       r"build|create|polish|promote|deploy|implement|follow up|reach out|prepare|"
                       r"finish|deliver|demo|document|migrate|configure|install|investigate|"
                       r"look into|talk to|figure out|communicate|grant|sync with|email)\b", re.I)
# a sentence cut off mid-thought by the transcript chunking
DANGLING_RE = re.compile(r"(\b(the|a|an|to|and|of|with|for|but|or|that|in|on|at|my|our|your)|-)\W*$", re.I)


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} live insights: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- text

def words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower().replace("’", "'"))


def content(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS and len(w) > 2 and not w.isdigit()]


def bigrams(text: str) -> list[str]:
    """Adjacent content words with no stopword between them."""
    out, prev = [], None
    for w in words(text):
        if w in STOPWORDS or len(w) <= 2 or w.isdigit():
            prev = None
            continue
        if prev and prev != w:
            out.append(f"{prev} {w}")
        prev = w
    return out


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "topic"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_RE.findall(text) if s.strip()]


def mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def durations(entries: list[dict]) -> list[float]:
    out = []
    for i, e in enumerate(entries):
        if i + 1 < len(entries):
            d = float(entries[i + 1]["t"]) - float(e["t"])
        else:
            d = len(words(e.get("text") or "")) / 2.5
        out.append(max(0.0, min(d, TURN_CAP)))
    return out


# ---------------------------------------------------------------- chunks + topics

def chunk(entries: list[dict]) -> list[list[int]]:
    """Consecutive entry indexes, a new chunk once CHUNK_SECONDS of t have passed."""
    out: list[list[int]] = []
    start = None
    for i, e in enumerate(entries):
        t = float(e["t"])
        if start is None or t - start >= CHUNK_SECONDS:
            out.append([])
            start = t
        out[-1].append(i)
    return out


def cosine(a: Counter, b: Counter, idf: dict) -> float:
    dot = sum(a[k] * b[k] * idf.get(k, 1.0) ** 2 for k in a if k in b)
    if not dot:
        return 0.0
    na = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in a.items()))
    nb = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in b.items()))
    return dot / (na * nb) if na and nb else 0.0


def best_term(counts: Counter, idf: dict) -> str:
    return sorted(counts, key=lambda k: (-counts[k] * idf.get(k, 1.0), k))[0]


def assign(entries: list[dict], chunks: list[list[int]]) -> tuple[list[dict], list[int]]:
    """Topics (internal dicts, creation order) and the topic index of every chunk."""
    topics: list[dict] = []
    of_chunk: list[int] = []
    df: Counter = Counter()
    for n, idx in enumerate(chunks):
        terms = Counter()
        for i in idx:
            terms.update(content(entries[i].get("text") or ""))
        df.update(set(terms))
        idf = {k: math.log(1 + (n + 1) / df[k]) for k in df}
        prev = of_chunk[-1] if of_chunk else None
        pick = None
        if len(terms) < MIN_CHUNK_TERMS and prev is not None:
            pick = prev
        else:
            scored = sorted(((cosine(terms, tp["terms"], idf), -k, k) for k, tp in enumerate(topics)),
                            reverse=True)
            if prev is not None and cosine(terms, topics[prev]["terms"], idf) >= STICKY_MIN \
                    and (not scored or scored[0][2] == prev or scored[0][0] < MATCH_MIN * 1.5):
                pick = prev
            elif scored and scored[0][0] >= MATCH_MIN:
                pick = scored[0][2]
        if pick is None:
            dom = best_term(terms, idf) if terms else "topic"
            topics.append({"id": f"{slug(dom)}-{len(topics) + 1}", "terms": Counter(), "chunks": [],
                           "visits": 1, "lastT": None})
            pick = len(topics) - 1
        tp = topics[pick]
        t0 = float(entries[idx[0]]["t"])
        if tp["lastT"] is not None and t0 - tp["lastT"] >= REVISIT_GAP:
            tp["visits"] += 1
        tp["terms"].update(terms)
        tp["chunks"].append(n)
        tp["lastT"] = float(entries[idx[-1]]["t"])
        of_chunk.append(pick)
    return topics, of_chunk


# ---------------------------------------------------------------- build

def build(meta: dict, entries: list[dict], session: str = "", now: float | None = None) -> dict:
    entries = sorted(entries, key=lambda e: float(e["t"]))
    last_t = float(entries[-1]["t"]) if entries else 0.0
    out = {"version": VERSION, "session": session, "updated": time.time() if now is None else now,
           "t": last_t, "lines": len(entries), "ended": bool(meta.get("ended")),
           "topics": [], "nodes": [], "summaries": [], "nextSteps": []}
    if not entries:
        return out
    dur = durations(entries)
    chunks = chunk(entries)
    topics, of_chunk = assign(entries, chunks)
    topic_of_entry = {}
    for n, idx in enumerate(chunks):
        for i in idx:
            topic_of_entry[i] = of_chunk[n]

    # global idf over chunks: what makes a term distinctive for its topic
    df = Counter()
    for idx in chunks:
        df.update({w for i in idx for w in content(entries[i].get("text") or "")})
    total_ms = finish(entries, chunks, topics, dur)
    nt = len(topics)

    order = sorted(range(nt), key=lambda k: (-topics[k]["ms"], topics[k]["firstT"], topics[k]["id"]))
    rank = {k: r + 1 for r, k in enumerate(order)}
    for k in order:
        tp = topics[k]
        out["topics"].append({
            "id": tp["id"], "rootId": tp["id"], "label": tp["label"], "rank": rank[k],
            "speakingMs": tp["ms"], "visits": tp["visits"],
            "share": round(tp["ms"] / total_ms, 4) if total_ms else round(1 / nt, 4),
            "new": tp["firstT"] >= last_t - NEW_WINDOW, "firstT": tp["firstT"], "lastT": tp["lastT"],
            "terms": tp["top"][:TOPIC_TERMS]})

    out["nodes"] = wordgraph(entries, topics, order)
    out["summaries"] = summaries(entries, chunks, of_chunk, topics, df, last_t)
    out["nextSteps"] = next_steps(entries, topics, topic_of_entry)
    return out


def finish(entries: list[dict], chunks: list[list[int]], topics: list[dict], dur: list[float]) -> int:
    """Fill in every topic's entries, speaking time, term weights, top terms, bigrams
    and label (in place); returns the total speaking ms. Shared with topics.py."""
    tdf = Counter()
    for tp in topics:
        tdf.update(set(tp["terms"]))
    nt = len(topics)

    total_ms = 0
    for k, tp in enumerate(topics):
        idx = [i for n in tp["chunks"] for i in chunks[n]]
        tp["idx"] = idx
        tp["ms"] = int(round(sum(dur[i] for i in idx) * 1000))
        tp["firstT"] = float(entries[idx[0]]["t"])
        total_ms += tp["ms"]
        tp["weight"] = {w: c * math.log(1 + nt / tdf[w]) for w, c in tp["terms"].items()}
        tp["top"] = sorted(tp["weight"], key=lambda w: (-tp["weight"][w], w))
        bg = Counter()
        for i in idx:
            bg.update(bigrams(entries[i].get("text") or ""))
        tp["bigrams"] = bg
        dom = tp["top"][0] if tp["top"] else tp["id"].rsplit("-", 1)[0]
        pair = sorted((b for b, c in bg.items() if c >= 2 and dom in b.split()),
                      key=lambda b: (-bg[b], b))
        if not pair:  # a phrase from the topic's top terms beats one bare word
            top = set(tp["top"][:TOPIC_TERMS])
            pair = sorted((b for b, c in bg.items() if c >= 2 and set(b.split()) <= top),
                          key=lambda b: (-bg[b], b))
        if pair:
            tp["label"] = pair[0].title()
        else:  # no repeated phrase: the two strongest terms say more than one
            tp["label"] = " · ".join(w.title() for w in tp["top"][:2]) or dom.title()
    return total_ms


def node(nid, topic, parent, depth, label, description="", source="meeting") -> dict:
    return {"id": nid, "topicId": topic, "parentId": parent, "depth": depth, "label": label,
            "description": description, "source": source, "evidence": []}


def wordgraph(entries: list[dict], topics: list[dict], order: list[int]) -> list[dict]:
    nodes: list[dict] = []
    for k in order:
        tp = topics[k]
        tid = tp["id"]
        times = "time" if tp["visits"] == 1 else "times"
        nodes.append(node(tid, tid, None, 0, tp["label"],
                          f"Discussed {tp['visits']} {times}, {mmss(tp['ms'] / 1000)} total", "topic"))
        label_words = set(tp["label"].lower().split())
        terms = [w for w in tp["top"] if w not in label_words][:TOPIC_TERMS]
        used = set(terms) | label_words
        texts = [entries[i].get("text") or "" for i in tp["idx"]]
        toks = [set(content(t)) for t in texts]
        for term in terms:
            tnid = f"{tid}/{slug(term)}"
            nodes.append(node(tnid, tid, tid, 1, term))
            kids: list[tuple[str, str]] = []
            bg = tp["bigrams"]
            for b in sorted((b for b in bg if term in b.split()), key=lambda b: (-bg[b], b)):
                if b not in used and len(kids) < MAX_CHILDREN:
                    kids.append((b, "meeting"))
                    used.add(b)
            if len(kids) < MAX_CHILDREN:
                co = Counter()
                for s in toks:
                    if term in s:
                        co.update(w for w in s if w != term)
                for w in sorted(co, key=lambda w: (-co[w] * tp["weight"].get(w, 0), w)):
                    if len(kids) >= MAX_CHILDREN:
                        break
                    if w not in used and co[w] >= 1:
                        kids.append((w, "related"))
                        used.add(w)
            for label, source in kids:
                nodes.append(node(f"{tnid}/{slug(label)}", tid, tnid, 2, label, "", source))
            if len(nodes) >= MAX_NODES:
                return nodes[:MAX_NODES]
    return nodes


def summaries(entries, chunks, of_chunk, topics, df, last_t) -> list[dict]:
    n = len(chunks)
    idf = {w: math.log(1 + n / c) for w, c in df.items()}
    freq = Counter(w for e in entries for w in content(e.get("text") or ""))
    out = []
    windows = max(1, math.ceil((last_t + 1e-9) / SUMMARY_SECONDS))
    for k in range(windows):
        lo, hi = k * SUMMARY_SECONDS, (k + 1) * SUMMARY_SECONDS
        cands = []
        for i, e in enumerate(entries):
            if lo <= float(e["t"]) < hi or (k == windows - 1 and float(e["t"]) >= hi):
                for j, s in enumerate(sentences(e.get("text") or "")):
                    ws = sorted(set(content(s)))  # sorted: float sums independent of hash order
                    if len(ws) < 2:
                        continue
                    score = sum(math.log(1 + freq[w]) * idf.get(w, 1.0) for w in ws) / math.sqrt(len(ws) + 1)
                    cands.append((score, i, j, e["who"], s))
        top = sorted(cands, key=lambda c: (-c[0], c[1], c[2]))[:3 if len(cands) >= 6 else 2]
        top.sort(key=lambda c: (c[1], c[2]))
        bullets = [f"{c[3]}: {c[4]}" for c in top]
        seen: list[str] = []
        for c, idx in enumerate(chunks):
            a, b = float(entries[idx[0]]["t"]), float(entries[idx[-1]]["t"])
            if b >= lo and a < hi and topics[of_chunk[c]]["id"] not in seen:
                seen.append(topics[of_chunk[c]]["id"])
        out.append({"from": int(lo), "to": int(hi), "text": " ".join(c[4] for c in top),
                    "bullets": bullets, "topics": seen})
    return out


def classify(sentence: str) -> str | None:
    s = sentence.strip()
    if len(words(s)) < 4 or NAV_RE.search(s) or DANGLING_RE.search(s):
        return None
    if AGREED_RE.search(s) and ACTION_RE.search(s) and not s.endswith("?") \
            and not re.search(r"\b(could|might|maybe)\b", s, re.I):
        return "agreed"
    if SUGGESTED_RE.search(s) and ACTION_RE.search(s):
        return "suggested"
    if AGREED_SOFT_RE.search(s) and ACTION_RE.search(s):
        return "agreed"
    if AGREED_RE.search(s) and ACTION_RE.search(s):
        return "suggested"   # "I'll maybe ..." / a question: an offer, not a commitment
    return None


def next_steps(entries, topics, topic_of_entry) -> list[dict]:
    out, seen = [], set()
    for i, e in enumerate(entries):
        for s in sentences(e.get("text") or ""):
            kind = classify(s)
            key = " ".join(words(s))
            if kind and key not in seen:
                seen.add(key)
                out.append({"id": f"ns-{len(out) + 1}", "kind": kind, "who": e["who"], "t": float(e["t"]),
                            "clock": e.get("clock") or "", "text": s,
                            "topicId": topics[topic_of_entry[i]]["id"]})
    return out


# ---------------------------------------------------------------- files

def insights_path(jl: Path) -> Path:
    return jl.with_name(jl.name[: -len(".jsonl")] + ".insights.json" if jl.name.endswith(".jsonl")
                        else jl.name + ".insights.json")


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


def build_file(jl: Path, out: Path | None = None, now: float | None = None) -> tuple[Path, dict]:
    meta, entries = live.read_entries(jl)
    stem = jl.name[: -len(".jsonl")] if jl.name.endswith(".jsonl") else jl.stem
    res = build(meta, entries, stem, now)
    out = out or insights_path(jl)
    write_json(out, res)
    return out, res


def follow(into: Path, poll: float = 1.0, once: bool = False, max_hours: float = 12.0,
           stop=lambda: False) -> int:
    deadline = time.monotonic() + max_hours * 3600
    seen: dict[Path, tuple[int, int]] = {}
    waiting = False
    while not stop() and time.monotonic() < deadline:
        found = live.sessions(into)
        if not found:
            if not waiting:
                log(f"no live session under {into / live.LIVE_DIR}; waiting")
                waiting = True
            time.sleep(poll)
            continue
        waiting = False
        jl = found[-1]
        try:
            st = jl.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if seen.get(jl) != sig:
                out, res = build_file(jl)
                seen[jl] = sig
                log(f"{out.name}: {res['lines']} line(s), {len(res['topics'])} topic(s), "
                    f"{len(res['nodes'])} node(s){', ended' if res['ended'] else ''}")
                if once and res["ended"]:
                    return 0
        except (OSError, ValueError) as e:  # a half-seen file: try again next poll
            log(f"{jl.name}: {e}")
        time.sleep(poll)
    return 0


# ---------------------------------------------------------------- ask

def _claude_client(model: str, timeout: int, binary: str = ""):
    """The Claude CLI client; tests replace this."""
    from claude_engine import ClaudeCLI
    return ClaudeCLI(model, binary=binary, timeout=timeout)


def stem(w: str) -> str:
    """Crude suffix strip for search only: dropped, drops, dropping -> drop."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            if len(w) > 3 and w[-1] == w[-2]:
                w = w[:-1]
            break
    return w


def search(entries: list[dict], question: str) -> list[dict]:
    q = sorted({stem(w) for w in content(question)})
    if not q:
        return []
    qb = {" ".join(stem(x) for x in b.split()) for b in bigrams(question)}
    df = Counter()
    toks = [{stem(w) for w in content(e.get("text") or "")} for e in entries]
    for s in toks:
        df.update(s)
    n = len(entries) or 1
    scored = []
    for i, e in enumerate(entries):
        hit = [w for w in q if w in toks[i]]
        if not hit:
            continue
        score = sum(math.log(1 + n / df[w]) for w in hit)
        score += 2.0 * len(qb & {" ".join(stem(x) for x in b.split()) for b in bigrams(e.get("text") or "")})
        if (e.get("text") or "").rstrip().endswith("?"):
            score *= 0.6   # a line that asks is a weaker answer than one that tells
        scored.append((score, i))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"t": entries[i]["t"], "clock": entries[i].get("clock") or "", "who": entries[i]["who"],
             "text": entries[i].get("text") or "", "score": round(sc, 3)} for sc, i in scored[:ASK_TOP]]


def lexical_answer(matches: list[dict]) -> str:
    if not matches:
        return "Nothing in the live transcript matches that."
    return " ".join(f"[{m['clock']}] {m['who']}: {m['text']}" for m in matches[:2])


ASK_SYSTEM = ("You answer questions about a call that is in progress, using only the transcript given. "
              "'me' is the user (their mic), 'them' is everyone on the other side. Answer in at most "
              "three sentences and cite the [HH:MM:SS] times you used. If the transcript does not say, "
              "say so.")


def ask(into: Path, question: str, ws: str | None = None, minutes: float | None = None) -> dict:
    found = live.sessions(into)
    if not found:
        return {"ok": False, "mode": "search", "answer": "", "matches": [], "session": None,
                "error": f"no live transcript under {into / live.LIVE_DIR}"}
    jl = found[-1]
    _meta, entries = live.read_entries(jl)
    entries.sort(key=lambda e: float(e["t"]))
    if minutes and entries:
        entries = [e for e in entries if float(e["t"]) >= float(entries[-1]["t"]) - minutes * 60]
    matches = search(entries, question)
    res = {"ok": True, "mode": "search", "answer": lexical_answer(matches), "matches": matches,
           "session": jl.name[: -len(".jsonl")]}
    summ: dict = {}
    if ws:  # never the env or cwd: only an explicit --ws can opt in to Claude
        import wsconfig
        summ = wsconfig.load_config(Path(ws).expanduser()).get("summarizer") or {}
    if str(summ.get("engine") or "") != "claude":
        return res
    try:
        client = _claude_client(str(summ.get("claude_model") or "opus"),
                                int(summ.get("claude_timeout") or 900), str(summ.get("claude_bin") or ""))
        transcript = "\n".join(live.fmt_line(e) for e in entries)
        reply = client.chat(ASK_SYSTEM, f"Transcript:\n{transcript}\n\nQuestion: {question}")
        res.update(mode="claude", answer=str(reply).strip())
    except Exception as e:  # noqa: BLE001  any failure: the lexical answer stands
        res["engine_error"] = str(e)
    return res


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid live insights")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="one .jsonl -> its .insights.json")
    b.add_argument("--jsonl", required=True)
    b.add_argument("--out")
    f = sub.add_parser("follow", help="keep the newest session's .insights.json current")
    f.add_argument("--into", default=str(live.DEFAULT_INTO))
    f.add_argument("--poll", type=float, default=1.0)
    f.add_argument("--once", action="store_true", help="exit after the ended session is written")
    f.add_argument("--max-hours", type=float, default=12.0)
    a = sub.add_parser("ask", help="answer a question from the newest live session")
    a.add_argument("--into", default=str(live.DEFAULT_INTO))
    a.add_argument("--question", "-q", required=True)
    a.add_argument("--ws", help="workspace whose whosaid.toml [summarizer] engine may be claude")
    a.add_argument("--minutes", type=float)
    a.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "build":
        jl = Path(args.jsonl).expanduser()
        if not jl.is_file():
            print(f"live insights: no such file {jl}", file=sys.stderr)
            return 1
        out, res = build_file(jl, Path(args.out).expanduser() if args.out else None)
        print(f"{out}: {len(res['topics'])} topic(s), {len(res['nodes'])} node(s), "
              f"{len(res['nextSteps'])} next step(s)")
        return 0
    if args.cmd == "follow":
        state = {"stop": False}

        def on_signal(_s, _f):
            state["stop"] = True
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        return follow(Path(args.into).expanduser(), args.poll, args.once, args.max_hours,
                      stop=lambda: state["stop"])
    res = ask(Path(args.into).expanduser(), args.question, args.ws, args.minutes)
    if args.json:
        print(json.dumps(res, indent=1))
    elif not res["ok"]:
        print(f"live ask: {res['error']}", file=sys.stderr)
    else:
        print(res["answer"])
        if res.get("engine_error"):
            print(f"(claude failed, search answer: {res['engine_error']})", file=sys.stderr)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

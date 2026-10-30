#!/usr/bin/env python3
"""
live_quality.py: score live insights (topic labels, summaries, next steps) so a
change to lib/live_insights.py can be checked before and after, on real calls,
without reading them by hand.

Sources: live sessions (--jsonl <stem>.jsonl, repeatable) and a sample of past
meetings from a workspace (--workspace DIR --sample N; their speaker turns become
live-style entries, "me" = the registry's self voice). Every source is rebuilt
with the current lib/live_insights.py, so run it on two checkouts to compare.

Prints one line per source and an aggregate; --out writes the numbers as JSON.
Real transcript text is only printed with --show (local eyeballing, never paste).
Lower is better for every rate; "score" is 100 minus the weighted problem rate.

--judge also asks Claude (`claude -p`, Sonnet by default: dev tooling on the
subscription) to grade every topic label 1-5 from that topic's own lines and to
offer a better one: a measure the regex checks above cannot give. It sends
transcript lines to Anthropic; --judge-out keeps the grades (real text, local).

Run: python3 test/live_quality.py --jsonl ~/Recordings/.live/X.jsonl --workspace ~/Desktop/transcripts --sample 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live_insights as li  # noqa: E402
import wsconfig  # noqa: E402

# a bullet that stops mid-thought: wider than the builder's own DANGLING_RE on purpose
DANGLING = re.compile(r"\b(the|a|an|to|and|of|with|for|but|or|that|in|on|at|my|our|your|so|if|then|because|"
                      r"which|is|are|was|were|be|this|these|those|like|just|about|from|as|i|we|you|they|"
                      r"it's|i'm|we're|you're|um|uh)\W*$|-\W*$", re.I)
TERMINAL = re.compile(r"[.!?]['\")\]]*\s*$")
FILLER_START = re.compile(r"^(so|yeah|okay|ok|um|uh|well|and|but|like|right|oh|alright|all right)\b[,.]?\s", re.I)
TITLE_APOS = re.compile(r"'[A-Z]")
CAP_WORD = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]+)\b")
WEIGHTS = {"label_fallback": 1.0, "label_apostrophe": 1.0, "label_name": 2.0, "label_apart": 1.0,
           "label_generic": 0.5, "bullet_dangling": 2.0, "bullet_long": 1.0, "bullet_lower": 1.0, "bullet_filler": 1.0,
           "bullet_short": 1.0, "bullet_dup": 1.0, "step_dangling": 2.0, "step_lower": 1.0, "step_long": 1.0}
LONG_WORDS = 35  # a bullet or step longer than this is a run-on, not a point
MAX_SOURCES = 200
JUDGE_TOPICS = 12        # the biggest topics per source the judge grades
JUDGE_LINES = 8          # lines shown per topic
JUDGE_TIMEOUT = 240
JUDGE_SYSTEM = (
    "You grade topic labels on a live meeting dashboard. Each topic comes with its label and lines "
    "from that part of the call. Grade each label 1-5: 5 = what a participant would call this topic; "
    "4 = right gist, a bit clunky; 3 = vague but not wrong; 2 = a side word, not the topic; "
    "1 = meaningless or misleading. Also give a better label of 2-4 words. Reply with JSON only, no "
    'prose: [{"n": 1, "score": 4, "better": "..."}, ...] with one object per topic.')


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} live quality: {msg}", file=sys.stderr, flush=True)


def load_jsonl(path: Path) -> tuple[dict, list[dict]]:
    meta, entries = {}, []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "meta" in row:
            meta = row["meta"]
        elif row.get("text") is not None and "t" in row:
            row.setdefault("who", "them")
            entries.append(row)
    return meta, entries


def self_names() -> set[str]:
    try:
        d = json.loads((Path.home() / ".config/whosaid/speakers.json").read_text())
        return {s["name"] for s in d.get("speakers", []) if s.get("role") == "self"}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def meeting_sources(ws: Path, n: int) -> list[tuple[str, dict, list[dict], set[str]]]:
    """An even sample of n non-chat meetings: (id, meta, entries, speaker names)."""
    man = {}
    try:
        man = {m["folder"]: m for m in json.loads((ws / "_workspace.json").read_text()).get("meetings", [])}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    me = self_names()
    folders = [(f, files) for f, files in wsconfig.iter_meetings(ws)
               if (man.get(f) or {}).get("kind") not in ("chat", "teams-chat", "channel")
               and not f.startswith("teams")]
    step = max(1, len(folders) // max(1, n))
    out = []
    for f, files in folders[::step][:n]:
        best = max(files, key=lambda p: (p.stat().st_size, p.name))
        turns = wsconfig.parse_turns(best.read_text(errors="replace"))
        entries = [{"t": float(t.t_sec), "who": "me" if t.speaker in me else "them", "text": t.text}
                   for t in turns if t.text.strip()]
        if len(entries) >= 20:
            out.append((f, {"ended": True}, entries, {t.speaker for t in turns}))
    return out


def name_words(speakers: set[str]) -> set[str]:
    out = set()
    for s in speakers:
        for w in re.findall(r"[A-Za-z][a-z']+", s):
            if len(w) > 2 and not w.lower().startswith("speaker"):
                out.add(w.lower())
    return out


def label_words(label: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z']*", label.lower()) if w != "s"]


def norm_set(text: str) -> set[str]:
    return set(li.content(text))


def score_source(ins: dict, entries: list[dict], names: set[str], df: dict[str, float]) -> dict:
    c = Counter()
    topics = ins.get("topics") or []
    texts = [set(li.words(e.get("text") or "")) for e in entries]
    for tp in topics:
        label = tp.get("label") or ""
        c["labels"] += 1
        ws = label_words(label)
        c["label_fallback"] += "·" in label
        c["label_apostrophe"] += bool(TITLE_APOS.search(label))
        c["label_name"] += any(w in names for w in ws)
        c["label_generic"] += any(df.get(w, 0) >= 0.6 for w in ws)
        if "·" in label and len(ws) >= 2:
            a, b = ws[0], ws[-1]
            c["label_apart"] += not any(a in t and b in t for t in texts)
    bullets = [b.split(": ", 1)[-1] for s in ins.get("summaries") or [] for b in s.get("bullets") or []]
    seen: list[set[str]] = []
    for b in bullets:
        c["bullets"] += 1
        c["bullet_dangling"] += bool(DANGLING.search(b)) or not TERMINAL.search(b)
        c["bullet_lower"] += b[:1].islower()
        c["bullet_filler"] += bool(FILLER_START.match(b))
        c["bullet_short"] += len(li.words(b)) < 6
        c["bullet_long"] += len(li.words(b)) > LONG_WORDS
        ws = norm_set(b)
        c["bullet_dup"] += any(ws and len(ws & o) / len(ws | o) >= 0.6 for o in seen)
        seen.append(ws)
    for st in ins.get("nextSteps") or []:
        s = st.get("text") or ""
        c["steps"] += 1
        c["step_dangling"] += bool(DANGLING.search(s)) or not TERMINAL.search(s)
        c["step_lower"] += s[:1].islower()
        c["step_long"] += len(li.words(s)) > LONG_WORDS
    return dict(c)


def rates(c: dict) -> dict:
    out = {}
    for k in WEIGHTS:
        den = c.get("labels" if k.startswith("label") else "bullets" if k.startswith("bullet") else "steps", 0)
        out[k] = round(c.get(k, 0) / den, 3) if den else 0.0
    wsum = sum(WEIGHTS.values())
    out["score"] = round(100 * (1 - sum(WEIGHTS[k] * out[k] for k in WEIGHTS) / wsum), 1)
    return out


def topic_samples(entries: list[dict]) -> list[dict]:
    """The builder's own topics with the lines that carry each one's top terms."""
    entries = sorted(entries, key=lambda e: float(e["t"]))
    chunks = li.chunk(entries)
    topics, _ = li.assign(entries, chunks)
    li.finish(entries, chunks, topics, li.durations(entries))
    out = []
    for tp in sorted(topics, key=lambda tp: -tp["ms"])[:JUDGE_TOPICS]:
        top = set(tp["top"][:li.TOPIC_TERMS])
        ranked = sorted(tp["idx"], key=lambda i: -len(top & set(li.content(entries[i].get("text") or ""))))
        lines = [f"{entries[i]['who']}: {(entries[i].get('text') or '')[:220]}" for i in sorted(ranked[:JUDGE_LINES])]
        out.append({"label": tp["label"], "terms": tp["top"][:li.TOPIC_TERMS], "lines": lines})
    return out


def judge_prompt(samples: list[dict]) -> str:
    parts = []
    for n, sm in enumerate(samples, 1):
        parts.append(f"Topic {n}. Label: {sm['label']}\n" + "\n".join(f"  {ln}" for ln in sm["lines"]))
    return "\n\n".join(parts)


def parse_grades(reply: str, k: int) -> list[dict]:
    m = re.search(r"\[.*\]", reply, re.S)
    rows = json.loads(m.group(0)) if m else []
    by_n = {int(r.get("n", 0)): r for r in rows if isinstance(r, dict)}
    out = []
    for n in range(1, k + 1):
        r = by_n.get(n) or {}
        try:
            sc = max(1, min(5, int(r.get("score"))))
        except (TypeError, ValueError):
            sc = None
        out.append({"score": sc, "better": str(r.get("better") or "")})
    return out


def judge(sources: list, model: str, workers: int) -> tuple[list[dict], int]:
    """Grade every source's labels -> ([{source, labels:[{label, score, better}]}], tokens)."""
    from concurrent.futures import ThreadPoolExecutor
    from claude_engine import ClaudeCLI, SummarizerError

    def one(n_src):
        n, (sid, meta, entries, _sp) = n_src
        samples = topic_samples(entries)
        if not samples:
            return {"source": n, "labels": []}, 0
        client = ClaudeCLI(model, timeout=JUDGE_TIMEOUT)
        try:
            grades = parse_grades(client.chat(JUDGE_SYSTEM, judge_prompt(samples)), len(samples))
        except (SummarizerError, ValueError) as e:
            log(f"judge [{n}]: {e}")
            return {"source": n, "labels": [], "error": str(e)[:200]}, client.tokens
        log(f"judge [{n}/{len(sources)}]: {len(samples)} labels, {client.tokens} tokens")
        return {"source": n, "labels": [{"label": sm["label"], **g} for sm, g in zip(samples, grades)]}, client.tokens

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, enumerate(sources, 1)))
    return [r for r, _ in res], sum(t for _, t in res)


def judge_summary(graded: list[dict]) -> dict:
    scores = [lb["score"] for g in graded for lb in g["labels"] if lb["score"]]
    if not scores:
        return {"judged": 0}
    return {"judged": len(scores), "judge_mean": round(sum(scores) / len(scores), 2),
            "judge_low": round(sum(s <= 2 for s in scores) / len(scores), 3),
            "judge_good": round(sum(s >= 4 for s in scores) / len(scores), 3)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--jsonl", action="append", default=[], help="a live session .jsonl (repeatable)")
    ap.add_argument("--workspace", help="a whosaid meeting workspace to sample past meetings from")
    ap.add_argument("--sample", type=int, default=30, help="meetings to sample from --workspace (default 30)")
    ap.add_argument("--out", help="write per-source and aggregate numbers here as JSON")
    ap.add_argument("--show", action="store_true", help="print labels and bullets (real text: local only)")
    ap.add_argument("--judge", action="store_true", help="grade labels with `claude -p` (sends lines to Anthropic)")
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--judge-workers", type=int, default=4)
    ap.add_argument("--judge-out", help="write each label, grade and suggestion here (real text: local only)")
    a = ap.parse_args(argv)

    sources = []
    for p in a.jsonl:
        meta, entries = load_jsonl(Path(p).expanduser())
        sources.append((Path(p).name.split(".")[0], meta, entries, set()))
    if a.workspace:
        sources += meeting_sources(Path(a.workspace).expanduser(), min(a.sample, MAX_SOURCES))
    if not sources:
        ap.error("give --jsonl and/or --workspace")

    # how many sources use a word: a label made of words every meeting says is generic
    df_count = Counter()
    for _, _, entries, _ in sources:
        df_count.update({w for e in entries for w in li.content(e.get("text") or "")})
    df = {w: n / len(sources) for w, n in df_count.items()} if len(sources) >= 5 else {}

    total, per = Counter(), []
    for n, (sid, meta, entries, speakers) in enumerate(sources, 1):
        t0 = time.time()
        ins = li.build(meta, entries, session=sid, now=0)
        c = score_source(ins, entries, name_words(speakers) | registry_words(), df)
        total.update(c)
        r = rates(c)
        per.append({"source": n, "lines": len(entries), **c, **r})
        log(f"[{n}/{len(sources)}] {len(entries)} lines, {c.get('labels', 0)} topics, "
            f"{c.get('bullets', 0)} bullets: score {r['score']} ({time.time() - t0:.1f}s)")
        if a.show:
            print(f"--- source {n} ({sid})")
            for tp in ins["topics"]:
                print(f"  topic  {tp['label']}  [{' '.join(tp['terms'][:5])}]")
            for s in ins["summaries"]:
                for b in s["bullets"]:
                    print(f"  bullet {b}")
            for st in ins["nextSteps"][:10]:
                print(f"  step   {st['kind']}: {st['text']}")
    agg = rates(total)
    if a.judge:
        t0 = time.time()
        graded, tokens = judge(sources, a.judge_model, a.judge_workers)
        agg.update(judge_summary(graded), judge_tokens=tokens)
        log(f"judge: {tokens} tokens in {time.time() - t0:.0f}s")
        if a.judge_out:
            Path(a.judge_out).write_text(json.dumps(graded, indent=1))
    print(json.dumps({"sources": len(sources), **{k: total[k] for k in ("labels", "bullets", "steps")}, **agg}))
    if a.out:
        Path(a.out).write_text(json.dumps({"aggregate": {**dict(total), **agg}, "sources": per}, indent=1))
    return 0


def registry_words() -> set[str]:
    try:
        d = json.loads((Path.home() / ".config/whosaid/speakers.json").read_text())
        return name_words({s.get("name", "") for s in d.get("speakers", [])})
    except (OSError, ValueError, TypeError):
        return set()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
live_ai.py: the opt-in AI layer over live insights. Off unless the workspace says

  [live]
  ai = "claude"            # anything else (or no [live]) = off
  ai_model = "sonnet"      # claude -p --model
  ai_every = 180           # transcript seconds between calls
  ai_max_calls = 40        # per session
  ai_timeout = 90          # wall clock per call (one retry)
  ai_max_tokens = 1500     # output tokens per call

and `whosaid live insights follow` is given that workspace (--ws). It is a second
opt-in on purpose: `[summarizer] engine = "claude"` sends a finished meeting once;
this sends the call in progress every few minutes. CLOUD: each call sends the
newest transcript lines and the local topics to Anthropic through `claude -p`.

A worker thread (never the follow loop: a call takes seconds) asks Claude for
topic labels, a short recap and next steps, and writes <stem>.ai.json (tmp +
os.replace) with the transcript time it saw, its call count and its tokens, so a
restarted follower waits for new talk instead of calling again. A failed call is
recorded and waits `ai_every` too. build() stays local and deterministic: the
sidecar is overlaid when the insights file is written (overlay()), and every
overlaid topic keeps its `localLabel`. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402

AI_VERSION = 1
MIN_LINES = 12           # a call this short has nothing to label yet
PROMPT_CHARS = 24000     # newest transcript lines sent per call (about 6k tokens)
TOPICS_SENT = 12
LINES_PER_TOPIC = 4
DEFAULTS = {"ai": "", "ai_model": "sonnet", "ai_every": 180, "ai_max_calls": 40,
            "ai_timeout": 90, "ai_max_tokens": 1500}
SYSTEM = (
    "You keep a live dashboard for a meeting that is in progress. 'me' is the user (their mic); "
    "'them' is everyone on the other side. You get the topic buckets found so far (id, current "
    "label, key terms, a few lines) and the newest part of the transcript, each line as "
    "[seconds] who: text. Reply with JSON only, no prose:\n"
    '{"labels": {"<topic id>": "<2-4 word label>"}, "recap": ["<bullet>"], '
    '"nextSteps": [{"kind": "agreed" or "suggested", "who": "me" or "them", "text": "<one sentence>", '
    '"t": <seconds of the line it comes from>}]}\n'
    "Labels name what the bucket is about, never a person and never a word said in passing; label "
    "every id you are given. Recap: at most 5 short bullets, the most important first, decisions "
    "and open questions over chatter. Next steps: only actions someone took on or proposed, at most "
    "8, each a plain sentence. Use only what the transcript says.")


def log(msg: str) -> None:
    print(f"whosaid live ai: {msg}", file=sys.stderr, flush=True)


def config(ws: str | Path | None) -> dict | None:
    """The [live] AI settings when the workspace opted in, else None."""
    if not ws:
        return None
    import wsconfig
    raw = wsconfig.load_config(Path(ws).expanduser()).get("live") or {}
    if str(raw.get("ai") or "").strip().lower() != "claude":
        return None
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and k != "ai":
            cfg[k] = type(DEFAULTS[k])(raw[k])
    cfg["ai"] = "claude"
    summ = wsconfig.load_config(Path(ws).expanduser()).get("summarizer") or {}
    cfg["claude_bin"] = str(raw.get("claude_bin") or summ.get("claude_bin") or "")
    return cfg


def ai_path(jl: Path) -> Path:
    name = jl.name[: -len(".jsonl")] if jl.name.endswith(".jsonl") else jl.stem
    return jl.with_name(f"{name}.ai.json")


def read(jl: Path) -> dict:
    try:
        d = json.loads(ai_path(jl).read_text())
        return d if isinstance(d, dict) and d.get("version") == AI_VERSION else {}
    except (OSError, ValueError):
        return {}


def write(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def prompt(insights: dict, entries: list[dict]) -> str:
    """The topics (with a few of their lines) and the newest transcript lines."""
    entries = sorted(entries, key=lambda e: float(e["t"]))
    parts = ["Topics so far:"]
    for tp in insights.get("topics", [])[:TOPICS_SENT]:
        lo, hi = float(tp.get("firstT") or 0), float(tp.get("lastT") or 0)
        terms = set(tp.get("terms") or [])
        inside = [e for e in entries if lo <= float(e["t"]) <= hi]
        inside.sort(key=lambda e: -len(terms & set(re.findall(r"[a-z']+", (e.get("text") or "").lower()))))
        lines = sorted(inside[:LINES_PER_TOPIC], key=lambda e: float(e["t"]))
        parts.append(f"- id {tp['id']} | label: {tp.get('label', '')} | terms: {', '.join(tp.get('terms') or [])}")
        parts += [f"    [{int(float(e['t']))}] {e['who']}: {(e.get('text') or '')[:200]}" for e in lines]
    tail, size = [], 0
    for e in reversed(entries):
        line = f"[{int(float(e['t']))}] {e['who']}: {e.get('text') or ''}"
        size += len(line) + 1
        if size > PROMPT_CHARS:
            break
        tail.append(line)
    parts += ["", "Transcript (newest part):", *reversed(tail)]
    return "\n".join(parts)


def parse(reply: str, topic_ids: set[str]) -> dict:
    m = re.search(r"\{.*\}", reply, re.S)
    d = json.loads(m.group(0)) if m else {}
    labels = {str(k): " ".join(str(v).split())[:60] for k, v in (d.get("labels") or {}).items()
              if str(k) in topic_ids and str(v).strip()}
    recap = [" ".join(str(b).split()) for b in (d.get("recap") or []) if str(b).strip()][:5]
    steps = []
    for s in (d.get("nextSteps") or [])[:8]:
        if not isinstance(s, dict) or not str(s.get("text") or "").strip():
            continue
        try:
            t = float(s.get("t") or 0)
        except (TypeError, ValueError):
            t = 0.0
        steps.append({"kind": "agreed" if s.get("kind") == "agreed" else "suggested",
                      "who": "me" if s.get("who") == "me" else "them",
                      "text": " ".join(str(s["text"]).split()), "t": t})
    return {"labels": labels, "recap": recap, "nextSteps": steps}


def _client(cfg: dict):
    """The Claude CLI client; tests replace this."""
    from claude_engine import ClaudeCLI
    return ClaudeCLI(cfg["ai_model"], binary=cfg.get("claude_bin", ""), timeout=int(cfg["ai_timeout"]),
                     max_tokens=int(cfg["ai_max_tokens"]))


def due(state: dict, meta: dict, entries: list[dict], cfg: dict) -> bool:
    if len(entries) < MIN_LINES or int(state.get("calls") or 0) >= int(cfg["ai_max_calls"]):
        return False
    last_t = max(float(e["t"]) for e in entries)
    seen = float(state.get("t") or -1e9)
    if meta.get("ended"):
        return last_t > seen and not state.get("final")  # one last pass over the whole call
    return last_t - seen >= float(cfg["ai_every"])


def step(jl: Path, cfg: dict, now: float | None = None) -> dict | None:
    """One AI pass for this session when it is due -> the sidecar written, else None."""
    import live_insights as li
    meta, entries = live.read_entries(jl)
    state = read(jl)
    if not due(state, meta, entries, cfg):
        return None
    ins = li.build(meta, entries, jl.name[: -len(".jsonl")])
    last_t = ins["t"]
    out = {"version": AI_VERSION, "session": ins["session"], "model": cfg["ai_model"], "t": last_t,
           "final": bool(meta.get("ended")), "calls": int(state.get("calls") or 0) + 1,
           "tokens": int(state.get("tokens") or 0), "updated": time.time() if now is None else now,
           "labels": state.get("labels") or {}, "recap": state.get("recap") or [],
           "nextSteps": state.get("nextSteps") or [], "error": ""}
    client = None
    try:
        client = _client(cfg)
        reply = client.chat(SYSTEM, prompt(ins, entries))
        got = parse(reply, {tp["id"] for tp in ins["topics"]})
        out["labels"] = {**out["labels"], **got["labels"]}
        out["recap"], out["nextSteps"] = got["recap"] or out["recap"], got["nextSteps"] or out["nextSteps"]
        out["model"] = getattr(client, "resolved_model", "") or cfg["ai_model"]
    except Exception as e:  # noqa: BLE001  any failure: keep the last good answer, wait ai_every
        out["error"] = str(e)[:300]
        log(f"{jl.name}: {out['error']}")
    out["tokens"] += int(getattr(client, "tokens", 0) or 0)
    write(ai_path(jl), out)
    return out


class Worker(threading.Thread):
    """Runs step() on the newest session every `poll` seconds until stop() is true."""

    def __init__(self, into: Path, cfg: dict, stop, poll: float = 2.0):
        super().__init__(daemon=True, name="whosaid-live-ai")
        self.into, self.cfg, self.stop, self.poll = into, cfg, stop, poll

    def run(self) -> None:
        while not self.stop():
            found = live.sessions(self.into)
            if found:
                try:
                    res = step(found[-1], self.cfg)
                    if res is not None:
                        log(f"{found[-1].name}: call {res['calls']}, {res['tokens']} tokens so far"
                            + (f", failed: {res['error']}" if res["error"] else ""))
                except (OSError, ValueError) as e:  # a half-written transcript: next poll
                    log(f"{found[-1].name}: {e}")
            time.sleep(self.poll)


def overlay(res: dict, ai: dict) -> dict:
    """Put the sidecar's labels, recap and next steps over a local build (in place)."""
    if not ai:
        return res
    labels = ai.get("labels") or {}
    for tp in res.get("topics", []):
        if tp["id"] in labels:
            tp["localLabel"], tp["label"], tp["labelSource"] = tp["label"], labels[tp["id"]], "ai"
    for nd in res.get("nodes", []):
        if nd.get("depth") == 0 and nd.get("id") in labels:
            nd["label"] = labels[nd["id"]]
    if ai.get("nextSteps"):
        res["localNextSteps"] = res.get("nextSteps", [])
        res["nextSteps"] = [{"id": f"ai-{n}", "source": "ai", **s} for n, s in enumerate(ai["nextSteps"], 1)]
    res["ai"] = {"model": ai.get("model", ""), "t": ai.get("t", 0), "calls": ai.get("calls", 0),
                 "tokens": ai.get("tokens", 0), "recap": ai.get("recap") or [], "error": ai.get("error", "")}
    return res

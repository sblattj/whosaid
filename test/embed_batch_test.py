#!/usr/bin/env python3
"""
Unit test for TextMatcher.prime() batching: roll-up primed every text in one
/api/embed call, which timed out on a large workspace and silently dropped dedupe
to difflib. prime() must send at most EMBED_BATCH texts per call, keep vectors in
order, and still fall back to difflib when any batch fails.

Uses a fake _post_embed (no Ollama, no network).

Run:
    python3 test/embed_batch_test.py
"""

import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import workspace as ws  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


texts = [f"item number {i}" for i in range(ws.EMBED_BATCH * 2 + 7)]

m = ws.TextMatcher(url="http://127.0.0.1:11434", model="fake", mode="embed")
calls = []
m._post_embed = lambda batch: (calls.append(len(batch)), [[float(len(t)), 1.0] for t in batch])[1]
m.prime(texts)
check("splits into ceil(n/EMBED_BATCH) calls", calls == [ws.EMBED_BATCH, ws.EMBED_BATCH, 7])
check("no call exceeds EMBED_BATCH", max(calls) <= ws.EMBED_BATCH)
check("stays in embed mode", m.mode == "embed")
check("every text has its own vector",
      all(m._vectors[ws.normalize_text(t)] == ws.unit_vector([float(len(t)), 1.0]) for t in texts))

m.prime(texts)
check("cached texts are not re-sent", len(calls) == 3)

m2 = ws.TextMatcher(url="http://127.0.0.1:11434", model="fake", mode="embed")
n = {"i": 0}


def flaky(batch):
    n["i"] += 1
    if n["i"] == 2:
        raise TimeoutError("timed out")
    return [[1.0, 0.0] for _ in batch]


m2._post_embed = flaky
m2.prime(texts)
check("a failed batch falls back to difflib", m2.mode == "difflib")

sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""
live_replay.py: replay a live-session fixture as if `whosaid live` were writing it.

Writes the fixture's entries into <into>/.live/<stem>.jsonl a few lines at a
time with the same tmp + os.replace rewrite lib/live.py uses (meta.ended false
until the last write, meta.updated = now), printing `write n=<lines> wall=<epoch>`
per write. For tests and the macOS app's fixture mode only: refuses any --into
outside the temp folders unless its path holds "whosaid-app-fixture".

Run: python3 test/live_replay.py --fixture test/fixtures/live/call.jsonl --into "$(mktemp -d)"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402

MAX_WRITES = 5000


def allowed(into: Path) -> bool:
    p = into.expanduser().resolve()
    if "whosaid-app-fixture" in str(p):
        return True
    for root in (tempfile.gettempdir(), "/private/var/folders", "/tmp"):
        r = Path(root).resolve()
        if p == r or r in p.parents:
            return True
    return False


def write(jl: Path, meta: dict, entries: list[dict]) -> None:
    tmp = jl.with_suffix(".jsonl.tmp")
    tmp.write_text(json.dumps({"meta": meta}) + "\n" + "".join(json.dumps(e) + "\n" for e in entries))
    os.replace(tmp, jl)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="live_replay.py")
    p.add_argument("--fixture", required=True)
    p.add_argument("--into", required=True)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--lines-per-write", type=int, default=2)
    a = p.parse_args(argv)
    into = Path(a.into)
    if not allowed(into):
        print(f"live_replay: refusing {into}: not under a temp folder or *whosaid-app-fixture*", file=sys.stderr)
        return 2
    fx = Path(a.fixture)
    meta, entries = live.read_entries(fx)
    step = max(1, a.lines_per_write)
    jl = into.expanduser() / live.LIVE_DIR / fx.name
    jl.parent.mkdir(parents=True, exist_ok=True)
    started = float(meta.get("started") or time.time())
    n, writes = 0, 0
    while writes < MAX_WRITES:
        n = min(len(entries), n + step)
        done = n >= len(entries)
        write(jl, {"started": started, "ended": done, "updated": time.time()}, entries[:n])
        writes += 1
        print(f"write n={n} wall={time.time():.3f}", flush=True)
        if done:
            return 0
        time.sleep(max(0.0, a.interval))
    return 1


if __name__ == "__main__":
    sys.exit(main())

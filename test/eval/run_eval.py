#!/usr/bin/env python3
"""
run_eval.py: offline eval harness for whosaid's action-item summarizer
(lib/action_items.py). docs/eval.md is the full write-up.

Drafts every synthetic fixture under test/eval/fixtures/ with one model backend,
scores the drafts against each fixture's gold.json (test/eval/score.py), and
can record every model reply to a cassette so the run replays later with no
network and no model at all.

Backends:
  ollama      the production client (lib's own Ollama class), local only
  claude-cli  a REFERENCE model through the Claude Code CLI (`claude -p`), used
              only to put the local model's number in context. Dev tooling:
              it lives here in test/eval/, never in lib/, and it refuses any
              fixtures directory other than the committed synthetic one unless
              --allow-external-fixtures is given.

Usage:
  python3 test/eval/run_eval.py --backend ollama --model qwen2.5:14b [--record]
  python3 test/eval/run_eval.py --backend claude-cli --model opus --record --jobs 4
  python3 test/eval/run_eval.py --replay ollama-qwen2.5-14b
  python3 test/eval/run_eval.py --report

  --fixtures a,b        only these fixture slugs
  --fixtures-dir DIR    fixtures root (default test/eval/fixtures)
  --out-dir DIR         root holding cassettes/ and results/ (default test/eval)
  --ollama URL          Ollama base URL (default http://127.0.0.1:11434)
  --jobs N              fixtures drafted concurrently (threads; default 1)
  -v                    print every bullet's verdict and every missed item

Exit codes: 0 ok, 1 replay mismatch (or a cassette miss), 2 usage or run error.
Stdlib only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_DIR = EVAL_DIR.parent.parent
for _p in (REPO_DIR / "lib", EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import action_items as ai  # noqa: E402
import score as sc  # noqa: E402
import wsconfig  # noqa: E402

SCHEMA = 1
DEFAULT_FIXTURES = EVAL_DIR / "fixtures"
DEFAULT_OUT = EVAL_DIR
DEFAULT_OLLAMA = str(wsconfig.DEFAULTS["search"]["ollama"])
DEFAULT_MODELS = {"ollama": ai.DEFAULT_MODEL, "claude-cli": "opus"}
TRANSCRIPT_NAME = "transcript.speakers.txt"
GOLD_NAME = "gold.json"

# config env overrides lib/wsconfig.py honors; scrubbed so a caller's shell
# cannot silently rewrite a fixture's owner, model or URL
CONFIG_ENV_OVERRIDES = ("WHOSAID_OWNER", "WHOSAID_OLLAMA", "WHOSAID_SUMMARIZER_MODEL")

CLAUDE_BIN_ENV = "WHOSAID_EVAL_CLAUDE_BIN"
CLAUDE_TIMEOUT = 300           # seconds per `claude -p` call
# no tools, no MCP servers, no user/project/local settings (so no hooks, no
# CLAUDE.md), no skills, nothing written to the session store, JSON output
CLAUDE_ISOLATION = ("--tools", "", "--strict-mcp-config", "--setting-sources", "",
                    "--disable-slash-commands", "--no-session-persistence",
                    "--output-format", "json")
# these outrank the subscription OAuth token and would bill the API instead
CLAUDE_SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CLAUDE_AUTH_HINT = ("log in with `claude` interactively, or mint a subscription token with "
                    "`claude setup-token` and export it as CLAUDE_CODE_OAUTH_TOKEN")

_env_lock = threading.Lock()


# ---- keys, slugs, fixtures -------------------------------------------------------------

def call_key(system: str, user: str, temperature: float = 0.0) -> str:
    """Cassette key: sha256 of json.dumps([system, user, temperature]). The
    temperature is coerced to float so 0 and 0.0 are the same call."""
    raw = json.dumps([system, user, float(temperature)], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_slug(backend: str, model: str) -> str:
    return re.sub(r"[^a-z0-9.-]", "-", f"{backend}-{model}".lower())


def list_fixtures(fixtures_dir: Path, only: list[str] | None = None) -> list[str]:
    """Fixture slugs (dirs holding a transcript and a gold.json), sorted.
    Raises ValueError for a requested slug that is not there."""
    fixtures_dir = Path(fixtures_dir)
    found = sorted(p.name for p in fixtures_dir.iterdir()
                   if p.is_dir() and (p / TRANSCRIPT_NAME).is_file() and (p / GOLD_NAME).is_file()
                   ) if fixtures_dir.is_dir() else []
    if only:
        missing = [s for s in only if s not in found]
        if missing:
            raise ValueError(f"no such fixture(s) in {fixtures_dir}: {', '.join(missing)}")
        return [s for s in found if s in only]
    return found


def fixture_files(fixtures_dir: Path, slugs: list[str]) -> list[tuple[str, Path]]:
    """(relative posix path, path) for every regular file in the given fixtures,
    dotfiles and __pycache__ skipped, sorted by relative path."""
    out = []
    for slug in slugs:
        for p in (Path(fixtures_dir) / slug).rglob("*"):
            rel = p.relative_to(fixtures_dir)
            if p.is_file() and not any(part.startswith(".") or part == "__pycache__"
                                       for part in rel.parts):
                out.append((rel.as_posix(), p))
    return sorted(out)


def fixtures_sha256(fixtures_dir: Path, slugs: list[str]) -> str:
    """sha256 over the fixtures IN THE RUN: for each file sorted by relative
    path, `<relpath>\\0<size>\\0<bytes>`. Adding a new fixture does not
    invalidate runs that did not include it."""
    h = hashlib.sha256()
    for rel, p in fixture_files(fixtures_dir, slugs):
        data = p.read_bytes()
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def fixture_config(fixture_dir: Path) -> dict:
    """wsconfig.load_config(fixture_dir) with the WHOSAID_* env overrides removed
    for the duration, so the fixture's whosaid.toml is the only input."""
    with _env_lock:
        saved = {k: os.environ.pop(k) for k in CONFIG_ENV_OVERRIDES if k in os.environ}
        try:
            return wsconfig.load_config(Path(fixture_dir))
        finally:
            os.environ.update(saved)


# ---- clients ---------------------------------------------------------------------------

class CassetteMiss(RuntimeError):
    """A replay asked for a call the cassette does not hold."""


class Recorder:
    """Wraps a client and keeps every reply by call_key. A repeated identical
    call is answered from the recording, so a replay sees exactly what the
    recorded run saw even when the model is not deterministic."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.model = inner.model
        self.calls = 0
        self.cassette: dict[str, str] = {}

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls += 1
        key = call_key(system, user, temperature)
        if key not in self.cassette:
            self.cassette[key] = self.inner.chat(system, user, temperature=temperature)
        return self.cassette[key]


class ReplayClient:
    """Answers only from a cassette; a missing key raises CassetteMiss and never
    falls through to a live call."""

    def __init__(self, calls: dict[str, str], model: str, name: str = "") -> None:
        self.recorded = dict(calls)
        self.model = model
        self.name = name
        self.calls = 0

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls += 1
        key = call_key(system, user, temperature)
        if key not in self.recorded:
            raise CassetteMiss(f"cassette {self.name or '?'} has no reply for call #{self.calls} "
                               f"(key {key[:12]}...): the prompts changed since it was recorded; "
                               f"re-record")
        return self.recorded[key]


class _Retry(Exception):
    pass


class ClaudeCLI:
    """Reference backend: one `claude -p` process per .chat call, prompt on
    stdin, isolated from the caller's settings, tools, MCP servers and CLAUDE.md,
    run in a fresh empty temp dir, with API-key env vars removed so the
    subscription login is what pays. The CLI has no temperature flag, so
    `temperature` is ignored. A failed call is retried once; `is_error` in the
    result raises at once (the CLI already retries API errors itself)."""

    def __init__(self, model: str = "opus", *, binary: str | None = None,
                 timeout: int = CLAUDE_TIMEOUT) -> None:
        self.model = model
        self.binary = binary or os.environ.get(CLAUDE_BIN_ENV) or shutil.which("claude") or ""
        if not self.binary:
            raise ai.SummarizerError("the Claude Code CLI (`claude`) is not on PATH",
                                     f"install it, or set {CLAUDE_BIN_ENV} to its path")
        self.timeout = timeout
        self.calls = 0
        self.resolved_model = ""      # what the alias resolved to, from the init event

    def argv(self, system: str) -> list[str]:
        return [self.binary, "-p", "--system-prompt", system, "--model", self.model,
                *CLAUDE_ISOLATION]

    @staticmethod
    def child_env() -> dict[str, str]:
        env = dict(os.environ)
        for k in CLAUDE_SCRUBBED_ENV:
            env.pop(k, None)
        return env

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        del temperature                  # no such flag on the CLI
        self.calls += 1
        why = ""
        for _attempt in (1, 2):
            try:
                return self._once(system, user)
            except _Retry as e:
                why = str(e)
        raise ai.SummarizerError(f"`claude -p` failed twice: {why}",
                                 f"check `claude -p hi` by hand; {CLAUDE_AUTH_HINT}")

    def _once(self, system: str, user: str) -> str:
        cwd = tempfile.mkdtemp(prefix="whosaid-eval-claude-")
        try:
            proc = subprocess.run(self.argv(system), input=user, capture_output=True, text=True,
                                  cwd=cwd, env=self.child_env(), timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise _Retry(f"no reply within {self.timeout}s") from e
        except OSError as e:
            raise ai.SummarizerError(f"cannot run {self.binary}: {e}",
                                     f"set {CLAUDE_BIN_ENV} to the claude binary") from e
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        try:
            events = json.loads(proc.stdout)
        except ValueError:
            raise _Retry(f"exit {proc.returncode}, stdout is not JSON: "
                         f"{(proc.stdout or proc.stderr).strip()[:300]!r}") from None
        if isinstance(events, dict):
            events = [events]
        if not isinstance(events, list):
            raise _Retry(f"exit {proc.returncode}, unexpected JSON: {str(events)[:300]}")
        for ev in events:
            if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("model"):
                self.resolved_model = str(ev["model"])
        result = next((ev for ev in reversed(events)
                       if isinstance(ev, dict) and ev.get("type") == "result"), None)
        if result is None:
            raise _Retry(f"exit {proc.returncode}, no result event: {proc.stderr.strip()[:300]!r}")
        if result.get("is_error"):
            raise ai.SummarizerError(f"`claude -p` returned an error: "
                                     f"{str(result.get('result') or result.get('subtype'))[:300]}",
                                     CLAUDE_AUTH_HINT)
        if proc.returncode != 0:
            raise _Retry(f"exit {proc.returncode}: {proc.stderr.strip()[:300]!r}")
        return str(result.get("result") or "").strip()


def client_factory(backend: str, model: str, ollama: str = DEFAULT_OLLAMA):
    """-> make(cfg) that builds one fresh client per fixture (draft reads
    client.calls, so clients are never shared between fixtures or threads)."""
    if backend == "ollama":
        def make(cfg: dict):
            plan = ai.Plan(cfg, model=model, ollama=ollama)
            return ai.Ollama(plan.url, plan.model, plan.num_ctx, plan.timeout,
                             num_predict=plan.num_predict, think=plan.think)
        return make
    if backend == "claude-cli":
        return lambda cfg: ClaudeCLI(model)
    raise ValueError(f"unknown backend {backend!r}")


# ---- running ---------------------------------------------------------------------------

def draft_fixture(fixtures_dir: Path, slug: str, cfg: dict, client, model: str) -> dict:
    fdir = Path(fixtures_dir) / slug
    text = (fdir / TRANSCRIPT_NAME).read_text()
    gold = json.loads((fdir / GOLD_NAME).read_text())
    t0 = time.monotonic()
    markdown, stats = ai.draft(text, meeting=slug, cfg=cfg, model=model, client=client)
    wall = time.monotonic() - t0
    result = sc.score(markdown, gold, model_calls=stats["model_calls"])
    return {"slug": slug, "markdown": markdown, "stats": stats, "score": result,
            "wall": round(wall, 3), "client": client}


def run_fixtures(fixtures_dir: Path, slugs: list[str], make_client, model: str, *,
                 jobs: int = 1, wrap=None, progress=None) -> dict[str, dict]:
    """Draft and score each fixture with a fresh client (wrapped by `wrap` when
    given). Exceptions propagate; with jobs > 1 the first one wins after the
    running fixtures finish."""
    cfgs = {s: fixture_config(Path(fixtures_dir) / s) for s in slugs}

    def one(slug: str) -> dict:
        client = make_client(cfgs[slug])
        if wrap:
            client = wrap(slug, client)
        res = draft_fixture(fixtures_dir, slug, cfgs[slug], client, model)
        if progress:
            progress(res)
        return res

    if jobs <= 1:
        return {s: one(s) for s in slugs}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {s: pool.submit(one, s) for s in slugs}
        return {s: futs[s].result() for s in slugs}


def results_doc(run: str, backend: str, model: str, recorded: str, sha: str,
                runs: dict[str, dict]) -> dict:
    per = {s: sc.public(r["score"]) for s, r in sorted(runs.items())}
    return {"schema": SCHEMA, "run": run, "backend": backend, "model": model,
            "recorded": recorded, "fixtures_sha256": sha, "per_fixture": per,
            "total": sc.aggregate(list(per.values())),
            "wall_seconds": {s: r["wall"] for s, r in sorted(runs.items())}}


def paths(out_dir: Path, run: str) -> dict[str, Path]:
    out_dir = Path(out_dir)
    return {"cassettes": out_dir / "cassettes" / run, "results": out_dir / "results" / f"{run}.json",
            "drafts": out_dir / "results" / run}


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def record(fixtures_dir: Path, slugs: list[str], backend: str, model: str, make_client,
           out_dir: Path, *, jobs: int = 1, progress=None) -> tuple[dict, dict[str, dict]]:
    """Live run through Recorders; writes the cassettes, the results json and
    the drafts. Nothing is written unless every fixture succeeds."""
    run = run_slug(backend, model)
    sha = fixtures_sha256(fixtures_dir, slugs)
    runs = run_fixtures(fixtures_dir, slugs, make_client, model, jobs=jobs,
                        wrap=lambda slug, c: Recorder(c), progress=progress)
    today = date.today().isoformat()
    doc = results_doc(run, backend, model, today, sha, runs)
    p = paths(out_dir, run)
    for d in (p["cassettes"], p["drafts"]):
        if d.is_dir():
            shutil.rmtree(d)            # a re-record replaces the run wholesale
    for slug, r in runs.items():
        rec: Recorder = r["client"]
        cassette = {"schema": SCHEMA, "backend": backend, "model": model, "recorded": today}
        resolved = getattr(rec.inner, "resolved_model", "")
        if resolved:
            cassette["resolved_model"] = resolved
        cassette["calls"] = rec.cassette
        write_json(p["cassettes"] / f"{slug}.json", cassette)
        p["drafts"].mkdir(parents=True, exist_ok=True)
        md = r["markdown"]
        if backend != "ollama":
            # lib stamps every draft "(local Ollama, offline)"; say which backend made this one
            md = md.replace("(local Ollama, offline)", f"({backend} reference backend, eval only)")
        (p["drafts"] / f"{slug}.md").write_text(md)
    write_json(p["results"], doc)
    return doc, runs


def compare(expected, got, path: str = "") -> list[str]:
    """Differences between two results docs, ignoring wall_seconds and
    recorded; floats are compared after rounding to 4 decimal places."""
    diffs: list[str] = []
    if isinstance(expected, dict) and isinstance(got, dict):
        for k in sorted(set(expected) | set(got)):
            if not path and k in ("wall_seconds", "recorded"):
                continue
            sub = f"{path}.{k}" if path else k
            if k not in got:
                diffs.append(f"{sub}: missing from the replay (recorded {expected[k]!r})")
            elif k not in expected:
                diffs.append(f"{sub}: not in the recorded results (replay {got[k]!r})")
            else:
                diffs += compare(expected[k], got[k], sub)
        return diffs
    if isinstance(expected, float) or isinstance(got, float):
        try:
            if round(float(expected), sc.DP) == round(float(got), sc.DP):
                return []
        except (TypeError, ValueError):
            pass
    elif expected == got:
        return []
    return [f"{path}: recorded {expected!r}, replay {got!r}"]


def replay(run: str, out_dir: Path = DEFAULT_OUT, fixtures_dir: Path = DEFAULT_FIXTURES, *,
           jobs: int = 1) -> tuple[int, list[str], dict | None]:
    """Re-draft every fixture of a recorded run from its cassettes and compare
    with the committed results. Writes nothing. -> (rc, messages, replayed doc)
    with rc 0 match, 1 mismatch or cassette miss, 2 cannot compare."""
    p = paths(out_dir, run)
    if not p["results"].is_file():
        return 2, [f"no recorded results at {p['results']}"], None
    expected = json.loads(p["results"].read_text())
    slugs = sorted(expected.get("per_fixture") or {})
    have = set(list_fixtures(fixtures_dir))
    gone = [s for s in slugs if s not in have]
    if gone:
        return 2, [f"fixtures changed since this run was recorded; re-record "
                   f"(missing from {fixtures_dir}: {', '.join(gone)})"], None
    sha = fixtures_sha256(fixtures_dir, slugs)
    if sha != expected.get("fixtures_sha256"):
        return 2, ["fixtures changed since this run was recorded; re-record "
                   f"(fixtures_sha256 recorded {str(expected.get('fixtures_sha256'))[:12]}..., "
                   f"now {sha[:12]}...)"], None
    cassettes = {}
    for s in slugs:
        cp = p["cassettes"] / f"{s}.json"
        if not cp.is_file():
            return 2, [f"no cassette for fixture {s} at {cp}"], None
        cassettes[s] = json.loads(cp.read_text())
    model = str(expected.get("model", ""))
    try:
        runs = run_fixtures(
            fixtures_dir, slugs, lambda cfg: None, model, jobs=jobs,
            wrap=lambda slug, _c: ReplayClient(cassettes[slug].get("calls") or {},
                                               str(cassettes[slug].get("model", model)),
                                               name=f"{run}/{slug}"))
    except CassetteMiss as e:
        return 1, [f"MISMATCH {run}: {e}"], None
    got = results_doc(run, str(expected.get("backend", "")), model, "", sha, runs)
    diffs = compare(expected, got)
    if diffs:
        return 1, [f"MISMATCH {run}: {len(diffs)} field(s) differ"] + [f"  {d}" for d in diffs], got
    return 0, [f"MATCH {run}: {len(slugs)} fixture(s) reproduce the recorded scores"], got


# ---- reporting -------------------------------------------------------------------------

def fmt(x: float) -> str:
    return f"{x:.3f}"


def score_table(doc: dict) -> str:
    rows = ["| fixture | TP | FP | FN | precision | recall | F1 | flag rate | inferred | calls "
            "| FP tags | wall s |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    walls = doc.get("wall_seconds") or {}
    for name, r in list((doc.get("per_fixture") or {}).items()) + [("**total**", doc["total"])]:
        tags = ", ".join(f"{k} {v}" for k, v in r["fp_tags"].items() if v) or "-"
        wall = walls.get(name) if name in walls else sum(walls.values())
        rows.append(f"| {name} | {r['tp']} | {r['fp']} | {r['fn']} | {fmt(r['precision'])} | "
                    f"{fmt(r['recall'])} | {fmt(r['f1'])} | {fmt(r['flag_rate'])} | "
                    f"{r['inferred']} | {r['model_calls']} | {tags} | {wall:.1f} |")
    return "\n".join(rows)


def report(out_dir: Path = DEFAULT_OUT) -> str:
    docs = []
    for jp in sorted((Path(out_dir) / "results").glob("*.json")):
        try:
            d = json.loads(jp.read_text())
            d["total"], d["per_fixture"]
        except (ValueError, KeyError, TypeError) as e:
            wsconfig.log(f"WARN skipping {jp.name}: {e}")
            continue
        docs.append(d)
    if not docs:
        return f"No recorded runs under {Path(out_dir) / 'results'}.\n"
    out = ["| run | model | precision | recall | F1 | flag rate | model calls | wall s |",
           "|---|---|---|---|---|---|---|---|"]
    for d in docs:
        t = d["total"]
        out.append(f"| {d['run']} | {d['model']} | {fmt(t['precision'])} | {fmt(t['recall'])} | "
                   f"{fmt(t['f1'])} | {fmt(t['flag_rate'])} | {t['model_calls']} | "
                   f"{sum((d.get('wall_seconds') or {}).values()):.1f} |")
    fixtures = sorted({f for d in docs for f in d["per_fixture"]})
    out += ["", "Per-fixture F1:", "",
            "| fixture | " + " | ".join(d["run"] for d in docs) + " |",
            "|---|" + "---|" * len(docs)]
    for f in fixtures:
        cells = [fmt(d["per_fixture"][f]["f1"]) if f in d["per_fixture"] else "-" for d in docs]
        out.append(f"| {f} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def print_detail(runs: dict[str, dict]) -> None:
    for slug, r in sorted(runs.items()):
        print(f"\n### {slug}")
        for b in r["score"]["detail"]["bullets"]:
            verdict = b["result"].upper() + (f" ({b['tag']})" if b.get("tag") else "") \
                + (f" {b['gold']}" if b.get("gold") else "")
            print(f"- {verdict} [{b['t']}] {b['body'][:140]}")
        for m in r["score"]["detail"]["missed"]:
            print(f"- FN {m['id']} [{m['t']}] {m['desc']}")


# ---- CLI -------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_eval.py",
        description="Offline eval of whosaid's action-item summarizer against synthetic "
                    "fixtures, with record/replay. See docs/eval.md.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--replay", metavar="RUN", help="re-score a recorded run from its cassettes")
    mode.add_argument("--report", action="store_true", help="compare every recorded run")
    p.add_argument("--backend", choices=sorted(DEFAULT_MODELS), help="model backend for a live run")
    p.add_argument("--model", help="model name (default: qwen2.5:14b for ollama, opus for claude-cli)")
    p.add_argument("--ollama", default=DEFAULT_OLLAMA, help="Ollama base URL")
    p.add_argument("--record", action="store_true",
                   help="save cassettes, results json and drafts for this live run")
    p.add_argument("--fixtures", help="comma-separated fixture slugs (default: all)")
    p.add_argument("--fixtures-dir", default=str(DEFAULT_FIXTURES), help="fixtures root")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT),
                   help="root holding cassettes/ and results/")
    p.add_argument("--jobs", type=int, default=1, help="fixtures drafted concurrently (threads)")
    p.add_argument("--allow-external-fixtures", action="store_true",
                   help="let --backend claude-cli read a fixtures dir other than the committed "
                        "synthetic one (it sends those transcripts to a cloud model)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every bullet's verdict and every missed gold item")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.version_info < (3, 11):
        print("run_eval.py needs Python 3.11+ (tomllib), or every fixture's whosaid.toml is "
              "silently ignored", file=sys.stderr)
        return 2
    fixtures_dir = Path(args.fixtures_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    jobs = max(1, args.jobs)

    if args.report:
        sys.stdout.write(report(out_dir))
        return 0
    if args.replay:
        rc, msgs, _doc = replay(args.replay, out_dir, fixtures_dir, jobs=jobs)
        print("\n".join(msgs), file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if not args.backend:
        build_parser().error("give --backend for a live run, or --replay RUN, or --report")

    if (args.backend == "claude-cli" and fixtures_dir != DEFAULT_FIXTURES.resolve()
            and not args.allow_external_fixtures):
        print(f"refusing: --backend claude-cli sends transcripts to a cloud model and only reads "
              f"the committed synthetic fixtures ({DEFAULT_FIXTURES.relative_to(REPO_DIR)}); "
              f"pass --allow-external-fixtures only for other SYNTHETIC fixtures", file=sys.stderr)
        return 2
    model = args.model or DEFAULT_MODELS[args.backend]
    only = [s.strip() for s in args.fixtures.split(",") if s.strip()] if args.fixtures else None
    try:
        slugs = list_fixtures(fixtures_dir, only)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not slugs:
        print(f"no fixtures under {fixtures_dir}", file=sys.stderr)
        return 2
    run = run_slug(args.backend, model)
    make = client_factory(args.backend, model, args.ollama)

    def progress(r: dict) -> None:
        s = r["score"]
        wsconfig.log(f"eval {run} {r['slug']}: {r['wall']:.1f}s, {s['model_calls']} calls, "
                     f"tp {s['tp']} fp {s['fp']} fn {s['fn']}, f1 {s['f1']:.3f}")

    try:
        if args.record:
            doc, runs = record(fixtures_dir, slugs, args.backend, model, make, out_dir,
                               jobs=jobs, progress=progress)
        else:
            runs = run_fixtures(fixtures_dir, slugs, make, model, jobs=jobs, progress=progress)
            doc = results_doc(run, args.backend, model, date.today().isoformat(),
                              fixtures_sha256(fixtures_dir, slugs), runs)
    except ai.SummarizerError as e:
        print(f"{run}: {e}" + (f" ({e.hint})" if e.hint else ""), file=sys.stderr)
        return 2
    print(f"## {run}\n\n{score_table(doc)}")
    if args.verbose:
        print_detail(runs)
    if not args.record:
        return 0
    p = paths(out_dir, run)
    print(f"\nrecorded -> {p['results']}, {p['cassettes']}/, {p['drafts']}/")
    rc, msgs, _ = replay(run, out_dir, fixtures_dir, jobs=jobs)
    print("\n".join(msgs))
    return rc


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)

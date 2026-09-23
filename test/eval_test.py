#!/usr/bin/env python3
"""
Offline test for the action-item eval harness (test/eval/run_eval.py and
test/eval/score.py, see docs/eval.md). Needs no network, no Ollama and no
Claude: every model is a fake.

Covers: the scorer on hand-built drafts (TP, every FP tag, FN, optional gold
items, [inferred] exclusion, flag counting, t normalization, the zero-division
rules, micro aggregation) and its parity with lib's own render_bullet; the
client injection in lib/action_items.py (draft/prepare use the given client and
never construct Ollama); record -> replay on a tiny inline fixture in a temp
dir (identical scores, a strict cassette miss, a tampered result, a changed
fixture, replay writing nothing); the WHOSAID_* env scrub around load_config;
the claude-cli backend against a fake `claude` executable (API-key env vars
removed, isolation flags, prompt on stdin, temp cwd, is_error, one retry) and
its refusal of non-committed fixtures; and a replay of every committed run
under test/eval/results/ whose cassettes exist (zero is fine).

Run:
    python3 test/eval_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_DIR / "test" / "eval"
sys.path.insert(0, str(REPO_DIR / "lib"))
sys.path.insert(0, str(EVAL_DIR))

import action_items as ai  # noqa: E402
import run_eval as rv  # noqa: E402
import score as sc  # noqa: E402
import wsconfig  # noqa: E402

RUN_EVAL = EVAL_DIR / "run_eval.py"
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


# ---- the tiny inline fixture ---------------------------------------------------------------

TINY_TRANSCRIPT = """# Speakers (3): Alice_Example, Bob_Example, Carol_Example
[00:00:05] Bob_Example: Morning everyone. Alice, can you send the vendor report to me by Thursday so I can review it before the board sync?
[00:00:20] Alice_Example: Yes, I will send the vendor report on Wednesday afternoon and include the cost breakdown you asked for.
[00:00:40] Carol_Example: Alice, could you refresh the dashboard data source before the demo on Friday please?
[00:01:00] Bob_Example: Carol, please book the conference room for the offsite next month when you get a chance.
[00:01:20] Carol_Example: Sure, and I will order lunch for the whole team on Friday, nobody else needs to do anything.
[00:01:40] Alice_Example: One more from me: I will update the runbook with the new alert thresholds once the load test finishes.
"""

TINY_TOML = """[workspace]
owner = "Alice_Example"
aliases = ["Alice"]

[groups]
leadership = ["Bob_Example"]
team = ["Carol_Example"]
"""

TINY_GOLD = {
    "schema": 1, "fixture": "tiny", "description": "Inline fixture for the harness self-test.",
    "owner": "Alice_Example",
    "items": [
        {"id": "G1", "t": "00:00:05", "speaker": "Bob_Example", "kind": "ask",
         "keywords": ["vendor report"], "desc": "Send Bob the vendor report by Thursday.",
         "optional": False},
        {"id": "G2", "t": "00:00:20", "speaker": "Alice_Example", "kind": "commit",
         "keywords": ["vendor report"], "desc": "Send the vendor report Wednesday.", "optional": False},
        {"id": "G3", "t": "00:00:20", "speaker": "Alice_Example", "kind": "commit",
         "keywords": ["cost breakdown"], "desc": "Include the cost breakdown.", "optional": True},
        {"id": "G4", "t": "00:00:40", "speaker": "Carol_Example", "kind": "ask",
         "keywords": ["dashboard", "data source"], "desc": "Refresh the dashboard data source.",
         "optional": False},
        {"id": "G5", "t": "00:01:40", "speaker": "Alice_Example", "kind": "commit",
         "keywords": ["runbook"], "desc": "Update the runbook after the load test.", "optional": False},
    ],
    "distractors": [
        {"t": "00:01:00", "why": "Bob asks Carol, not Alice."},
        {"t": "00:01:20", "why": "Carol's own commitment."},
    ],
}


def write_tiny_fixture(dest: Path) -> Path:
    """Write the tiny fixture to `dest` (a fixture dir). Also used by hand for
    the live smoke run, always into a temp dir."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "transcript.speakers.txt").write_text(TINY_TRANSCRIPT)
    (dest / "whosaid.toml").write_text(TINY_TOML)
    (dest / "gold.json").write_text(json.dumps(TINY_GOLD, indent=2) + "\n")
    return dest


def turn_of(user: str) -> str:
    """The TURN part of a BULLETS prompt (context turns excluded)."""
    return user.split("TURN [", 1)[1] if "TURN [" in user else ""


def fake_reply(system: str, user: str) -> str:
    if system.startswith("You are given part of a diarized meeting transcript"):   # SELECT
        ids = [ln.split()[0] for ln in user.splitlines() if "book the conference room" in ln]
        return "\n".join(f"{i}: book the room" for i in ids) or "NONE"
    if system.startswith("You write action items"):                                 # BULLETS
        turn = turn_of(user)
        if "send the vendor report to me" in turn:
            return ('- **Send the vendor report to Bob.** Bob reviews it before the board sync. '
                    '"send the vendor report to me by Thursday"')
        if "send the vendor report on Wednesday" in turn:
            return ('- **Send the vendor report on Wednesday.** Alice commits to it. '
                    '"I will send the vendor report on Wednesday afternoon"\n'
                    '- **Include the cost breakdown.** Bob asked for it. '
                    '"include the cost breakdown you asked for"')
        if "dashboard data source" in turn:
            return ('- **Refresh the dashboard data source.** Needed before the demo. '
                    '"refresh the numbers before the demo"')              # not verbatim -> flagged
        if "book the conference room" in turn:
            return "- **Book the conference room.** For the offsite."        # no quote -> flagged
        return "SKIP"
    if "implied next steps" in system:                                               # INFER
        return "- (inferred) Confirm the board sync agenda."
    return "NONE"


class FakeClient:
    def __init__(self, model: str = "fake-model") -> None:
        self.model = model
        self.calls = 0
        self.log: list[tuple[str, str, float]] = []

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls += 1
        self.log.append((system, user, temperature))
        return fake_reply(system, user)


class NoOllama:
    def __init__(self, *a, **k) -> None:
        raise AssertionError("the real Ollama client must not be constructed")


@contextlib.contextmanager
def patched_ollama(cls):
    saved = ai.Ollama
    ai.Ollama = cls
    try:
        yield
    finally:
        ai.Ollama = saved


@contextlib.contextmanager
def env(**kv):
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_cli(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(extra_env or {})
    return subprocess.run([sys.executable, str(RUN_EVAL), *args], capture_output=True, text=True,
                          env=e, timeout=120)


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


# ---- 1. the scorer ------------------------------------------------------------------------

UNIT_GOLD = {
    "schema": 1, "fixture": "unit", "owner": "Alice_Example",
    "items": [
        {"id": "G1", "t": "0:00:05", "keywords": ["vendor report"], "optional": False},
        {"id": "G2", "t": "00:00:20", "keywords": ["vendor report"], "optional": False},
        {"id": "G3", "t": "00:00:20", "keywords": ["cost breakdown"], "optional": True},
        {"id": "G4", "t": "00:00:40", "keywords": ["dashboard", "data source"], "optional": False},
        {"id": "G5", "t": "00:02:00", "keywords": ["budget"], "optional": False},
    ],
    "distractors": [{"t": "00:01:00", "why": "Bob asks Carol"}],
}

UNIT_MD = """# Action items — unit

_Auto-drafted 2026-01-01T09:00-08:00 by `fake` (local Ollama, offline): a DRAFT._

## 1. Asks from leadership (Bob)
- **Alice_Example** [Bob 00:00:05] Send the vendor report to Bob. Bob needs it. "send the vendor report to me by Thursday"
- **Alice_Example** [Bob 00:00:05] Email the Vendor Report! again. Same thing. _(⚠ no quote)_
- **Alice_Example** [Bob 00:01:00] Book the conference room. For the offsite. "book the conference room"
- **Alice_Example** [Bob 00:03:00] Plan the offsite. Nobody asked. "plan the offsite"

## 2. Asks from team (Carol)
- **Alice_Example** [Carol 00:00:40] Order lunch for the team. Friday. "order lunch"
- **Alice_Example** [Carol 00:00:40] Refresh the dashboard data source. Before the demo. "refresh it" _(⚠ quote not verbatim from that turn)_

## 3. Alice's own commitments
- **Alice_Example** [Alice 00:00:20] Include the cost breakdown. Asked for. "include the cost breakdown"
- **Alice_Example** [Alice 00:00:20] Send the vendor report Wednesday. Committed. "send the vendor report"
- **Alice_Example** [Alice 00:00:20] Recheck the numbers. Also. "recheck"

## 4. Inferred next steps
- **Alice_Example** [inferred] Confirm the board sync agenda.
- **Alice_Example** [inferred] Follow up on the vendor report.

<details><summary>Evidence turns the draft was built from (verbatim, 3)</summary>

- [00:00:05] Bob_Example: Alice, can you send the vendor report to me by Thursday?
- [00:00:20] Alice_Example: I will send the vendor report and include the cost breakdown.

</details>
"""


def test_scorer() -> None:
    parsed = sc.parse_draft(UNIT_MD)
    check(len(parsed["bullets"]) == 9, f"9 scored bullets, got {len(parsed['bullets'])}")
    check(parsed["inferred"] == 2, "2 inferred bullets counted separately")
    check(all(b["label"] == "Alice_Example" for b in parsed["bullets"]), "bold label parsed")
    check(parsed["bullets"][0]["who"] == "Bob" and parsed["bullets"][0]["t"] == "00:00:05",
          "requester and time parsed")

    r = sc.score(UNIT_MD, UNIT_GOLD, model_calls=7)
    verdicts = [(b["t"], b["result"], b.get("tag") or b.get("gold")) for b in r["detail"]["bullets"]]
    check(verdicts == [
        ("00:00:05", "tp", "G1"),              # gold t "0:00:05" normalized
        ("00:00:05", "fp", "duplicate"),       # keyword hit, but G1 is taken
        ("00:01:00", "fp", "distractor"),
        ("00:03:00", "fp", "unlabeled"),
        ("00:00:40", "fp", "wrong-item"),      # gold at t, no keyword hit
        ("00:00:40", "tp", "G4"),
        ("00:00:20", "optional", "G3"),        # matched optional: neither TP nor FP
        ("00:00:20", "tp", "G2"),
        ("00:00:20", "fp", "duplicate"),       # no keyword hit, every gold item at t taken
    ], f"per-bullet verdicts: {verdicts}")
    check((r["tp"], r["fp"], r["fn"]) == (3, 5, 1), f"tp/fp/fn 3/5/1, got {r['tp']}/{r['fp']}/{r['fn']}")
    check(r["fp_tags"] == {"distractor": 1, "duplicate": 2, "wrong-item": 1, "unlabeled": 1},
          f"fp tags {r['fp_tags']}")
    check([m["id"] for m in r["detail"]["missed"]] == ["G5"], "G5 is the one false negative")
    check((r["bullets"], r["flagged"], r["inferred"], r["model_calls"]) == (9, 2, 2, 7),
          "bullets / flagged / inferred / model_calls")
    check(r["precision"] == 0.375 and r["recall"] == 0.75 and r["f1"] == 0.5,
          f"ratios {r['precision']} {r['recall']} {r['f1']}")
    check(r["flag_rate"] == 0.2222, f"flag rate rounded to 4 dp: {r['flag_rate']}")
    check(set(sc.public(r)) == set(sc.COUNT_KEYS) | set(sc.RATIO_KEYS) | {"fp_tags"},
          "public() drops the detail")

    # non-optional first, then optional, then duplicate
    g = {"items": [{"id": "O1", "t": "00:00:10", "keywords": ["report"], "optional": True},
                   {"id": "N1", "t": "00:00:10", "keywords": ["report"], "optional": False}]}
    md = "\n".join(f"- **A** [Bob 00:00:10] File the report {k}." for k in range(3))
    r = sc.score(md, g)
    check([(b["result"], b.get("gold") or b.get("tag")) for b in r["detail"]["bullets"]]
          == [("tp", "N1"), ("optional", "O1"), ("fp", "duplicate")],
          "non-optional candidate is assigned before an optional one")
    check((r["tp"], r["fp"], r["fn"]) == (1, 1, 0), "one TP, one duplicate FP, optional ignored")

    # a keyword hit on a taken item while another item at the same t is still open
    g = {"items": [{"id": "A", "t": "00:00:10", "keywords": ["alpha"], "optional": False},
                   {"id": "B", "t": "00:00:10", "keywords": ["beta"], "optional": False}]}
    md = ("- **A** [Bob 00:00:10] Do alpha.\n- **A** [Bob 00:00:10] Do alpha again.\n"
          "- **A** [Bob 00:00:10] Do gamma.")
    r = sc.score(md, g)
    check([b.get("tag") for b in r["detail"]["bullets"]] == [None, "duplicate", "wrong-item"],
          "taken keyword match -> duplicate; open item but no keyword -> wrong-item")
    check(r["fn"] == 1, "B is missed")

    # unmatched optional gold is not a false negative
    g = {"items": [{"id": "O", "t": "00:00:10", "keywords": ["x"], "optional": True}]}
    r = sc.score("# nothing\n", g)
    check((r["tp"], r["fp"], r["fn"]) == (0, 0, 0), "optional-only gold, no bullets: nothing counts")
    check((r["precision"], r["recall"], r["f1"]) == (1.0, 1.0, 1.0),
          "no scorable gold and no bullets: precision/recall/f1 1.0")

    # zero-division rules
    r = sc.score("", {"items": []})
    check((r["precision"], r["recall"], r["f1"], r["flag_rate"]) == (1.0, 1.0, 1.0, 0.0),
          "no gold, no bullets: 1/1/1, flag rate 0")
    r = sc.score("", {"items": [{"id": "G", "t": "00:00:01", "keywords": ["x"]}]})
    check((r["precision"], r["recall"], r["f1"]) == (0.0, 0.0, 0.0), "gold but no bullets: 0/0/0")
    r = sc.score("- **A** [Bob 00:00:01] Do x. _(⚠ no quote)_", {"items": []})
    check((r["precision"], r["recall"], r["f1"], r["flag_rate"]) == (0.0, 1.0, 0.0, 1.0),
          "bullets but no gold: precision 0, recall 1, f1 0")
    check(r["fp_tags"]["unlabeled"] == 1, "no gold at all: unlabeled")
    check(sc.ratios(0, 0, 0, 0, 0) == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "flag_rate": 0.0},
          "ratios() on all zeros")

    # micro aggregation: sums first, ratios from the sums (not an average of ratios)
    a = sc.score("- **A** [B 00:00:01] Do x.", {"items": [{"id": "1", "t": "00:00:01", "keywords": ["x"]}]},
                 model_calls=2)
    b = sc.score("- **A** [B 00:00:01] Do y.\n- **A** [B 00:00:02] Do z. _(⚠ no quote)_\n"
                 "- **A** [B 00:00:03] Do w.\n- **A** [B 00:00:04] Do y.",
                 {"items": [{"id": "1", "t": "00:00:01", "keywords": ["y"]},
                            {"id": "2", "t": "00:00:09", "keywords": ["q"]}]}, model_calls=5)
    tot = sc.aggregate([sc.public(a), sc.public(b)])
    check((tot["tp"], tot["fp"], tot["fn"], tot["bullets"], tot["flagged"], tot["model_calls"])
          == (2, 3, 1, 5, 1, 7), f"summed counts {tot}")
    check((tot["precision"], tot["recall"], tot["f1"], tot["flag_rate"]) == (0.4, 0.6667, 0.5, 0.2),
          f"micro ratios {tot['precision']} {tot['recall']} {tot['f1']} {tot['flag_rate']}")
    macro_p = (a["precision"] + b["precision"]) / 2
    check(tot["precision"] != round(macro_p, 4), "micro precision differs from the macro mean here")
    check(tot["fp_tags"] == {"distractor": 0, "duplicate": 0, "wrong-item": 0, "unlabeled": 3},
          f"fp tags summed {tot['fp_tags']}")
    check(sc.aggregate([])["f1"] == 1.0 and sc.aggregate([])["tp"] == 0, "aggregate of nothing")

    # parity with lib's own renderer
    plan = ai.Plan({"workspace": {"owner": "Alice_Example"}, "groups": {}, "summarizer": {},
                    "search": {}})
    turn = wsconfig.Turn(t_sec=3725, t_str="1:02:05", speaker="Bob_Example", text="x", line=1)
    lines = [ai.render_bullet(plan, turn, ai.Bullet(0, "Send it", 'Ctx. "send it now please"', "ask",
                                                     True, True)),
             ai.render_bullet(plan, turn, ai.Bullet(0, "Send it again", "Ctx.", "ask", False, False)),
             ai.render_bullet(plan, turn, ai.Bullet(0, "Send", 'C. "wrong words here"', "ask",
                                                     True, False)),
             ai.render_inferred(plan, "Confirm it.")]
    parsed = sc.parse_draft("\n".join(lines))
    check([(b["t"], b["who"], b["flagged"]) for b in parsed["bullets"]]
          == [("01:02:05", "Bob", False), ("01:02:05", "Bob", True), ("01:02:05", "Bob", True)],
          f"render_bullet output parses: {parsed}")
    check(parsed["inferred"] == 1, "render_inferred output counts as inferred")
    noowner = ai.Plan({"workspace": {}, "groups": {}, "summarizer": {}, "search": {}})
    check(sc.parse_draft(ai.render_inferred(noowner, "Do it."))["inferred"] == 1,
          "owner-less [inferred] bullet counts as inferred")
    check(sc.norm("The Vendor-Report,  NOW!") == ai.norm("The Vendor-Report,  NOW!"),
          "score.norm matches lib norm")


# ---- 2. the client injection ----------------------------------------------------------------

def test_injection(tmp: Path) -> None:
    fdir = write_tiny_fixture(tmp / "inject" / "tiny")
    cfg = rv.fixture_config(fdir)
    fake = FakeClient()
    with patched_ollama(NoOllama):
        md, stats = ai.draft(TINY_TRANSCRIPT, "tiny", cfg, model="fake-model", client=fake)
        plan, _turns, _speakers, got = ai.prepare(TINY_TRANSCRIPT, cfg, client=fake)
    check(got is fake, "prepare returns the injected client")
    check(fake.calls > 0 and stats["model_calls"] == fake.calls == len(fake.log),
          f"every model call went to the fake ({fake.calls} calls, stats {stats['model_calls']})")
    check(stats["items"] == 5 and "[Bob 00:00:05] Send the vendor report to Bob." in md,
          f"draft built from the fake's replies ({stats['items']} items)")
    check(any(t == 0.2 for _s, _u, t in fake.log), "the INFER call passes temperature=0.2")

    built = []

    class StubOllama:
        def __init__(self, url, model, num_ctx, timeout=0, num_predict=None):
            built.append((url, model, num_ctx, timeout, num_predict))
            self.model, self.calls = model, 0

    with patched_ollama(StubOllama):
        _p, _t, _s, default = ai.prepare(TINY_TRANSCRIPT, cfg, model="m1")
    check(isinstance(default, StubOllama)
          and built == [("http://127.0.0.1:11434", "m1", 32768, 900, 2048)],
          f"without client=, prepare still builds the Ollama client, num_predict included ({built})")


# ---- 3. record -> replay ----------------------------------------------------------------------

def test_record_replay(tmp: Path) -> None:
    fixtures = tmp / "rr" / "fixtures"
    out = tmp / "rr" / "out"
    write_tiny_fixture(fixtures / "tiny")
    run = rv.run_slug("fake", "fake-model")
    check(run == "fake-fake-model", "run slug")
    check(rv.run_slug("ollama", "qwen2.5:14b") == "ollama-qwen2.5-14b", "slug keeps dots")
    check(rv.run_slug("claude-cli", "Opus[1m]") == "claude-cli-opus-1m-", "slug replaces the rest")
    check(rv.call_key("s", "u", 0) == rv.call_key("s", "u", 0.0) != rv.call_key("s", "u", 0.2),
          "call key: 0 == 0.0, temperature matters")

    with patched_ollama(NoOllama):
        doc, runs = rv.record(fixtures, ["tiny"], "fake", "fake-model", lambda cfg: FakeClient(), out)
    p = rv.paths(out, run)
    check(p["results"].is_file() and (p["cassettes"] / "tiny.json").is_file()
          and (p["drafts"] / "tiny.md").is_file(), "record writes results, cassette and draft")
    saved = json.loads(p["results"].read_text())
    check(saved == json.loads(json.dumps(doc)), "results file is the returned doc")
    check(set(saved) == {"schema", "run", "backend", "model", "recorded", "fixtures_sha256",
                         "per_fixture", "total", "wall_seconds"}, f"results keys {sorted(saved)}")
    t = saved["per_fixture"]["tiny"]
    check(set(t) == {"tp", "fp", "fn", "bullets", "flagged", "inferred", "precision", "recall", "f1",
                     "flag_rate", "model_calls", "fp_tags"}, f"per-fixture keys {sorted(t)}")
    check((t["tp"], t["fp"], t["fn"], t["bullets"], t["flagged"], t["inferred"])
          == (3, 1, 1, 5, 2, 1), f"tiny fixture scores {t}")
    check(t["fp_tags"]["distractor"] == 1, "the room booking is a distractor FP")
    check(saved["total"]["tp"] == 3 and saved["total"]["f1"] == t["f1"], "total of one fixture")
    cas = json.loads((p["cassettes"] / "tiny.json").read_text())
    check({"schema", "backend", "model", "recorded", "calls"} <= set(cas)
          and cas["backend"] == "fake" and len(cas["calls"]) == t["model_calls"],
          f"cassette holds one reply per call ({len(cas['calls'])} vs {t['model_calls']})")
    check(all(len(k) == 64 for k in cas["calls"]), "cassette keys are sha256 hex")
    saved_md = (p["drafts"] / "tiny.md").read_text()
    check(saved_md == runs["tiny"]["markdown"].replace(
        "(local Ollama, offline)", "(fake reference backend, eval only)"), "draft markdown saved")
    check("(local Ollama, offline)" in runs["tiny"]["markdown"]
          and "(local Ollama, offline)" not in saved_md
          and "(fake reference backend, eval only)" in saved_md,
          "a non-ollama draft is not labeled local Ollama")

    before = snapshot(tmp / "rr")
    with patched_ollama(NoOllama):
        rc, msgs, got = rv.replay(run, out, fixtures)
    check(rc == 0 and msgs[0].startswith("MATCH"), f"replay matches: {msgs}")
    check(got["per_fixture"] == saved["per_fixture"] and got["total"] == saved["total"],
          "replay reproduces identical scores")
    check(snapshot(tmp / "rr") == before, "replay writes nothing")

    cp = run_cli("--replay", run, "--out-dir", str(out), "--fixtures-dir", str(fixtures))
    check(cp.returncode == 0 and "MATCH" in cp.stdout, f"CLI replay exit 0: {cp.stdout}{cp.stderr}")
    cp = run_cli("--report", "--out-dir", str(out))
    check(cp.returncode == 0 and f"| {run} | fake-model |" in cp.stdout and "Per-fixture F1" in cp.stdout
          and "| tiny |" in cp.stdout, f"CLI report: {cp.stdout}{cp.stderr}")

    # a cassette with one key removed: the replay client raises, never falls through
    calls = dict(cas["calls"])
    calls.pop(next(iter(calls)))
    try:
        with patched_ollama(NoOllama):
            ai.draft(TINY_TRANSCRIPT, "tiny", rv.fixture_config(fixtures / "tiny"),
                     model="fake-model", client=rv.ReplayClient(calls, "fake-model", "t"))
        raised = False
    except rv.CassetteMiss:
        raised = True
    check(raised, "a missing cassette key raises CassetteMiss")
    (p["cassettes"] / "tiny.json").write_text(json.dumps(dict(cas, calls=calls)))
    rc, msgs, _ = rv.replay(run, out, fixtures)
    check(rc == 1 and "MISMATCH" in msgs[0] and "re-record" in msgs[0], f"cassette miss -> rc 1: {msgs}")
    (p["cassettes"] / "tiny.json").write_text(json.dumps(cas))

    # a tampered result: exit 1 with a diff naming the field
    (p["results"]).write_text(json.dumps(dict(saved, per_fixture={"tiny": dict(t, tp=99)})))
    cp = run_cli("--replay", run, "--out-dir", str(out), "--fixtures-dir", str(fixtures))
    check(cp.returncode == 1 and "per_fixture.tiny.tp: recorded 99, replay 3" in cp.stderr,
          f"tampered result -> exit 1 with a diff: {cp.stderr}")
    # floats within 4 dp and the ignored fields do not count
    fuzz = dict(saved, recorded="1999-01-01", wall_seconds={"tiny": 12345.0},
                per_fixture={"tiny": dict(t, f1=t["f1"] + 0.00001)})
    check(rv.compare(saved, fuzz) == [], "wall_seconds / recorded ignored, floats at 4 dp")
    p["results"].write_text(json.dumps(saved))

    # a changed fixture: refuse
    gold = fixtures / "tiny" / "gold.json"
    orig = gold.read_text()
    gold.write_text(orig.replace("Send Bob the vendor report", "Send Bob the vendor reports"))
    cp = run_cli("--replay", run, "--out-dir", str(out), "--fixtures-dir", str(fixtures))
    check(cp.returncode == 2 and "fixtures changed since this run was recorded; re-record" in cp.stderr,
          f"changed fixture -> refused: {cp.stderr}")
    gold.write_text(orig)
    # a NEW fixture that the run did not include does not invalidate it
    write_tiny_fixture(fixtures / "tiny2")
    check(rv.fixtures_sha256(fixtures, ["tiny"]) == saved["fixtures_sha256"],
          "the sha covers only the fixtures in the run")
    (fixtures / "tiny" / ".DS_Store").write_text("x")
    check(rv.fixtures_sha256(fixtures, ["tiny"]) == saved["fixtures_sha256"], "dotfiles ignored")
    rc, _msgs, _ = rv.replay(run, out, fixtures)
    check(rc == 0, "replay still matches with an extra fixture present")
    check(rv.list_fixtures(fixtures) == ["tiny", "tiny2"] and rv.list_fixtures(fixtures, ["tiny2"])
          == ["tiny2"], "fixture listing and filter")
    try:
        rv.list_fixtures(fixtures, ["nope"])
        bad = False
    except ValueError:
        bad = True
    check(bad, "an unknown --fixtures slug is an error")

    # --jobs: threads give the same scores, and every fixture gets its own client
    both = ["tiny", "tiny2"]
    with patched_ollama(NoOllama):
        seq = rv.run_fixtures(fixtures, both, lambda cfg: FakeClient(), "fake-model")
        par = rv.run_fixtures(fixtures, both, lambda cfg: FakeClient(), "fake-model", jobs=2)
    check({s: sc.public(r["score"]) for s, r in seq.items()}
          == {s: sc.public(r["score"]) for s, r in par.items()}
          and par["tiny"]["client"] is not par["tiny2"]["client"],
          "jobs=2 reproduces jobs=1 with one client per fixture")

    # a fixture that fails mid-record leaves nothing behind
    class Broken(FakeClient):
        def chat(self, system, user, temperature=0.0):
            raise ai.SummarizerError("model went away")

    fresh_out = tmp / "rr" / "out-failed"
    try:
        rv.record(fixtures, both, "fake", "broken", lambda cfg: Broken()
                  if cfg["workspace"]["owner"] else FakeClient(), fresh_out)
        failed = False
    except ai.SummarizerError:
        failed = True
    check(failed and not fresh_out.exists(), "a failed record raises and writes nothing")

    # the Recorder answers a repeated identical call from the recording
    inner = FakeClient()
    rec = rv.Recorder(inner)
    rec.chat("s", "u")
    rec.chat("s", "u")
    check(rec.calls == 2 and inner.calls == 1 and len(rec.cassette) == 1,
          "repeated call recorded once, counted twice")


def test_config_env_scrub(tmp: Path) -> None:
    fdir = write_tiny_fixture(tmp / "envscrub" / "tiny")
    with env(WHOSAID_OWNER="Mallory_Example", WHOSAID_SUMMARIZER_MODEL="other:1b",
             WHOSAID_OLLAMA="http://10.0.0.9:1"):
        cfg = rv.fixture_config(fdir)
        still = os.environ.get("WHOSAID_OWNER")
    check(cfg["workspace"]["owner"] == "Alice_Example", "WHOSAID_OWNER does not reach the fixture config")
    check(cfg["summarizer"]["model"] == wsconfig.DEFAULTS["summarizer"]["model"]
          and cfg["search"]["ollama"] == wsconfig.DEFAULTS["search"]["ollama"],
          "WHOSAID_SUMMARIZER_MODEL / WHOSAID_OLLAMA ignored")
    check(still == "Mallory_Example", "the caller's env is restored")


# ---- 4. the claude-cli backend ---------------------------------------------------------------

FAKE_CLAUDE = """#!{python}
import json, os, sys
dump = os.environ["FAKE_CLAUDE_DUMP"]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
stdin = sys.stdin.read()
n = len([f for f in os.listdir(os.path.dirname(dump)) if f.startswith(os.path.basename(dump))]) + 1
with open(f"{{dump}}.{{n}}", "w") as fh:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "stdin": stdin, "cwd": os.getcwd()}}, fh)
if mode == "fail_once" and n == 1:
    sys.stderr.write("transient failure\\n")
    sys.exit(1)
if mode == "garbage":
    print("this is not json")
    sys.exit(0)
bad = mode == "is_error"
print(json.dumps([
    {{"type": "system", "subtype": "init", "apiKeySource": "none", "model": "claude-fake-1"}},
    {{"type": "assistant", "message": {{}}}},
    {{"type": "result", "subtype": "success", "is_error": bad,
     "result": "Failed to authenticate: OAuth session expired" if bad else "  NONE  "}},
]))
sys.exit(1 if bad else 0)
"""


def dumps(dump: Path) -> list[dict]:
    files = sorted(dump.parent.glob(dump.name + ".*"), key=lambda p: int(p.suffix[1:]))
    return [json.loads(p.read_text()) for p in files]


def after(argv: list[str], flag: str):
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_claude_cli(tmp: Path) -> None:
    home = tmp / "claude"
    home.mkdir()
    fake = home / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)

    def fresh(name: str) -> Path:
        d = home / name
        d.mkdir()
        return d / "call"

    dump = fresh("ok")
    with env(WHOSAID_EVAL_CLAUDE_BIN=str(fake), FAKE_CLAUDE_DUMP=str(dump), FAKE_CLAUDE_MODE="ok",
             ANTHROPIC_API_KEY="sk-must-not-leak", ANTHROPIC_AUTH_TOKEN="tok-must-not-leak",
             CLAUDE_CODE_OAUTH_TOKEN="oauth-passes-through"):
        c = rv.ClaudeCLI("sonnet")
        reply = c.chat("SYSTEM PROMPT TEXT", "USER MESSAGE TEXT", temperature=0.2)
        parent_still = os.environ.get("ANTHROPIC_API_KEY")
    d = dumps(dump)
    check(len(d) == 1 and reply == "NONE", f"one process, stripped reply ({len(d)}, {reply!r})")
    argv, cenv = d[0]["argv"], d[0]["env"]
    check("ANTHROPIC_API_KEY" not in cenv and "ANTHROPIC_AUTH_TOKEN" not in cenv,
          "API-key env vars are removed from the child")
    check(cenv.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-passes-through", "the OAuth token passes through")
    check(parent_still == "sk-must-not-leak", "the parent env is not modified")
    check(argv[0] == "-p" and after(argv, "--system-prompt") == "SYSTEM PROMPT TEXT"
          and after(argv, "--model") == "sonnet", f"-p, system prompt and model in argv: {argv}")
    check(after(argv, "--tools") == "" and after(argv, "--setting-sources") == ""
          and after(argv, "--output-format") == "json"
          and {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(argv),
          f"isolation flags present: {argv}")
    check("--bare" not in argv and not any("temperature" in a for a in argv), "no --bare, no temperature")
    check(d[0]["stdin"] == "USER MESSAGE TEXT" and not any("USER MESSAGE" in a for a in argv),
          "the user message goes on stdin, not argv")
    cwd = Path(d[0]["cwd"])
    check(cwd.name.startswith("whosaid-eval-claude-") and cwd != Path.cwd() and not cwd.exists(),
          f"runs in a fresh temp dir that is removed afterwards ({cwd})")
    check(c.calls == 1 and c.resolved_model == "claude-fake-1" and c.model == "sonnet",
          "calls counted, resolved model read from the init event")

    dump = fresh("err")
    with env(WHOSAID_EVAL_CLAUDE_BIN=str(fake), FAKE_CLAUDE_DUMP=str(dump), FAKE_CLAUDE_MODE="is_error"):
        c = rv.ClaudeCLI("sonnet")
        try:
            c.chat("s", "u")
            err = None
        except ai.SummarizerError as e:
            err = e
    check(err is not None and "Failed to authenticate" in str(err) and "setup-token" in err.hint,
          f"is_error: true raises SummarizerError with the CLI's text ({err})")
    check(len(dumps(dump)) == 1, "is_error is not retried")

    dump = fresh("retry")
    with env(WHOSAID_EVAL_CLAUDE_BIN=str(fake), FAKE_CLAUDE_DUMP=str(dump), FAKE_CLAUDE_MODE="fail_once"):
        c = rv.ClaudeCLI("sonnet")
        reply = c.chat("s", "u")
    check(reply == "NONE" and len(dumps(dump)) == 2 and c.calls == 1,
          "a failed call is retried once and counted as one call")

    dump = fresh("garbage")
    with env(WHOSAID_EVAL_CLAUDE_BIN=str(fake), FAKE_CLAUDE_DUMP=str(dump), FAKE_CLAUDE_MODE="garbage"):
        c = rv.ClaudeCLI("sonnet")
        try:
            c.chat("s", "u")
            err = None
        except ai.SummarizerError as e:
            err = e
    check(err is not None and "failed twice" in str(err) and len(dumps(dump)) == 2,
          f"non-JSON output twice -> SummarizerError ({err})")

    empty = home / "emptypath"
    empty.mkdir()
    with env(WHOSAID_EVAL_CLAUDE_BIN=None, PATH=str(empty)):
        try:
            rv.ClaudeCLI("sonnet")
            err = None
        except ai.SummarizerError as e:
            err = e
    check(err is not None and "WHOSAID_EVAL_CLAUDE_BIN" in err.hint, "no claude binary -> clear error")

    # the CLI refuses to send non-committed fixtures to the cloud model...
    fixtures = tmp / "claude-fixtures"
    write_tiny_fixture(fixtures / "tiny")
    dump = fresh("refuse")
    fenv = {"WHOSAID_EVAL_CLAUDE_BIN": str(fake), "FAKE_CLAUDE_DUMP": str(dump), "FAKE_CLAUDE_MODE": "ok"}
    cp = run_cli("--backend", "claude-cli", "--fixtures-dir", str(fixtures), extra_env=fenv)
    check(cp.returncode == 2 and "refusing" in cp.stderr and not dumps(dump),
          f"claude-cli + other fixtures dir -> refused before any call ({cp.returncode}, {cp.stderr})")
    # ...unless told they are synthetic; then the full record path runs end to end
    out = tmp / "claude-out"
    cp = run_cli("--backend", "claude-cli", "--model", "sonnet", "--fixtures-dir", str(fixtures),
                 "--allow-external-fixtures", "--record", "--out-dir", str(out), extra_env=fenv)
    run = "claude-cli-sonnet"
    check(cp.returncode == 0 and "MATCH claude-cli-sonnet" in cp.stdout,
          f"claude-cli record + self-replay via the CLI: {cp.stdout}{cp.stderr}")
    cas = json.loads((out / "cassettes" / run / "tiny.json").read_text())
    res = json.loads((out / "results" / f"{run}.json").read_text())
    check(cas["resolved_model"] == "claude-fake-1" and cas["model"] == "sonnet"
          and res["backend"] == "claude-cli", "cassette records the resolved model")
    check(len(dumps(dump)) == res["per_fixture"]["tiny"]["model_calls"] > 0,
          "one claude process per model call")


# ---- 5. every committed run replays ------------------------------------------------------------

def test_committed_runs() -> int:
    n = 0
    results = rv.DEFAULT_OUT / "results"
    for jp in sorted(results.glob("*.json")) if results.is_dir() else []:
        run = jp.stem
        if not (rv.DEFAULT_OUT / "cassettes" / run).is_dir():
            continue
        with patched_ollama(NoOllama):
            rc, msgs, _ = rv.replay(run)
        check(rc == 0, f"committed run {run} must replay: " + "\n".join(msgs))
        n += 1
    return n


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="whosaid-eval-test-"))
    try:
        with contextlib.redirect_stderr(io.StringIO()) as quiet:
            test_scorer()
            test_injection(tmp)
            test_record_replay(tmp)
            test_config_env_scrub(tmp)
            test_claude_cli(tmp)
        del quiet
        n = test_committed_runs()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"PASS: {CHECKS} assertions ({n} committed run(s) replayed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Offline test for lib/decisions.py (`whosaid decisions`, issue #213): one ledger per
call that joins every turn on a subject across revisits.

  * Done-when: on test/fixtures/decisions/2026-03-02-1000 one topic (the shared
    libraries) gets two separate decisions 28 minutes apart; the ledger has both,
    each citing its own turns, and the topic is marked "split across visits".
  * statuses: agreed (an explicit yes from someone else), proposed (no reply),
    contested (push-back that nobody resolved).
  * a later visit that repeats a decision with more detail is a refinement; one
    that undoes it is a reversal; a restatement in the same visit joins the row.
  * live entries (me/them + voices) give the same ledger shape; ids stay put as a
    live call grows, and changes() reports only what is new (lane #217 builds on it).
  * the Claude engine is opt-in, gets turn numbers only from the script, and any
    failure falls back to the local ledger. No test spawns a process or opens a socket.

Run: python3 test/decisions_test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
os.environ["WHOSAID_SPEAKER_DB"] = str(Path(tempfile.gettempdir()) / "whosaid-no-registry.json")  # no names
import decisions as dc  # noqa: E402

FIXTURE_WS = ROOT / "test" / "fixtures" / "decisions"
MEETING = "2026-03-02-1000"
FIXTURE = FIXTURE_WS / MEETING / "call.speakers.txt"
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def by_text(res: dict, needle: str) -> dict:
    hits = [d for d in res["decisions"] if needle in d["text"].lower()]
    check(len(hits) == 1, f"exactly one decision mentions {needle!r}: {[d['text'] for d in res['decisions']]}")
    return hits[0]


# no process, no socket: the local engine is pure
def _no(*_a, **_k):
    raise AssertionError("the local decisions engine must not spawn a process or open a socket")


real_popen, real_socket = subprocess.Popen, socket.socket
subprocess.Popen = _no  # type: ignore[assignment]
socket.socket = _no  # type: ignore[assignment,misc]

# ---------------------------------------------------------------- done-when: split across visits
turns = dc.turns_from_transcript(FIXTURE.read_text(), meeting=MEETING)
check(len(turns) == 29, f"fixture parses into 29 turns: {len(turns)}")
res = dc.ledger(turns, meeting=MEETING)
check(res["engine"] == "local", f"local engine by default: {res['engine']}")

parser = by_text(res, "date parser in the service lib")
auth = by_text(res, "auth client to a separate platform lib")
check(auth["t"] - parser["t"] >= 20 * 60, f"the two decisions are 20+ minutes apart: {parser['t']} {auth['t']}")
check(parser["topic"] == auth["topic"], f"both decisions sit on one topic: {parser['topic']} {auth['topic']}")
check(parser["visit"] == 1 and auth["visit"] == 2, f"on different visits: {parser['visit']} {auth['visit']}")

# each cites its own turns, verbatim, with meeting, time and speaker
for d, lo, hi in ((parser, 10 * 60, 13 * 60), (auth, 38 * 60, 41 * 60)):
    check(len(d["turns"]) >= 2, f"{d['id']} cites the proposal and its replies: {d['turns']}")
    for tr in d["turns"]:
        check(lo <= tr["t"] <= hi, f"{d['id']} cites only its own visit's turns: {tr}")
        check(set(tr) >= {"meeting", "t", "t_str", "speaker", "text"}, f"turn row shape: {tr}")
        check(tr["meeting"] == MEETING, f"turn carries the meeting: {tr}")
        src = [t for t in turns if t["t"] == tr["t"] and t["speaker"] == tr["speaker"]]
        check(src and src[0]["text"] == tr["text"], f"turn text is verbatim from the transcript: {tr}")
check(parser["turns"][0]["speaker"] == "Carol_Example" and parser["turns"][0]["t_str"] == "00:11:20",
      f"the parser decision starts at Carol's proposal: {parser['turns'][0]}")
check({"Bob_Example", "Alice_Example"} <= {t["speaker"] for t in parser["turns"]},
      f"both yeses are cited: {parser['turns']}")
check(auth["turns"][0]["t_str"] == "00:39:20", f"the auth decision starts at its proposal: {auth['turns'][0]}")
check(parser["status"] == "agreed" and auth["status"] == "agreed", f"both agreed: {parser['status']} {auth['status']}")

topic = next(t for t in res["topics"] if t["id"] == parser["topic"])
check("split across visits" in topic["flags"], f"topic marked split across visits: {topic}")
check(topic["visits"] >= 2 and topic["decisions"][:2] == [parser["id"], auth["id"]], f"topic row: {topic}")
rel = [r for r in topic["relations"] if {r["from"], r["to"]} == {parser["id"], auth["id"]}]
check(rel and rel[0]["kind"] == "split", f"the pair is a split: {topic['relations']}")
check({"id": parser["id"], "kind": "split"} in auth["related"], f"the auth row points back at the parser row: {auth}")

# the rendered ledger says it too, and names both decisions
text = dc.render(res)
check("split across visits" in text, "rendered text marks the split")
check("date parser" in text and "auth client" in text, "rendered text keeps both decisions")
check("—" not in text, "no em dash in the rendered text")

# the onboarding docs decision is its own single-visit topic, agreed, no flags
guide = by_text(res, "setup guide should move into the repo readme")
gt = next(t for t in res["topics"] if t["id"] == guide["topic"])
check(guide["topic"] != parser["topic"] and gt["flags"] == [] and guide["status"] == "agreed",
      f"setup guide decision: {guide} {gt}")

# nothing from the meeting chatter
check(not any("keep it moving" in d["text"].lower() for d in res["decisions"]), "meeting navigation is not a decision")
check(len(res["decisions"]) == 3, f"three decisions in the fixture: {[d['text'] for d in res['decisions']]}")

# determinism
check(json.dumps(dc.ledger(turns, meeting=MEETING), sort_keys=True) == json.dumps(res, sort_keys=True),
      "same turns, same ledger")


# ---------------------------------------------------------------- statuses, refinement, reversal
def T(t, speaker, text):
    return {"t": float(t), "speaker": speaker, "text": text}


SHARED = [
    T(0, "A", "Okay the cache layer for the pricing service, the cache keeps serving stale prices to the checkout page."),
    T(20, "B", "The cache layer needs a shorter expiry for the pricing service, stale prices cost real orders."),
]
FILLER = [
    T(300, "A", "Different subject, the hiring loop for the design role has three candidates this month."),
    T(330, "B", "The design role hiring loop needs a portfolio review step before the onsite interviews."),
    T(360, "A", "Portfolio review before onsite interviews sounds right for the design candidates."),
]

proposed = dc.ledger(SHARED + [T(40, "A", "Let's set the cache expiry for the pricing service to five minutes."),
                               T(70, "A", "Pricing data comes from the vendor feed every hour anyway.")])
d = by_text(proposed, "cache expiry")
check(d["status"] == "proposed", f"no reply: proposed: {d}")

contested = dc.ledger(SHARED + [T(40, "A", "Let's set the cache expiry for the pricing service to five minutes."),
                                T(60, "B", "I don't think five minutes is enough, the vendor feed lags more than that.")])
d = by_text(contested, "cache expiry")
check(d["status"] == "contested", f"push-back: contested: {d}")
check(any(t["role"] == "contest" for t in d["turns"]), f"the push-back is cited: {d['turns']}")

restated = dc.ledger(SHARED + [T(40, "A", "Let's set the cache expiry for the pricing service to five minutes."),
                               T(60, "B", "Sounds good."),
                               T(80, "A", "So we set the cache expiry to five minutes for the pricing service.")])
check(len(restated["decisions"]) == 1, f"a restatement joins the row: {restated['decisions']}")
check([t["role"] for t in restated["decisions"][0]["turns"]] == ["proposal", "agree", "restate"],
      f"proposal, agree, restate: {restated['decisions'][0]['turns']}")

REVISIT = [T(600, "A", "Back to the cache layer for the pricing service and the stale prices on checkout."),
           T(620, "B", "The cache layer for the pricing service still serves stale prices on the checkout page.")]
refined = dc.ledger(SHARED + [T(40, "A", "Let's set the cache expiry for the pricing service to five minutes."),
                              T(60, "B", "Agreed.")] + FILLER + REVISIT
                    + [T(640, "A", "Let's set the cache expiry for the pricing service to five minutes, and one minute for sale items."),
                       T(660, "B", "Yes, that works.")])
pair = [d for d in refined["decisions"] if "cache expiry" in d["text"].lower()]
check(len(pair) == 2 and pair[0]["topic"] == pair[1]["topic"], f"two cache decisions on one topic: {pair}")
tp = next(t for t in refined["topics"] if t["id"] == pair[0]["topic"])
check(tp["flags"] == ["refined across visits"], f"refinement flagged: {tp}")

reversed_ = dc.ledger(SHARED + [T(40, "A", "Let's set the cache expiry for the pricing service to five minutes."),
                                T(60, "B", "Agreed.")] + FILLER + REVISIT
                      + [T(640, "B", "Actually, let's not use a cache expiry for the pricing service, we invalidate on every vendor update instead."),
                         T(660, "A", "Yes, agreed.")])
pair = [d for d in reversed_["decisions"] if "cache expiry" in d["text"].lower()]
tp = next(t for t in reversed_["topics"] if t["id"] == pair[0]["topic"])
check(len(pair) == 2 and tp["flags"] == ["reversed across visits"], f"reversal flagged: {pair} {tp}")

# ---------------------------------------------------------------- live entries, stable ids, changes()
entries = [{"t": t["t"], "clock": f"10:{int(t['t']) // 60:02d}:{int(t['t']) % 60:02d}",
            "who": "me" if t["speaker"] == "Alice_Example" else "them",
            **({"voice": "v1" if t["speaker"] == "Bob_Example" else "v2"} if t["speaker"] != "Alice_Example" else {}),
            "text": t["text"]} for t in turns]
lt = dc.turns_from_live(entries, meeting="live-session")
check(lt[0]["speaker"] == "me" and lt[0]["clock"] == "10:00:05", f"live turn shape: {lt[0]}")
check(len({t["speaker"] for t in lt}) == 3, f"two 'them' voices stay two speakers: {sorted({t['speaker'] for t in lt})}")
full = dc.ledger(lt, meeting="live-session")
lp = by_text(full, "date parser in the service lib")
check(lp["status"] == "agreed" and lp["turns"][0]["clock"], f"live decision agreed with clock: {lp}")
lt_topic = next(t for t in full["topics"] if t["id"] == lp["topic"])
check("split across visits" in lt_topic["flags"], f"live ledger marks the split too: {lt_topic}")

half = dc.ledger([t for t in lt if t["t"] < 30 * 60], meeting="live-session")
ids_half = [d["id"] for d in half["decisions"]]
check(ids_half == [d["id"] for d in full["decisions"]][:len(ids_half)] and len(ids_half) == 2,
      f"ids stay put as the call grows: {ids_half} vs {[d['id'] for d in full['decisions']]}")
ev = dc.changes(half, full)
kinds = [(e["event"], e.get("id") or e.get("topic")) for e in ev]
check(("decision", by_text(full, "auth client")["id"]) in kinds, f"changes: the new decision: {kinds}")
check(any(e["event"] == "flag" and e["flag"] == "split across visits" for e in ev), f"changes: the new flag: {ev}")
check(dc.changes(full, full) == [], "no change, no events")

# ---------------------------------------------------------------- the opt-in Claude engine
class Stub:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat(self, system, user, temperature=0.0):
        self.calls.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


n_parser = next(i for i, t in enumerate(turns) if t["t_str"] == "00:11:20")
n_auth = next(i for i, t in enumerate(turns) if t["t_str"] == "00:39:20")
reply = json.dumps({"decisions": [
    {"subject": "Shared libraries", "text": "The date parser stays in the service lib for now.",
     "turns": [n_parser, n_parser + 1, 999], "status": "agreed"},
    {"subject": "Shared libraries", "text": "The auth client moves to a new platform lib.",
     "turns": [n_auth, n_auth + 1], "status": "agreed", "relation": "split"},
]})
stub = Stub("```json\n" + reply + "\n```")
cres = dc.ledger(turns, meeting=MEETING, engine="claude", client=stub)
check(cres["engine"] == "claude" and len(stub.calls) == 1, f"one claude call: {cres['engine']} {len(stub.calls)}")
check(f"#{n_parser} [00:11:20] Carol_Example:" in stub.calls[0], "the prompt numbers every turn")
check(len(cres["decisions"]) == 2, f"two claude decisions: {cres['decisions']}")
c1, c2 = cres["decisions"]
check([t["t_str"] for t in c1["turns"]] == ["00:11:20", "00:11:40"], f"turns come from the transcript, bad numbers dropped: {c1['turns']}")
check(c1["turns"][0]["text"] == turns[n_parser]["text"], "claude rows quote the transcript, not the model")
check(c1["visit"] == 1 and c2["visit"] == 2, f"claude visits by time gap: {c1['visit']} {c2['visit']}")
ct = next(t for t in cres["topics"] if t["id"] == c1["topic"])
check(ct["flags"] == ["split across visits"] and ct["label"] == "Shared libraries", f"claude topic: {ct}")

fres = dc.ledger(turns, meeting=MEETING, engine="claude", client=Stub(RuntimeError("no reply within 90s")))
check(fres["engine"] == "local" and "no reply" in fres.get("engine_error", ""), f"failure falls back to local: {fres.get('engine_error')}")
check(len(fres["decisions"]) == 3, "the fallback is the full local ledger")
gres = dc.ledger(turns, meeting=MEETING, engine="claude", client=Stub("not json at all"))
check(gres["engine"] == "local" and gres.get("engine_error"), "garbage reply falls back to local")

subprocess.Popen, socket.socket = real_popen, real_socket  # type: ignore[assignment,misc]

# ---------------------------------------------------------------- engine selection
with tempfile.TemporaryDirectory() as d:
    ws = Path(d)
    check(dc.engine_for(ws, "") == "local", "no whosaid.toml: local")
    (ws / "whosaid.toml").write_text('[summarizer]\nengine = "claude"\n')
    check(dc.engine_for(ws, "") == "claude", "[summarizer] engine = claude opts in")
    check(dc.engine_for(ws, "local") == "local", "--engine local wins")
    check(dc.engine_for(None, "") == "local", "no workspace: never claude")

# ---------------------------------------------------------------- CLI (module entry point)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = dc.main([str(FIXTURE_WS), MEETING, "--json"])
data = json.loads(out.getvalue())
check(rc == 0 and len(data["decisions"]) == 3 and data["meeting"] == MEETING, f"CLI --json: {rc} {data.get('meeting')}")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = dc.main([str(FIXTURE)])
check(rc == 0 and "split across visits" in out.getvalue(), f"CLI on a transcript path: {out.getvalue()[:300]}")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    rc = dc.main([str(FIXTURE_WS), "no-such-meeting"])
check(rc == 1 and "no-such-meeting" in err.getvalue(), f"unknown meeting: exit 1 with its name: {err.getvalue()}")

# live mode: the newest session under --into, written to <stem>.decisions.json
with tempfile.TemporaryDirectory() as d:
    into = Path(d)
    jl = into / ".live" / "20260302T100000.jsonl"
    jl.parent.mkdir()
    jl.write_text("\n".join([json.dumps({"meta": {"started": 0, "ended": True}})] + [json.dumps(e) for e in entries]) + "\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = dc.main(["--live", "--into", d, "--json"])
    live_res = json.loads(out.getvalue())
    check(rc == 0 and live_res["meeting"] == "20260302T100000" and len(live_res["decisions"]) == 3, f"--live: {rc}")
    side = jl.with_name("20260302T100000.decisions.json")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = dc.main(["--live", "--into", d, "--follow", "--once", "--poll", "0.01"])
    check(rc == 0 and side.is_file(), "--follow --once writes <stem>.decisions.json and exits on an ended call")
    check(len(json.loads(side.read_text())["decisions"]) == 3, "the sidecar holds the ledger")

print(f"OK decisions: {CHECKS} checks")

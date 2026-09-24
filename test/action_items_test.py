#!/usr/bin/env python3
"""
Offline test for lib/action_items.py, the built-in action-item summarizer
(GitHub issue #14), and its wiring into lib/workspace.py.

No Ollama is needed: a stub HTTP server on 127.0.0.1 (random port) answers
/api/tags and /api/chat with canned replies keyed by the prompt it receives
(SELECT rows with a trailing NONE, bullets, SKIP, a "- **SKIP.**" bullet, a
parroted placeholder, a deliberately wrong quote, a bullet with no quote, a
duplicate title, and a 4-line inferred list that must be cut to 3). The
transcript is synthetic: owner Alice_Example, Bob_Example in "leadership",
Carol_Example in "team", one unidentified SPEAKER_03.

Covers: candidate selection (owner turns >= min_chars, name/alias matching
including the possessive, model-selected ids, per-slice SELECT calls),
sentence-group splitting of a long turn with the two-previous-turns context,
section assignment by group (ungrouped speakers fall into the last group),
the exact bullet format, quote verification flags, dedupe, the evidence
block, the header stats line, the no-groups and no-owner modes, the
Ollama-down stub and exit codes, the CLI, and workspace.py's
`action-items --engine ollama|auto|none` plus section-typed roll-up items.

Run:
    python3 test/action_items_test.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import action_items as ai  # noqa: E402
import workspace  # noqa: E402
import wsconfig  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


# ---- fixture transcript ------------------------------------------------------------------

LONG_TURN = (
    "Okay, here is where I am on my side. I will draft the migration plan by Friday and "
    "circulate it to everyone before the review so nobody is surprised by the sequencing. "
    "The plan covers the database move, the queue swap, and the rollback path, and I want "
    "each section to have an owner and a date. After that I will book the review slot with "
    "the vendor for early next week and confirm that the sandbox environment is ready for "
    "the dry run, because last time we lost a day waiting on access. I also need to write "
    "up the cost comparison that Bob asked about, which means pulling the last three "
    "invoices and normalizing them per environment."
)
LONG_TURN_TAIL = (
    "Finally, I will update the runbook with the new alert thresholds once the load test "
    "finishes, and post the results in the channel so the team can review them asynchronously."
)

TRANSCRIPT = f"""# Speakers (4): Alice_Example, Bob_Example, Carol_Example, SPEAKER_03
[00:00:05] Bob_Example: Morning everyone. Alice, can you send the vendor report to me by Thursday so I can review it before the board sync?
[00:00:20] Alice_Example: Yes, I will send the vendor report on Wednesday afternoon and include the cost breakdown you asked for last week.
[00:00:40] Carol_Example: Thanks. Alice's dashboard is still showing the old numbers, could you refresh the data source before the demo?
[00:01:00] SPEAKER_03: Also please update the onboarding checklist for the new hires, we have three starting Monday.
[00:01:15] Alice_Example: Sure.
[00:01:20] Bob_Example: Carol, you own the retro notes this time.
[00:01:30] Alice_Example: {LONG_TURN}
{LONG_TURN_TAIL}
[00:02:30] Carol_Example: Great, thanks.
[00:02:40] Bob_Example: Alicia, one more thing, loop in finance when the report goes out.
[00:02:50] Carol_Example: No malice intended, the numbers were just stale.
"""

# canned BULLETS replies keyed by (turn time, part number or "")
BULLET_REPLIES = {
    ("00:00:05", ""): '- **Send the vendor report to Bob by Thursday.** Bob wants it before the board sync. '
                      '"send the vendor report to me by Thursday"',
    ("00:00:20", ""): '- **Send the vendor report on Wednesday.** Alice will include the cost breakdown. '
                      '"I will send the vendor report on Wednesday afternoon"',
    ("00:00:40", ""): '- **Refresh the dashboard data source before the demo.** The dashboard shows old numbers. '
                      '"could you refresh the data source before the demo"',
    ("00:01:00", ""): '- **Update the onboarding checklist for the new hires.** Three start Monday. '
                      '"update the onboarding checklist for the new hires"',
    ("00:01:20", ""): "SKIP",
    ("00:01:30", "1"): '- **Draft the migration plan by Friday.** Alice will circulate it before the review. '
                       '"I will draft the migration plan by Friday"\n- **SKIP.**',
    ("00:01:30", "2"): '- **Book the review slot with the vendor.** Early next week. '
                       '"this sentence does not appear in the turn"\n'
                       '- **Draft the migration plan by Friday.** Repeated from part 1. '
                       '"I will draft the migration plan by Friday"\n'
                       '- **<imperative title: what Alice must do>.** parroted placeholder '
                       '"<at most 15 words copied word for word from the TURN>"\n'
                       '- **Post the load test results in the channel.** Once the load test finishes.',
    ("00:02:40", ""): '- **Loop in finance when the vendor report goes out.** Bob asked for it. '
                      '"loop in finance when the report goes out"',
    # DIRECTIVE_TRANSCRIPT (times kept clear of TRANSCRIPT's)
    ("00:03:15", ""): '**TEAM: Link a ticket to every pull request before review.** A new team rule. '
                      '"everyone links a ticket to every pull request"',
    ("00:03:30", ""): '- **TEAM: Fill in the team survey by Friday.** Carol asks the team. '
                      '"please fill in the team survey by Friday"',
    ("00:03:45", ""): '- **TEAM: Finish the security training by the end of the month.** Dana sets a deadline. '
                      '"all of you need to finish the security training"\n'
                      '- **Send Dana the audit export by Wednesday.** Dana asks Alice. '
                      '"Alice, can you send me the audit export by Wednesday"',
    ("00:04:10", ""): '- **TEAM: Update the team wiki.** Dana asks for it. "please update the team wiki as well"',
}
NO_OWNER_BULLET_REPLIES = {
    ("00:00:05", ""): '- **ASK: Send the vendor report to Bob by Thursday.** Bob asks Alice. '
                      '"send the vendor report to me by Thursday"',
    ("00:00:20", ""): '- **COMMIT: Send the vendor report on Wednesday.** Alice commits. '
                      '"I will send the vendor report on Wednesday afternoon"',
    ("00:01:20", ""): '- **ASK: Own the retro notes.** Bob asks Carol. "you own the retro notes this time"',
}
DIRECTIVE_TRANSCRIPT = """# Speakers (4): Alice_Example, Bob_Example, Carol_Example, Dana_Example
# Role: Dana_Example = boss
# Role: Alice_Example = boss
[00:03:05] Bob_Example: Morning. Quick one before we start, nothing else from me.
[00:03:15] Bob_Example: From now on everyone links a ticket to every pull request before asking for review.
[00:03:30] Carol_Example: Everyone, please fill in the team survey by Friday.
[00:03:45] Dana_Example: Also, all of you need to finish the security training by the end of the month. Alice, can you send me the audit export by Wednesday?
[00:04:00] Bob_Example: Carol, you own the retro notes this time.
[00:04:10] Dana_Example: Carol, please update the team wiki as well. Thanks all.
"""
INFER_REPLY = ("- (inferred) Confirm the board sync agenda with Bob.\n"
               "- (inferred) Share the refreshed dashboard link with Carol.\n"
               "- (inferred) Tell the new hires where the checklist lives.\n"
               "- (inferred) A fourth line that must be cut.")

TURN_RE = re.compile(r"TURN \[(\d\d:\d\d:\d\d)\] by (\S+) \(([^)]*)\)(?: \(part (\d+) of (\d+)\))?:")


def router(payload: dict) -> str | tuple[int, str]:
    system = payload["messages"][0]["content"]
    user = payload["messages"][1]["content"]
    if payload["model"] == "missing-model":
        return 404, json.dumps({"error": "model 'missing-model' not found, try pulling it first"})
    if payload["model"] == "no-think-stub" and payload.get("think"):
        return 400, json.dumps({"error": '"no-think-stub" does not support thinking'})
    if "Select every turn" in system:
        if "No owner is configured" in system:
            rows = [f"#{i}: something" for i in (0, 1, 5) if f"#{i} [" in user]
        else:
            rows = []
            if "#3 [" in user:
                rows.append("#3: update the onboarding checklist")
            if "#2 [" in user:
                rows.append("#2: refresh the data source")
            if "#5 [" in user:
                rows.append("#5: retro notes (over-included)")
            if "#1 [" in user:
                rows.append("#1: an owner turn the model should never see")
        return ("\n".join(rows) + "\nNONE") if rows else "NONE"
    if "from ONE turn" in system:
        m = TURN_RE.search(user)
        assert m, f"unexpected BULLETS prompt: {user[:200]!r}"
        key = (m.group(1), m.group(4) or "")
        table = NO_OWNER_BULLET_REPLIES if "COMMIT:" in system else BULLET_REPLIES
        return table.get(key, "SKIP")
    if "implied next steps" in system:
        if payload["model"] == "infer-fail-stub":
            return 500, json.dumps({"error": "synthetic INFER failure (for tests)"})
        return INFER_REPLY
    raise AssertionError(f"unexpected system prompt: {system[:120]!r}")


class StubOllama:
    """Threaded stub of the two Ollama endpoints the summarizer uses."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send(200, json.dumps({"models": [{"name": "qwen-stub"}]}))
                else:
                    self._send(404, "{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
                stub.requests.append(payload)
                if self.path != "/api/chat":
                    self._send(404, "{}")
                    return
                reply = router(payload)
                if isinstance(reply, tuple):
                    self._send(reply[0], reply[1])
                    return
                if payload["model"] == "inline-think-stub":
                    # a thinking model that leaves its reasoning in content,
                    # bullet-shaped so a leak would show up as a bogus item
                    reply = ('<think>\n- **Leaked reasoning bullet.** "should never be parsed"\n'
                             "</think>\n" + reply)
                self._send(200, json.dumps({"model": payload["model"], "done": True,
                                            "message": {"role": "assistant", "content": reply}}))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def of_kind(self, marker: str) -> list[dict]:
        return [r for r in self.requests if marker in r["messages"][0]["content"]]


def dead_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def write_config(ws: Path, url: str, *, owner: str = "Alice_Example", groups: bool = True,
                 aliases: bool = True, num_predict: int | None = None,
                 think: bool | str | None = None, model: str = "qwen-stub") -> None:
    lines = ["[workspace]", f'owner = "{owner}"']
    if aliases:
        lines.append('aliases = ["Alice", "Alicia"]')
    if groups:
        lines += ["", "[groups]", 'leadership = ["Bob_Example"]', 'team = ["Carol_Example"]']
    lines += ["", "[summarizer]", f'model = "{model}"', "num_ctx = 4096", "chunk_chars = 300"]
    if num_predict is not None:
        lines.append(f"num_predict = {num_predict}")
    if isinstance(think, str):
        lines.append(f"think = {think}")          # a raw TOML value, e.g. an inline table
    elif think is not None:
        lines.append(f"think = {'true' if think else 'false'}")
    lines += ["", "[search]", f'ollama = "{url}"', ""]
    (ws / "whosaid.toml").write_text("\n".join(lines))


def section(md: str, heading_prefix: str) -> list[str]:
    """Bullet lines under the '## ' heading that starts with heading_prefix."""
    out, on = [], False
    for line in md.splitlines():
        if line.startswith("## "):
            on = line.startswith(heading_prefix)
            continue
        if line.startswith("<details"):
            on = False
        if on and line.startswith("- "):
            out.append(line)
    return out


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHOSAID_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ---- tests -------------------------------------------------------------------------------

def test_name_regex_and_pieces() -> None:
    rx = ai.compile_name_re(["Alice", "Alicia"])
    assert rx is not None
    check(bool(rx.search("Alice, can you")), "alias matches a plain mention")
    check(bool(rx.search("Alice's dashboard")), "alias matches the possessive")
    check(bool(rx.search("Alicia, one more thing")), "second alias matches")
    check(bool(rx.search("alice lowercase")), "match is case-insensitive")
    check(not rx.search("No malice intended"), "word boundary: 'malice' is not 'Alice'")
    check(ai.compile_name_re([]) is None and ai.compile_name_re(["", " "]) is None,
          "no aliases -> no regex")

    text = LONG_TURN + " " + LONG_TURN_TAIL
    parts = ai.pieces_of(text, 600)
    check(len(parts) == 2, f"long turn splits into 2 sentence groups, got {len(parts)}")
    check(all(len(p) <= 600 for p in parts), "every sentence group is <= split_chars")
    check(" ".join(parts) == text, "sentence groups re-join to the original text")
    check(all(p.endswith(".") for p in parts), "groups end on sentence boundaries")
    check(ai.pieces_of("short", 600) == ["short"], "short turns are one piece")
    runon = "word " * 300
    rp = ai.pieces_of(runon.strip(), 100)
    check(len(rp) > 1 and all(len(p) <= 100 for p in rp) and all(" " not in (p[0], p[-1]) for p in rp),
          "a run-on without punctuation is cut at spaces")
    check(ai.chunks(["a" * 50, "b" * 50, "c" * 50], 120) == [["a" * 50, "b" * 50], ["c" * 50]],
          "chunks never cut a row and respect the limit")


def plan_of(cfg: dict, text: str) -> "ai.Plan":
    plan = ai.Plan(cfg)
    turns = wsconfig.parse_turns(text)
    plan.add_bosses(ai.bosses_of(text))
    plan.build_prompts(ai.speakers_of(text, turns))
    return plan


def test_team_directives(stub: StubOllama, tmp: Path) -> None:
    ws = tmp / "ws-directives"
    ws.mkdir()
    write_config(ws, stub.url)
    cfg = wsconfig.load_config(ws)
    stub.requests.clear()
    md, stats = ai.draft(DIRECTIVE_TRANSCRIPT, "2026-09-24-0700", cfg)
    select = stub.of_kind("Select every turn")[0]["messages"][0]["content"]
    check("Also select every turn in which a LEADERSHIP speaker (Bob_Example, Dana_Example)" in select
          and "even when Alice is not named and never speaks" in select,
          "SELECT asks for team-wide leadership directives, boss roles included")
    check("- LEADERSHIP (role-tagged boss): Dana_Example." in select, "roster names role-tagged bosses")
    bullets = stub.of_kind("from ONE turn")
    check("TEAM: <imperative title" in bullets[0]["messages"][0]["content"], "BULLETS offers the TEAM: shape")
    check(any("by Dana_Example (leadership)" in r["messages"][1]["content"] for r in bullets),
          "a role-tagged boss outside every group reads as leadership")
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    check(heads == ["## 1. Asks from leadership (Bob)", "## 2. Asks from team (Carol)",
                    "## 3. Team directives from leadership (Bob, Dana)", "## 4. Alice's own commitments",
                    "## 5. Inferred next steps"], f"boss roles join the directive heading, owner never: {heads}")
    check(section(md, "## 3.") == [
        '- **Alice_Example** [Bob 00:03:15] Link a ticket to every pull request before review. A new team '
        'rule. "everyone links a ticket to every pull request"',
        '- **Alice_Example** [Dana 00:03:45] Finish the security training by the end of the month. Dana '
        'sets a deadline. "all of you need to finish the security training"',
    ], f"leadership TEAM: bullets (one with no '- ' marker) are the owner's items, prefix stripped: "
       f"{section(md, '## 3.')}")
    check(section(md, "## 1.") == [
        '- **Alice_Example** [Dana 00:03:45] Send Dana the audit export by Wednesday. Dana asks Alice. '
        '"Alice, can you send me the audit export by Wednesday"',
    ], "a role-tagged boss's direct ask lands in the leadership group's section")
    check("survey" not in md.split("<details>")[0], "a TEAM: bullet from a non-leader is dropped")
    check("wiki" not in md.split("<details>")[0],
          "a leadership TEAM: bullet whose sentence opens 'Carol, ...' is Carol's task: dropped")
    check(ai.addressee(plan_of(cfg, DIRECTIVE_TRANSCRIPT), "Perfect. Carol, can you send it? Thanks.",
                       "can you send it") == "Carol"
          and ai.addressee(plan_of(cfg, DIRECTIVE_TRANSCRIPT), "Team, please send it.", "please send it") == ""
          and ai.addressee(plan_of(cfg, DIRECTIVE_TRANSCRIPT), "Alice, can you send it?", "can you send it") == "",
          "addressee: a named other participant opening the sentence; not 'Team,' and not the owner")
    parsed = workspace.parse_bullets_with_sections(md)
    check([s for _, _, _, s in parsed if "directive" in s] == ["Team directives from leadership"] * 2,
          "the roll-up type keeps 'leadership' so the worklist ranks directives as boss asks")

    plan = ai.Plan(wsconfig.load_config(ws), owner="Bob_Example")
    plan.build_prompts(["Bob_Example", "Carol_Example"])
    check(plan.leaders == [] and plan.directive_section is None and "TEAM directive" not in plan.bullets_prompt
          and "LEADERSHIP speaker" not in plan.select_prompt,
          "the owner is never their own leadership; no leadership -> no directive section or TEAM rules")
    ng = tmp / "ws-directives-nogroups"
    ng.mkdir()
    write_config(ng, stub.url, groups=False)
    plan = ai.Plan(wsconfig.load_config(ng))
    plan.add_bosses(ai.bosses_of(DIRECTIVE_TRANSCRIPT))
    check(plan.headings == ["Asks of Alice", "Team directives from leadership (Dana)", "Alice's own commitments",
                            "Inferred next steps"] and plan.commit_section == 2 and plan.inferred_section == 3,
          f"boss roles alone add the directive section: {plan.headings}")
    check(plan.section_for("Dana_Example", "ask") == 0 and plan.section_for("Dana_Example", "directive") == 1,
          "without groups a boss's ask goes to 'Asks of', its directive to team directives")


def test_owner_mode(stub: StubOllama, tmp: Path) -> None:
    ws = tmp / "ws-owner"
    ws.mkdir()
    write_config(ws, stub.url)
    cfg = wsconfig.load_config(ws)
    check(cfg["workspace"]["aliases"] == ["Alice", "Alicia"], "aliases read from whosaid.toml")
    check(list(cfg["groups"]) == ["leadership", "team"], "groups keep file order")

    stub.requests.clear()
    md, stats = ai.draft(TRANSCRIPT, "2026-09-16-0703", cfg)

    # -- candidates
    check(stats["by_owner"] == 2, f"2 owner turns >= min_chars ('Sure.' excluded): {stats}")
    check(stats["naming_owner"] == 3, f"3 turns name the owner (Alice, Alice's, Alicia): {stats}")
    check(stats["model_added"] == 2, f"model adds #3 and #5 (not the already-named #2): {stats}")
    check(stats["candidates"] == 7, f"7 candidate turns: {stats}")
    selects = stub.of_kind("Select every turn")
    check(stats["slices"] == len(selects) >= 2, f"one SELECT call per slice, {len(selects)} slices")
    check(all("(part " in r["messages"][1]["content"] for r in selects), "slices are labelled part k of n")
    check(not any("Alice_Example:" in r["messages"][1]["content"] for r in selects),
          "the owner's turns are never in a SELECT slice")
    check("Alicia" in selects[0]["messages"][0]["content"] and "LEADERSHIP: Bob_Example" in
          selects[0]["messages"][0]["content"], "roster names the aliases and the groups")

    # -- one BULLETS call per candidate turn, long turn split with context
    bullets = stub.of_kind("from ONE turn")
    prompts = [r["messages"][1]["content"] for r in bullets]
    check(len(bullets) == 8, f"7 candidates -> 8 BULLETS calls (long turn in 2 parts), got {len(bullets)}")
    parts = [p for p in prompts if "TURN [00:01:30]" in p]
    check(len(parts) == 2 and "(part 1 of 2)" in parts[0] and "(part 2 of 2)" in parts[1],
          "the long turn is read in two parts")
    check("(earlier in this same turn)" in parts[1] and "(earlier in this same turn)" not in parts[0],
          "part 2 sees the tail of part 1 as context")
    carol = next(p for p in prompts if "TURN [00:00:40]" in p)
    check(carol.startswith("Context (earlier turns, do not quote from these):\n[00:00:05] Bob_Example:")
          and "\n[00:00:20] Alice_Example:" in carol, "two previous turns are shown as context")
    check("by Carol_Example (team)" in carol, "the speaker's role comes from the config groups")
    check(any("by SPEAKER_03 (unidentified speaker)" in p for p in prompts), "SPEAKER_NN role")
    check(any("by Alice_Example (Alice, the person these items are for)" in p for p in prompts),
          "the owner's role")

    # -- request shape
    for r in selects + bullets:
        check(r["model"] == "qwen-stub" and r["stream"] is False
              and r["options"] == {"temperature": 0.0, "num_ctx": 4096, "num_predict": ai.DEFAULT_NUM_PREDICT},
              f"select/bullets: temperature 0, num_ctx from config, stream off, default num_predict: {r['options']}")
    infers = stub.of_kind("implied next steps")
    check(len(infers) == 1 and infers[0]["options"]["temperature"] == 0.2
          and infers[0]["options"]["num_predict"] == ai.DEFAULT_NUM_PREDICT,
          "one INFER call at 0.2, default num_predict")
    check(stats["model_calls"] == len(stub.requests), "stats count every model call")

    # -- headings, in config order, numbered
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    check(heads == ["## 1. Asks from leadership (Bob)", "## 2. Asks from team (Carol)",
                    "## 3. Team directives from leadership (Bob)", "## 4. Alice's own commitments",
                    "## 5. Inferred next steps"], f"headings: {heads}")
    check(section(md, "## 3.") == ["- none"],
          "no directive in this meeting -> '- none'")

    # -- bullets: exact format, section by group, ungrouped SPEAKER_03 in the last group
    lead = section(md, "## 1.")
    check(lead == [
        '- **Alice_Example** [Bob 00:00:05] Send the vendor report to Bob by Thursday. Bob wants it '
        'before the board sync. "send the vendor report to me by Thursday"',
        '- **Alice_Example** [Bob 00:02:40] Loop in finance when the vendor report goes out. Bob asked '
        'for it. "loop in finance when the report goes out"',
    ], f"leadership section: {lead}")
    team = section(md, "## 2.")
    check(team == [
        '- **Alice_Example** [Carol 00:00:40] Refresh the dashboard data source before the demo. The '
        'dashboard shows old numbers. "could you refresh the data source before the demo"',
        '- **Alice_Example** [SPEAKER_03 00:01:00] Update the onboarding checklist for the new hires. '
        'Three start Monday. "update the onboarding checklist for the new hires"',
    ], f"team section (SPEAKER_03 falls into the last group): {team}")
    own = section(md, "## 4.")
    check(own == [
        '- **Alice_Example** [Alice 00:00:20] Send the vendor report on Wednesday. Alice will include '
        'the cost breakdown. "I will send the vendor report on Wednesday afternoon"',
        '- **Alice_Example** [Alice 00:01:30] Draft the migration plan by Friday. Alice will circulate '
        'it before the review. "I will draft the migration plan by Friday"',
        '- **Alice_Example** [Alice 00:01:30] Book the review slot with the vendor. Early next week. '
        '"this sentence does not appear in the turn" _(⚠ quote not verbatim from that turn)_',
        '- **Alice_Example** [Alice 00:01:30] Post the load test results in the channel. Once the load '
        'test finishes. _(⚠ no quote)_',
    ], f"own commitments (dedupe, placeholder dropped, flags): {own}")
    check("imperative title" not in md and "SKIP" not in md, "SKIP bullets and placeholders never render")
    inferred = section(md, "## 5.")
    check(inferred == [
        "- **Alice_Example** [inferred] Confirm the board sync agenda with Bob.",
        "- **Alice_Example** [inferred] Share the refreshed dashboard link with Carol.",
        "- **Alice_Example** [inferred] Tell the new hires where the checklist lives.",
    ], f"inferred lines capped at 3: {inferred}")
    check("[Bob 00:01:20]" not in md, "a turn the model answered SKIP for yields no bullet")

    # -- header, note, evidence
    lines = md.splitlines()
    check(lines[0] == "# Action items — 2026-09-16-0703" and lines[1] == "", "h1 is the meeting folder")
    check(lines[2] == "Speakers in this meeting: Alice_Example, Bob_Example, Carol_Example, SPEAKER_03",
          f"speakers line: {lines[2]}")
    k = stats["slices"]
    note = lines[4]
    check(re.match(r"^_Auto-drafted \d{4}-\d\d-\d\dT\d\d:\d\d[+-]\d\d:\d\d by `qwen-stub` \(local Ollama, "
                   r"offline\) via `whosaid action-items --engine ollama`: ", note) is not None,
          f"note line prefix: {note}")
    check(f": 7 candidate turns (2 by Alice, 3 naming them, 2 added by the model over {k} slices), "
          "6 kept as evidence, 8 items drafted, 2 flagged ⚠. A DRAFT: read the evidence and the "
          "transcript before trusting it._" in note, f"note line stats: {note}")
    check(stats["evidence"] == 6 and stats["items"] == 8 and stats["flagged"] == 2
          and stats["inferred"] == 3, f"stats agree with the note: {stats}")
    check(stats["sections"] == {"Asks from leadership (Bob)": 2, "Asks from team (Carol)": 2,
                                "Team directives from leadership (Bob)": 0,
                                "Alice's own commitments": 4, "Inferred next steps": 3}, stats["sections"])
    ev_start = md.index("<details><summary>Evidence turns the draft was built from (verbatim, 6)</summary>")
    ev = md[ev_start:]
    check(ev.rstrip().endswith("</details>"), "evidence block closes")
    ev_lines = [ln for ln in ev.splitlines() if ln.startswith("- ")]
    check(ev_lines[0] == "- [00:00:05] Bob_Example: Morning everyone. Alice, can you send the vendor report "
          "to me by Thursday so I can review it before the board sync?", f"evidence verbatim: {ev_lines[0]}")
    check(ev_lines[4] == f"- [00:01:30] Alice_Example: {LONG_TURN} {LONG_TURN_TAIL}",
          "continuation line merged into the long turn's evidence")
    check(len(ev_lines) == 6 and "[00:01:20]" not in ev, "SKIP turns are not evidence")
    check(md.endswith("</details>\n"), "markdown ends with a newline")

    # -- the roll-up parser reads it the way the graph expects
    parsed = workspace.parse_bullets_with_sections(md)
    check([o for _, o, _, _ in parsed] == ["Alice_Example"] * 11, "every bullet's bold prefix is the owner")
    check([s for _, _, _, s in parsed] == ["Asks from leadership"] * 2 + ["Asks from team"] * 2
          + ["Alice's own commitments"] * 4 + ["Inferred next steps"] * 3, "sections strip 'N. ' and '(...)'")
    check(all(re.match(r"^\[(?:Bob|Carol|SPEAKER_03|Alice) \d\d:\d\d:\d\d\] |\[inferred\] ", t)
              for _, _, t, _ in parsed), "every bullet text starts with its [Name HH:MM:SS] tag")

    # -- overrides on draft()
    stub.requests.clear()
    md2, stats2 = ai.draft(TRANSCRIPT, "m", cfg, model="other-stub", ollama=stub.url + "/")
    check(stats2["model"] == "other-stub" and all(r["model"] == "other-stub" for r in stub.requests),
          "model override reaches every call")
    check("by `other-stub`" in md2, "note line names the model used")


def test_num_predict(stub: StubOllama, tmp: Path) -> None:
    """[summarizer] num_predict reaches options.num_predict on every request;
    unset falls back to the DEFAULT_NUM_PREDICT module constant (issue: an
    uncapped Ollama generation could run until the 900s per-call timeout)."""
    ws = tmp / "ws-num-predict"
    ws.mkdir()
    write_config(ws, stub.url, num_predict=256)
    cfg = wsconfig.load_config(ws)
    check(cfg["summarizer"]["num_predict"] == 256, "num_predict read from whosaid.toml")

    stub.requests.clear()
    md, stats = ai.draft(TRANSCRIPT, "m", cfg)
    check(bool(stub.requests) and all(r["options"]["num_predict"] == 256 for r in stub.requests),
          f"configured num_predict reaches every request's options: "
          f"{[r['options'] for r in stub.requests]}")

    ws2 = tmp / "ws-num-predict-default"
    ws2.mkdir()
    write_config(ws2, stub.url)
    cfg2 = wsconfig.load_config(ws2)
    check(cfg2["summarizer"]["num_predict"] == ai.DEFAULT_NUM_PREDICT,
          f"num_predict defaults to DEFAULT_NUM_PREDICT ({ai.DEFAULT_NUM_PREDICT}) when unset")
    stub.requests.clear()
    ai.draft(TRANSCRIPT, "m", cfg2)
    check(bool(stub.requests) and all(r["options"]["num_predict"] == ai.DEFAULT_NUM_PREDICT
                                      for r in stub.requests),
          "an unset num_predict reaches every request as the default")


def test_think(stub: StubOllama, tmp: Path) -> None:
    """Ollama's top-level "think" flag goes out on every request (false unless
    [summarizer] think = true), reasoning left inline in content is stripped
    before parsing, and a model with no thinking mode gets an actionable hint."""
    ws = tmp / "ws-think-default"
    ws.mkdir()
    write_config(ws, stub.url)
    cfg = wsconfig.load_config(ws)
    check(cfg["summarizer"]["think"] is False and ai.Plan(cfg).think is False,
          "think defaults to false when unset")
    stub.requests.clear()
    md, stats = ai.draft(TRANSCRIPT, "m", cfg)
    check(bool(stub.requests) and all(r.get("think") is False for r in stub.requests),
          f"every request sends think=false by default: {[r.get('think') for r in stub.requests]}")

    ws2 = tmp / "ws-think-on"
    ws2.mkdir()
    write_config(ws2, stub.url, think=True)
    stub.requests.clear()
    ai.draft(TRANSCRIPT, "m", wsconfig.load_config(ws2))
    check(bool(stub.requests) and all(r.get("think") is True for r in stub.requests),
          "[summarizer] think = true reaches every request")
    for raw, want in (("yes", True), ("off", False), ("0", False), ("maybe", False), (None, False)):
        check(ai.config_flag(raw, False) is want, f"config_flag({raw!r}) -> {want}")

    # per model: exact name, then the name without its tag, then "default"
    ws_pm = tmp / "ws-think-per-model"
    ws_pm.mkdir()
    write_config(ws_pm, stub.url, think='{ "qwen-stub" = true, "qwen3" = true, default = false }')
    cfg_pm = wsconfig.load_config(ws_pm)
    stub.requests.clear()
    _md, st = ai.draft(TRANSCRIPT, "m", cfg_pm)
    check(bool(stub.requests) and all(r.get("think") is True for r in stub.requests) and st["think"] is True,
          "a per-model think table turns thinking on for the model it names (and stats say so)")
    stub.requests.clear()
    ai.draft(TRANSCRIPT, "m", cfg_pm, model="no-think-stub")
    check(bool(stub.requests) and all(r.get("think") is False for r in stub.requests),
          "a model the table does not name gets the table's default")
    table = {"qwen3": True, "qwen3.8:27b": False, "llama3:latest": True, "default": False}
    for model, want in (("qwen3:14b", True), ("qwen3.8:27b", False), ("qwen3.8:9b", False),
                        ("llama3", True), ("mistral", False)):
        check(ai.think_for(table, model) is want, f"think_for(table, {model!r}) -> {want}")
    check(ai.think_for({"llama3": True}, "llama3:latest") is True, "':latest' also matches the bare name")
    check(ai.think_for({}, "x", default=True) is True and ai.think_for(True, "x") is True,
          "an empty table falls back to the default; a bare bool applies to every model")

    ws3 = tmp / "ws-think-inline"
    ws3.mkdir()
    write_config(ws3, stub.url, model="inline-think-stub")
    md3, stats3 = ai.draft(TRANSCRIPT, "m", wsconfig.load_config(ws3))
    check("Leaked reasoning" not in md3 and stats3["items"] == stats["items"],
          f"inline <think> blocks are stripped before parsing ({stats3['items']} vs {stats['items']} items)")
    for raw, want in (("<think>x</think>\nSKIP", "SKIP"),
                      ("<THINK>a\nb</THINK> - **Do it.**", "- **Do it.**"),
                      ("<think>cut off by num_predict", ""),
                      ("reasoning with no opening tag</think>\nNONE", "NONE"),
                      ("- **No thinking here.**", "- **No thinking here.**")):
        check(ai.strip_thinking(raw) == want, f"strip_thinking({raw!r}) -> {want!r}")

    try:
        ai.Ollama(stub.url, "no-think-stub", 4096, think=True).chat("s", "u")
        check(False, "think=true on a model without thinking raises")
    except ai.SummarizerError as e:
        check("HTTP 400" in str(e) and "think = false" in e.hint,
              f"think=true on a model without thinking says how to fix it: {e} / {e.hint}")
    out = ai.Ollama(stub.url, "no-think-stub", 4096).chat(
        "Select every turn", "Meeting: m\n\nTranscript:\n#3 [00:00:01] Bob: hi")
    check(out.startswith("#3"), "the default think=false is accepted by a model without thinking")


def test_infer_failure(stub: StubOllama, tmp: Path) -> None:
    """A SummarizerError from the INFER call (issue #14 follow-up: a runaway,
    sampled INFER generation used to hang for 900s and then discard the whole
    draft) must not discard the already-drafted bullets: INFER is best-effort."""
    ws = tmp / "ws-infer-fail"
    ws.mkdir()
    write_config(ws, stub.url)
    cfg = wsconfig.load_config(ws)

    stub.requests.clear()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        md, stats = ai.draft(TRANSCRIPT, "m", cfg, model="infer-fail-stub")
    check("WARN INFER step failed" in err.getvalue() and "HTTP 500" in err.getvalue(),
          f"the INFER failure is logged: {err.getvalue()!r}")
    check(stats["items"] == 8, f"all 8 bullets still drafted despite the INFER failure: {stats}")
    check(stats["inferred"] == 0, f"zero inferred items when INFER fails: {stats}")
    check(stats.get("infer_error") is not None and "HTTP 500" in stats["infer_error"],
          f"the INFER failure is recorded in stats: {stats.get('infer_error')!r}")
    check(section(md, "## 1.") and section(md, "## 2.") and section(md, "## 4."),
          "the asks/commitments sections are unaffected by the INFER failure")
    check(section(md, "## 5.") == ["- none"], "the inferred section is empty, not corrupted")
    note = md.splitlines()[4]
    check("Inferred next steps skipped" in note and "HTTP 500" in note,
          f"a short visible note about the INFER failure is in the note line: {note}")

    # a matched control: the same fixture with a working INFER call gets inferred items
    stub.requests.clear()
    md_ok, stats_ok = ai.draft(TRANSCRIPT, "m", cfg)
    check(stats_ok["inferred"] == 3 and "infer_error" not in stats_ok,
          f"the control run (INFER working) is unaffected: {stats_ok}")
    check("Inferred next steps skipped" not in md_ok, "no failure note when INFER succeeds")


def test_no_groups_and_no_owner(stub: StubOllama, tmp: Path) -> None:
    ws = tmp / "ws-nogroups"
    ws.mkdir()
    write_config(ws, stub.url, groups=False, aliases=False)
    cfg = wsconfig.load_config(ws)
    check(cfg["groups"] == {}, "no groups configured")
    md, stats = ai.draft(TRANSCRIPT, "m", cfg)
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    check(heads == ["## 1. Asks of Alice", "## 2. Alice's own commitments", "## 3. Inferred next steps"],
          f"no-groups headings: {heads}")
    check(stats["naming_owner"] == 2, "default alias is the owner's first name (Alicia no longer matches)")
    asks = section(md, "## 1.")
    check([ln.split("] ")[0] + "]" for ln in asks] == ["- **Alice_Example** [Bob 00:00:05]",
                                                        "- **Alice_Example** [Carol 00:00:40]",
                                                        "- **Alice_Example** [SPEAKER_03 00:01:00]"],
          f"every other speaker's ask lands in 'Asks of Alice': {asks}")
    check(len(section(md, "## 2.")) == 4, "own commitments unchanged without groups")

    ws2 = tmp / "ws-noowner"
    ws2.mkdir()
    write_config(ws2, stub.url, owner="", groups=True)
    cfg2 = wsconfig.load_config(ws2)
    stub.requests.clear()
    md, stats = ai.draft(TRANSCRIPT, "m", cfg2)
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    check(heads == ["## 1. Asks", "## 2. Commitments", "## 3. Inferred next steps"], f"no-owner headings: {heads}")
    check(stats["by_owner"] == 0 and stats["naming_owner"] == 0 and stats["model_added"] == 3
          and stats["candidates"] == 3, f"no owner: only the model selects: {stats}")
    selects = stub.of_kind("Select every turn")
    check(any("Alice_Example:" in r["messages"][1]["content"] for r in selects),
          "without an owner every speaker's turns are in the SELECT slices")
    check(section(md, "## 1.") == [
        '- **Bob_Example** [Bob 00:00:05] Send the vendor report to Bob by Thursday. Bob asks Alice. '
        '"send the vendor report to me by Thursday"',
        '- **Bob_Example** [Bob 00:01:20] Own the retro notes. Bob asks Carol. "you own the retro notes this time"',
    ], f"no-owner asks carry the speaker's label: {section(md, '## 1.')}")
    check(section(md, "## 2.") == [
        '- **Alice_Example** [Alice 00:00:20] Send the vendor report on Wednesday. Alice commits. '
        '"I will send the vendor report on Wednesday afternoon"',
    ], f"no-owner commitments: {section(md, '## 2.')}")
    check(section(md, "## 3.")[0] == "- [inferred] Confirm the board sync agenda with Bob.",
          "no-owner inferred lines have no bold prefix")
    note = md.splitlines()[4]
    check(re.search(r": 3 candidate turns \(all added by the model over \d+ slices?\), 3 kept as evidence, "
                    r"3 items drafted, 0 flagged ⚠\.", note) is not None, f"no-owner note: {note}")

    md, stats = ai.draft("# Speakers (0):\n\n", "empty", cfg)
    check("## " not in md and "empty transcript, nothing to extract" in md and stats["items"] == 0,
          "an empty transcript yields a header-only draft without model calls")


def test_failures_and_cli(stub: StubOllama, tmp: Path) -> None:
    ws = tmp / "ws-cli"
    meeting = ws / "2026-09-17-0800"
    meeting.mkdir(parents=True)
    tr = meeting / "meeting.speakers.txt"
    tr.write_text(TRANSCRIPT)
    write_config(ws, stub.url)
    cfg = wsconfig.load_config(ws)

    dead = dead_port_url()
    try:
        ai.draft(TRANSCRIPT, "m", cfg, ollama=dead)
        check(False, "draft() must raise when Ollama is unreachable")
    except ai.SummarizerError as e:
        check("cannot reach Ollama" in str(e) and "ollama pull qwen-stub" in e.hint, f"unreachable error: {e} / {e.hint}")
    try:
        ai.draft(TRANSCRIPT, "m", cfg, model="missing-model")
        check(False, "draft() must raise on HTTP 404")
    except ai.SummarizerError as e:
        check("HTTP 404" in str(e) and "ollama pull missing-model" in e.hint, f"404 error: {e} / {e.hint}")

    py = [sys.executable, str(REPO_DIR / "lib" / "action_items.py")]
    env = clean_env()

    out = tmp / "cli-out.md"
    r = subprocess.run(py + ["--transcript", str(tr), "--out", str(out)], env=env,
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout == "", f"CLI --out exits 0 and prints nothing: {r.stderr}")
    text = out.read_text()
    check(text.startswith("# Action items — 2026-09-17-0800\n") and "## 1. Asks from leadership (Bob)" in text,
          "CLI finds whosaid.toml via the transcript's parent's parent")
    check("action items (qwen-stub): 7 candidate turns, 8 items, 2 flagged" in r.stderr, f"CLI log: {r.stderr}")

    r = subprocess.run(py + ["--transcript", str(tr), "--ws", str(ws), "--select-only"], env=env,
                       capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.startswith("7 candidate turns of 10 (2 by owner, 3 naming the owner, "
                                                    "2 added by the model over "), f"--select-only: {r.stdout}")
    check("- #2 named+model [00:00:40] Carol_Example:" in r.stdout and "- #3 model" in r.stdout
          and "- #6 owner" in r.stdout and "- #8 named" in r.stdout and "- #4 " not in r.stdout,
          f"--select-only tags: {r.stdout}")

    r = subprocess.run(py + ["--transcript", str(tr), "--ollama", dead], env=env, capture_output=True, text=True)
    check(r.returncode == 0, f"Ollama down: exit 0 without --strict (got {r.returncode})")
    check(r.stdout.startswith("# Action items — 2026-09-17-0800\n\nSpeakers in this meeting: Alice_Example")
          and "## " not in r.stdout, "stub has the header and no sections")
    check("_No action items extracted (model `qwen-stub`): cannot reach Ollama at " in r.stdout
          and "`ollama serve`" in r.stdout and "`ollama pull qwen-stub`" in r.stdout
          and "then re-run `whosaid action-items --engine ollama`._" in r.stdout, f"stub text: {r.stdout}")
    check("WARN cannot reach Ollama" in r.stderr, "stub reason is logged")
    r = subprocess.run(py + ["--transcript", str(tr), "--ollama", dead, "--strict"], env=env,
                       capture_output=True, text=True)
    check(r.returncode == 3 and r.stdout.startswith("# Action items"), "--strict exits 3 and still prints the stub")
    r = subprocess.run(py + ["--transcript", str(tr), "--model", "missing-model"], env=env,
                       capture_output=True, text=True)
    check(r.returncode == 0 and "HTTP 404" in r.stdout and "`ollama pull missing-model`" in r.stdout,
          f"missing model -> stub with a pull hint: {r.stdout}")
    r = subprocess.run(py + ["--transcript", str(tmp / "nope.txt")], env=env, capture_output=True, text=True)
    check(r.returncode == 1 and "transcript not found" in r.stderr, "missing transcript exits 1")
    r = subprocess.run(py + ["--transcript", str(tr), "--owner", "Bob_Example", "--out", str(out)], env=env,
                       capture_output=True, text=True)
    check(r.returncode == 0 and "## 1. Asks from leadership (nobody yet)" in out.read_text()
          and "## 3. Bob's own commitments" in out.read_text(), "--owner overrides the config owner")
    env_owner = dict(env, WHOSAID_OWNER="", WHOSAID_SUMMARIZER_MODEL="env-stub")
    r = subprocess.run(py + ["--transcript", str(tr)], env=env_owner, capture_output=True, text=True)
    check(r.returncode == 0 and "by `env-stub`" in r.stdout and "## 1. Asks from leadership (Bob)" in r.stdout,
          "WHOSAID_SUMMARIZER_MODEL applies through wsconfig")


def test_workspace_integration(stub: StubOllama, tmp: Path) -> None:
    ws = tmp / "ws-int"
    meeting = ws / "2026-09-16-0703"
    meeting.mkdir(parents=True)
    tr = meeting / "meeting.speakers.txt"
    tr.write_text(TRANSCRIPT)
    write_config(ws, stub.url)
    for k in ("WHOSAID_ACTION_ITEMS_HOOK", "WHOSAID_OLLAMA", "WHOSAID_OWNER", "WHOSAID_SUMMARIZER_MODEL"):
        os.environ.pop(k, None)

    def run(argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = workspace.main(argv)
        return rc, err.getvalue()

    json_out = meeting / "action-items.json"
    rc, err = run(["action-items", "--transcript", str(tr), "--engine", "ollama", "--json-out", str(json_out)])
    check(rc == 0, "action-items --engine ollama exits 0")
    check(f"action items (ollama:qwen-stub) -> {meeting / 'action-items.md'}" in err, f"engine log: {err}")
    md = (meeting / "action-items.md").read_text()
    check("## 4. Alice's own commitments" in md and "<details>" in md, "engine ollama wrote the sectioned draft")
    payload = json.loads(json_out.read_text())
    check(payload["source"] == "ollama" and payload["engine"] == "ollama:qwen-stub"
          and payload["stats"]["items"] == 8, f"--json-out carries engine + stats: {payload['engine']}")
    check(len(payload["items"]) == 11 and payload["items"][0]["section"] == "Asks from leadership"
          and payload["items"][-1]["section"] == "Inferred next steps", "json items carry their section")

    rc, err = run(["action-items", "--transcript", str(tr), "--md-out", str(tmp / "auto.md")])
    check(rc == 0 and "action items (ollama:qwen-stub)" in err, f"engine auto picks ollama when it answers: {err}")
    rc, err = run(["action-items", "--transcript", str(tr), "--md-out", str(tmp / "none.md"), "--engine", "none"])
    check(rc == 0 and "action items (skeleton)" in err and "No summarizer hook" in (tmp / "none.md").read_text(),
          "engine none writes the skeleton")
    other = tmp / "ws-dead"
    other.mkdir()
    write_config(other, dead_port_url())
    rc, err = run(["action-items", "--transcript", str(tr), "--md-out", str(tmp / "ws2.md"), "--engine", "ollama",
                   "--ws", str(other)])
    check(rc == 0 and "engine ollama failed (SummarizerError: cannot reach Ollama" in err
          and "action items (skeleton)" in err and "No summarizer hook" in (tmp / "ws2.md").read_text(),
          f"--ws selects that workspace's config (dead Ollama there) -> skeleton + WARN, exit 0: {err}")
    os.environ["WHOSAID_OLLAMA"] = dead_port_url()
    rc, err = run(["action-items", "--transcript", str(tr), "--md-out", str(tmp / "auto2.md")])
    os.environ.pop("WHOSAID_OLLAMA")
    check(rc == 0 and "engine auto: no hook set and Ollama at" in err and "action items (skeleton)" in err,
          f"engine auto falls back to the skeleton when Ollama is down: {err}")

    rc, err = run(["rollup", str(ws), "--action-items"])
    check(rc == 0, f"rollup exits 0: {err}")
    corpus = json.loads((ws / "_action-items.json").read_text())
    items = corpus["items"]
    check(len(items) == 11 and corpus["next_id"] == 12, f"11 bullets fold into 11 items: {len(items)}")
    types = [it["type"] for it in items]
    check(types == ["Asks from leadership"] * 2 + ["Asks from team"] * 2 + ["Alice's own commitments"] * 4
          + ["Inferred next steps"] * 3, f"item types come from the sections: {types}")
    check(all(it["owner"] == "Alice_Example" for it in items), "the bold prefix is the corpus owner")
    check(not any(it["text"].startswith("[00:") for it in items), "evidence turns are not folded")
    check(not any(it["text"] == "none" for it in items), "'- none' placeholders are not folded")
    ai_md = (ws / "_ACTION-ITEMS.md").read_text()
    check("- **AI-001** [open] (Asks from leadership) 2026-09-16-0703 (1×): [Bob 00:00:05] Send the vendor "
          "report to Bob by Thursday." in ai_md, "rendered corpus line shows the section as the type")
    check("[inferred] Confirm the board sync agenda with Bob." in ai_md, "inferred items reach the corpus")

    # a bullet under no heading, and a hand-typed item, keep the existing semantics
    parsed = workspace.parse_bullets_with_sections(
        "# T\n\n- **Bob:** first\n\n## 2. Peer asks (Carol, Dan)\n- **Bob:** second\n- none\n"
        "<details><summary>x</summary>\n\n- [00:00:01] Bob: evidence\n\n</details>\n### Sub (x)\n- third\n")
    check(parsed == [(3, "Bob", "first", ""), (6, "Bob", "second", "Peer asks"), (14, "", "third", "Sub")],
          f"parse_bullets_with_sections: {parsed}")
    check(workspace.parse_bullets("- none\n- **A** b") == [(1, "", "none"), (2, "A", "b")],
          "parse_bullets itself is unchanged")


def test_hook_env_roles(tmp: Path) -> None:
    """The external --hook engine gets WHOSAID_ROLES (from '# Role:' headers),
    the same context the commitments hook receives (GitHub issue #27), alongside
    WHOSAID_SPEAKERS and WHOSAID_TRANSCRIPT_PATH."""
    for k in ("WHOSAID_ACTION_ITEMS_HOOK", "WHOSAID_OWNER"):
        os.environ.pop(k, None)
    ws = tmp / "ws-hook-roles"
    meeting = ws / "2026-09-19-0900"
    meeting.mkdir(parents=True)
    tr = meeting / "meeting.speakers.txt"
    tr.write_text(
        "# Speakers (3): Alice_Example, Bob_Example, Carol_Example\n"
        "# Role: Alice_Example = self\n"
        "# Role: Bob_Example = boss\n"
        "# Role: Carol_Example = peer\n\n"
        "[00:00:01] Bob_Example: Alice, send the vendor report.\n"
        "[00:00:05] Alice_Example: I'll send it by Thursday.\n"
    )
    # The hook echoes what it was handed; run_hook captures stdout as the markdown.
    hook = ('printf "ROLES=%s\\nSPEAKERS=%s\\nHASPATH=%s\\n" '
            '"$WHOSAID_ROLES" "$WHOSAID_SPEAKERS" '
            '"$([ -f "$WHOSAID_TRANSCRIPT_PATH" ] && echo yes || echo no)"')

    def run(argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = workspace.main(argv)
        return rc, err.getvalue()

    out = tmp / "hook-roles.md"
    rc, err = run(["action-items", "--transcript", str(tr), "--engine", "hook",
                   "--hook", hook, "--md-out", str(out)])
    got = out.read_text()
    check(rc == 0 and "action items (hook)" in err, f"hook engine exits 0: {err}")
    check('ROLES={"Alice_Example":"self","Bob_Example":"boss","Carol_Example":"peer"}' in got,
          f"hook receives WHOSAID_ROLES from the '# Role:' headers: {got!r}")
    check("SPEAKERS=Alice_Example,Bob_Example,Carol_Example" in got, f"hook still gets speakers: {got!r}")
    check("HASPATH=yes" in got, f"hook still gets a readable transcript path: {got!r}")

    # No roles declared -> an empty JSON object still reaches the hook.
    tr2 = meeting / "noroles.speakers.txt"
    tr2.write_text("# Speakers (1): Alice_Example\n\n[00:00:01] Alice_Example: I'll follow up.\n")
    out2 = tmp / "hook-noroles.md"
    rc, _ = run(["action-items", "--transcript", str(tr2), "--engine", "hook",
                 "--hook", hook, "--md-out", str(out2)])
    check(rc == 0 and "ROLES={}" in out2.read_text(), "no '# Role:' headers -> WHOSAID_ROLES is an empty object")


def main() -> None:
    stub = StubOllama()
    try:
        check(wsconfig.ollama_up(stub.url), "stub answers /api/tags")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            test_name_regex_and_pieces()
            test_owner_mode(stub, tmp)
            test_team_directives(stub, tmp)
            test_num_predict(stub, tmp)
            test_think(stub, tmp)
            test_infer_failure(stub, tmp)
            test_no_groups_and_no_owner(stub, tmp)
            test_failures_and_cli(stub, tmp)
            test_workspace_integration(stub, tmp)
            test_hook_env_roles(tmp)
    finally:
        stub.close()
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()

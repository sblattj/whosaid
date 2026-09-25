#!/usr/bin/env python3
"""
claude_engine_test.py: the opt-in claude engine (lib/claude_engine.py, issue #44)
against a fake `claude` binary. No network, no model, no real CLI.

Covers: the isolated argv and scrubbed env, the reply parsing, quote
verification (speaker and time taken from the turn that holds the quote, ⚠ when
none does), the section mapping and the guards (directive from an identified
non-leader or on a "Name, ..." sentence dropped, from SPEAKER_NN kept),
duplicates, bad kinds and placeholders dropped, inferred capped at 3, the
no-owner layout, failures (is_error, garbage, retry), the empty transcript
(nothing sent), binary resolution, and `workspace.py action-items --engine
claude` including the fallback to a skeleton.

Run: python3 test/claude_engine_test.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "lib"))

import action_items as ai  # noqa: E402
import claude_engine as ce  # noqa: E402
import wsconfig  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    assert cond, msg


FAKE = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("FAKE_CLAUDE_LOG")
stdin = sys.stdin.read()
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin,
                             "api_key": "ANTHROPIC_API_KEY" in os.environ,
                             "auth_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
                             "leaked": sorted(k for k in os.environ if k in (
                                 "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_MODEL", "CLAUDECODE",
                                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET")),
                             "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}) + "\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "garbage":
    print("not json at all")
    sys.exit(1)
reply = open(os.environ["FAKE_CLAUDE_REPLY"]).read() if os.environ.get("FAKE_CLAUDE_REPLY") else "{}"
events = [{"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
          {"type": "result", "is_error": mode == "error",
           "result": "Invalid API key" if mode == "error" else reply}]
print(json.dumps(events))
'''

TRANSCRIPT = """# Speakers (5): Alice_Example, Bob_Example, Carol_Example, Dana_Example, SPEAKER_03
# Role: Dana_Example = boss
[00:00:05] Bob_Example: Morning. Alice, can you send the vendor report to me by Thursday?
[00:00:20] Alice_Example: Yes, I will send the vendor report on Wednesday afternoon with the cost breakdown.
[00:00:40] Carol_Example: Alice, could you refresh the dashboard data source before the demo?
[00:01:00] Bob_Example: From now on everyone links a ticket to every pull request before review.
[00:01:15] Carol_Example: Everyone, please fill in the team survey by Friday.
[00:01:30] Bob_Example: Carol, please update the team wiki with the new process.
[00:01:45] SPEAKER_03: All of you need to finish the security training by the end of the month.
[00:02:00] Dana_Example: Starting next sprint, every design needs a threat model section.
[00:02:15] Alice_Example: I'll draft the migration plan by Friday and circulate it before the review.
"""

REPLY = {
    "items": [
        {"kind": "ask", "speaker": "Bob_Example", "t": "00:00:05", "title": "Send the vendor report to Bob by Thursday",
         "context": "Bob wants it before the board sync.", "quote": "can you send the vendor report to me by Thursday"},
        # wrong speaker and time from the model: the quote's turn wins
        {"kind": "ask", "speaker": "Bob_Example", "t": "00:09:99", "title": "Refresh the dashboard data source",
         "context": "Before the demo.", "quote": "could you refresh the dashboard data source before the demo"},
        {"kind": "directive", "speaker": "Bob_Example", "t": "00:01:00", "title": "Link a ticket to every pull request",
         "context": "New team rule.", "quote": "everyone links a ticket to every pull request"},
        {"kind": "directive", "speaker": "Carol_Example", "t": "00:01:15", "title": "Fill in the team survey",
         "context": "Carol asks everyone.", "quote": "please fill in the team survey by Friday"},
        {"kind": "directive", "speaker": "Bob_Example", "t": "00:01:30", "title": "Update the team wiki",
         "context": "Aimed at Carol.", "quote": "please update the team wiki with the new process"},
        {"kind": "directive", "speaker": "SPEAKER_03", "t": "00:01:45", "title": "Finish the security training",
         "context": "An unidentified voice, likely leadership.", "quote": "finish the security training by the end of the month"},
        {"kind": "directive", "speaker": "Dana_Example", "t": "00:02:00", "title": "Add a threat model section to every design",
         "context": "Dana is role-tagged boss.", "quote": "every design needs a threat model section"},
        {"kind": "commit", "speaker": "Alice_Example", "t": "00:00:20", "title": "Send the vendor report on Wednesday",
         "context": "With the cost breakdown.", "quote": "I will send the vendor report on Wednesday afternoon"},
        {"kind": "commit", "speaker": "Alice_Example", "t": "00:02:15", "title": "Draft the migration plan by Friday",
         "context": "Circulate before the review.", "quote": "I will write the migration plan tonight"},
        {"kind": "commit", "speaker": "Alice_Example", "t": "00:02:15", "title": "Draft the migration plan by Friday!",
         "context": "A repeat.", "quote": "I'll draft the migration plan by Friday"},
        {"kind": "status", "speaker": "Bob_Example", "t": "00:00:05", "title": "Not a kind we know",
         "context": "", "quote": "Morning"},
        {"kind": "ask", "speaker": "Bob_Example", "t": "00:00:05", "title": "<imperative: what Alice must do>",
         "context": "", "quote": ""},
    ],
    "inferred": ["Confirm the board sync agenda with Bob.", "Book the demo room.",
                 "Tell finance the report is coming.", "A fourth that must be cut."],
}


def write_config(ws: Path, *, owner: str = "Alice_Example", groups: bool = True,
                 fallback: str = "none", extra: str = "") -> None:
    lines = ["[workspace]", f'owner = "{owner}"'] if owner else ["[workspace]"]
    if groups:
        lines += ["", "[groups]", 'leadership = ["Bob_Example"]', 'team = ["Carol_Example"]']
    lines += ["", "[summarizer]", 'engine = "claude"', f'fallback = "{fallback}"', extra,
              "", "[search]", f'ollama = "{dead_url()}"', ""]
    (ws / "whosaid.toml").write_text("\n".join(lines))


def dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def section(md: str, prefix: str) -> list[str]:
    out, on = [], False
    for line in md.splitlines():
        if line.startswith("## "):
            on = line.startswith(prefix)
            continue
        if line.startswith("<details"):
            on = False
        if on and line.startswith("- "):
            out.append(line)
    return out


def set_env(tmp: Path, reply: dict | str | None, mode: str = "ok") -> Path:
    log = tmp / "calls.jsonl"
    log.write_text("")
    rp = tmp / "reply.txt"
    rp.write_text(reply if isinstance(reply, str) else json.dumps(reply or {}))
    os.environ.update({"FAKE_CLAUDE_LOG": str(log), "FAKE_CLAUDE_REPLY": str(rp), "FAKE_CLAUDE_MODE": mode,
                       ce.BIN_ENV: str(tmp / "claude")})
    return log


def calls(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]


def test_owner_mode(tmp: Path) -> None:
    ws = tmp / "ws"
    ws.mkdir()
    write_config(ws)
    cfg = wsconfig.load_config(ws)
    log = set_env(tmp, REPLY)
    os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-leak"
    parent = {"ANTHROPIC_AUTH_TOKEN": "tok-should-not-leak", "ANTHROPIC_DEFAULT_OPUS_MODEL": "remapped",
              "ANTHROPIC_MODEL": "remapped", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent",
              "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/parent.sock", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-ok"}
    saved = {k: os.environ.get(k) for k in parent}
    os.environ.update(parent)
    try:
        md, stats = ce.draft(TRANSCRIPT, "2026-09-24-0700", cfg)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY")
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- the one call
    c = calls(log)
    check(len(c) == 1, f"one claude call per meeting, got {len(c)}")
    argv = c[0]["argv"]
    check(argv[:1] == ["-p"] and argv[argv.index("--model") + 1] == "opus", f"-p and the default model: {argv}")
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        check(flag in argv, f"isolation flag {flag}")
    check(argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == "",
          "no tools and no settings sources")
    check(argv[argv.index("--output-format") + 1] == "json", "JSON output")
    sysprompt = argv[argv.index("--system-prompt") + 1]
    check("- directive: a LEADERSHIP speaker (Bob_Example, Dana_Example)" in sysprompt,
          "the system prompt names leadership, boss roles included")
    check(not c[0]["api_key"] and not c[0]["auth_token"], "API-key env vars are scrubbed")
    check(c[0]["leaked"] == [], f"model-alias and parent-session vars are scrubbed: {c[0]['leaked']}")
    check(c[0]["oauth"] == "oauth-ok", "the OAuth token passes through")
    check(c[0]["cwd"] != os.getcwd() and "whosaid-claude-" in c[0]["cwd"], "runs in an empty temp dir")
    check(not Path(c[0]["cwd"]).exists(), "the temp dir is removed")
    check("Meeting: 2026-09-24-0700" in c[0]["stdin"] and "[00:02:15] Alice_Example:" in c[0]["stdin"]
          and "- LEADERSHIP: Bob_Example." in c[0]["stdin"], "stdin: roster, meeting, whole transcript")

    # -- layout and bullets
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    check(heads == ["## 1. Asks from leadership (Bob)", "## 2. Asks from team (Carol)",
                    "## 3. Team directives from leadership (Bob, Dana)", "## 4. Alice's own commitments",
                    "## 5. Inferred next steps"], f"the local engine's layout: {heads}")
    check(section(md, "## 1.") == [
        '- **Alice_Example** [Bob 00:00:05] Send the vendor report to Bob by Thursday. Bob wants it before '
        'the board sync. "can you send the vendor report to me by Thursday"'], f"leadership: {section(md, '## 1.')}")
    check(section(md, "## 2.") == [
        '- **Alice_Example** [Carol 00:00:40] Refresh the dashboard data source. Before the demo. '
        '"could you refresh the dashboard data source before the demo"'],
        f"speaker and time come from the quote's turn, not the model: {section(md, '## 2.')}")
    direc = section(md, "## 3.")
    check([ln.split("] ")[0] for ln in direc] == ["- **Alice_Example** [Bob 00:01:00",
                                                 "- **Alice_Example** [SPEAKER_03 00:01:45",
                                                 "- **Alice_Example** [Dana 00:02:00"],
          f"directives kept: leadership, an unidentified voice, a boss role: {direc}")
    body = md.split("<details>")[0]
    check("survey" not in body, "a directive from an identified non-leader is dropped")
    check("wiki" not in body, "a directive on a 'Carol, ...' sentence is dropped")
    own = section(md, "## 4.")
    check(own == [
        '- **Alice_Example** [Alice 00:00:20] Send the vendor report on Wednesday. With the cost breakdown. '
        '"I will send the vendor report on Wednesday afternoon"',
        '- **Alice_Example** [Alice 00:02:15] Draft the migration plan by Friday. Circulate before the '
        'review. "I will write the migration plan tonight" _(⚠ quote not verbatim from that turn)_',
    ], f"commitments, a non-verbatim quote flagged at the stated turn, the repeat dropped: {own}")
    check(len(section(md, "## 5.")) == 3 and "fourth" not in md, "inferred capped at 3")
    check("Not a kind we know" not in md and "imperative title" not in md, "bad kinds and placeholders dropped")

    note = md.splitlines()[4]
    check(note.startswith("_Auto-drafted ") and "by `claude-opus-5-5` (Claude, cloud: this transcript was "
          "sent to Anthropic) via `whosaid action-items --engine claude`" in note
          and "7 items drafted, 1 flagged ⚠" in note, f"note line: {note}")
    ev = md.split("<details>")[1]
    check("- [00:00:40] Carol_Example: Alice, could you refresh the dashboard data source before the demo?" in ev
          and "[00:02:15]" not in ev, "evidence: verified turns only, verbatim")
    check(stats["engine"] == "claude" and stats["model"] == "claude-opus-5-5" and stats["requested_model"] == "opus"
          and stats["items"] == 7 and stats["flagged"] == 1 and stats["dropped"] == 5 and stats["inferred"] == 3
          and stats["model_calls"] == 1, f"stats: {stats}")

    import workspace  # noqa: E402
    types = [s for _, _, _, s in workspace.parse_bullets_with_sections(md)]
    check(types.count("Team directives from leadership") == 3 and types.count("Inferred next steps") == 3,
          "the roll-up reads the sections the local engine would write")


def test_no_owner_and_no_leadership(tmp: Path) -> None:
    ws = tmp / "ws-noowner"
    ws.mkdir()
    write_config(ws, owner="", groups=False)
    reply = {"items": [
        {"kind": "ask", "speaker": "Bob_Example", "t": "00:01:30", "assignee": "Carol_Example",
         "title": "Update the team wiki", "context": "Bob asks Carol.", "quote": "please update the team wiki"},
        {"kind": "commit", "speaker": "Alice_Example", "t": "00:02:15", "title": "Draft the migration plan",
         "context": "", "quote": "I'll draft the migration plan by Friday"},
        {"kind": "directive", "speaker": "Bob_Example", "t": "00:01:00", "title": "Link tickets",
         "context": "", "quote": "everyone links a ticket to every pull request"}], "inferred": []}
    log = set_env(tmp, reply)
    md, stats = ce.draft(TRANSCRIPT, "m", wsconfig.load_config(ws))
    sysprompt = calls(log)[0]["argv"][calls(log)[0]["argv"].index("--system-prompt") + 1]
    check("directive" not in sysprompt and '"assignee"' in sysprompt, "no-owner prompt: asks and commits, assignee")
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    check(heads == ["## 1. Asks", "## 2. Commitments", "## 3. Inferred next steps"], f"no-owner layout: {heads}")
    check(section(md, "## 1.") == ['- **Carol_Example** [Bob 00:01:30] Update the team wiki. Bob asks Carol. '
                                   '"please update the team wiki"'], "the assignee is the bold label")
    check(section(md, "## 2.")[0].startswith("- **Alice_Example** [Alice 00:02:15]"), "a commitment is the speaker's")
    check("Link tickets" not in md and stats["dropped"] == 1, "no owner: no directive section, directive dropped")

    ws2 = tmp / "ws-nolead"
    ws2.mkdir()
    write_config(ws2, groups=False)
    set_env(tmp, {"items": [], "inferred": []})
    plain = TRANSCRIPT.replace("# Role: Dana_Example = boss\n", "")
    md2, _ = ce.draft(plain, "m", wsconfig.load_config(ws2))
    check("Team directives" not in md2 and "## 1. Asks of Alice\n- none" in md2,
          "no leadership: no directive section, empty sections render '- none'")


def test_failures(tmp: Path) -> None:
    ws = tmp / "ws-fail"
    ws.mkdir()
    write_config(ws)
    cfg = wsconfig.load_config(ws)
    set_env(tmp, None, mode="error")
    try:
        ce.draft(TRANSCRIPT, "m", cfg)
        check(False, "is_error must raise")
    except ai.SummarizerError as e:
        check("returned an error" in str(e) and "claude setup-token" in e.hint, f"is_error -> error + hint: {e}")
    log = set_env(tmp, None, mode="garbage")
    try:
        ce.draft(TRANSCRIPT, "m", cfg)
        check(False, "garbage must raise")
    except ai.SummarizerError as e:
        check("failed twice" in str(e) and len(calls(log)) == 2, f"a transient failure is retried once: {e}")
    set_env(tmp, "Sorry, I can't help with that.")
    try:
        ce.draft(TRANSCRIPT, "m", cfg)
        check(False, "a prose reply must raise")
    except ai.SummarizerError as e:
        check("no JSON object" in str(e), f"a prose reply -> error: {e}")
    log = set_env(tmp, REPLY)
    md, stats = ce.draft("# Speakers (0):\n", "m", cfg)
    check(calls(log) == [] and "nothing was sent" in md and stats["model_calls"] == 0,
          "an empty transcript sends nothing")

    os.environ[ce.BIN_ENV] = str(tmp / "missing-claude")
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = str(tmp / "empty-bin")
    try:
        check(ce.resolve_bin(str(tmp / "claude")) == str(tmp / "claude"), "a configured binary wins")
        found = ce.resolve_bin("")
        check(found == "" or found in [str(Path(p).expanduser()) for p in ce.BIN_CANDIDATES],
              f"else the env, PATH, then the common install paths: {found!r}")
    finally:
        os.environ["PATH"] = saved


def test_workspace_cli(tmp: Path) -> None:
    ws = tmp / "ws-cli"
    meeting = ws / "2026-09-24-0700"
    meeting.mkdir(parents=True)
    (meeting / "transcript.speakers.txt").write_text(TRANSCRIPT)
    write_config(ws, fallback="ollama")
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHOSAID_")}
    env.update({ce.BIN_ENV: str(tmp / "claude"), "PYTHONDONTWRITEBYTECODE": "1"})
    set_env(tmp, REPLY)
    env.update({k: os.environ[k] for k in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_REPLY", "FAKE_CLAUDE_MODE")})
    md_out, js_out = meeting / "action-items.md", meeting / "action-items.json"
    argv = [sys.executable, str(REPO_DIR / "lib" / "workspace.py"), "action-items",
            "--transcript", str(meeting / "transcript.speakers.txt"), "--md-out", str(md_out),
            "--json-out", str(js_out)]
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    payload = json.loads(js_out.read_text())
    check(r.returncode == 0 and "action items (claude:claude-opus-5-5)" in r.stderr, f"config engine claude: {r.stderr}")
    check(payload["source"] == "claude" and payload["engine"] == "claude:claude-opus-5-5"
          and payload["stats"]["items"] == 7 and len(payload["items"]) == 10,
          f"JSON mirror: {payload['source']} {payload['engine']} {len(payload['items'])}")

    env["FAKE_CLAUDE_MODE"] = "error"
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    check(r.returncode == 0 and "WARN action-items engine claude failed" in r.stderr
          and "fallback is off or Ollama is down" in r.stderr and "action items (skeleton)" in r.stderr,
          f"claude fails, Ollama down -> skeleton, exit 0: {r.stderr}")
    check(json.loads(js_out.read_text())["source"] == "skeleton", "skeleton recorded as the source")

    env["FAKE_CLAUDE_MODE"] = "ok"
    r = subprocess.run(argv + ["--engine", "auto"], capture_output=True, text=True, env=env)
    check("claude" not in json.loads(js_out.read_text())["engine"], "auto never picks claude")


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fake = tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        (tmp / "empty-bin").mkdir()
        saved = {k: os.environ.get(k) for k in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_REPLY", "FAKE_CLAUDE_MODE",
                                                 ce.BIN_ENV)}
        try:
            test_owner_mode(tmp)
            test_no_owner_and_no_leadership(tmp)
            test_failures(tmp)
            test_workspace_cli(tmp)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
claude_engine.py: the opt-in cloud action-items engine (issue #44).

`[summarizer] engine = "claude"` (or `action-items --engine claude`) drafts a
meeting's action items with ONE isolated `claude -p` call that reads the whole
transcript. THE TRANSCRIPT TEXT GOES TO ANTHROPIC: that is the whole point of
the opt-in, and the note line of every draft says so. `auto` never picks this
engine.

The model only reads and proposes. It answers JSON:

  {"items": [{"kind": "ask|directive|commit", "speaker": "<label>", "t": "HH:MM:SS",
              "title": "<imperative>", "context": "<one sentence>",
              "quote": "<at most 15 words, verbatim>"}],
   "inferred": ["<next step>", ...]}

and the script does the rest, with the same Plan as the local engine
(lib/action_items.py): each quote is looked up in the transcript and the
speaker and time come from the turn that holds it (a quote found in no turn is
kept and flagged ⚠); sections, labels and the bullet shape are the local
engine's, including "Team directives from leadership" (#42); at most 3 inferred
next steps; the evidence turns are appended verbatim. Guards the model cannot
override: a directive from an identified participant outside leadership, or on
a sentence that opens by naming one other participant ("Sam, can you ..."), is
dropped. A directive from an unidentified SPEAKER_NN voice is kept, since
diarization often fails to name the boss. Titles that repeat are dropped.

The call: `claude -p --system-prompt ... --model <claude_model>` with no tools,
no MCP servers, no user/project settings (so no hooks and no CLAUDE.md), no
slash commands, no session persistence, JSON output, run in an empty temp dir,
with ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN removed so the Claude Code login
pays. One retry on a transient failure.

Config ([summarizer] in whosaid.toml):
  engine = "claude"
  claude_model = "opus"          # alias or full id; stats record what it resolved to
  claude_timeout = 900           # seconds for the one call
  claude_bin = ""                # default: WHOSAID_CLAUDE_BIN, PATH, then common installs
  fallback = "ollama"            # when the claude call fails: "ollama" (if it answers) or "none"

The fallback is applied by `workspace.py action-items`; this module raises
SummarizerError on any failure. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import action_items as ai
from action_items import SummarizerError
from wsconfig import Turn, hms, parse_turns, speaker_first_name

DEFAULT_MODEL = "opus"
DEFAULT_TIMEOUT = 900
BIN_ENV = "WHOSAID_CLAUDE_BIN"
# launchd agents run with a minimal PATH; these are where the installers put `claude`
BIN_CANDIDATES = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude",
                  "/usr/local/bin/claude")
ISOLATION = ("--tools", "", "--strict-mcp-config", "--setting-sources", "",
             "--disable-slash-commands", "--no-session-persistence", "--output-format", "json")
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")   # would bill the API instead
AUTH_HINT = ("check `claude -p hi` by hand; log in with `claude`, or mint a token with "
             "`claude setup-token` and export CLAUDE_CODE_OAUTH_TOKEN")
KINDS = ("ask", "directive", "commit")
HMS_RE = re.compile(r"^\d{1,3}:\d\d(?::\d\d)?$")


def resolve_bin(configured: str = "") -> str:
    for cand in (configured, os.environ.get(BIN_ENV, ""), shutil.which("claude") or "",
                 *BIN_CANDIDATES):
        if cand:
            p = Path(cand).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return ""


class _Retry(Exception):
    pass


class ClaudeCLI:
    """One isolated `claude -p` process per .chat call (see the module docstring)."""

    def __init__(self, model: str = DEFAULT_MODEL, *, binary: str = "",
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        self.model = model
        self.binary = resolve_bin(binary)
        if not self.binary:
            raise SummarizerError("the Claude Code CLI (`claude`) was not found",
                                  f"install it, or set [summarizer] claude_bin or {BIN_ENV} to its path")
        self.timeout = timeout
        self.calls = 0
        self.resolved_model = ""

    def argv(self, system: str) -> list[str]:
        return [self.binary, "-p", "--system-prompt", system, "--model", self.model, *ISOLATION]

    @staticmethod
    def child_env() -> dict[str, str]:
        env = dict(os.environ)
        for k in SCRUBBED_ENV:
            env.pop(k, None)
        return env

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        del temperature                  # the CLI has no such flag
        self.calls += 1
        why = ""
        for _attempt in (1, 2):
            try:
                return self._once(system, user)
            except _Retry as e:
                why = str(e)
        raise SummarizerError(f"`claude -p` failed twice: {why}", AUTH_HINT)

    def _once(self, system: str, user: str) -> str:
        cwd = tempfile.mkdtemp(prefix="whosaid-claude-")
        try:
            proc = subprocess.run(self.argv(system), input=user, capture_output=True, text=True,
                                  cwd=cwd, env=self.child_env(), timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise _Retry(f"no reply within {self.timeout}s") from e
        except OSError as e:
            raise SummarizerError(f"cannot run {self.binary}: {e}",
                                  f"set [summarizer] claude_bin or {BIN_ENV}") from e
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        try:
            events = json.loads(proc.stdout)
        except ValueError:
            raise _Retry(f"exit {proc.returncode}, stdout is not JSON: "
                         f"{(proc.stdout or proc.stderr).strip()[:300]!r}") from None
        events = [events] if isinstance(events, dict) else events
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
            raise SummarizerError("`claude -p` returned an error: "
                                  f"{str(result.get('result') or result.get('subtype'))[:300]}", AUTH_HINT)
        if proc.returncode != 0:
            raise _Retry(f"exit {proc.returncode}: {proc.stderr.strip()[:300]!r}")
        usage = result.get("modelUsage")
        if not self.resolved_model and isinstance(usage, dict) and len(usage) == 1:
            self.resolved_model = next(iter(usage))
        return str(result.get("result") or "").strip()


# ---- prompt ------------------------------------------------------------------------------

def system_prompt(plan: ai.Plan) -> str:
    quote = '"quote":"<at most 15 words copied verbatim from that turn>"'
    if plan.owner:
        f = plan.owner_first
        lead = ", ".join(plan.leaders)
        kinds = [f"- ask: someone other than {f} asks or tells {f} to do something, gives {f} a task, or "
                 f"tells {f} how something {f} owns should be done (by name, or clearly addressed to "
                 f"{f}, including an unnamed \"can you ...\" right after {f} spoke)."]
        if plan.leaders:
            kinds.append(
                f"- directive: a LEADERSHIP speaker ({lead}) sets a priority, focus, deadline, process, "
                f"rule or expectation for the whole team {f} is part of, which {f} would have to act on "
                f"or follow. Include these even if {f} never spoke or was not named. Not a directive: "
                f"an ask aimed at one specific other person, a status report, an opinion, a "
                f"hypothetical, or the leader's own commitment.")
        kinds.append(f"- commit: {f} commits to doing something (\"I'll ...\", \"let me ...\", agreeing "
                     f"to an ask). Not a commitment: a status update, work already done, or a hedge.")
        names = "|".join(k.split(":")[0][2:] for k in kinds)
        head = (f"You extract action items for one person, {f} (the OWNER, label {plan.owner}), from a "
                f"diarized meeting transcript.\n")
        title = f"<imperative: what {f} must do>"
        excl = (f"Exclude other people's individual tasks, requests aimed at someone other than {f}, "
                f"status with no ask or commitment, questions answered in the meeting, hypotheticals "
                f"and someday ideas, scheduling chatter and small talk.")
        infer = f"things that obviously have to happen next for {f} but were not stated as items"
        item = (f'{{"kind":"{names}","speaker":"<label of who said it>","t":"HH:MM:SS",'
                f'"title":"{title}","context":"<one sentence>",{quote}}}')
    else:
        kinds = ["- ask: a speaker asks or instructs another participant to do something.",
                 "- commit: a speaker commits to doing something themselves."]
        head = "You extract every action item from a diarized meeting transcript.\n"
        excl = ("Exclude status with no ask or commitment, questions answered in the meeting, "
                "hypotheticals, scheduling chatter and small talk.")
        infer = "things that obviously have to happen next but were not stated as items"
        item = ('{"kind":"ask|commit","speaker":"<label of who said it>","t":"HH:MM:SS",'
                '"assignee":"<label of who must do it, if known>","title":"<imperative>",'
                f'"context":"<one sentence>",{quote}}}')
    return (head +
            "One turn per line: [HH:MM:SS] Speaker_Label: words. Labels like SPEAKER_03 are "
            "unidentified voices. Diarization and speech recognition are imperfect: judge by content, "
            "not only by the label.\n\nInclude these kinds of items:\n" + "\n".join(kinds) + "\n\n"
            + excl + " One item per distinct task; merge repeats.\n"
            f"Also list at most {ai.MAX_INFERRED} inferred next steps: {infer}.\n\n"
            "Answer with JSON only, no prose, no code fence:\n"
            f'{{"items":[{item}],"inferred":["<next step>"]}}\n'
            'If nothing qualifies: {"items":[],"inferred":[]}')


def user_prompt(plan: ai.Plan, speakers: list[str], meeting: str, text: str) -> str:
    return f"{plan.roster(speakers)}\n\nMeeting: {meeting}\n\nTranscript:\n{text}"


def parse_reply(raw: str) -> tuple[list[dict], list[str]]:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        raise SummarizerError(f"the reply holds no JSON object: {(raw or '')[:200]!r}",
                              "re-run; if it repeats, try another claude_model")
    try:
        data = json.loads(m.group(0))
    except ValueError as e:
        raise SummarizerError(f"the reply is not valid JSON ({e})", "re-run") from None
    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
    inferred = [str(x).strip() for x in (data.get("inferred") or []) if str(x).strip()]
    return items, inferred


# ---- evidence ----------------------------------------------------------------------------

def find_turn(turns: list[Turn], quote: str, t: str, speaker: str) -> tuple[int | None, bool]:
    """(index of the turn that holds the quote verbatim, True), nearest to t and preferring
    the stated speaker; else (the stated speaker's turn at t, False); else (None, False)."""
    ts = hms_sec(t)
    q = ai.norm(quote)
    if len(q) >= 6:
        hits = [i for i, x in enumerate(turns) if q in ai.norm(x.text)]
        if hits:
            return min(hits, key=lambda i: (abs(turns[i].t_sec - ts) if ts is not None else 0,
                                           turns[i].speaker != speaker)), True
    if ts is not None:
        near = [i for i, x in enumerate(turns) if x.speaker == speaker and abs(x.t_sec - ts) <= 2]
        if near:
            return near[0], False
    return None, False


def hms_sec(t: str) -> int | None:
    t = str(t or "").strip()
    if not HMS_RE.match(t):
        return None
    parts = [int(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


# ---- the engine --------------------------------------------------------------------------

def draft(transcript_text: str, meeting: str, cfg: dict, *, owner: str | None = None,
          client=None) -> tuple[str, dict]:
    """The whole engine in-process -> (markdown, stats). Raises SummarizerError when the
    model cannot be used. `client` (duck type: .chat(system, user) -> str, .calls,
    .resolved_model) replaces the claude CLI in tests."""
    summ = cfg.get("summarizer") or {}
    requested = str(summ.get("claude_model") or DEFAULT_MODEL)
    plan = ai.Plan(cfg, owner=owner, model=requested)
    turns = parse_turns(transcript_text)
    speakers = ai.speakers_of(transcript_text, turns)
    plan.add_bosses(ai.bosses_of(transcript_text))
    plan.build_prompts(speakers)          # the local prompts are unused; this fills other_firsts
    stamp = ai.local_stamp(plan.tz)
    stats: dict = {"engine": "claude", "model": requested, "owner": plan.owner, "meeting": meeting,
                   "speakers": speakers, "items": 0, "flagged": 0, "inferred": 0, "evidence": 0,
                   "dropped": 0, "sections": {}, "model_calls": 0}
    lines = ai.header_lines(meeting, speakers)
    if not turns:
        lines += [f"_Auto-drafted {stamp} by `{requested}` via `whosaid action-items --engine claude`: "
                  f"empty transcript, nothing to extract (nothing was sent)._", ""]
        return "\n".join(lines), stats
    if client is None:
        client = ClaudeCLI(requested, binary=str(summ.get("claude_bin") or ""),
                           timeout=int(summ.get("claude_timeout") or DEFAULT_TIMEOUT))
    raw = client.chat(system_prompt(plan), user_prompt(plan, speakers, meeting, transcript_text))
    items, inferred = parse_reply(raw)
    model = getattr(client, "resolved_model", "") or requested

    sections: list[list[tuple[int, str]]] = [[] for _ in plan.headings]
    order = {s: k for k, s in enumerate(speakers)}
    evidence: set[int] = set()
    seen: set[str] = set()
    nitems = nflagged = dropped = 0
    for it in items:
        kind = str(it.get("kind") or "").strip().lower()
        title = str(it.get("title") or "").strip()
        if kind not in KINDS or len(ai.norm(title)) < 4 or ai.norm(title) in seen \
                or ai.PLACEHOLDER_RE.search(title):
            dropped += 1
            continue
        quote = str(it.get("quote") or "").strip().strip('"').strip()
        said_by = str(it.get("speaker") or "").strip()
        idx, verified = find_turn(turns, quote, str(it.get("t") or ""), said_by)
        if idx is not None:
            turn = turns[idx]
        else:
            ts = hms_sec(str(it.get("t") or "")) or 0
            turn = Turn(ts, hms(ts), said_by or "SPEAKER_??", "", 0)
        spk = turn.speaker
        if kind == "directive":
            identified_outsider = (not spk.startswith("SPEAKER_") and spk not in plan.leaders)
            if not plan.owner or plan.directive_section is None or identified_outsider \
                    or (quote and ai.addressee(plan, turn.text, quote)):
                dropped += 1
                continue
        seen.add(ai.norm(title))
        context = str(it.get("context") or "").strip()
        rest = " ".join(x for x in (context, f'"{quote}"' if quote else "") if x)
        b = ai.Bullet(turn=idx if idx is not None else -1, title=title, rest=rest, kind=kind,
                      has_quote=bool(quote), verified=verified)
        if kind == "commit":
            sec = plan.commit_section
        elif kind == "directive":
            sec = plan.directive_section
        else:
            sec = plan.section_for(spk, "ask")
        line = ai.render_bullet(plan, turn, b)
        if not plan.owner:
            who = str(it.get("assignee") or "").strip()
            label = who if who in speakers else spk
            line = line.replace(f"- **{spk}**", f"- **{label}**", 1)
        sections[sec].append((turn.t_sec if plan.owner else order.get(spk, 0) * 10**7 + turn.t_sec, line))
        if idx is not None and verified:
            evidence.add(idx)
        nitems += 1
        nflagged += b.flagged
    inferred = inferred[:ai.MAX_INFERRED]
    sections[plan.inferred_section] = [(0, ai.render_inferred(plan, x)) for x in inferred]

    lines.append(f"_Auto-drafted {stamp} by `{model}` (Claude, cloud: this transcript was sent to "
                 f"Anthropic) via `whosaid action-items --engine claude`: the whole transcript read in "
                 f"one call, {len(evidence)} turns kept as evidence, {nitems} items drafted, {nflagged} "
                 f"flagged ⚠. A DRAFT: read the evidence and the transcript before trusting it._")
    for n, (heading, bucket) in enumerate(zip(plan.headings, sections), 1):
        lines += ["", f"## {n}. {heading}"]
        bucket = sorted(bucket, key=lambda x: x[0])
        lines += [b for _, b in bucket] if bucket else ["- none"]
        stats["sections"][heading] = len(bucket)
    if evidence:
        ev = sorted(evidence)
        lines += ["", f"<details><summary>Evidence turns the draft was built from (verbatim, "
                      f"{len(ev)})</summary>", ""]
        lines += [f"- [{hms(turns[i].t_sec)}] {turns[i].speaker}: {turns[i].text}" for i in ev]
        lines += ["", "</details>"]
    lines.append("")
    stats.update({"model": model, "requested_model": requested, "items": nitems, "flagged": nflagged,
                  "inferred": len(inferred), "evidence": len(evidence), "dropped": dropped,
                  "model_calls": getattr(client, "calls", 1)})
    return "\n".join(lines), stats

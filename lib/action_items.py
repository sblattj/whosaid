#!/usr/bin/env python3
"""
action_items.py: whosaid's built-in action-item summarizer (GitHub issue #14).

Drafts a meeting's action-items.md from its speaker-labeled transcript with a
small local model served by Ollama on 127.0.0.1. Nothing leaves the machine and
there is no cloud fallback. The `--hook` path in workspace.py stays for people
who bring their own summarizer.

How it stays honest with a small model: every step the model does is small and
checkable, and turn text always comes from the transcript, never from the model
(small models mis-copy timestamps and lose focus in long prompts).

  1. CANDIDATES  mostly deterministic, so recall does not depend on the model's
                 attention span: every substantive turn by the workspace owner
                 (>= min_chars), every turn by someone else that names the
                 owner (the configured aliases, word-bounded, so "Alice" also
                 matches "Alice's"), plus a model SELECT pass over ~chunk_chars
                 slices of the other speakers' turns that adds turns where the
                 owner is asked something WITHOUT being named (over-including
                 is fine, the next step filters).
  2. BULLETS     one model call PER candidate turn (long turns are read in
                 sentence groups of ~split_chars so a 2,000-character status
                 update yields ALL its commitments), with the two previous
                 turns shown as context so the model can tell who is being
                 addressed: it answers SKIP or writes one bullet per distinct
                 item with a <= 15-word verbatim quote. The SECTION is decided
                 by this script from the speaker's configured group, so nothing
                 can be misfiled, and the [Name HH:MM:SS] tag is stamped by the
                 script, not the model.
  3. VERIFY      every quote is checked against its own evidence turn; a quote
                 that is not a verbatim substring is flagged, never dropped.
  4. INFER       one small call (temperature 0.2) turns the accepted titles
                 into at most 3 "[inferred]" next steps. Best-effort: if this
                 call fails, the drafted bullets are kept and the note line
                 says so.

Configuration is <workspace>/whosaid.toml (see lib/wsconfig.py):

  [workspace]
  owner = "Alice_Example"            # whose items these are (a speaker label)
  aliases = ["Alice", "Alicia"]      # spellings ASR produces for the owner;
                                     # default: the owner's first name
  [groups]                           # ordered; one "Asks from ..." section each
  leadership = ["Bob_Example"]
  team = ["Carol_Example", "Dan_Example"]
  [summarizer]
  model = "qwen2.5:14b"              # qwen2.5:7b is ~2x faster and noisier
  num_ctx = 32768
  min_chars = 60                     # shorter owner turns are noise
  split_chars = 600                  # sentence-group size for long turns
  chunk_chars = 8000                 # SELECT slice size (~5 minutes of meeting)
  timeout = 900                      # seconds per model call
  num_predict = 2048                 # max tokens per model reply; caps a runaway generation
  think = false                      # thinking models (qwen3, qwen3.x, deepseek-r1) reason
                                     # before every answer when this is unset; off keeps each
                                     # small call fast and the reply inside num_predict
  [search]
  ollama = "http://127.0.0.1:11434"

Sections, numbered in this order: one "Asks from <group> (<first names>)" per
configured group (speakers in no group, SPEAKER_NN included, fall into the LAST
group, the way unidentified voices were treated as peers in the original
tuning), then "<Owner>'s own commitments", then "Inferred next steps". With an
owner but no groups: "Asks of <Owner>", own commitments, inferred. With no owner
at all only the model SELECT pass runs, over every speaker, and the sections
are "Asks" and "Commitments" grouped by speaker, then inferred. Empty sections
render "- none".

Bullet shape (workspace.py's roll-up reads the bold prefix as the assignee):

  - **Alice_Example** [Bob 00:12:41] Send the vendor report. Bob wants it first. "send the vendor report to me by Thursday"
  - **Alice_Example** [inferred] Confirm the sync agenda with Bob.

The bold label is the configured owner for every section; without an owner it
is the speaking person's label. The bracket holds the requester's first name
(the speaker of that turn) and the turn time. Evidence turns are appended
verbatim in a <details> block.

CLI (the launcher and `workspace.py action-items --engine ollama` use this):

  python3 lib/action_items.py --transcript <path.speakers.txt> [--ws DIR] [--out FILE]
      [--model M] [--ollama URL] [--owner NAME] [--select-only] [--strict]

--ws defaults to the transcript's parent's parent (the workspace root) so
whosaid.toml is found. Markdown goes to stdout unless --out. When Ollama is
unreachable, or anything else fails, a short stub is written and the exit code
is 0 so an ingest never fails because of the summarizer; --strict exits 3
instead. Stdlib only, urllib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import wsconfig
from wsconfig import Turn, hms, log, parse_turns, speaker_first_name

DEFAULT_MODEL = "qwen2.5:14b"
DEFAULT_TIMEOUT = 900        # seconds per model call
DEFAULT_NUM_PREDICT = 2048   # max tokens per model reply; caps a runaway (repetition-loop) generation
DEFAULT_THINK = False        # Ollama's "think" flag; thinking tokens count against num_predict
CONTEXT_TURNS = 2            # previous turns shown to each BULLETS call
CONTEXT_CHARS = 300          # each context turn is cut to this many chars
MAX_INFERRED = 3
RERUN_HINT = "`whosaid action-items --engine ollama`"

SPEAKERS_HEADER_RE = re.compile(r"^#\s+Speakers?\s*\(\d+\)\s*:\s*(.+)$", re.IGNORECASE)
ID_RE = re.compile(r"^\W*#?(\d+)\b")
MODEL_BULLET_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*(.*)$")
QUOTE_RE = re.compile(r'"([^"]{6,})"')
INFERRED_RE = re.compile(r"^\s*[-*]\s*\(inferred\)\s*:?\s*(\S.*)$", re.IGNORECASE)
KIND_RE = re.compile(r"^\s*(ASK|COMMIT)\s*:\s*(.*)$", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
THINK_BLOCK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


class SummarizerError(RuntimeError):
    """The model could not be used. `.hint` says what to do about it."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ---- small helpers -------------------------------------------------------------------

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", s.lower())).strip()


def compile_name_re(aliases: list[str]) -> re.Pattern | None:
    """Word-bounded, case-insensitive match on any alias, allowing a trailing
    [a-z]* so "Alice" also matches "Alice's" and "Alicia" as transcribed."""
    alts = [re.escape(a.strip()) for a in aliases if a and a.strip()]
    if not alts:
        return None
    return re.compile(r"\b(?:" + "|".join(alts) + r")[a-z]*\b", re.IGNORECASE)


def speakers_of(text: str, turns: list[Turn]) -> list[str]:
    """Distinct speaker labels, header names first, then first-appearance order."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        m = SPEAKERS_HEADER_RE.match(line.strip())
        if m:
            for name in m.group(1).split(","):
                if name.strip():
                    seen.setdefault(name.strip(), None)
    for t in turns:
        seen.setdefault(t.speaker, None)
    return list(seen)


def turn_row(i: int, t: Turn) -> str:
    return f"#{i} [{hms(t.t_sec)}] {t.speaker}: {t.text}"


def chunks(rows: list[str], limit: int) -> list[list[str]]:
    """Group rows into slices of <= limit chars; a row is never cut."""
    parts: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for row in rows:
        if cur and size + len(row) > limit:
            parts.append(cur)
            cur, size = [], 0
        cur.append(row)
        size += len(row) + 1
    if cur:
        parts.append(cur)
    return parts


def pieces_of(text: str, limit: int) -> list[str]:
    """A long turn in sentence groups of about `limit` chars (a run-on with no
    punctuation is cut at spaces)."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for s in re.split(r"(?<=[.?!])\s+", text):
        while len(s) > limit:                       # one giant run-on sentence
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + len(s) + 1 > limit:
            out.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def local_stamp(tz: str = "") -> str:
    now = datetime.now().astimezone()
    if tz:
        try:
            from zoneinfo import ZoneInfo
            now = now.astimezone(ZoneInfo(tz))
        except Exception:  # noqa: BLE001
            pass
    return now.isoformat(timespec="minutes")


# ---- Ollama client ---------------------------------------------------------------------

class Ollama:
    """POST /api/chat, stream off, urllib only. Every failure becomes a SummarizerError."""

    def __init__(self, url: str, model: str, num_ctx: int, timeout: int = DEFAULT_TIMEOUT,
                 num_predict: int = DEFAULT_NUM_PREDICT, think: bool = DEFAULT_THINK) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.num_predict = num_predict
        self.think = think
        self.calls = 0

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        # "think" is always sent: false is accepted by every model, and leaving it
        # out lets a thinking model reason at length before each small answer
        payload = {
            "model": self.model, "stream": False, "think": self.think,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            self.url + "/api/chat", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                body = ""
            if "not found" in body.lower():
                hint = f"pull the model first (`ollama pull {self.model}`)"
            elif "does not support thinking" in body.lower():
                hint = f"this model has no thinking mode; set think = false under [summarizer] in {wsconfig.CONFIG_NAME}"
            else:
                hint = f"check `ollama list` and the [summarizer] model in {wsconfig.CONFIG_NAME}"
            raise SummarizerError(
                f"Ollama at {self.url} answered HTTP {e.code} for model `{self.model}`"
                f"{' (' + body[:160] + ')' if body else ''}", hint) from e
        except urllib.error.URLError as e:
            raise SummarizerError(
                f"cannot reach Ollama at {self.url} ({e.reason})",
                f"start it (`ollama serve`, or `brew services start ollama`) and make sure "
                f"the model is pulled (`ollama pull {self.model}`)") from e
        except (OSError, TimeoutError, ValueError) as e:
            raise SummarizerError(
                f"Ollama at {self.url} failed ({type(e).__name__}: {e})",
                "check that it is running and not overloaded") from e
        try:
            return strip_thinking(str(out["message"]["content"]))
        except (KeyError, TypeError) as e:
            raise SummarizerError(f"unexpected reply from Ollama: {str(out)[:160]}") from e


def strip_thinking(text: str) -> str:
    """Drop reasoning a thinking model left inline in the answer: <think> blocks
    (an unclosed one is a reply num_predict cut off mid-thought) and anything
    before a stray </think> whose opening tag was in the chat template."""
    text = THINK_BLOCK_RE.sub("", text)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


def config_flag(value, default: bool) -> bool:
    """A TOML bool, or a string/number spelling of one; anything else is the default."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower() if value is not None else ""
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


# ---- the plan: everything derived from the config -----------------------------------------

class Plan:
    """Owner, aliases, groups, section layout, model settings and prompts for one run."""

    def __init__(self, cfg: dict, *, owner: str | None = None, model: str | None = None,
                 ollama: str | None = None) -> None:
        ws = dict(cfg.get("workspace") or {})
        summ = dict(cfg.get("summarizer") or {})
        self.owner = (str(ws.get("owner") or "") if owner is None else str(owner)).strip()
        self.owner_first = speaker_first_name(self.owner) if self.owner else ""
        aliases = ws.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        aliases = [str(a).strip() for a in aliases if str(a).strip()]
        if not aliases and self.owner_first:
            aliases = [self.owner_first]
        self.aliases = aliases if self.owner else []
        self.name_re = compile_name_re(self.aliases)
        self.groups: dict[str, list[str]] = {}
        if self.owner:
            for name, members in (cfg.get("groups") or {}).items():
                if isinstance(members, str):
                    members = members.split(",")
                members = [str(m).strip() for m in members if str(m).strip()]
                self.groups[str(name)] = [m for m in members if m != self.owner]
        self._group_index = {m: i for i, ms in enumerate(self.groups.values()) for m in ms}

        self.model = str(model or summ.get("model") or DEFAULT_MODEL)
        url = str(ollama or wsconfig.ollama_url(cfg)).strip().rstrip("/")
        self.url = url if url.startswith("http") else "http://" + url
        self.num_ctx = int(summ.get("num_ctx") or 32768)
        self.min_chars = int(summ.get("min_chars") or 60)
        self.split_chars = int(summ.get("split_chars") or 600)
        self.chunk_chars = int(summ.get("chunk_chars") or 8000)
        self.timeout = int(summ.get("timeout") or DEFAULT_TIMEOUT)
        self.num_predict = int(summ.get("num_predict") or DEFAULT_NUM_PREDICT)
        self.think = config_flag(summ.get("think"), DEFAULT_THINK)
        self.tz = str(ws.get("tz") or "")

        if self.owner and self.groups:
            self.headings = [
                f"Asks from {g} ({', '.join(speaker_first_name(m) for m in ms) or 'nobody yet'})"
                for g, ms in self.groups.items()
            ] + [f"{self.owner_first}'s own commitments"]
        elif self.owner:
            self.headings = [f"Asks of {self.owner_first}", f"{self.owner_first}'s own commitments"]
        else:
            self.headings = ["Asks", "Commitments"]
        self.headings.append("Inferred next steps")
        self.commit_section = len(self.headings) - 2
        self.inferred_section = len(self.headings) - 1
        self.select_prompt = self.bullets_prompt = self.infer_prompt = ""

    # sections and roles are decided here, never by the model
    def section_for(self, speaker: str, kind: str) -> int:
        if not self.owner:
            return self.commit_section if kind == "commit" else 0
        if speaker == self.owner:
            return self.commit_section
        if self.groups:
            return self._group_index.get(speaker, len(self.groups) - 1)
        return 0

    def group_of(self, speaker: str) -> str:
        for g, ms in self.groups.items():
            if speaker in ms:
                return g
        return ""

    def role_of(self, speaker: str) -> str:
        if self.owner and speaker == self.owner:
            return f"{self.owner_first}, the person these items are for"
        g = self.group_of(speaker)
        if g:
            return g
        return "unidentified speaker" if speaker.startswith("SPEAKER_") else "other participant"

    def roster(self, speakers: list[str]) -> str:
        lines = ["People (voice-matched labels in the transcript; SPEAKER_NN = unidentified):"]
        if self.owner:
            also = f" (also transcribed as: {', '.join(self.aliases)})" if self.aliases else ""
            lines.append(f"- {self.owner} = {self.owner_first.upper()}, the person these action "
                         f"items are for{also}.")
            for g, ms in self.groups.items():
                lines.append(f"- {g.upper()}: {', '.join(ms) or '(nobody)'}.")
            others = [s for s in speakers if s != self.owner and s not in self._group_index]
            if others:
                lines.append(f"- OTHER PARTICIPANTS: {', '.join(others)}.")
        else:
            lines.append(f"- Speakers: {', '.join(speakers) or '(none)'}. No owner is configured: "
                         "track asks and commitments for every speaker.")
        return "\n".join(lines)

    def build_prompts(self, speakers: list[str]) -> None:
        roster = self.roster(speakers)
        head = ("You are given part of a diarized meeting transcript. Each speaker turn is one line: "
                "an ID like `#37`, then `[HH:MM:SS] Speaker_Name:`, then the words spoken.\n"
                f"{roster}\n\n")
        tail = ("Output one row per selected turn: the ID, a colon, and at most 10 words saying what "
                "is asked. Nothing else. If no turn qualifies, output the single word NONE.")
        quote = '"<at most 15 words copied word for word from the TURN>"'
        rules = ("A long turn often holds several separate items: list every one of them, one bullet "
                 "each. The quote must be an exact substring of the TURN (not of the context). Never "
                 "invent facts that are not in the turn.")
        if self.owner:
            f, up = self.owner_first, self.owner_first.upper()
            self.select_prompt = head + (
                f"Select every turn in which someone OTHER than {f} asks {up} to do something, gives "
                f"{f} a task, or tells {f} how something {f} owns should be done. This includes turns "
                f"that continue such an ask in the following sentences without repeating the name. "
                f"Skip turns that only invite {f} to speak, other people's tasks, scheduling chatter, "
                f"and small talk. If you are unsure, select it; a later step filters.\n") + tail
            self.bullets_prompt = (
                f"You write action items for {up} from ONE turn of a meeting transcript. Earlier turns "
                f"are shown only as context for who is being addressed.\n{roster}\n\n"
                f"Decide whether the TURN contains an ask, task, or instruction directed at {up} by "
                f"someone else, or, when the speaker is {f}, a commitment by {f} to do something.\n"
                f"If it does not (someone else's task, a request aimed at a different person, a "
                f"statement about {f} with no ask, status with no commitment, an invitation to speak, "
                f"scheduling, small talk), output the single word SKIP.\n"
                f"Otherwise output one bullet per distinct item and nothing else, each in exactly "
                f"this shape:\n"
                f"- **<imperative title: what {f} must do>.** <one sentence of context> {quote}\n"
                + rules)
            self.infer_prompt = (
                f"You are given {f}'s action items from one meeting (titles only). Write at most "
                f"{MAX_INFERRED} implied next steps for {f} that follow from them: things that "
                f"obviously have to happen next but were not stated as items. Each line starts with "
                f"\"- (inferred)\". Output only those lines. If nothing sensible follows, output the "
                f"single word NONE.")
        else:
            self.select_prompt = head + (
                "Select every turn in which a speaker asks or instructs another participant to do "
                "something, or commits to doing something themselves. This includes turns that "
                "continue such an ask or commitment in the following sentences. Skip turns that only "
                "invite someone to speak, scheduling chatter, and small talk. If you are unsure, "
                "select it; a later step filters.\n") + tail
            self.bullets_prompt = (
                "You write action items from ONE turn of a meeting transcript. Earlier turns are "
                f"shown only as context for who is being addressed.\n{roster}\n\n"
                "Decide whether the TURN contains an ASK (the speaker asks or instructs another "
                "participant to do something) or a COMMIT (the speaker commits to doing something).\n"
                "If it contains neither (a statement with no ask, status with no commitment, an "
                "invitation to speak, scheduling, small talk), output the single word SKIP.\n"
                "Otherwise output one bullet per distinct item and nothing else, each in exactly one "
                "of these two shapes:\n"
                f"- **ASK: <imperative title: what must be done, naming who is asked if the turn "
                f"says so>.** <one sentence of context> {quote}\n"
                f"- **COMMIT: <imperative title: what the speaker will do>.** <one sentence of "
                f"context> {quote}\n" + rules)
            self.infer_prompt = (
                f"You are given the action items from one meeting (titles only). Write at most "
                f"{MAX_INFERRED} implied next steps that follow from them: things that obviously have "
                f"to happen next but were not stated as items. Each line starts with \"- (inferred)\". "
                f"Output only those lines. If nothing sensible follows, output the single word NONE.")


# ---- the pipeline ----------------------------------------------------------------------

@dataclass
class Bullet:
    turn: int
    title: str
    rest: str
    kind: str           # "ask" | "commit"
    has_quote: bool
    verified: bool

    @property
    def flagged(self) -> bool:
        return not (self.has_quote and self.verified)


def select_candidates(plan: Plan, turns: list[Turn], meeting: str, chat
                      ) -> tuple[list[int], dict, dict[int, list[str]]]:
    """Candidate turn ids: owner's substantive turns + turns naming the owner
    (deterministic) + turns the model flags as unnamed asks. Returns
    (ids, counts, tags) where tags says why each id is in."""
    mine: set[int] = set()
    named: set[int] = set()
    picked: set[int] = set()
    if plan.owner:
        for i, t in enumerate(turns):
            if t.speaker == plan.owner:
                if len(t.text) >= plan.min_chars:
                    mine.add(i)
            elif plan.name_re and plan.name_re.search(t.text):
                named.add(i)
        pool = [i for i, t in enumerate(turns) if t.speaker != plan.owner]
    else:
        pool = list(range(len(turns)))
    pool_set = set(pool)
    slices = chunks([turn_row(i, turns[i]) for i in pool], plan.chunk_chars)
    for k, rows in enumerate(slices, 1):
        part = f" (part {k} of {len(slices)})" if len(slices) > 1 else ""
        out = chat(plan.select_prompt, f"Meeting: {meeting}{part}\n\nTranscript:\n" + "\n".join(rows))
        for ln in out.splitlines():
            m = ID_RE.match(ln.strip())
            if m and int(m.group(1)) in pool_set:
                picked.add(int(m.group(1)))
    ids = sorted(mine | named | picked)
    tags = {i: [tag for tag, s in (("owner", mine), ("named", named), ("model", picked)) if i in s]
            for i in ids}
    counts = {"by_owner": len(mine), "naming_owner": len(named),
              "model_added": len(picked - named - mine), "slices": len(slices)}
    return ids, counts, tags


def bullets_for(plan: Plan, turns: list[Turn], idx: int, chat) -> list[Bullet]:
    """Model calls for one candidate turn (one per sentence group) -> bullets,
    each verified against the turn text."""
    t = turns[idx]
    ctx = []
    for j in range(max(0, idx - CONTEXT_TURNS), idx):
        c = turns[j]
        cut = c.text[:CONTEXT_CHARS] + ("..." if len(c.text) > CONTEXT_CHARS else "")
        ctx.append(f"[{hms(c.t_sec)}] {c.speaker}: {cut}")
    ntext = norm(t.text)
    parts = pieces_of(t.text, plan.split_chars)
    res: list[Bullet] = []
    prev = ""
    for k, piece in enumerate(parts):
        lines = list(ctx)
        if prev:
            lines.append(f"[{hms(t.t_sec)}] {t.speaker} (earlier in this same turn): ...{prev[-CONTEXT_CHARS:]}")
        prompt = ("Context (earlier turns, do not quote from these):\n" + "\n".join(lines) + "\n\n"
                  if lines else "")
        part = f" (part {k + 1} of {len(parts)})" if len(parts) > 1 else ""
        prompt += f"TURN [{hms(t.t_sec)}] by {t.speaker} ({plan.role_of(t.speaker)}){part}:\n{piece}"
        out = chat(plan.bullets_prompt, prompt)
        prev = piece
        if norm(out) in ("skip", "none") or out.strip().upper().startswith("SKIP"):
            continue
        for ln in out.splitlines():
            m = MODEL_BULLET_RE.match(ln)
            if not m:
                continue
            title, rest = m.group(1).strip(), m.group(2).strip()
            kind = "commit" if (plan.owner and t.speaker == plan.owner) else "ask"
            km = KIND_RE.match(title)
            if km:
                title = km.group(2).strip()
                if not plan.owner:
                    kind = "commit" if km.group(1).upper() == "COMMIT" else "ask"
            # "- **SKIP.**" written as a bullet, or a parroted "<placeholder>"
            if norm(title) in ("skip", "none") or len(norm(title)) < 4 or PLACEHOLDER_RE.search(title):
                continue
            quotes = QUOTE_RE.findall(rest)
            res.append(Bullet(turn=idx, title=title, rest=rest, kind=kind, has_quote=bool(quotes),
                              verified=any(norm(q) in ntext for q in quotes)))
    return res


def render_bullet(plan: Plan, t: Turn, b: Bullet) -> str:
    label = plan.owner or t.speaker
    title = b.title.rstrip()
    if not title.endswith((".", "!", "?")):
        title += "."
    line = f"- **{label}** [{speaker_first_name(t.speaker)} {hms(t.t_sec)}] {title}"
    if b.rest:
        line += " " + b.rest
    if not b.has_quote:
        line += " _(⚠ no quote)_"
    elif not b.verified:
        line += " _(⚠ quote not verbatim from that turn)_"
    return line


def render_inferred(plan: Plan, text: str) -> str:
    return f"- **{plan.owner}** [inferred] {text}" if plan.owner else f"- [inferred] {text}"


def header_lines(meeting: str, speakers: list[str]) -> list[str]:
    lines = [f"# Action items — {meeting}", ""]
    if speakers:
        lines += [f"Speakers in this meeting: {', '.join(speakers)}", ""]
    return lines


def note_line(plan: Plan, stamp: str, counts: dict, ncand: int, nev: int,
              nitems: int, nflagged: int, infer_error: str | None = None) -> str:
    k = counts.get("slices", 0)
    slices = f"{k} slice" + ("" if k == 1 else "s")
    if plan.owner:
        detail = (f"{counts['by_owner']} by {plan.owner_first}, {counts['naming_owner']} naming them, "
                  f"{counts['model_added']} added by the model over {slices}")
    else:
        detail = f"all added by the model over {slices}"
    infer_note = f" Inferred next steps skipped ({infer_error})." if infer_error else ""
    return (f"_Auto-drafted {stamp} by `{plan.model}` (local Ollama, offline) via "
            f"`whosaid action-items --engine ollama`: {ncand} candidate turns ({detail}), "
            f"{nev} kept as evidence, {nitems} items drafted, {nflagged} flagged ⚠.{infer_note} "
            f"A DRAFT: read the evidence and the transcript before trusting it._")


def stub_markdown(meeting: str, speakers: list[str], model: str, reason: str, hint: str = "") -> str:
    """Header plus one italic line saying why nothing was extracted and how to
    fix it. No sections, so a roll-up folds nothing from it."""
    lines = header_lines(meeting, speakers)
    fix = f" {hint[0].upper() + hint[1:]}, then re-run {RERUN_HINT}." if hint else f" Re-run {RERUN_HINT}."
    lines.append(f"_No action items extracted (model `{model}`): {reason}.{fix}_")
    lines.append("")
    return "\n".join(lines)


def prepare(transcript_text: str, cfg: dict, *, model: str | None = None,
            ollama: str | None = None, owner: str | None = None, client=None
            ) -> tuple[Plan, list[Turn], list[str], Ollama]:
    """`client`, when given, is used instead of a new Ollama client. It needs the
    same duck type: .chat(system, user, temperature=0.0) -> str, .calls, .model
    (the offline eval harness in test/eval/ passes fakes and recorders)."""
    plan = Plan(cfg, owner=owner, model=model, ollama=ollama)
    turns = parse_turns(transcript_text)
    speakers = speakers_of(transcript_text, turns)
    plan.build_prompts(speakers)
    if client is None:
        client = Ollama(plan.url, plan.model, plan.num_ctx, plan.timeout, num_predict=plan.num_predict,
                        think=plan.think)
    return plan, turns, speakers, client


def draft(transcript_text: str, meeting: str, cfg: dict, *, model: str | None = None,
          ollama: str | None = None, owner: str | None = None, client=None) -> tuple[str, dict]:
    """The whole pipeline in-process -> (markdown, stats). Raises SummarizerError
    when the model cannot be used; callers decide whether to write a stub.
    `client` replaces the Ollama client (see prepare)."""
    plan, turns, speakers, client = prepare(transcript_text, cfg, model=model, ollama=ollama,
                                            owner=owner, client=client)
    stamp = local_stamp(plan.tz)
    stats: dict = {"engine": "ollama", "model": plan.model, "ollama": plan.url, "owner": plan.owner,
                   "meeting": meeting, "speakers": speakers, "candidates": 0, "by_owner": 0,
                   "naming_owner": 0, "model_added": 0, "slices": 0, "evidence": 0, "items": 0,
                   "flagged": 0, "inferred": 0, "sections": {}, "model_calls": 0}
    lines = header_lines(meeting, speakers)
    if not turns:
        lines += [f"_Auto-drafted {stamp} by `{plan.model}` (local Ollama, offline) via "
                  f"`whosaid action-items --engine ollama`: empty transcript, nothing to extract._", ""]
        return "\n".join(lines), stats

    ids, counts, _tags = select_candidates(plan, turns, meeting, client.chat)
    sections: list[list[tuple[int, str]]] = [[] for _ in plan.headings]
    order = {s: k for k, s in enumerate(speakers)}
    evidence: list[int] = []
    titles: list[str] = []
    seen: set[str] = set()
    nitems = nflagged = 0
    for i in ids:
        got = bullets_for(plan, turns, i, client.chat)
        if got:
            evidence.append(i)
        for b in got:
            key = norm(b.title)
            if key in seen:
                continue
            seen.add(key)
            sec = plan.section_for(turns[i].speaker, b.kind)
            sections[sec].append((0 if plan.owner else order.get(turns[i].speaker, 0),
                                  render_bullet(plan, turns[i], b)))
            titles.append(b.title)
            nitems += 1
            nflagged += b.flagged

    inferred: list[str] = []
    infer_error: str | None = None
    if nitems:
        try:
            out = client.chat(plan.infer_prompt,
                              f"Meeting: {meeting}\n\nItems:\n" + "\n".join(f"- {t}" for t in titles),
                              temperature=0.2)
        except SummarizerError as e:
            # INFER is best-effort: it only turns accepted titles into <= 3 extra
            # "[inferred]" lines, so losing it must never discard the drafted bullets.
            infer_error = str(e)
            log(f"WARN INFER step failed ({e}); keeping the drafted bullets with no inferred items")
        else:
            for ln in out.splitlines():
                m = INFERRED_RE.match(ln)
                if m:
                    inferred.append(m.group(1).strip())
            inferred = inferred[:MAX_INFERRED]
    sections[plan.inferred_section] = [(0, render_inferred(plan, x)) for x in inferred]

    lines.append(note_line(plan, stamp, counts, len(ids), len(evidence), nitems, nflagged, infer_error))
    for n, (heading, bucket) in enumerate(zip(plan.headings, sections), 1):
        lines += ["", f"## {n}. {heading}"]
        bucket = sorted(bucket, key=lambda x: x[0])      # stable: by speaker only without an owner
        lines += [b for _, b in bucket] if bucket else ["- none"]
        stats["sections"][heading] = len(bucket)
    if evidence:
        lines += ["", f"<details><summary>Evidence turns the draft was built from (verbatim, "
                      f"{len(evidence)})</summary>", ""]
        lines += [f"- [{hms(turns[i].t_sec)}] {turns[i].speaker}: {turns[i].text}" for i in evidence]
        lines += ["", "</details>"]
    lines.append("")
    stats.update(counts)
    stats.update({"candidates": len(ids), "evidence": len(evidence), "items": nitems,
                  "flagged": nflagged, "inferred": len(inferred), "model_calls": client.calls})
    if infer_error:
        stats["infer_error"] = infer_error
    return "\n".join(lines), stats


# ---- CLI --------------------------------------------------------------------------------

def select_only(transcript_text: str, meeting: str, cfg: dict, args: argparse.Namespace) -> str:
    plan, turns, _speakers, client = prepare(transcript_text, cfg, model=args.model,
                                             ollama=args.ollama, owner=args.owner)
    ids, counts, tags = select_candidates(plan, turns, meeting, client.chat)
    k = counts["slices"]
    out = [f"{len(ids)} candidate turns of {len(turns)} ({counts['by_owner']} by owner, "
           f"{counts['naming_owner']} naming the owner, {counts['model_added']} added by the model "
           f"over {k} slice{'' if k == 1 else 's'}; model `{plan.model}`)"]
    for i in ids:
        t = turns[i]
        text = t.text if len(t.text) <= 160 else t.text[:157] + "..."
        out.append(f"- #{i} {'+'.join(tags[i]):<11} [{hms(t.t_sec)}] {t.speaker}: {text}")
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="action_items.py",
        description="whosaid's built-in action-item summarizer: local Ollama model, "
                    "deterministic candidates, verified quotes (issue #14).")
    p.add_argument("--transcript", required=True, help="speaker-labeled transcript (*.speakers.txt)")
    p.add_argument("--ws", default=None,
                   help="workspace dir holding whosaid.toml (default: the transcript's parent's parent)")
    p.add_argument("--out", default=None, help="write the markdown here instead of stdout")
    p.add_argument("--model", default=None, help="Ollama model (default: [summarizer] model)")
    p.add_argument("--ollama", default=None, help="Ollama base URL (default: [search] ollama)")
    p.add_argument("--owner", default=None, help="owner speaker label (default: [workspace] owner)")
    p.add_argument("--select-only", action="store_true",
                   help="print the candidate turns and stop (runs only the SELECT pass)")
    p.add_argument("--strict", action="store_true",
                   help="exit 3 instead of writing a stub when the model cannot be used")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transcript = Path(args.transcript)
    if not transcript.is_file():
        log(f"action-items: transcript not found: {transcript}")
        return 1
    ws = Path(args.ws).expanduser() if args.ws else transcript.resolve().parent.parent
    cfg = wsconfig.load_config(ws)
    text = transcript.read_text()
    meeting = transcript.resolve().parent.name
    rc = 0
    try:
        if args.select_only:
            out = select_only(text, meeting, cfg, args)
        else:
            out, stats = draft(text, meeting, cfg, model=args.model, ollama=args.ollama, owner=args.owner)
            log(f"action items ({stats['model']}): {stats['candidates']} candidate turns, "
                f"{stats['items']} items, {stats['flagged']} flagged, {stats['model_calls']} model calls")
    except SummarizerError as e:
        log(f"WARN {e}; writing a stub")
        out = stub_markdown(meeting, speakers_of(text, parse_turns(text)),
                            args.model or cfg["summarizer"].get("model", DEFAULT_MODEL), str(e), e.hint)
        rc = 3 if args.strict else 0
    except Exception as e:  # noqa: BLE001  (an ingest must never fail because of the summarizer)
        log(f"WARN summarizer error {type(e).__name__}: {e}; writing a stub")
        out = stub_markdown(meeting, speakers_of(text, parse_turns(text)),
                            args.model or cfg["summarizer"].get("model", DEFAULT_MODEL),
                            f"summarizer error {type(e).__name__}: {e}")
        rc = 3 if args.strict else 0
    if args.out:
        Path(args.out).write_text(out)
        log(f"-> {args.out}")
    else:
        sys.stdout.write(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())

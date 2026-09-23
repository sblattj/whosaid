#!/usr/bin/env python3
"""
validate_fixtures.py: structural checks for the synthetic action-item eval
fixtures under test/eval/fixtures/ (the offline quality eval of
lib/action_items.py).

Each fixture is one directory holding exactly three files:

  transcript.speakers.txt  "# Speakers (N): A, B, ..." then one turn per line,
                           "[HH:MM:SS] Label: text", zero-padded, strictly
                           increasing, no continuation lines
  whosaid.toml             [workspace] owner (+ aliases), optional [groups];
                           read with wsconfig.load_config(<fixture dir>)
  gold.json                schema 1: items (the owner's asks and commitments
                           a good draft must contain) and distractors (turns
                           that must produce no item)

A predicted bullet matches a gold item when the bullet's turn time equals the
item's `t` and any of the item's keywords, after action_items.norm(), is a
substring of norm(title + context + quote). So every keyword must occur in its
own turn's text, and keywords of items that share a turn must be disjoint.

Stdlib only. Validates every fixture directory, or the directories given as
arguments; prints one "ok" line per clean fixture and exits 1 listing every
violation otherwise. `validate(fixture_dir)` returns the violations as a list
so the harness test can call it.

Run:
    python3 test/eval/validate_fixtures.py [FIXTURE_DIR ...]
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "lib"))

import action_items as ai  # noqa: E402
import wsconfig  # noqa: E402

FIXTURES_DIR = REPO_DIR / "test" / "eval" / "fixtures"
TRANSCRIPT = "transcript.speakers.txt"
CONFIG = wsconfig.CONFIG_NAME
GOLD = "gold.json"
FILES = {TRANSCRIPT, CONFIG, GOLD}

# fixture-set design rules (see the eval brief); change them here, not per fixture
TURNS_MIN, TURNS_MAX = 25, 45
GOLD_MIN, GOLD_MAX = 6, 10          # non-optional items
MAX_OPTIONAL = 1
MIN_DISTRACTORS = 4
KEYWORDS_MIN, KEYWORDS_MAX = 1, 4
COMMIT_MIN_CHARS = 80               # owner turns shorter than min_chars are never candidates
SPLIT_CHARS = int(wsconfig.DEFAULTS["summarizer"]["split_chars"])

HMS_RE = re.compile(r"^\d\d:\d\d:\d\d$")
TURN_LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] ")
HEADER_RE = re.compile(r"^# Speakers \((\d+)\): (.+)$")
ITEM_KEYS = {"id", "t", "speaker", "kind", "keywords", "desc", "optional"}
DISTRACTOR_KEYS = {"t", "why"}
TOP_KEYS = {"schema", "fixture", "description", "owner", "items", "distractors"}


@contextlib.contextmanager
def _no_whosaid_env():
    """load_config honors WHOSAID_* overrides; a fixture must not depend on the shell."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("WHOSAID_")}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        os.environ.update(saved)


def _check_transcript(text: str, bad) -> tuple[list[wsconfig.Turn], list[str]]:
    """Parse and check the transcript format; return (turns, header labels)."""
    lines = text.splitlines()
    header: list[str] = []
    m = HEADER_RE.match(lines[0]) if lines else None
    if not m:
        bad(f"{TRANSCRIPT} line 1 is not '# Speakers (N): Label, ...'")
    else:
        header = [s.strip() for s in m.group(2).split(",")]
        if int(m.group(1)) != len(header):
            bad(f"{TRANSCRIPT} header says {m.group(1)} speakers but lists {len(header)}")
        if len(set(header)) != len(header) or not all(header):
            bad(f"{TRANSCRIPT} header labels are empty or repeated: {header}")
    stamped = 0
    for n, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        if TURN_LINE_RE.match(line):
            stamped += 1
        else:
            bad(f"{TRANSCRIPT} line {n} is not a '[HH:MM:SS] Label: text' turn: {line[:60]!r}")
    turns = wsconfig.parse_turns(text)
    if len(turns) != stamped:
        bad(f"{TRANSCRIPT}: parse_turns found {len(turns)} turns but {stamped} lines carry [HH:MM:SS]")
    prev = -1
    for t in turns:
        if not HMS_RE.match(t.t_str) or t.t_str != wsconfig.hms(t.t_sec):
            bad(f"{TRANSCRIPT} line {t.line}: timestamp {t.t_str!r} is not zero-padded HH:MM:SS")
        if t.t_sec <= prev:
            bad(f"{TRANSCRIPT} line {t.line}: timestamp {t.t_str} does not increase")
        prev = t.t_sec
        if header and t.speaker not in header:
            bad(f"{TRANSCRIPT} line {t.line}: speaker {t.speaker!r} is not in the # Speakers header")
        if not t.text:
            bad(f"{TRANSCRIPT} line {t.line}: empty turn text")
    spoke = {t.speaker for t in turns}
    for label in header:
        if label not in spoke:
            bad(f"{TRANSCRIPT}: header speaker {label!r} never speaks")
    if not TURNS_MIN <= len(turns) <= TURNS_MAX:
        bad(f"{TRANSCRIPT}: {len(turns)} turns, want {TURNS_MIN}-{TURNS_MAX}")
    return turns, header


def _turn_at(t: object, by_time: dict[str, list[wsconfig.Turn]], where: str, bad):
    if not isinstance(t, str) or not HMS_RE.match(t):
        bad(f"{where}: t {t!r} is not HH:MM:SS")
        return None
    hits = by_time.get(t, [])
    if len(hits) != 1:
        bad(f"{where}: t {t} resolves to {len(hits)} turns, want exactly 1")
        return None
    return hits[0]


def validate(fixture_dir: str | os.PathLike) -> list[str]:
    """Every contract violation of one fixture directory (empty list = valid)."""
    d = Path(fixture_dir)
    out: list[str] = []
    bad = out.append
    if not d.is_dir():
        return [f"{d}: not a directory"]
    names = {p.name for p in d.iterdir()}
    for missing in sorted(FILES - names):
        bad(f"missing file {missing}")
    for extra in sorted(names - FILES):
        bad(f"unexpected file {extra} (a fixture holds exactly {', '.join(sorted(FILES))})")
    if FILES - names:
        return out

    # -- transcript
    turns, header = _check_transcript((d / TRANSCRIPT).read_text(encoding="utf-8"), bad)
    by_time: dict[str, list[wsconfig.Turn]] = {}
    for t in turns:
        by_time.setdefault(wsconfig.hms(t.t_sec), []).append(t)

    # -- gold.json
    try:
        gold = json.loads((d / GOLD).read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        bad(f"{GOLD} unreadable: {e}")
        return out
    if not isinstance(gold, dict):
        bad(f"{GOLD} is not a JSON object")
        return out
    for k in sorted(TOP_KEYS - set(gold)):
        bad(f"{GOLD}: missing key {k!r}")
    for k in sorted(set(gold) - TOP_KEYS):
        bad(f"{GOLD}: unknown key {k!r}")
    if gold.get("schema") != 1:
        bad(f"{GOLD}: schema is {gold.get('schema')!r}, want 1")
    if gold.get("fixture") != d.name:
        bad(f"{GOLD}: fixture {gold.get('fixture')!r} != directory name {d.name!r}")
    if not isinstance(gold.get("description"), str) or not gold.get("description", "").strip():
        bad(f"{GOLD}: description must be a non-empty string")
    owner = gold.get("owner")
    if not isinstance(owner, str) or not owner:
        bad(f"{GOLD}: owner must be a non-empty speaker label")
        owner = ""
    elif header and owner not in header:
        bad(f"{GOLD}: owner {owner!r} is not in the # Speakers header")

    # -- whosaid.toml, through the same loader the summarizer uses
    with _no_whosaid_env():
        cfg = wsconfig.load_config(d)
    cfg_owner = str(cfg["workspace"].get("owner") or "")
    if cfg_owner != owner:
        bad(f"{CONFIG}: load_config owner {cfg_owner!r} != gold owner {owner!r}")
    for g, members in cfg["groups"].items():
        for m in members:
            if m == owner:
                bad(f"{CONFIG}: owner {owner!r} is listed in group {g!r}")
            elif header and m not in header:
                bad(f"{CONFIG}: group {g!r} member {m!r} is not in the # Speakers header")

    # -- items
    items = gold.get("items") if isinstance(gold.get("items"), list) else []
    if not isinstance(gold.get("items"), list):
        bad(f"{GOLD}: items must be a list")
    ids: set[str] = set()
    per_turn: dict[int, list[tuple[str, list[str]]]] = {}
    n_optional = 0
    for n, it in enumerate(items):
        if not isinstance(it, dict):
            bad(f"items[{n}] is not an object")
            continue
        iid = it.get("id")
        where = f"item {iid}" if isinstance(iid, str) and iid else f"items[{n}]"
        for k in sorted(ITEM_KEYS - set(it)):
            bad(f"{where}: missing key {k!r}")
        for k in sorted(set(it) - ITEM_KEYS):
            bad(f"{where}: unknown key {k!r}")
        if not isinstance(iid, str) or not iid:
            bad(f"{where}: id must be a non-empty string")
        elif iid in ids:
            bad(f"{where}: duplicate id")
        else:
            ids.add(iid)
        if not isinstance(it.get("optional"), bool):
            bad(f"{where}: optional must be true or false")
        elif it["optional"]:
            n_optional += 1
        if not isinstance(it.get("desc"), str) or not it.get("desc", "").strip():
            bad(f"{where}: desc must be a non-empty string")
        turn = _turn_at(it.get("t"), by_time, where, bad)
        speaker = it.get("speaker")
        if turn is not None and speaker != turn.speaker:
            bad(f"{where}: speaker {speaker!r} != turn {it['t']} speaker {turn.speaker!r}")
        want = "commit" if speaker == owner else "ask"
        if it.get("kind") != want:
            bad(f"{where}: kind {it.get('kind')!r}, want {want!r} (commit iff speaker is the owner)")
        if want == "commit" and turn is not None and len(turn.text) < COMMIT_MIN_CHARS:
            bad(f"{where}: owner commitment turn {it['t']} is {len(turn.text)} chars, "
                f"want >= {COMMIT_MIN_CHARS}")
        kws = it.get("keywords")
        if not isinstance(kws, list) or not KEYWORDS_MIN <= len(kws) <= KEYWORDS_MAX:
            bad(f"{where}: keywords must be a list of {KEYWORDS_MIN}-{KEYWORDS_MAX} strings")
            kws = []
        normed: list[str] = []
        for kw in kws:
            if not isinstance(kw, str) or kw != kw.lower() or not ai.norm(kw):
                bad(f"{where}: keyword {kw!r} must be a non-empty lowercase string")
                continue
            if ai.norm(kw) in normed:
                bad(f"{where}: keyword {kw!r} repeats another keyword after norm()")
                continue
            normed.append(ai.norm(kw))
            if turn is not None and ai.norm(kw) not in ai.norm(turn.text):
                bad(f"{where}: keyword {kw!r} (norm {ai.norm(kw)!r}) is not in turn {it['t']} text")
        if turn is not None:
            per_turn.setdefault(turn.line, []).append((where, normed))

    # keywords of items sharing a turn must be disjoint and substring-free, and in a
    # multi-item or split (> split_chars) turn each keyword must occur exactly once,
    # so a bullet quoting one item's sentence cannot match another item
    for line, group in per_turn.items():
        turn = next(t for t in turns if t.line == line)
        ntext = ai.norm(turn.text)
        if len(group) > 1 or len(turn.text) > SPLIT_CHARS:
            for where, normed in group:
                for kw in normed:
                    c = ntext.count(kw)
                    if c > 1:
                        bad(f"{where}: keyword {kw!r} occurs {c} times in turn "
                            f"{wsconfig.hms(turn.t_sec)}; want 1 in a multi-item or split turn")
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                (wa, ka), (wb, kb) = group[a], group[b]
                for x in ka:
                    for y in kb:
                        if x in y or y in x:
                            bad(f"{wa} and {wb} share turn {wsconfig.hms(turn.t_sec)} but keywords "
                                f"{x!r} and {y!r} overlap")

    required = sum(1 for it in items if isinstance(it, dict) and it.get("optional") is False)
    if not GOLD_MIN <= required <= GOLD_MAX:
        bad(f"{GOLD}: {required} non-optional items, want {GOLD_MIN}-{GOLD_MAX}")
    if n_optional > MAX_OPTIONAL:
        bad(f"{GOLD}: {n_optional} optional items, want at most {MAX_OPTIONAL}")

    # -- distractors
    dists = gold.get("distractors") if isinstance(gold.get("distractors"), list) else []
    if not isinstance(gold.get("distractors"), list):
        bad(f"{GOLD}: distractors must be a list")
    gold_ts = {it.get("t") for it in items if isinstance(it, dict)}
    seen_d: set[str] = set()
    for n, ds in enumerate(dists):
        where = f"distractor[{n}] {ds.get('t') if isinstance(ds, dict) else ''}".rstrip()
        if not isinstance(ds, dict):
            bad(f"{where}: not an object")
            continue
        for k in sorted(DISTRACTOR_KEYS - set(ds)):
            bad(f"{where}: missing key {k!r}")
        for k in sorted(set(ds) - DISTRACTOR_KEYS):
            bad(f"{where}: unknown key {k!r}")
        if not isinstance(ds.get("why"), str) or not ds.get("why", "").strip():
            bad(f"{where}: why must be a non-empty string")
        _turn_at(ds.get("t"), by_time, where, bad)
        if ds.get("t") in gold_ts:
            bad(f"{where}: t is also a gold item's turn")
        if ds.get("t") in seen_d:
            bad(f"{where}: t listed twice")
        seen_d.add(ds.get("t"))
    if len(dists) < MIN_DISTRACTORS:
        bad(f"{GOLD}: {len(dists)} distractors, want at least {MIN_DISTRACTORS}")
    return out


def summary(fixture_dir: str | os.PathLike) -> str:
    """The one-line 'ok' summary of a fixture that validate() passed."""
    d = Path(fixture_dir)
    turns = wsconfig.parse_turns((d / TRANSCRIPT).read_text(encoding="utf-8"))
    gold = json.loads((d / GOLD).read_text(encoding="utf-8"))
    items = gold["items"]
    opt = sum(1 for it in items if it["optional"])
    return (f"ok {d.name}: {len(turns)} turns, {len(items)} gold ({opt} optional), "
            f"{len(gold['distractors'])} distractors")


def fixture_dirs() -> list[Path]:
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dirs = [Path(a) for a in args] if args else fixture_dirs()
    if not dirs:
        print(f"FAIL: no fixture directories under {FIXTURES_DIR}", file=sys.stderr)
        return 1
    failed = 0
    for d in dirs:
        problems = validate(d)
        if problems:
            failed += 1
            for p in problems:
                print(f"FAIL {Path(d).name}: {p}", file=sys.stderr)
        else:
            print(summary(d))
    if failed:
        print(f"{failed} of {len(dirs)} fixtures invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

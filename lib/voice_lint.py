"""Deterministic voice lint for message drafts: counts rule hits from a profile, no model calls.

A profile is a JSON object; every rule is off unless the profile sets it:

  max_words       int    hit when the text has more words than this
  max_paragraphs  int    hit when the text has more blank-line-separated paragraphs than this
  greeting        regex  hit when the text opens with it (case-insensitive), e.g. "(hi|hey)\\s+\\w+"
  closers         [str]  hit when the last paragraph contains any of them (case-insensitive)
  ask_phrases     [str]  hit when any appears as a whole phrase (case-insensitive)
  banned_words    [str]  hit when any appears as a whole word (case-insensitive)
  banned_chars    [str]  hit when any appears anywhere
  groups          {rule: label}  optional labels so callers can score rule groups separately

DEFAULT_PROFILE is what `whosaid drafts verify` uses (whosaid.toml [drafts.voice] merges over it).
Group B is the message shape (one paragraph, no greeting opener, no "attached page" / "Thanks!"
closer); group A is advisory (length, "want me to" asks, the em dash). banned_words is empty.

Links are reduced to their labels and bare URLs dropped before counting, so a long
link does not inflate the word count. The score is the number of rules hit.

CLI: python3 lib/voice_lint.py [--profile P.json] [--fail-over N] [FILE|-]
prints the result as JSON; exits 1 when the score is above --fail-over (default: never).
"""
import argparse, json, re, sys

RULES = ("max_words", "max_paragraphs", "greeting", "closers", "ask_phrases", "banned_words", "banned_chars")

DEFAULT_PROFILE = {
    "max_paragraphs": 1,
    "greeting": r"(hi|hey|hello)\s+(all|team|everyone|[A-Z]\w+)",
    "closers": ["attached page", "thanks!"],
    "max_words": 47,
    "ask_phrases": ["want me to", "do you want me to", "would you like me to"],
    "banned_words": [],
    "banned_chars": ["\u2014"],
    "groups": {"max_paragraphs": "B", "greeting": "B", "closers": "B",
               "max_words": "A", "ask_phrases": "A", "banned_words": "A", "banned_chars": "A"},
}


def normalize(text: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)   # [label](url) -> label
    t = re.sub(r"\s*\((https?://[^)\s]+)\)", "", t)                 # "x (url)" -> "x"
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def paragraphs(text: str) -> list:
    return [p for p in re.split(r"\n\s*\n", normalize(text)) if p.strip()]


def _phrase(p: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, p.split())) + r"(?!\w)", re.I)


def lint(text: str, profile: dict) -> dict:
    t = normalize(text)
    paras = paragraphs(text)
    words = len(t.split())
    hits = {}
    if profile.get("max_words") is not None and words > profile["max_words"]:
        hits["max_words"] = f"{words} words > {profile['max_words']}"
    if profile.get("max_paragraphs") is not None and len(paras) > profile["max_paragraphs"]:
        hits["max_paragraphs"] = f"{len(paras)} paragraphs > {profile['max_paragraphs']}"
    if profile.get("greeting") and re.match(profile["greeting"], t, re.I):
        hits["greeting"] = t.split("\n", 1)[0][:40]
    last = paras[-1].lower() if paras else ""
    found = [c for c in profile.get("closers") or [] if c.lower() in last]
    if found:
        hits["closers"] = found
    for rule in ("ask_phrases", "banned_words"):
        found = [p for p in profile.get(rule) or [] if _phrase(p).search(t)]
        if found:
            hits[rule] = found
    found = [c for c in profile.get("banned_chars") or [] if c in t]
    if found:
        hits["banned_chars"] = found
    groups = profile.get("groups") or {}
    by_group = {}
    for rule in hits:
        g = groups.get(rule)
        if g:
            by_group[g] = by_group.get(g, 0) + 1
    return {"score": len(hits), "hits": hits, "where": {r: _where(r, hits[r], paras) for r in hits},
            "by_group": by_group, "words": words, "paragraphs": len(paras)}


def _line_with(paras: list, pred) -> str:
    for p in paras:
        for line in p.split("\n"):
            if pred(line):
                return line.strip()[:80]
    return ""


def _where(rule: str, hit, paras: list) -> str:
    """The line a hit points at, so a caller can quote it."""
    if not paras:
        return ""
    if rule == "max_paragraphs":
        return paras[1].split("\n", 1)[0].strip()[:80]
    if rule in ("greeting", "max_words"):
        return paras[0].split("\n", 1)[0].strip()[:80]
    if rule == "closers":
        return _line_with(paras[-1:], lambda l: any(c.lower() in l.lower() for c in hit))
    if rule in ("ask_phrases", "banned_words"):
        found = _line_with(paras, lambda l: any(_phrase(x).search(l) for x in hit))
        return found or paras[0].split("\n", 1)[0].strip()[:80]  # a phrase can wrap a line
    if rule == "banned_chars":
        return _line_with(paras, lambda l: any(c in l for c in hit))
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic voice lint for a message draft.")
    ap.add_argument("file", nargs="?", default="-")
    ap.add_argument("--profile", help="JSON profile; every rule is off without one")
    ap.add_argument("--fail-over", type=int, default=None, help="exit 1 when the score is above this")
    a = ap.parse_args(argv)
    profile = json.load(open(a.profile)) if a.profile else {}
    unknown = set(profile) - set(RULES) - {"groups"}
    if unknown:
        print(f"unknown profile keys: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    text = sys.stdin.read() if a.file == "-" else open(a.file).read()
    r = lint(text, profile)
    print(json.dumps(r, indent=1, ensure_ascii=False))
    return 1 if a.fail_over is not None and r["score"] > a.fail_over else 0


if __name__ == "__main__":
    sys.exit(main())

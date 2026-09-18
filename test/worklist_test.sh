#!/bin/bash
#
# test/worklist_test.sh: offline test for the ranked personal worklist
# (lib/workspace.py worklist + the roll-up's _WORKLIST-<Owner>.md, the
# `whosaid commitments` launcher command, and the semantic union dedupe).
#
# Fully offline and self-contained: synthetic dated meeting folders with
# hand-written commitments.json and action-items.md, placeholder speakers
# (Alice_Example / Bob_Example / Carol_Example). No audio, no models, no
# network: WHOSAID_OLLAMA points at a closed port so the dedupe is difflib
# only unless a section opts into WHOSAID_EMBED_FAKE=1 (a deterministic
# bag-of-words embedder that stands in for a local model).
#
# Sections:
#   1. guards + module sanity
#   2. unit rules: name matching, cue strength, deadline/blocking cues,
#      tier assignment per rule, negative never P1, [commitments] overrides
#   3. roll-up writes _WORKLIST-<Owner>.md: owner from the self role, union
#      of CM + AI items (name match, '_'/' ' interchangeable, aliases),
#      tiers, ordering, why strings, line shape, --all-owners
#   4. the worklist is a regenerated view: a hand edit in it is overwritten,
#      a hand edit in _COMMITMENTS.md still flows through (Done / history)
#   5. owner resolution: --owner NAME, me -> self role, toml owner fallback,
#      no owner at all
#   6. semantic union dedupe: a reworded action item folds into the matching
#      commitment as "(also AI-NNN)" under WHOSAID_EMBED_FAKE=1 and stays its
#      own line with the embed server unreachable
#   7. --json shape and --all-owners --json
#   8. `whosaid commitments` launcher dispatch + help
#   9. determinism: a second roll-up rewrites nothing
#
# macOS/BSD only: BSD grep/sed, bash 3.2 (no associative arrays). Python
# checker scripts are written to files, not inline in "$( ... )", because
# bash 3.2 quote-scans heredoc bodies inside command substitution and the
# fixtures contain apostrophes ("I'll", "I won't").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WS_PY="$REPO/lib/workspace.py"
WHOSAID="$REPO/whosaid"

PASS=0
TEST_FAILED=0
TMP="$(mktemp -d)"

export WHOSAID_OLLAMA="http://127.0.0.1:9"   # closed port: difflib-only dedupe
unset WHOSAID_EMBED_FAKE WHOSAID_OWNER

cleanup() {
  if [ "$TEST_FAILED" -eq 0 ]; then
    rm -rf "$TMP"
  else
    echo "" >&2
    echo "FAIL: $PASS check(s) passed before the failure; leaving temp dir for inspection: $TMP" >&2
  fi
}
trap cleanup EXIT

fail() {
  TEST_FAILED=1
  echo "" >&2
  echo "FAIL: $1" >&2
  exit 1
}

assert_eq() {  # assert_eq <actual> <expected> <what>
  if [ "$1" = "$2" ]; then
    PASS=$((PASS + 1))
  else
    fail "$3: expected [$2], got [$1]"
  fi
}

assert_has() {  # assert_has <fixed-string> <file> <what>
  if grep -qF -- "$1" "$2"; then
    PASS=$((PASS + 1))
  else
    fail "$3: [$1] not found in $2"
  fi
}

assert_not_has() {  # assert_not_has <fixed-string> <file> <what>
  if grep -qF -- "$1" "$2"; then
    fail "$3: [$1] unexpectedly found in $2"
  else
    PASS=$((PASS + 1))
  fi
}

assert_file() {  # assert_file <path> <what>
  if [ -f "$1" ]; then
    PASS=$((PASS + 1))
  else
    fail "$2: file missing: $1"
  fi
}

assert_absent() {  # assert_absent <path> <what>
  if [ -e "$1" ]; then
    fail "$2: unexpectedly exists: $1"
  else
    PASS=$((PASS + 1))
  fi
}

# run_ws <args...>: run lib/workspace.py; rc in RC, stdout in OUT, stderr in ERR.
run_ws() {
  set +e
  OUT="$(python3 "$WS_PY" "$@" 2> "$TMP/.last.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.last.err")"
}

# run_w <args...>: run the whosaid launcher; rc in RC, combined output in OUT.
run_w() {
  set +e
  OUT="$("$WHOSAID" "$@" 2>&1)"
  RC=$?
  set -e
}

# run_pycheck <script-path> <args...>: run a checker script; print its stdout.
run_pycheck() {
  local script="$1"
  shift
  python3 "$script" "$@"
}

# make_ws <dir>: the main fixture. Three meetings; Alice_Example is the
# self-roled speaker, Bob_Example the boss, Carol_Example a peer.
make_ws() {
  local ws="$1"
  mkdir -p "$ws/2026-09-14-0900" "$ws/2026-09-15-0900" "$ws/2026-09-16-0900"
  cat > "$ws/whosaid.toml" <<'EOF'
[workspace]
owner = "Alice_Example"
aliases = ["Ali"]

[groups]
leadership = ["Bob_Example"]
team = ["Carol_Example"]
EOF
  cat > "$ws/2026-09-14-0900/commitments.json" <<'EOF'
{
  "roles": {"Alice_Example": "self", "Bob_Example": "boss", "Carol_Example": "peer"},
  "items": [
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I'll send the exec deck update tomorrow",
     "time": "00:00:04", "cue": "i'll send", "negative": false, "priority": "high",
     "requested_by": "Bob_Example", "requested_by_role": "boss"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I can look at the flaky test",
     "time": "00:00:09", "cue": "i can", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I won't rewrite the parser",
     "time": "00:00:19", "cue": "i won't", "negative": true, "priority": "normal"},
    {"speaker": "Carol_Example", "speaker_role": "peer", "text": "I'll fix the prod outage runbook",
     "time": "00:00:29", "cue": "i'll", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "let me draft the retro notes",
     "time": "00:00:39", "cue": "let me", "negative": false, "priority": "normal"}
  ]
}
EOF
  cat > "$ws/2026-09-15-0900/commitments.json" <<'EOF'
{
  "roles": {"Alice_Example": "self"},
  "items": [
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I can look at the flaky test",
     "time": "00:00:04", "cue": "i can", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "let me draft the retro notes",
     "time": "00:00:09", "cue": "let me", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I'll write the rollout runbook for the platform team",
     "time": "00:00:14", "cue": "i'll", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I could help with the migration script",
     "time": "00:00:19", "cue": "i could", "negative": false, "priority": "normal",
     "requested_by": "Carol_Example", "requested_by_role": "peer"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I can send the metrics summary",
     "time": "00:00:24", "cue": "i can", "negative": false, "priority": "normal",
     "requested_by": "Bob_Example", "requested_by_role": ""}
  ]
}
EOF
  cat > "$ws/2026-09-16-0900/commitments.json" <<'EOF'
{
  "roles": {"Alice_Example": "self"},
  "items": [
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I can look at the flaky test",
     "time": "00:00:04", "cue": "i can", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I'll own the customer escalation",
     "time": "00:00:09", "cue": "i'll own", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I plan to tidy the README",
     "time": "00:00:14", "cue": "i plan to", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self", "text": "I will pair with Bob on the review",
     "time": "00:00:19", "cue": "i will", "negative": false, "priority": "normal"}
  ]
}
EOF
  cat > "$ws/2026-09-16-0900/action-items.md" <<'EOF'
# Action items

## 1. Asks from leadership (Bob)

- **Ali:** for the platform team write the rollout runbook
- **Alice_Example:** book the offsite room

## 2. Team

- **Carol_Example:** rotate the on-call schedule
- **Alice Example:** update the onboarding checklist
EOF
}

echo "== worklist_test: temp dir $TMP =="

# ---------------------------------------------------------------------------
# 1. Guards + module sanity.
# ---------------------------------------------------------------------------
echo "-- guards --"

command -v python3 >/dev/null 2>&1 || fail "python3 is required"
[ -f "$WS_PY" ] || fail "lib/workspace.py missing at $WS_PY"
[ -x "$WHOSAID" ] || fail "whosaid launcher missing or not executable at $WHOSAID"
python3 -m py_compile "$WS_PY" || fail "python3 -m py_compile failed on lib/workspace.py"
bash -n "$WHOSAID" || fail "bash -n failed on the whosaid launcher"
PASS=$((PASS + 1))

run_ws worklist --help
assert_eq "$RC" 0 "'workspace.py worklist --help' exits 0"
printf '%s\n' "$OUT" > "$TMP/help.txt"
assert_has "--all-owners" "$TMP/help.txt" "worklist --help documents --all-owners"
assert_has "--json" "$TMP/help.txt" "worklist --help documents --json"

# ---------------------------------------------------------------------------
# 2. Unit rules, straight from the module.
# ---------------------------------------------------------------------------
echo "-- unit rules --"

cat > "$TMP/check_rules.py" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import workspace as w

# name matching: case, '_' vs ' ', single-token first-name rule
assert w.same_person("Alice_Example", "alice example")
assert w.same_person("Bob", "Bob_Example") and w.same_person("Bob_Example", "bob")
assert not w.same_person("Bob_Example", "Bob_Other")
assert not w.same_person("", "Bob_Example")

# cue strength
assert w.cue_strength("i'll send") == "strong" and w.cue_strength("i will") == "strong"
assert w.cue_strength("let me") == "weak" and w.cue_strength("i plan to") == "weak"
assert w.cue_strength("") == "" and w.cue_strength("i won't") == ""

# deadline cues: literal list + structural dates; blocking cues word-bounded
cfg = w.commitments_config({})
assert w.deadline_cue("send it tomorrow", cfg["deadline_cues"]) == "tomorrow"
assert w.deadline_cue("ship by end of day", cfg["deadline_cues"]) == "end of day"
assert w.deadline_cue("I'll have it by Friday", cfg["deadline_cues"]) == "by friday"
assert w.deadline_cue("due 2026-09-30 at the latest", cfg["deadline_cues"]) == "2026-09-30"
assert w.deadline_cue("on sept 3 we start", cfg["deadline_cues"]) == "on sept 3"
assert w.deadline_cue("todays notes", cfg["deadline_cues"]) == "", "no substring hits"
assert w.cue_hit("fix the prod outage", cfg["blocking_cues"]) == "prod"
assert w.cue_hit("improve the product page", cfg["blocking_cues"]) == "", "prod must not match product"

# tiers per rule; latest meeting is M3
def entry(**kw):
    e = {"id": kw.pop("id", "CM-001"), "source": "commitments", "text": kw.pop("text", "do the thing"),
         "status": "open", "open": True, "merged_into": "", "first_seen": "M1", "last_seen": "M1",
         "meetings": {"M1"}, "requested_by": "", "requested_by_role": "", "priority": "normal",
         "cue": "", "negative": False, "type": "", "also": [], "_norm": ""}
    e.update(kw)
    return e

r = w.Ranker({"groups": {"leadership": ["Bob_Example"]}}, "M3")
def tier(e):
    r.rank(e)
    return e["tier"], e["score"], e["why"]

assert tier(entry(priority="high")) == ("P1", 5, ["boss"])
assert tier(entry(requested_by="Bob_Example", requested_by_role="boss")) == ("P1", 5, ["boss"])
assert tier(entry(requested_by="Bob_Example")) == ("P1", 5, ["boss"]), "leadership group stands in for roles"
assert tier(entry(requested_by="Bob_Example", requested_by_role="peer"))[0] == "P2", "a registry role wins over the group"
assert tier(entry(text="fix the prod outage")) == ("P1", 4, ["blocking=prod"])
assert tier(entry(text="send it by friday")) == ("P1", 4, ["due=by friday"])
assert tier(entry(meetings={"M1", "M2", "M3"}, last_seen="M3")) == ("P1", 5, ["3 meetings", "latest meeting"])
assert tier(entry(meetings={"M1", "M2"}, last_seen="M2")) == ("P2", 2, ["2 meetings"])
assert tier(entry(requested_by="Carol_Example", requested_by_role="peer")) == ("P2", 1, ["asked by Carol_Example"])
assert tier(entry(cue="i'll own", last_seen="M3")) == ("P2", 2, ["latest meeting", "strong cue"])
assert tier(entry(cue="i'll own", last_seen="M1")) == ("P3", 1, ["strong cue"]), "strong cue alone is not P2"
assert tier(entry(cue="let me", last_seen="M3")) == ("P3", 1, ["latest meeting"]), "weak cue earns nothing"
assert tier(entry()) == ("P3", 0, [])
assert tier(entry(priority="high", negative=True)) == ("P2", 2, ["boss", "negative"]), "negative is never P1"
assert tier(entry(negative=True)) == ("P3", -3, ["negative"])
assert tier(entry(source="action-items", type="Asks from leadership")) == ("P1", 5, ["boss"])

# ordering: tier, score desc, last_seen desc, id
es = [entry(id="CM-003", cue="i'll", last_seen="M3"), entry(id="CM-002", priority="high"),
      entry(id="CM-001"), entry(id="AI-001", source="action-items", last_seen="M3"),
      entry(id="CM-004", status="done", open=False, last_seen="M2")]
w.rank_entries(es, r)
assert [e["id"] for e in es] == ["CM-002", "CM-003", "AI-001", "CM-001", "CM-004"], [e["id"] for e in es]
assert es[-1]["tier"] == "" and es[-1]["why"] == [], "history entries carry no tier"

# [commitments] overrides: lists replace, weights merge, embed_threshold parses
c = w.commitments_config({"commitments": {"boss": ["Carol_Example"], "blocking_cues": "kraken, wumpus",
                                          "weights": {"boss": 7, "bogus": "x"}, "embed_threshold": "0.8"}})
assert c["boss"] == ["carol_example"] and c["blocking_cues"] == ["kraken", "wumpus"], c
assert c["weights"]["boss"] == 7 and c["weights"]["deadline"] == 4 and "bogus" not in c["weights"], c["weights"]
assert c["embed_threshold"] == 0.8 and c["deadline_cues"] == w.WORKLIST_DEFAULTS["deadline_cues"], c
r2 = w.Ranker({"commitments": {"boss": ["Carol_Example"], "blocking_cues": ["kraken"],
                               "weights": {"boss": 7}}}, "M3")
e = entry(requested_by="Carol_Example"); r2.rank(e)
assert (e["tier"], e["score"]) == ("P1", 7), e
e = entry(text="poke the kraken service"); r2.rank(e)
assert e["why"] == ["blocking=kraken"], e
e = entry(text="fix the prod outage"); r2.rank(e)
assert e["tier"] == "P3", "an overridden blocking list drops the default cues"

# md line shape keeps the corpus prefix so parse_commitments_md still reads it
e = entry(id="CM-002", first_seen="2026-09-14-1802", last_seen="2026-09-21-1802",
          meetings={"2026-09-14-1802", "2026-09-21-1802"}, priority="high", text="update the exec deck",
          also=["AI-012"])
r.rank(e)
line = w.worklist_line(e)
assert line == ("- **CM-002** [open] 2026-09-14-1802 → 2026-09-21-1802 (2×) P1 · boss · 2 meetings: "
                "update the exec deck (also AI-012)"), line
parsed = w.parse_commitments_md(line)
assert parsed["CM-002"]["status"] == "open" and parsed["CM-002"]["text"].startswith("update the exec deck"), parsed
assert w.worklist_filename("Alice Example") == "_WORKLIST-Alice_Example.md"

# fold-time "stronger cue" upgrade reads the same [commitments] lists as Ranker
def fold_twice(cues):
    items, nid = [], [1]
    w.fold_commitments("M1", [{"text": "ship the exec deck", "speaker": "Alice_Example", "cue": "i'll send"}], items, nid)
    w.fold_commitments("M2", [{"text": "ship the exec deck", "speaker": "Alice_Example", "cue": "let me"}], items, nid, cues=cues)
    assert len(items) == 1, items
    return items[0].cue
assert fold_twice(None) == "i'll send", "default lists: 'let me' is weak, the strong cue stays"
flipped = w.commitments_config({"commitments": {"strong_cues": ["let me"], "weak_cues": ["i'll send"]}})
assert fold_twice(flipped) == "let me", "toml-flipped cue lists drive the fold-time upgrade too"
print("ok")
PY
RULES="$(run_pycheck "$TMP/check_rules.py" "$REPO/lib")" || fail "unit rules check crashed"
assert_eq "$RULES" "ok" "name matching, cues, tiers per rule, ordering, overrides, line shape"

# ---------------------------------------------------------------------------
# 3. Roll-up writes _WORKLIST-<Owner>.md for the self-roled speaker.
# ---------------------------------------------------------------------------
echo "-- roll-up worklist --"

WS="$TMP/ws"
make_ws "$WS"
run_ws rollup "$WS" --action-items
assert_eq "$RC" 0 "rollup exit code"
WL="$WS/_WORKLIST-Alice_Example.md"
assert_file "$WL" "_WORKLIST-Alice_Example.md written (owner = the self-roled speaker)"
assert_absent "$WS/_WORKLIST-Carol_Example.md" "no per-participant files without --all-owners"
assert_has "# Worklist: Alice_Example" "$WL" "worklist header names the owner"
assert_has "Tiers: P1 = boss-requested" "$WL" "header states the tier rules"
assert_has "rebuilt on every roll-up" "$WL" "header says the file is a regenerated view"
assert_has "_Built from 3 meeting(s): 2026-09-14-0900 → 2026-09-16-0900._" "$WL" "header lists the meetings folded"
assert_has "## P1" "$WL" "P1 section"
assert_has "## P2" "$WL" "P2 section"
assert_has "## P3" "$WL" "P3 section"
assert_has "## Done / history" "$WL" "Done / history section"
assert_has "- **CM-001** [open] 2026-09-14-0900 (1×) P1 · boss · due=tomorrow · strong cue: I'll send the exec deck update tomorrow" "$WL" \
  "boss-requested item with a deadline cue renders as P1 with its why strings"
assert_has "- **CM-002** [open] 2026-09-14-0900 → 2026-09-16-0900 (3×) P1 · 3 meetings · latest meeting: I can look at the flaky test" "$WL" \
  "an item seen in 3 meetings is P1 with the span and count"
assert_has "- **CM-008** [open] 2026-09-15-0900 (1×) P1 · boss: I can send the metrics summary" "$WL" \
  "a requester in [groups] leadership counts as boss when the item has no role"
assert_has "- **CM-009** [open] 2026-09-16-0900 (1×) P1 · blocking=customer · latest meeting · strong cue: I'll own the customer escalation" "$WL" \
  "a blocking cue is P1"
assert_has "- **AI-001** [open] 2026-09-16-0900 (1×) P1 · boss · latest meeting: for the platform team write the rollout runbook" "$WL" \
  "an action item owned by an alias (Ali) joins the worklist; a leadership ask counts as boss"
assert_has "- **AI-004** [open] 2026-09-16-0900 (1×) P3 · latest meeting: update the onboarding checklist" "$WL" \
  "an action item owned as 'Alice Example' (space) joins the worklist"
assert_has "- **CM-011** [open] 2026-09-16-0900 (1×) P2 · latest meeting · strong cue: I will pair with Bob on the review" "$WL" \
  "a strong cue in the latest meeting is P2"
assert_has "- **CM-005** [open] 2026-09-14-0900 → 2026-09-15-0900 (2×) P2 · 2 meetings: let me draft the retro notes" "$WL" \
  "an item seen in 2 meetings is P2"
assert_has "- **CM-007** [open] 2026-09-15-0900 (1×) P2 · asked by Carol_Example: I could help with the migration script" "$WL" \
  "requested by a peer is P2"
assert_has "- **CM-003** [open] 2026-09-14-0900 (1×) P3 · negative: I won't rewrite the parser" "$WL" \
  "a negated commitment is kept, marked, and never P1"
assert_not_has "CM-004" "$WL" "another speaker's commitment stays out of the owner's worklist"
assert_not_has "AI-003" "$WL" "another owner's action item stays out"
assert_not_has "**CM-006** [open] 2026-09-15-0900 (1×) P3 · strong cue: I'll write the rollout runbook for the platform team (also" "$WL" \
  "difflib only: the reworded action item is not folded into CM-006"

cat > "$TMP/check_order.py" <<'PY'
import re, sys
from pathlib import Path
md = Path(sys.argv[1]).read_text()
sections = {}
cur = None
for ln in md.splitlines():
    if ln.startswith("## "):
        cur = ln[3:]
        sections[cur] = []
    elif cur and ln.startswith("- **"):
        sections[cur].append(re.match(r"- \*\*([A-Z]+-\d+)\*\*", ln).group(1))
assert sections["P1"] == ["CM-001", "AI-001", "AI-002", "CM-009", "CM-002", "CM-008"], sections["P1"]
assert sections["P2"] == ["CM-011", "CM-005", "CM-007"], sections["P2"]
assert sections["P3"] == ["AI-004", "CM-010", "CM-006", "CM-003"], sections["P3"]
assert sections["Done / history"] == [], sections["Done / history"]
print("ok")
PY
ORDER="$(run_pycheck "$TMP/check_order.py" "$WL")" || fail "ordering check crashed"
assert_eq "$ORDER" "ok" "within a tier: score desc, then last_seen desc, then id"

cat > "$TMP/check_corpus_fields.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
by_id = {it["id"]: it for it in d["items"]}
assert by_id["CM-001"]["cue"] == "i'll send" and by_id["CM-001"]["requested_by_role"] == "boss", by_id["CM-001"]
assert by_id["CM-003"]["negative"] is True and by_id["CM-002"]["negative"] is False, by_id["CM-003"]
assert by_id["CM-008"]["requested_by"] == "Bob_Example" and by_id["CM-008"]["requested_by_role"] == "", by_id["CM-008"]
print("ok")
PY
FIELDS="$(run_pycheck "$TMP/check_corpus_fields.py" "$WS")" || fail "corpus fields check crashed"
assert_eq "$FIELDS" "ok" "_commitments.json persists cue, negative and requested_by_role for ranking"

run_ws rollup "$WS" --action-items --all-owners
assert_eq "$RC" 0 "rollup --all-owners exit code"
assert_file "$WS/_WORKLIST-Carol_Example.md" "--all-owners writes Carol_Example's worklist"
assert_absent "$WS/_WORKLIST-Ali.md" "--all-owners folds the alias label into the owner instead of a file of its own"
assert_absent "$WS/_WORKLIST-Alice_Example_.md" "--all-owners does not split 'Alice Example' from the owner"
assert_has "- **CM-004** [open] 2026-09-14-0900 (1×) P1 · blocking=prod · strong cue: I'll fix the prod outage runbook" \
  "$WS/_WORKLIST-Carol_Example.md" "Carol's commitment ranks in her own worklist"
assert_has "- **AI-003** [open] 2026-09-16-0900 (1×) P3 · latest meeting: rotate the on-call schedule" \
  "$WS/_WORKLIST-Carol_Example.md" "Carol's action item joins her worklist"
assert_absent "$WS/_WORKLIST-Bob_Example.md" "a requester with no items of their own gets no file"

# ---------------------------------------------------------------------------
# 4. A regenerated view, never reconciled; corpus hand edits still flow through.
# ---------------------------------------------------------------------------
echo "-- regenerated view --"

sed -i '' 's/\*\*CM-002\*\* \[open\]/**CM-002** [done]/' "$WL" || fail "sed failed to hand-edit the worklist"
PASS=$((PASS + 1))
run_ws rollup "$WS" --action-items
assert_eq "$RC" 0 "rollup after a worklist hand edit exit code"
assert_has "- **CM-002** [open]" "$WL" "a hand edit in the worklist is overwritten on the next roll-up (view, not store)"
assert_has "**CM-002** [open]" "$WS/_COMMITMENTS.md" "the corpus ignores worklist hand edits"

sed -i '' 's/\*\*CM-003\*\* \[open\]/**CM-003** [done]/' "$WS/_COMMITMENTS.md" || fail "sed failed to hand-edit _COMMITMENTS.md"
PASS=$((PASS + 1))
run_ws rollup "$WS" --action-items
assert_eq "$RC" 0 "rollup after a _COMMITMENTS.md hand edit exit code"
assert_has "- **CM-003** [done] 2026-09-14-0900 (1×): I won't rewrite the parser" "$WL" \
  "a [done] hand edit in _COMMITMENTS.md moves the item to Done / history (no tier)"
assert_not_has "P3 · negative: I won't rewrite the parser" "$WL" "the done item leaves the open tiers"

# ---------------------------------------------------------------------------
# 5. Owner resolution.
# ---------------------------------------------------------------------------
echo "-- owner resolution --"

run_ws worklist "$WS" --owner Carol_Example
assert_eq "$RC" 0 "worklist --owner NAME exit code"
printf '%s\n' "$OUT" > "$TMP/carol.md"
assert_has "# Worklist: Carol_Example" "$TMP/carol.md" "--owner NAME renders that person"
run_ws worklist "$WS" --owner me
assert_eq "$RC" 0 "worklist --owner me exit code"
printf '%s\n' "$OUT" > "$TMP/me.md"
assert_has "# Worklist: Alice_Example" "$TMP/me.md" "--owner me resolves to the self-roled speaker"

TOMLWS="$TMP/ws-toml"
mkdir -p "$TOMLWS/2026-09-15-0900"
printf '[workspace]\nowner = "Bob_Example"\n' > "$TOMLWS/whosaid.toml"
cat > "$TOMLWS/2026-09-15-0900/commitments.json" <<'EOF'
{"roles": {}, "items": [
  {"speaker": "Bob_Example", "speaker_role": null, "text": "I'll send the agenda", "time": "00:00:04",
   "cue": "i'll send", "negative": false, "priority": "normal"}
]}
EOF
run_ws rollup "$TOMLWS"
assert_eq "$RC" 0 "rollup (toml owner, no roles) exit code"
assert_file "$TOMLWS/_WORKLIST-Bob_Example.md" "no self role: the owner falls back to [workspace] owner"
export WHOSAID_OWNER="Carol_Example"
run_ws worklist "$TOMLWS"
unset WHOSAID_OWNER
assert_eq "$RC" 0 "worklist with WHOSAID_OWNER exit code"
printf '%s\n' "$OUT" > "$TMP/envowner.md"
assert_has "# Worklist: Carol_Example" "$TMP/envowner.md" "WHOSAID_OWNER overrides the toml owner (wsconfig rule)"

NOWS="$TMP/ws-noowner"
mkdir -p "$NOWS/2026-09-15-0900"
cp "$TOMLWS/2026-09-15-0900/commitments.json" "$NOWS/2026-09-15-0900/"
run_ws rollup "$NOWS"
assert_eq "$RC" 0 "rollup without any owner still exits 0"
printf '%s\n' "$ERR" > "$TMP/noowner.err"
assert_has "worklist skipped: no owner" "$TMP/noowner.err" "roll-up notes the missing owner instead of failing"
assert_eq "$(ls "$NOWS" | grep -c '^_WORKLIST-' || true)" "0" "no worklist file without an owner"
run_ws worklist "$NOWS"
assert_eq "$RC" 1 "worklist without any owner exits 1"
printf '%s\n' "$ERR" > "$TMP/noowner2.err"
assert_has "pass --owner NAME" "$TMP/noowner2.err" "worklist explains how to name an owner"
run_ws rollup "$NOWS" --all-owners
assert_eq "$RC" 0 "rollup --all-owners without an owner exit code"
assert_file "$NOWS/_WORKLIST-Bob_Example.md" "--all-owners needs no configured owner"

# ---------------------------------------------------------------------------
# 6. Semantic union dedupe: fake embeddings fold the reworded action item into
#    its commitment; the unreachable embed server keeps them apart.
# ---------------------------------------------------------------------------
echo "-- semantic union dedupe --"

cat > "$TMP/check_sem_fixture.py" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import workspace as w
a = w.normalize_text("I'll write the rollout runbook for the platform team")
b = w.normalize_text("for the platform team write the rollout runbook")
r = w.similarity(a, b)
c = w.cosine(w.fake_embed(a), w.fake_embed(b))
assert r < 0.82 <= 0.90 <= c, (r, c)
print("ok")
PY
SEMFIX="$(run_pycheck "$TMP/check_sem_fixture.py" "$REPO/lib")" || fail "semantic fixture check crashed"
assert_eq "$SEMFIX" "ok" "reworded pair: difflib < 0.82, fake cosine >= 0.90"

export WHOSAID_EMBED_FAKE=1
run_ws worklist "$WS"
unset WHOSAID_EMBED_FAKE
assert_eq "$RC" 0 "worklist (fake embeddings) exit code"
printf '%s\n' "$OUT" > "$TMP/fake.md"
printf '%s\n' "$ERR" > "$TMP/fake.err"
assert_has "fake embeddings" "$TMP/fake.err" "worklist logs the fake-embedding rule"
assert_has "- **CM-006** [open] 2026-09-15-0900 → 2026-09-16-0900 (2×) P1 · boss · 2 meetings · latest meeting · strong cue: I'll write the rollout runbook for the platform team (also AI-001)" \
  "$TMP/fake.md" "the reworded action item folds into its commitment as (also AI-001), lending its meeting and leadership ask"
assert_not_has "- **AI-001**" "$TMP/fake.md" "the folded action item has no line of its own"

run_ws worklist "$WS"
assert_eq "$RC" 0 "worklist (embed server unreachable) exit code"
printf '%s\n' "$OUT" > "$TMP/difflib.md"
printf '%s\n' "$ERR" > "$TMP/difflib.err"
assert_has "not reachable" "$TMP/difflib.err" "worklist logs the difflib-only fallback"
assert_has "- **AI-001** [open]" "$TMP/difflib.md" "difflib fallback keeps the action item as its own line"
assert_not_has "(also AI-001)" "$TMP/difflib.md" "difflib fallback folds nothing across sources"

# ---------------------------------------------------------------------------
# 7. --json shape.
# ---------------------------------------------------------------------------
echo "-- json --"

run_ws worklist "$WS" --json
assert_eq "$RC" 0 "worklist --json exit code"
printf '%s\n' "$OUT" > "$TMP/wl.json"
cat > "$TMP/check_json.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert set(d) == {"owner", "generated_from", "items"}, sorted(d)
assert d["owner"] == "Alice_Example", d["owner"]
assert d["generated_from"] == ["2026-09-14-0900", "2026-09-15-0900", "2026-09-16-0900"], d["generated_from"]
keys = {"id", "source", "text", "status", "tier", "score", "why", "first_seen", "last_seen",
        "occurrences", "requested_by", "negative", "also", "merged_into"}
assert all(set(it) == keys for it in d["items"]), [sorted(it) for it in d["items"]][:1]
by_id = {it["id"]: it for it in d["items"]}
assert by_id["CM-001"]["tier"] == "P1" and by_id["CM-001"]["score"] == 10, by_id["CM-001"]
assert by_id["CM-001"]["why"] == ["boss", "due=tomorrow", "strong cue"], by_id["CM-001"]
assert by_id["CM-001"]["requested_by"] == "Bob_Example" and by_id["CM-001"]["source"] == "commitments"
assert by_id["CM-002"]["occurrences"] == 3 and by_id["CM-002"]["first_seen"] == "2026-09-14-0900"
assert by_id["CM-003"]["status"] == "done" and by_id["CM-003"]["tier"] == "" and by_id["CM-003"]["negative"] is True
assert by_id["AI-001"]["source"] == "action-items" and by_id["AI-001"]["tier"] == "P1", by_id["AI-001"]
assert [it["id"] for it in d["items"]][:6] == ["CM-001", "AI-001", "AI-002", "CM-009", "CM-002", "CM-008"], [it["id"] for it in d["items"]]
assert [it["id"] for it in d["items"]][-1] == "CM-003", "history sorts last"
assert isinstance(by_id["CM-001"]["score"], int), type(by_id["CM-001"]["score"])
print("ok")
PY
JSONCHECK="$(run_pycheck "$TMP/check_json.py" "$TMP/wl.json")" || fail "json shape check crashed"
assert_eq "$JSONCHECK" "ok" "--json emits {owner, generated_from, items[...]} with the documented item keys"

run_ws worklist "$WS" --all-owners --json
assert_eq "$RC" 0 "worklist --all-owners --json exit code"
printf '%s\n' "$OUT" > "$TMP/all.json"
cat > "$TMP/check_all_json.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
owners = [o["owner"] for o in d["owners"]]
assert owners == ["Alice_Example", "Carol_Example"], owners
assert all(set(o) == {"owner", "generated_from", "items"} for o in d["owners"]), d["owners"][0].keys()
assert [it["id"] for it in d["owners"][1]["items"]] == ["CM-004", "AI-003"], d["owners"][1]["items"]
print("ok")
PY
ALLJSON="$(run_pycheck "$TMP/check_all_json.py" "$TMP/all.json")" || fail "all-owners json check crashed"
assert_eq "$ALLJSON" "ok" "--all-owners --json wraps one payload per participant (owner first, aliases folded)"

run_ws worklist "$WS" --json -o "$TMP/out.json"
assert_eq "$RC" 0 "worklist -o exit code"
assert_file "$TMP/out.json" "-o writes the file"
assert_eq "$OUT" "" "-o prints nothing on stdout"

# ---------------------------------------------------------------------------
# 8. `whosaid commitments` launcher dispatch + help.
# ---------------------------------------------------------------------------
echo "-- launcher --"

run_w commitments "$WS" --json
assert_eq "$RC" 0 "'whosaid commitments <ws> --json' exits 0 (output: $OUT)"
printf '%s\n' "$OUT" | sed -n '/^{/,$p' > "$TMP/launcher.json"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['owner']=='Alice_Example' and d['items'], d" "$TMP/launcher.json" \
  || fail "'whosaid commitments' JSON is not the worklist payload"
PASS=$((PASS + 1))
run_w commitments "$WS" --owner Carol_Example
assert_eq "$RC" 0 "'whosaid commitments <ws> --owner NAME' exits 0"
printf '%s\n' "$OUT" > "$TMP/launcher_carol.md"
assert_has "# Worklist: Carol_Example" "$TMP/launcher_carol.md" "the launcher forwards --owner"
run_w worklist "$WS" --owner Carol_Example
assert_eq "$RC" 0 "'whosaid worklist' is an alias of 'whosaid commitments'"
run_w commitments --help
assert_eq "$RC" 0 "'whosaid commitments --help' exits 0"
printf '%s\n' "$OUT" > "$TMP/launcher_help.txt"
assert_has "COMMITMENTS" "$TMP/launcher_help.txt" "'whosaid commitments --help' prints the COMMITMENTS section"
assert_has "P1  boss-requested" "$TMP/launcher_help.txt" "the help section documents the tiers"
assert_has "[commitments]" "$TMP/launcher_help.txt" "the help section documents the whosaid.toml [commitments] keys"
run_w commitments
assert_eq "$RC" 1 "'whosaid commitments' with no workspace exits 1"
run_w help
printf '%s\n' "$OUT" > "$TMP/help_all.txt"
assert_has "whosaid commitments <ws> [--owner NAME|me] [--all-owners] [--json] [-o FILE]" "$TMP/help_all.txt" \
  "'whosaid help' lists the commitments command"
assert_has "_WORKLIST-<Owner>.md" "$TMP/help_all.txt" "'whosaid help' names the worklist file"
assert_has "ingest --action-items" "$TMP/help_all.txt" "'whosaid help' WATCH section still names the ingest flags"
assert_has "--commitments', then roll-up and index" "$TMP/help_all.txt" "'whosaid help' WATCH section says ingest passes --commitments"

# ---------------------------------------------------------------------------
# 9. Determinism: a second roll-up (with --all-owners) rewrites nothing.
# ---------------------------------------------------------------------------
echo "-- determinism --"

run_ws rollup "$WS" --action-items --all-owners
assert_eq "$RC" 0 "rollup (settle) exit code"
shasum "$WS/_INDEX.md" "$WS/_COMMITMENTS.md" "$WS/_commitments.json" "$WS/_ACTION-ITEMS.md" \
  "$WS/_action-items.json" "$WS/_workspace.json" "$WS"/_WORKLIST-*.md > "$TMP/before.sha"
run_ws rollup "$WS" --action-items --all-owners
assert_eq "$RC" 0 "second rollup exit code"
shasum -c "$TMP/before.sha" >/dev/null 2>&1 \
  || fail "second rollup run rewrote artifacts (expected byte-identical)"
PASS=$((PASS + 1))
printf '%s\n' "$ERR" > "$TMP/settle.err"
assert_has "_WORKLIST-Alice_Example.md (unchanged)" "$TMP/settle.err" "the roll-up reports the unchanged worklist"

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

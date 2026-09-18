#!/bin/bash
#
# test/commitments_test.sh — offline test for the dev-commitments extractor
# and corpus (lib/workspace.py: parse_roles, extract_commitments,
# cmd_commitments, and the roll-up commitments path).
#
# Fully offline and self-contained: hand-written speaker-labeled transcripts
# in the real diarizer format ("[HH:MM:SS] Name: text" + "# Role:" header
# lines) and synthetic meeting folders. No audio, no models, no network —
# lib/workspace.py is stdlib-only, so plain python3 (no uv).
#
# Sections:
#   1. guards + module sanity
#   2. extraction with roles: self gating, boss-requested priority, question
#      skip, negative flag, dedupe
#   3. legacy transcript without roles: any speaker's first-person cues
#   4. --roles JSON string and @path override the headers
#   5. hook: WHOSAID_COMMITMENTS_HOOK replaces the markdown; WHOSAID_ROLES
#      env carries the compact roles JSON
#   6. roll-up: near-duplicate dedupe into one CM id, _COMMITMENTS.md +
#      _commitments.json + _INDEX.md Commitments section + manifest pointer
#   7. hand-edit reconcile: [open] -> [done] survives a re-run
#   8. determinism: a second roll-up run is byte-identical
#   9. workspace with NO commitments: no Commitments section, no corpus files
#  10. semantic dedupe: a reworded commitment difflib scores far below 0.82
#      folds under WHOSAID_EMBED_FAKE=1 (bag-of-words cosine) and stays a
#      separate item when the embed server is unreachable (difflib fallback)
#  11. fragment filter (issue #22): clause hygiene, the min_words content
#      rule at extraction and at fold, the requested_by / deadline rescue,
#      min_words=0, --min-words and [commitments] min_words, and the
#      near-miss reporting in commitments.md/.json and _COMMITMENTS.md
#
# macOS/BSD only: BSD grep/sed, bash 3.2 (no associative arrays). Python
# checker scripts are written to files (not inline in "$( ... )") because
# bash 3.2 quote-scans heredoc bodies inside command substitution, and the
# fixtures deliberately contain apostrophes ("I'll", "I won't").

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WS_PY="$REPO/lib/workspace.py"

PASS=0
TEST_FAILED=0
TMP="$(mktemp -d)"

# Hermetic dedupe: roll-up would ask a live Ollama on 127.0.0.1:11434 for
# embeddings if one answered. A closed port keeps every fold below difflib-only
# (section 10 opts into the fake embedder explicitly).
export WHOSAID_OLLAMA="http://127.0.0.1:9"
unset WHOSAID_EMBED_FAKE

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
    fail "$3 — expected [$2], got [$1]"
  fi
}

assert_has() {  # assert_has <fixed-string> <file> <what>
  if grep -qF -- "$1" "$2"; then
    PASS=$((PASS + 1))
  else
    fail "$3 — [$1] not found in $2"
  fi
}

assert_not_has() {  # assert_not_has <fixed-string> <file> <what>
  if grep -qF -- "$1" "$2"; then
    fail "$3 — [$1] unexpectedly found in $2"
  fi
  PASS=$((PASS + 1))
}

assert_file() {  # assert_file <path> <what>
  if [ -s "$1" ]; then
    PASS=$((PASS + 1))
  else
    fail "$2 — missing or empty: $1"
  fi
}

assert_absent() {  # assert_absent <path> <what>
  if [ -e "$1" ]; then
    fail "$2 — unexpectedly exists: $1"
  fi
  PASS=$((PASS + 1))
}

# run_ws <args...>: run lib/workspace.py; rc in RC, stdout in OUT, stderr in ERR.
run_ws() {
  set +e
  OUT="$(python3 "$WS_PY" "$@" 2> "$TMP/.last.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.last.err")"
}

# run_pycheck <script-path> <args...>: run a checker script; print its stdout.
run_pycheck() {
  local script="$1"
  shift
  python3 "$script" "$@"
}

echo "== commitments_test: temp dir $TMP =="

# ---------------------------------------------------------------------------
# 1. Guards: python3 present, module present and compilable.
# ---------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || {
  echo "SKIP: python3 not found on PATH — test/commitments_test.sh needs python3." >&2
  exit 0
}
[ -f "$WS_PY" ] || fail "required source file missing: $WS_PY"
bash -n "$SCRIPT_PATH" || fail "bash -n failed on test/commitments_test.sh"
python3 -m py_compile "$WS_PY" || fail "python3 -m py_compile failed on lib/workspace.py"

# ---------------------------------------------------------------------------
# 2. Extraction with roles (from '# Role:' headers).
# ---------------------------------------------------------------------------
echo "-- commitments extraction (roles header) --"

MTG="$TMP/2026-09-15-0900"
mkdir -p "$MTG"
cat > "$MTG/transcript.speakers.txt" <<'EOF'
# Speaker-labeled transcript: transcript
# Diarization: sherpa-onnx (pyannote segmentation-3.0 + NeMo TitaNet-small), local.
# Speakers (3): Alice, Bob, Carol
# Role: Alice = self
# Role: Bob = boss
# Role: Carol = peer

[00:00:01] Bob: Can you send the report?

[00:00:04] Alice: I'll send it tomorrow.

[00:00:10] Carol: I can take that other task.

[00:00:15] Alice: Will I need to attend the review?

[00:00:20] Alice: I won't make Friday.

[00:00:25] Alice: I'll send it tomorrow, no need to chase me.
EOF

run_ws commitments --transcript "$MTG/transcript.speakers.txt" --json-out "$MTG/commitments.json"
assert_eq "$RC" 0 "commitments extraction exit code"
assert_file "$MTG/commitments.md" "commitments.md written next to json-out"
assert_file "$MTG/commitments.json" "commitments.json written"

cat > "$TMP/check_extract.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for key in ("transcript", "md_out", "source", "speakers", "roles", "items"):
    assert key in d, "payload missing key %r: %r" % (key, sorted(d))
assert d["source"] == "heuristic", d["source"]
assert d["roles"] == {"Alice": "self", "Bob": "boss", "Carol": "peer"}, d["roles"]
items = d["items"]
texts = [it["text"] for it in items]
assert len(items) == 2, items
first, second = items
assert first["speaker"] == "Alice" and first["speaker_role"] == "self", first
assert first["text"] == "I'll send it tomorrow", first
assert first["requested_by"] == "Bob", first
assert first["requested_by_role"] == "boss", first
assert first["priority"] == "high", first
assert first["negative"] is False, first
assert first["cue"] == "i'll send", first
assert second["text"] == "I won't make Friday", second
assert second["negative"] is True, second
assert second["priority"] == "normal", second
assert "requested_by" not in second, second
# peer first-person turn is gated OUT when roles are present
assert not any("take that other task" in t for t in texts), texts
# a question clause is never a commitment
assert not any("review" in t for t in texts), texts
# exact (speaker, normalized text) repeat dedupes within the meeting
assert not any("chase me" in t for t in texts), texts
print("ok")
PY
EXTCHECK="$(run_pycheck "$TMP/check_extract.py" "$MTG/commitments.json")" \
  || fail "extraction JSON check crashed"
assert_eq "$EXTCHECK" "ok" "boss-requested item (priority high) + negative item extracted; peer/question/dupes skipped"

assert_has "- [ ] (Alice) I'll send it tomorrow (boss-requested)  — 00:00:04" \
  "$MTG/commitments.md" "md bullet carries the (boss-requested) marker"
assert_has "- [ ] (Alice) I won't make Friday  — 00:00:20" \
  "$MTG/commitments.md" "md bullet for the negative commitment"
assert_has '<!-- cm: ' "$MTG/commitments.md" "md bullets carry the cm metadata comment"

# ---------------------------------------------------------------------------
# 3. Legacy transcript without roles: any speaker's first-person cues count.
# ---------------------------------------------------------------------------
echo "-- legacy transcript (no roles) --"

LEG="$TMP/legacy"
mkdir -p "$LEG"
cat > "$LEG/transcript.speakers.txt" <<'EOF'
# Speaker-labeled transcript: transcript
# Diarization: sherpa-onnx, local.
# Speakers (2): Alice, Bob

[00:00:01] Bob: I will follow up with legal.

[00:00:05] Alice: I'll draft the notes.
EOF

run_ws commitments --transcript "$LEG/transcript.speakers.txt" --json-out "$LEG/commitments.json"
assert_eq "$RC" 0 "legacy commitments exit code"
cat > "$TMP/check_legacy.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["roles"] == {}, d["roles"]
by_speaker = {it["speaker"]: it["text"] for it in d["items"]}
assert by_speaker == {"Bob": "I will follow up with legal",
                      "Alice": "I'll draft the notes"}, by_speaker
assert d["items"][0]["speaker_role"] is None, d["items"][0]
print("ok")
PY
LEGCHECK="$(run_pycheck "$TMP/check_legacy.py" "$LEG/commitments.json")" \
  || fail "legacy JSON check crashed"
assert_eq "$LEGCHECK" "ok" "no roles: first-person turns from ANY speaker extracted"

# ---------------------------------------------------------------------------
# 4. --roles (inline JSON and @path) overrides the header lines.
# ---------------------------------------------------------------------------
echo "-- --roles override --"

run_ws commitments --transcript "$LEG/transcript.speakers.txt" \
  --json-out "$LEG/cm_roles.json" --roles '{"Alice": "self"}'
assert_eq "$RC" 0 "commitments --roles (inline JSON) exit code"
cat > "$TMP/check_roles.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["roles"] == {"Alice": "self"}, d["roles"]
assert [it["speaker"] for it in d["items"]] == ["Alice"], d["items"]
print("ok")
PY
ROLESJSON="$(run_pycheck "$TMP/check_roles.py" "$LEG/cm_roles.json")" \
  || fail "--roles inline JSON check crashed"
assert_eq "$ROLESJSON" "ok" "--roles JSON string gates extraction to the self speaker"

echo '{"Alice": "self"}' > "$TMP/roles.json"
run_ws commitments --transcript "$LEG/transcript.speakers.txt" \
  --json-out "$LEG/cm_roles_file.json" --roles "@$TMP/roles.json"
assert_eq "$RC" 0 "commitments --roles @path exit code"
ROLESFILE="$(run_pycheck "$TMP/check_roles.py" "$LEG/cm_roles_file.json")" \
  || fail "--roles @path check crashed"
assert_eq "$ROLESFILE" "ok" "--roles @path behaves like the inline JSON"

# ---------------------------------------------------------------------------
# 5. Hook: WHOSAID_COMMITMENTS_HOOK markdown wins; WHOSAID_ROLES env visible.
# ---------------------------------------------------------------------------
echo "-- commitments hook --"

cat > "$TMP/hook.sh" <<EOF
#!/bin/bash
cat > /dev/null  # transcript arrives on stdin
printf '%s\n' "\$WHOSAID_ROLES" > "$TMP/hook_roles_seen.txt"
echo "# custom commitments"
echo ""
echo "- [ ] (Alice) hook-derived commitment  — 00:00:42"
EOF
chmod +x "$TMP/hook.sh"

HOOKDIR="$TMP/hooked"
mkdir -p "$HOOKDIR"
cp "$MTG/transcript.speakers.txt" "$HOOKDIR/"

WHOSAID_COMMITMENTS_HOOK="$TMP/hook.sh" run_ws commitments \
  --transcript "$HOOKDIR/transcript.speakers.txt" --json-out "$HOOKDIR/commitments.json"
assert_eq "$RC" 0 "hooked commitments exit code"
assert_has "# custom commitments" "$HOOKDIR/commitments.md" \
  "commitments.md equals the hook markdown, not the heuristic rendering"
assert_not_has "boss-requested" "$HOOKDIR/commitments.md" \
  "heuristic markdown did not leak into the hooked run"
cat > "$TMP/check_hook.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["source"] == "hook", d["source"]
assert [it["text"] for it in d["items"]] == ["hook-derived commitment"], d["items"]
assert d["roles"] == {"Alice": "self", "Bob": "boss", "Carol": "peer"}, d["roles"]
print("ok")
PY
HOOKCHECK="$(run_pycheck "$TMP/check_hook.py" "$HOOKDIR/commitments.json")" \
  || fail "hook JSON check crashed"
assert_eq "$HOOKCHECK" "ok" "hook run reports source=hook and parses bullets from its markdown"
assert_has '{"Alice":"self","Bob":"boss","Carol":"peer"}' "$TMP/hook_roles_seen.txt" \
  "WHOSAID_ROLES env carries the compact roles JSON to the hook"

# ---------------------------------------------------------------------------
# 6. Roll-up: two dated folders with near-duplicate commitments dedupe into
#    ONE CM id with two occurrences; all artifacts appear.
# ---------------------------------------------------------------------------
echo "-- roll-up commitments corpus --"

WS="$TMP/ws"
mkdir -p "$WS/2026-09-15-0900" "$WS/2026-09-16-1000"
cat > "$WS/2026-09-15-0900/commitments.json" <<'EOF'
{
  "source": "heuristic",
  "roles": {"Alice": "self"},
  "items": [
    {"speaker": "Alice", "speaker_role": "self",
     "text": "I'll send the status update tomorrow", "time": "00:00:04",
     "cue": "i'll send", "negative": false, "priority": "normal"}
  ]
}
EOF
cat > "$WS/2026-09-16-1000/commitments.json" <<'EOF'
{
  "source": "heuristic",
  "roles": {"Alice": "self"},
  "items": [
    {"speaker": "Alice", "speaker_role": "self",
     "text": "I will send the status update tomorrow", "time": "00:01:04",
     "cue": "i will", "negative": false, "priority": "normal"},
    {"speaker": "Alice", "speaker_role": "self",
     "text": "I'll review the migration plan", "time": "00:02:04",
     "cue": "i'll", "negative": false, "priority": "normal"}
  ]
}
EOF

# fixture sanity: the pair must land at/above the 0.82 dedupe threshold
cat > "$TMP/check_sim.py" <<'PY'
import difflib, re
norm = lambda t: " ".join(re.sub(r"[^0-9a-z\s]", " ", t.lower()).split())
r = difflib.SequenceMatcher(None,
    norm("I'll send the status update tomorrow"),
    norm("I will send the status update tomorrow")).ratio()
assert 0.82 <= r < 1.0, "near-dup fixture scores %r; expected in [0.82, 1.0)" % r
print("ok")
PY
SIMCHECK="$(run_pycheck "$TMP/check_sim.py")" || fail "near-dup fixture sanity check failed"
assert_eq "$SIMCHECK" "ok" "near-duplicate fixture pair scores in [0.82, 1.0)"

run_ws rollup "$WS"
assert_eq "$RC" 0 "rollup exit code"
assert_file "$WS/_COMMITMENTS.md" "_COMMITMENTS.md written"
assert_file "$WS/_commitments.json" "_commitments.json written"
assert_has "## Commitments" "$WS/_INDEX.md" "_INDEX.md gains a Commitments section"
assert_has '"commitments_corpus": "_commitments.json"' "$WS/_workspace.json" \
  "_workspace.json gains the commitments_corpus pointer"

cat > "$TMP/check_rollup.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
items = d["items"]
assert [it["id"] for it in items] == ["CM-001", "CM-002"], items
by_id = {it["id"]: it for it in items}
assert len(by_id["CM-001"]["occurrences"]) == 2, by_id["CM-001"]
assert [o["meeting"] for o in by_id["CM-001"]["occurrences"]] == \
    ["2026-09-15-0900", "2026-09-16-1000"], by_id["CM-001"]
assert by_id["CM-001"]["first_seen"] == "2026-09-15-0900", by_id["CM-001"]
assert by_id["CM-001"]["last_seen"] == "2026-09-16-1000", by_id["CM-001"]
assert len(by_id["CM-002"]["occurrences"]) == 1, by_id["CM-002"]
assert d["next_id"] == 3, d["next_id"]
assert d["folded_meetings"] == ["2026-09-15-0900", "2026-09-16-1000"], d
print("ok")
PY
RUCHECK="$(run_pycheck "$TMP/check_rollup.py" "$WS")" || fail "rollup corpus JSON check crashed"
assert_eq "$RUCHECK" "ok" "near-duplicates fold into one CM-001 with 2 occurrences; ids stable"
assert_has "(2×): I'll send the status update tomorrow" "$WS/_COMMITMENTS.md" \
  "_COMMITMENTS.md renders the merged item with its occurrence count"

# ---------------------------------------------------------------------------
# 7. Hand-edit reconcile: flipping [open] -> [done] in _COMMITMENTS.md
#    survives the next roll-up (md AND json).
# ---------------------------------------------------------------------------
echo "-- hand-edit reconcile --"

sed -i '' 's/\*\*CM-001\*\* \[open\]/**CM-001** [done]/' "$WS/_COMMITMENTS.md" \
  || fail "sed failed to hand-edit CM-001 to done"
PASS=$((PASS + 1))
run_ws rollup "$WS"
assert_eq "$RC" 0 "rollup after hand-edit exit code"
assert_has "**CM-001** [done]" "$WS/_COMMITMENTS.md" \
  "the [done] hand edit survives the re-run (_COMMITMENTS.md)"
cat > "$TMP/check_handedit.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
by_id = {it["id"]: it for it in d["items"]}
assert by_id["CM-001"]["status"] == "done", by_id["CM-001"]
assert by_id["CM-002"]["status"] == "open", by_id["CM-002"]
assert len(by_id["CM-001"]["occurrences"]) == 2, by_id["CM-001"]
print("ok")
PY
HANDCHECK="$(run_pycheck "$TMP/check_handedit.py" "$WS")" || fail "hand-edit JSON check crashed"
assert_eq "$HANDCHECK" "ok" "the [done] hand edit persists in _commitments.json"

# ---------------------------------------------------------------------------
# 8. Determinism: a second roll-up run rewrites nothing.
# ---------------------------------------------------------------------------
echo "-- rollup determinism --"

shasum "$WS/_INDEX.md" "$WS/_COMMITMENTS.md" "$WS/_commitments.json" \
  "$WS/_workspace.json" > "$TMP/before.sha"
run_ws rollup "$WS"
assert_eq "$RC" 0 "second rollup exit code"
shasum -c "$TMP/before.sha" >/dev/null 2>&1 \
  || fail "second rollup run rewrote artifacts (expected byte-identical)"
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# 9. Workspace with NO commitments: no Commitments section, no corpus files.
# ---------------------------------------------------------------------------
echo "-- rollup without commitments --"

EMPTYWS="$TMP/ws-empty"
mkdir -p "$EMPTYWS/2026-09-15-0900"
run_ws rollup "$EMPTYWS"
assert_eq "$RC" 0 "rollup (no commitments) exit code"
assert_not_has "## Commitments" "$EMPTYWS/_INDEX.md" \
  "_INDEX.md omits the Commitments section when no commitments exist"
assert_absent "$EMPTYWS/_commitments.json" \
  "no _commitments.json is created when no commitments exist"
assert_absent "$EMPTYWS/_COMMITMENTS.md" \
  "no _COMMITMENTS.md is created when no commitments exist"

# ---------------------------------------------------------------------------
# 10. Semantic dedupe: a reworded commitment whose difflib ratio sits far
#     below 0.82 stays a separate item when the embed server is unreachable
#     (WHOSAID_OLLAMA is a closed port: difflib fallback) and folds into the
#     first item under WHOSAID_EMBED_FAKE=1 (bag-of-words cosine >= 0.90).
# ---------------------------------------------------------------------------
echo "-- semantic dedupe: difflib fallback vs fake embeddings --"

SEMWS="$TMP/ws-sem"
mkdir -p "$SEMWS/2026-09-15-0900" "$SEMWS/2026-09-16-1000"
cat > "$SEMWS/2026-09-15-0900/commitments.json" <<'EOF'
{
  "roles": {"Alice": "self"},
  "items": [
    {"speaker": "Alice", "speaker_role": "self",
     "text": "I'll write the rollout runbook for the platform team", "time": "00:00:04",
     "cue": "i'll", "negative": false, "priority": "normal"}
  ]
}
EOF
cat > "$SEMWS/2026-09-16-1000/commitments.json" <<'EOF'
{
  "roles": {"Alice": "self"},
  "items": [
    {"speaker": "Alice", "speaker_role": "self",
     "text": "for the platform team I'll write the rollout runbook", "time": "00:00:09",
     "cue": "i'll", "negative": false, "priority": "normal"}
  ]
}
EOF

# fixture sanity: difflib must miss the pair, the fake embedder must catch it
cat > "$TMP/check_sem_fixture.py" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import workspace as w
a = w.normalize_text("I'll write the rollout runbook for the platform team")
b = w.normalize_text("for the platform team I'll write the rollout runbook")
r = w.similarity(a, b)
assert r < 0.82, "reworded fixture scores %r on difflib; expected < 0.82" % r
c = w.cosine(w.fake_embed(a), w.fake_embed(b))
assert c >= 0.90, "reworded fixture scores %r on fake cosine; expected >= 0.90" % c
print("ok")
PY
SEMFIX="$(run_pycheck "$TMP/check_sem_fixture.py" "$REPO/lib")" || fail "semantic fixture sanity check crashed"
assert_eq "$SEMFIX" "ok" "reworded fixture: difflib < 0.82, fake-embedding cosine >= 0.90"

run_ws rollup "$SEMWS"
assert_eq "$RC" 0 "rollup (embed server unreachable) exit code"
printf '%s\n' "$ERR" > "$TMP/sem_fallback.err"
assert_has "not reachable" "$TMP/sem_fallback.err" \
  "roll-up logs the difflib-only fallback when Ollama is unreachable"
cat > "$TMP/check_sem_fallback.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
assert [it["id"] for it in d["items"]] == ["CM-001", "CM-002"], d["items"]
assert all(len(it["occurrences"]) == 1 for it in d["items"]), d["items"]
print("ok")
PY
SEMFALL="$(run_pycheck "$TMP/check_sem_fallback.py" "$SEMWS")" || fail "fallback corpus check crashed"
assert_eq "$SEMFALL" "ok" "difflib fallback keeps the reworded commitment as its own CM-002"

export WHOSAID_EMBED_FAKE=1
run_ws rollup "$SEMWS" --rebuild
unset WHOSAID_EMBED_FAKE
assert_eq "$RC" 0 "rollup --rebuild (fake embeddings) exit code"
printf '%s\n' "$ERR" > "$TMP/sem_fake.err"
assert_has "fake embeddings" "$TMP/sem_fake.err" \
  "roll-up logs the fake-embedding rule under WHOSAID_EMBED_FAKE=1"
cat > "$TMP/check_sem_fake.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
assert [it["id"] for it in d["items"]] == ["CM-001"], d["items"]
assert [o["meeting"] for o in d["items"][0]["occurrences"]] == \
    ["2026-09-15-0900", "2026-09-16-1000"], d["items"][0]
assert d["items"][0]["cue"] == "i'll" and d["items"][0]["negative"] is False, d["items"][0]
print("ok")
PY
SEMFAKE="$(run_pycheck "$TMP/check_sem_fake.py" "$SEMWS")" || fail "fake-embedding corpus check crashed"
assert_eq "$SEMFAKE" "ok" "fake embeddings fold the reworded commitment into CM-001 (2 occurrences)"
assert_has "(2×): I'll write the rollout runbook for the platform team" "$SEMWS/_COMMITMENTS.md" \
  "_COMMITMENTS.md renders the semantically merged item once"

# ---------------------------------------------------------------------------
# 11. Fragment filter (issue #22). The fixture is the issue's own sample: 14
#     clauses that carry something to act on and 13 that do not, one per
#     turn, plus a boss request and a deadline that rescue one-word clauses.
# ---------------------------------------------------------------------------
echo "-- fragment filter: extraction --"

FRAGWS="$TMP/frag"
FRAG="$FRAGWS/2026-09-17-0900"
mkdir -p "$FRAG"
cat > "$FRAG/transcript.speakers.txt" <<'EOF'
# Speaker-labeled transcript: transcript
# Diarization: sherpa-onnx, local.
# Speakers (3): Alice_Example, Bob_Example, Carol_Example
# Role: Alice_Example = self
# Role: Bob_Example = boss
# Role: Carol_Example = peer

[00:00:01] Alice_Example: I'll look into two things before tomorrow

[00:00:02] Alice_Example: I'm going to add those features today

[00:00:03] Alice_Example: I'll report back what savings we get

[00:00:04] Alice_Example: I'll post the top ranking first

[00:00:05] Alice_Example: I can create an Epic if needed

[00:00:06] Alice_Example: I will bump the version

[00:00:07] Alice_Example: I will just focus on their comments

[00:00:08] Alice_Example: I'll coordinate with the team on that

[00:00:09] Alice_Example: I'll ground myself on the latest

[00:00:10] Alice_Example: I can have AI control the browser

[00:00:11] Alice_Example: I can record the video for you

[00:00:12] Alice_Example: I'll take a look at this

[00:00:13] Alice_Example: I'll post it for maybe DocX

[00:00:14] Alice_Example: i'll i'll start investigating that while i

[00:00:20] Alice_Example: I'll do that

[00:00:21] Alice_Example: I'll do that secondarily

[00:00:22] Alice_Example: I'll check

[00:00:23] Alice_Example: I'll bring that up

[00:00:24] Alice_Example: I'll leave this one

[00:00:25] Alice_Example: I'll show that off

[00:00:26] Alice_Example: I can go towards

[00:00:27] Alice_Example: I can literally show

[00:00:28] Alice_Example: I could call him

[00:00:29] Alice_Example: i'll post it there

[00:00:30] Alice_Example: I'll see what I can do

[00:00:31] Alice_Example: i can i can have these

[00:00:32] Alice_Example: I'll probably talk to him about that

[00:00:40] Bob_Example: Can you own the rollout?

[00:00:41] Alice_Example: I'll own it

[00:00:42] Carol_Example: Thanks, that helps.

[00:00:43] Alice_Example: I'll do it today
EOF

run_ws commitments --transcript "$FRAG/transcript.speakers.txt" --json-out "$FRAG/commitments.json"
assert_eq "$RC" 0 "commitments extraction (fragment fixture) exit code"
printf '%s\n' "$ERR" > "$TMP/frag_extract.err"
assert_has "dropped 13 fragment(s) (min_words=2)" "$TMP/frag_extract.err" \
  "extraction logs the one-line dropped-fragments summary"

cat > "$TMP/check_frag.py" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[2])
import workspace as w
d = json.load(open(sys.argv[1]))
assert d["min_words"] == 2, d["min_words"]
texts = [it["text"] for it in d["items"]]
assert texts == [
    "I'll look into two things before tomorrow",
    "I'm going to add those features today",
    "I'll report back what savings we get",
    "I'll post the top ranking first",
    "I can create an Epic",
    "I will bump the version",
    "I will just focus on their comments",
    "I'll coordinate with the team on that",
    "I'll ground myself on the latest",
    "I can have AI control the browser",
    "I can record the video for you",
    "I'll take a look at this",
    "I'll post it for maybe DocX",
    "I'll start investigating that",
    "I'll own it",
    "I'll do it today",
], texts
by_text = {it["text"]: it for it in d["items"]}
own = by_text["I'll own it"]
assert own["requested_by"] == "Bob_Example" and own["priority"] == "high", own
assert "requested_by" not in by_text["I'll do it today"], by_text["I'll do it today"]
dropped = d["dropped"]
assert [x["text"] for x in dropped] == [
    "I'll do that", "I'll do that secondarily", "I'll check", "I'll bring that up",
    "I'll leave this one", "I'll show that off", "I can go towards", "I can literally show",
    "I could call him", "I'll post it there", "I'll see what I can do", "I can have these",
    "I'll probably talk to him about that",
], dropped
assert all(x["reason"].startswith("fragment") for x in dropped), dropped
assert dropped[0]["reason"] == "fragment: 0 content words, min 2", dropped[0]
assert dropped[2]["reason"] == "fragment: 1 content word, min 2", dropped[2]
assert dropped[0]["speaker"] == "Alice_Example" and dropped[0]["time"] == "00:00:20", dropped[0]
# the review lines in commitments.md are not item bullets
md = open(sys.argv[1].replace("commitments.json", "commitments.md")).read()
assert "## Dropped fragments (review)" in md, md
assert "- (Alice_Example) I'll check @ 00:00:22 (fragment: 1 content word, min 2)" in md, md
assert len(w.parse_commitment_bullets(md)) == len(texts), "dropped lines must not parse as items"
print("ok")
PY
FRAGCHECK="$(run_pycheck "$TMP/check_frag.py" "$FRAG/commitments.json" "$REPO/lib")" \
  || fail "fragment extraction JSON check crashed"
assert_eq "$FRAGCHECK" "ok" "sample: 14 kept (hygiene applied), 13 dropped with reasons; boss request and deadline rescue one-word clauses"

# --min-words 0 disables the filter: every clause is an item, nothing dropped.
run_ws commitments --transcript "$FRAG/transcript.speakers.txt" \
  --json-out "$FRAG/cm_min0.json" --min-words 0
assert_eq "$RC" 0 "commitments --min-words 0 exit code"
cat > "$TMP/check_frag_min0.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["min_words"] == 0 and d["dropped"] == [], (d["min_words"], d["dropped"])
texts = [it["text"] for it in d["items"]]
assert len(texts) == 29, texts
assert "I'll do that" in texts and "I can have these" in texts, texts
assert "I'll start investigating that" in texts, "hygiene still applies with the filter off"
print("ok")
PY
MIN0="$(run_pycheck "$TMP/check_frag_min0.py" "$FRAG/cm_min0.json")" || fail "--min-words 0 check crashed"
assert_eq "$MIN0" "ok" "--min-words 0 keeps every clause (29 items) and drops nothing"
assert_not_has "Dropped fragments" "$FRAG/commitments.md" \
  "commitments.md (rewritten by the --min-words 0 run) has no review section"

# [commitments] min_words in the workspace's whosaid.toml (the transcript's
# parent's parent) is the default; --ws points elsewhere.
printf '[commitments]\nmin_words = 3\n' > "$FRAGWS/whosaid.toml"
run_ws commitments --transcript "$FRAG/transcript.speakers.txt" --json-out "$FRAG/cm_toml.json"
assert_eq "$RC" 0 "commitments with [commitments] min_words = 3 exit code"
cat > "$TMP/check_frag_toml.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["min_words"] == 3, d["min_words"]
texts = [it["text"] for it in d["items"]]
assert "I'll look into two things before tomorrow" in texts, texts
assert "I will bump the version" not in texts, texts
assert any(x["text"] == "I will bump the version" and x["reason"] == "fragment: 2 content words, min 3"
           for x in d["dropped"]), d["dropped"]
assert "I'll own it" in texts, "requested items still pass with one content word"
print("ok")
PY
TOML="$(run_pycheck "$TMP/check_frag_toml.py" "$FRAG/cm_toml.json")" || fail "toml min_words check crashed"
assert_eq "$TOML" "ok" "[commitments] min_words = 3 raises the bar; requested items keep the one-word rule"

WS0="$TMP/frag-ws0"
mkdir -p "$WS0"
printf '[commitments]\nmin_words = 0\n' > "$WS0/whosaid.toml"
run_ws commitments --transcript "$FRAG/transcript.speakers.txt" --json-out "$FRAG/cm_ws.json" --ws "$WS0"
assert_eq "$RC" 0 "commitments --ws exit code"
cat > "$TMP/check_frag_ws.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["min_words"] == 0 and d["dropped"] == [] and len(d["items"]) == 29, (d["min_words"], len(d["items"]))
print("ok")
PY
WSCHK="$(run_pycheck "$TMP/check_frag_ws.py" "$FRAG/cm_ws.json")" || fail "--ws check crashed"
assert_eq "$WSCHK" "ok" "--ws DIR reads [commitments] min_words from that workspace"
rm -f "$FRAGWS/whosaid.toml"

# Config round-trip and the helpers, in-process.
cat > "$TMP/check_frag_cfg.py" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import workspace as w
assert w.commitments_config({})["min_words"] == 2
assert w.commitments_config({"commitments": {"min_words": 0}})["min_words"] == 0
assert w.commitments_config({"commitments": {"min_words": "3"}})["min_words"] == 3
assert w.commitments_config({"commitments": {"min_words": -1}})["min_words"] == 2, "negative falls back"
assert w.commitments_config({"commitments": {"min_words": True}})["min_words"] == 2, "bool falls back"
assert w.commitments_config({"commitments": {"min_words": "two"}})["min_words"] == 2, "junk falls back"
assert w.content_words("I'll take a look at this") == ["take", "look"]
assert w.content_words("i can i can have these") == []
assert w.fragment_check("I'll check", 2) == (1, 2)
assert w.fragment_check("I'll check", 2, requested=True) == (1, 1), "a request lowers the bar to 1"
assert w.fragment_check("I'll do it by Friday", 2) == (1, 1), "a deadline phrase lowers the bar to 1"
assert w.fragment_check("I'll check", 0) == (1, 0), "0 disables"
assert w.tidy_clause("i'll i'll start investigating that while i", "i'll") == "I'll start investigating that", \
    "stutter collapses, the dangling 'while i' tail drops, the pronoun is capitalized"
assert w.tidy_clause("I'll send the report to", "i'll send") == "I'll send the report"
assert w.tidy_clause("I can record the video for you", "i can") == "I can record the video for you"
print("ok")
PY
CFGCHK="$(run_pycheck "$TMP/check_frag_cfg.py" "$REPO/lib")" || fail "config/helpers check crashed"
assert_eq "$CFGCHK" "ok" "[commitments] min_words validates (int >= 0, junk falls back); helpers behave"

# Fold time: a legacy commitments.json (written before the filter) with two
# fragments, one requested fragment and one real item. A CM item that is
# already a fragment in the corpus stays untouched.
echo "-- fragment filter: fold --"

FWS="$TMP/ws-frag"
mkdir -p "$FWS/2026-09-15-0900"
cat > "$FWS/_commitments.json" <<'EOF'
{
  "next_id": 2,
  "similarity_threshold": 0.82,
  "folded_meetings": [],
  "items": [
    {"id": "CM-001", "text": "I'll do that", "speaker": "Alice_Example", "priority": "normal",
     "requested_by": "", "requested_by_role": "", "cue": "i'll", "negative": false,
     "status": "open", "first_seen": "2026-09-01-0900", "last_seen": "2026-09-01-0900",
     "merged_into": "", "occurrences": [{"meeting": "2026-09-01-0900", "line": 1}],
     "md_status": "open", "md_text": "I'll do that", "md_speaker": "Alice_Example"}
  ]
}
EOF
cat > "$FWS/2026-09-15-0900/commitments.json" <<'EOF'
{
  "source": "heuristic",
  "roles": {"Alice_Example": "self", "Bob_Example": "boss"},
  "items": [
    {"speaker": "Alice_Example", "speaker_role": "self",
     "text": "I'll do that", "time": "00:00:04",
     "cue": "i'll", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self",
     "text": "I'll check", "time": "00:00:09",
     "cue": "i'll", "negative": false, "priority": "normal"},
    {"speaker": "Alice_Example", "speaker_role": "self",
     "text": "I'll own it", "time": "00:00:14",
     "cue": "i'll own", "negative": false, "priority": "high",
     "requested_by": "Bob_Example", "requested_by_role": "boss"},
    {"speaker": "Alice_Example", "speaker_role": "self",
     "text": "I'll review the migration plan", "time": "00:00:19",
     "cue": "i'll", "negative": false, "priority": "normal"}
  ]
}
EOF

run_ws rollup "$FWS"
assert_eq "$RC" 0 "rollup (fold-time fragment filter) exit code"
printf '%s\n' "$ERR" > "$TMP/frag_fold.err"
assert_has "dropped 2 fragment(s) at fold (min_words=2)" "$TMP/frag_fold.err" \
  "roll-up logs the one-line fold-time dropped summary"
assert_has "skipped (fragment: 1 content word, min 2): I'll check" "$TMP/frag_fold.err" \
  "roll-up logs each skipped entry the way folds are logged"
cat > "$TMP/check_frag_fold.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
items = d["items"]
assert [(it["id"], it["text"]) for it in items] == [
    ("CM-001", "I'll do that"), ("CM-002", "I'll own it"), ("CM-003", "I'll review the migration plan")], items
assert len(items[0]["occurrences"]) == 1 and items[0]["status"] == "open", "existing CM-001 stays untouched"
assert items[1]["priority"] == "high" and items[1]["requested_by"] == "Bob_Example", items[1]
assert d["next_id"] == 4, d["next_id"]
assert [(x["meeting"], x["text"], x["reason"]) for x in d["dropped"]] == [
    ("2026-09-15-0900", "I'll do that", "fragment: 0 content words, min 2"),
    ("2026-09-15-0900", "I'll check", "fragment: 1 content word, min 2")], d["dropped"]
assert d["dropped"][0]["speaker"] == "Alice_Example" and d["dropped"][0]["time"] == "00:00:04", d["dropped"][0]
print("ok")
PY
FOLDCHK="$(run_pycheck "$TMP/check_frag_fold.py" "$FWS")" || fail "fold-time fragment corpus check crashed"
assert_eq "$FOLDCHK" "ok" "fold skips fragments (no CM id), keeps the requested one, records near misses; existing CM-001 stays"
assert_has "## Dropped fragments (review)" "$FWS/_COMMITMENTS.md" \
  "_COMMITMENTS.md renders the dropped-fragments review section"
assert_has "- 2026-09-15-0900 (Alice_Example) I'll check @ 00:00:09 (fragment: 1 content word, min 2)" \
  "$FWS/_COMMITMENTS.md" "review line carries meeting, speaker, text, time and reason"
assert_has "**CM-001** [open] (Alice_Example)" "$FWS/_COMMITMENTS.md" \
  "the pre-existing fragment item still renders as CM-001"

shasum "$FWS/_COMMITMENTS.md" "$FWS/_commitments.json" > "$TMP/frag_before.sha"
run_ws rollup "$FWS"
assert_eq "$RC" 0 "second rollup (fold-time fragment filter) exit code"
shasum -c "$TMP/frag_before.sha" >/dev/null 2>&1 \
  || fail "second rollup rewrote the corpus (dropped near misses must persist byte-identically)"
PASS=$((PASS + 1))
printf '%s\n' "$ERR" > "$TMP/frag_fold2.err"
assert_not_has "fragment(s) at fold" "$TMP/frag_fold2.err" \
  "an incremental re-run drops nothing new and logs no summary"

# [commitments] min_words = 0 at fold: everything folds, no review section.
printf '[commitments]\nmin_words = 0\n' > "$FWS/whosaid.toml"
run_ws rollup "$FWS" --rebuild
assert_eq "$RC" 0 "rollup --rebuild with min_words = 0 exit code"
cat > "$TMP/check_frag_fold0.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_commitments.json"))
assert [it["text"] for it in d["items"]] == [
    "I'll do that", "I'll check", "I'll own it", "I'll review the migration plan"], d["items"]
assert "dropped" not in d, d.get("dropped")
print("ok")
PY
FOLD0="$(run_pycheck "$TMP/check_frag_fold0.py" "$FWS")" || fail "min_words = 0 fold check crashed"
assert_eq "$FOLD0" "ok" "[commitments] min_words = 0 turns the fold-time filter off"
assert_not_has "Dropped fragments" "$FWS/_COMMITMENTS.md" \
  "no review section when nothing was dropped"

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

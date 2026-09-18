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
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

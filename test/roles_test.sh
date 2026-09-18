#!/bin/bash
#
# test/roles_test.sh — offline unit/e2e test for speaker ROLE tags
# (lib/diarize_sherpa.py + the `whosaid` bash front end).
#
# Fully offline and self-contained: synthetic fixtures only — no audio, no
# real diarization, no model downloads. The diarizer module is exercised
# in-process (numpy only; sherpa-onnx is imported lazily by paths this test
# never takes) plus end-to-end through `--relabel` over a fabricated minimal
# sidecar, with WHOSAID_SPEAKER_DB pointed at a throwaway registry.
#
# Sections:
#   1. guards + module sanity
#   2. _registry_entry merge semantics (role survives overwrite / override /
#      removal; unknown keys preserved)
#   3. normalize_role
#   4. render_outputs roles rendering (header lines, card labels, unchanged
#      per-turn format; byte-identical legacy output with no roles)
#   5. do_relabel --save-speaker + --save-role end-to-end (registry gains the
#      role; the sidecar gains "roles"; a second relabel preserves the role;
#      --save-role for an unknown speaker errors without touching state)
#   6. whosaid bash: --role format validation + help text documents roles and
#      dev-commitments
#
# macOS/BSD only: BSD grep, bash 3.2 (no associative arrays, no bash-4-isms).
# Skips (exit 0, clear message) when uv is missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
DIARIZER="$REPO/lib/diarize_sherpa.py"

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

assert_grep() {  # assert_grep <ERE-pattern> <file> <what>
  if grep -qE -- "$1" "$2"; then
    PASS=$((PASS + 1))
  else
    fail "$3 — pattern [$1] not found in $2"
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

# run_py: run the diarizer module in-process (numpy only). stdout captured.
run_py() {
  set +e
  OUT="$(cd "$REPO" && PYTHONPATH="$REPO/lib" uv run --quiet --with numpy python3 -c "$1" 2> "$TMP/.last.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.last.err")"
}

# run_diarize <args...>: run lib/diarize_sherpa.py as a subprocess (relabel
# path only — no sherpa-onnx import, no models). stdout in OUT, rc in RC.
run_diarize() {
  set +e
  OUT="$(cd "$REPO" && uv run --quiet --with numpy python3 "$DIARIZER" "$@" 2> "$TMP/.last.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.last.err")"
}

echo "== roles_test: temp dir $TMP =="

# ---------------------------------------------------------------------------
# 1. Guards: uv present, module present and importable. Missing uv is a SKIP.
# ---------------------------------------------------------------------------
for tool in uv python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "SKIP: '$tool' not found on PATH — test/roles_test.sh needs uv and python3." >&2
    exit 0
  fi
done

[ -f "$DIARIZER" ] || fail "required source file missing: $DIARIZER"
bash -n "$SCRIPT_PATH" || fail "bash -n failed on test/roles_test.sh"
run_py "import diarize_sherpa"
assert_eq "$RC" 0 "diarize_sherpa imports (numpy-only, no sherpa-onnx)"

# ---------------------------------------------------------------------------
# 2. _registry_entry merge semantics.
# ---------------------------------------------------------------------------
echo "-- _registry_entry merge semantics --"

run_py "
import diarize_sherpa as d
old = {'name': 'Old', 'model': 'm', 'embedding': [9.0], 'added': 'old-run',
       'role': 'boss', 'note': 'hand-curated'}
# role=None: overwrite the same speaker; the old role + unknown keys survive
e = d._registry_entry('Alice', 'm', [1.0], 'run-2', old=old)
assert e['role'] == 'boss', e
assert e['note'] == 'hand-curated', e
assert e['name'] == 'Alice' and e['model'] == 'm' and e['embedding'] == [1.0], e
# explicit role overrides the old one; unknown keys still survive
e = d._registry_entry('Alice', 'm', [1.0], 'run-3', old=old, role='peer')
assert e['role'] == 'peer', e
assert e['note'] == 'hand-curated', e
# empty-string role REMOVES the key; unknown keys still survive
e = d._registry_entry('Alice', 'm', [1.0], 'run-4', old=old, role='')
assert 'role' not in e, e
assert e['note'] == 'hand-curated', e
# fresh entry (no old): no role key unless given; role=None leaves it absent
e = d._registry_entry('Bob', 'm', [2.0], 'run-5')
assert 'role' not in e, e
e = d._registry_entry('Bob', 'm', [2.0], 'run-6', role='external')
assert e['role'] == 'external', e
print('ok')
"
assert_eq "$RC" 0 "_registry_entry inline checks exited 0"
assert_eq "$OUT" "ok" "_registry_entry: role survives overwrite, override, removal; extras preserved"

# ---------------------------------------------------------------------------
# 3. normalize_role: strip + lower; "" stays ""; free-form passes through.
# ---------------------------------------------------------------------------
echo "-- normalize_role --"

run_py "
import diarize_sherpa as d
assert d.normalize_role(' Boss ') == 'boss', d.normalize_role(' Boss ')
assert d.normalize_role('') == '', repr(d.normalize_role(''))
assert d.normalize_role('Peer') == 'peer', d.normalize_role('Peer')
assert d.normalize_role('collaborator') == 'collaborator', d.normalize_role('collaborator')
assert d.VALID_ROLES == ('self', 'boss', 'peer', 'report', 'external'), d.VALID_ROLES
print('ok')
"
assert_eq "$RC" 0 "normalize_role inline checks exited 0"
assert_eq "$OUT" "ok" "normalize_role: strip+lower, empty stays empty, free-form passes, VALID_ROLES pinned"

# ---------------------------------------------------------------------------
# 4. render_outputs: '# Role: NAME = ROLE' header lines after the Speakers
#    header; 'NAME  [role]' card labels; per-turn '[time] Name: text' lines
#    unchanged; no roles -> byte-identical legacy output.
# ---------------------------------------------------------------------------
echo "-- render_outputs roles rendering --"

run_py "
import sys
from pathlib import Path
sys.path.insert(0, '$REPO/lib')
import diarize_sherpa as d

segs = [
    {'start': 0.0, 'end': 4.0, 'speaker': 'SPEAKER_00'},
    {'start': 10.0, 'end': 14.0, 'speaker': 'SPEAKER_01'},
]
turns = [
    ('SPEAKER_00', 0.0, ['i will send the report tomorrow']),
    ('SPEAKER_01', 10.0, ['a substantive remark from the second speaker']),
]
names = {'SPEAKER_00': 'Alice', 'SPEAKER_01': 'Bob'}
speakers = ['SPEAKER_00', 'SPEAKER_01']

for tag, roles in (('with', {'Alice': 'self', 'Bob': 'boss'}), ('without', None)):
    out = Path('$TMP/render_' + tag)
    out.mkdir(parents=True, exist_ok=True)
    d.render_outputs(out, 'meeting', segs, speakers, names, turns, 3, 'test',
                     roles=roles)
print('ok')
"
assert_eq "$RC" 0 "render_outputs synthetic calls exited 0"

SPTXT="$TMP/render_with/meeting.speakers.txt"
CARDS="$TMP/render_with/meeting.speaker-cards.txt"
assert_file "$SPTXT" "roles run wrote .speakers.txt"
assert_file "$CARDS" "roles run wrote .speaker-cards.txt"

# header line lands AFTER the '# Speakers (N):' header, one line per role
assert_has "# Speakers (2): Alice, Bob" "$SPTXT" "Speakers header present"
assert_has "# Role: Alice = self" "$SPTXT" "header line '# Role: Alice = self' rendered"
assert_has "# Role: Bob = boss" "$SPTXT" "header line '# Role: Bob = boss' rendered"
LINE_NO="$(awk '/^# Speakers \(2\)/{s=NR} /^# Role: Alice = self/{r=NR} END{print (r>s)}' "$SPTXT")"
assert_eq "$LINE_NO" "1" "Role header lines come after the Speakers header"
# per-turn format unchanged: '[time] Name: text'
assert_has "[00:00:00] Alice: i will send the report tomorrow" "$SPTXT" \
  "per-turn line format unchanged (Alice)"
assert_has "[00:00:10] Bob: a substantive remark from the second speaker" "$SPTXT" \
  "per-turn line format unchanged (Bob)"
# speaker cards carry 'NAME  [role]' (two spaces before the bracket)
assert_has "Alice  [self]" "$CARDS" "Alice card renders 'Alice  [self]'"
assert_has "Bob  [boss]" "$CARDS" "Bob card renders 'Bob  [boss]'"

# control: no roles -> byte-identical legacy output (no Role lines anywhere)
assert_not_has "# Role:" "$TMP/render_without/meeting.speakers.txt" \
  "legacy run has no '# Role:' header lines"
assert_not_has "[self]" "$TMP/render_without/meeting.speaker-cards.txt" \
  "legacy run has no [role] card labels"
if ! diff -q "$TMP/render_without/meeting.speakers.txt" "$TMP/render_without/meeting.speakers.txt" >/dev/null; then
  fail "sanity: diff against itself failed"
fi
PASS=$((PASS + 1))
run_py "
from pathlib import Path
a = Path('$TMP/render_with/meeting.speakers.txt').read_text().splitlines()
b = Path('$TMP/render_without/meeting.speakers.txt').read_text().splitlines()
role_lines = [ln for ln in a if ln.startswith('# Role:')]
assert role_lines == ['# Role: Alice = self', '# Role: Bob = boss'], role_lines
assert [ln for ln in a if not ln.startswith('# Role:')] == b, \
    'only the Role header lines may differ from the legacy output'
print('ok')
"
assert_eq "$OUT" "ok" "roles run differs from the legacy run ONLY by the Role header lines"

# ---------------------------------------------------------------------------
# 5. do_relabel end-to-end over a fabricated minimal sidecar + temp registry.
# ---------------------------------------------------------------------------
echo "-- do_relabel --save-speaker + --save-role end-to-end --"

export WHOSAID_SPEAKER_DB="$TMP/speakers.json"
SC="$TMP/mtg.diarization.json"
cat > "$SC" <<'EOF'
{
  "base": "mtg",
  "emb_model": "test-emb-model",
  "num_speakers": 1,
  "names": {"SPEAKER_00": "SPEAKER_00"},
  "registry_matches": [],
  "segments": [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"}],
  "cluster_emb": {"SPEAKER_00": [0.1, 0.2, 0.3]}
}
EOF

run_diarize --relabel "$SC" --save-speaker SPEAKER_00=Alice --save-role Alice=self
assert_eq "$RC" 0 "relabel 1 (save speaker + role) exit code"
if ! printf '%s\n' "$OUT" | grep -qF '"relabeled": true'; then
  fail "relabel 1 stdout must report relabeled: true — got [$OUT]"
fi
PASS=$((PASS + 1))

REGCHECK="$(python3 - "$TMP/speakers.json" <<'PY'
import json, sys
reg = json.load(open(sys.argv[1]))
assert len(reg["speakers"]) == 1, reg
e = reg["speakers"][0]
assert e["name"] == "Alice", e
assert e["model"] == "test-emb-model", e
assert e["role"] == "self", e
assert e["embedding"], e
print("ok")
PY
)" || fail "registry check after relabel 1 crashed"
assert_eq "$REGCHECK" "ok" "registry entry gains name Alice with role self"

SCCHECK="$(python3 - "$SC" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["names"] == {"SPEAKER_00": "Alice"}, d["names"]
assert d.get("roles") == {"Alice": "self"}, d.get("roles")
print("ok")
PY
)" || fail "sidecar check after relabel 1 crashed"
assert_eq "$SCCHECK" "ok" "sidecar names updated and top-level 'roles' map written"

# cards re-rendered with the role tag
assert_has "Alice  [self]" "$TMP/mtg.speaker-cards.txt" \
  "relabel 1 re-rendered cards with 'Alice  [self]'"

# --- second relabel: re-saving the SAME speaker (fresh --save-speaker, no
# --- --save-role) must preserve the registry role and the sidecar roles map.
run_diarize --relabel "$SC" --save-speaker SPEAKER_00=Alice
assert_eq "$RC" 0 "relabel 2 (re-save speaker, no --save-role) exit code"
REGCHECK2="$(python3 - "$TMP/speakers.json" <<'PY'
import json, sys
reg = json.load(open(sys.argv[1]))
e = [s for s in reg["speakers"] if s["name"] == "Alice"]
assert len(e) == 1, reg
assert e[0]["role"] == "self", e[0]
print("ok")
PY
)" || fail "registry check after relabel 2 crashed"
assert_eq "$REGCHECK2" "ok" "re-saving the speaker preserves the registry role"
SCCHECK2="$(python3 - "$SC" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("roles") == {"Alice": "self"}, d.get("roles")
print("ok")
PY
)" || fail "sidecar check after relabel 2 crashed"
assert_eq "$SCCHECK2" "ok" "sidecar roles map survives the second relabel"

# --- --save-role for a speaker that is neither in the registry nor assigned
# --- in this run: structured error, registry untouched, exit 0.
run_diarize --relabel "$SC" --save-speaker SPEAKER_00=Alice --save-role Ghost=boss
assert_eq "$RC" 0 "relabel 3 (unknown --save-role name) exit code"
ERRCHECK="$(python3 -c "
import json
d = json.loads('''$OUT''')
assert d.get('relabeled') is True, d
assert 'error' in d and 'Ghost' in d['error'], d
print('ok')
")" || fail "unknown-name error payload check crashed"
assert_eq "$ERRCHECK" "ok" "unknown --save-role name reports a structured error naming it"
REGCHECK3="$(python3 - "$TMP/speakers.json" <<'PY'
import json, sys
reg = json.load(open(sys.argv[1]))
assert [s["name"] for s in reg["speakers"]] == ["Alice"], reg
print("ok")
PY
)" || fail "registry check after relabel 3 crashed"
assert_eq "$REGCHECK3" "ok" "unknown --save-role name leaves the registry untouched"

# ---------------------------------------------------------------------------
# 6. whosaid bash: --role validation + help text.
# ---------------------------------------------------------------------------
echo "-- whosaid bash --role --"

set +e
BASHOUT="$("$REPO/whosaid" relabel foo --role badformat 2> "$TMP/role.err")"
BASHRC=$?
set -e
if [ "$BASHRC" -eq 0 ]; then
  fail "./whosaid relabel foo --role badformat must exit non-zero (got 0)"
fi
PASS=$((PASS + 1))
assert_has "--role wants NAME=ROLE" "$TMP/role.err" \
  "bad --role format fails with a clean '--role wants NAME=ROLE' error"

set +e
HELP_OUT="$("$REPO/whosaid" help 2>/dev/null)"
HELP_RC=$?
set -e
assert_eq "$HELP_RC" 0 "'whosaid help' exit code"
if ! printf '%s\n' "$HELP_OUT" | grep -qF -- "--role NAME=ROLE"; then
  fail "'whosaid help' must document --role NAME=ROLE"
fi
PASS=$((PASS + 1))
if ! printf '%s\n' "$HELP_OUT" | grep -qF -- "dev-commitments"; then
  fail "'whosaid help' must mention dev-commitments"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

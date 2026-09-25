#!/bin/bash
#
# test/workspace_test.sh — offline end-to-end test for lib/workspace.py
# (whosaid issue #2: the meeting-workspace layer).
#
# Fully offline and self-contained: synthesizes 1s m4a fixtures with
# ffmpeg, hand-writes speaker-labeled transcripts in the real diarizer
# format ("[HH:MM:SS] Name: text"), and exercises every subcommand
# (folder-name, hash, action-items, rollup) with the system python3 in
# a temp directory. Fast — no models, no inference; the module under
# test is stdlib-only.
#
# Skips (exit 0, clear message) when python3/ffmpeg/ffprobe/shasum are
# missing. macOS/BSD only: BSD grep/awk, bash 3.2 (no associative
# arrays, no bash-4-isms).
#
# Sections 9-16 cover issue #3 (the living roll-up): human edits to
# _ACTION-ITEMS.md surviving re-runs (status, retitle, hand-merge),
# --similarity-threshold on rollup, the "Possible duplicates (review)"
# section, the per-item type field, and --rebuild dropping curated
# state. Each section builds its own fresh workspace under $TMP so a
# missing feature fails loudly without corrupting earlier state.

set -euo pipefail

# Hermetic summarizer: `action-items --engine auto` (the default) would use a
# hook from the environment or a live Ollama on 127.0.0.1:11434 if it found
# one. Point it at a port nobody listens on and drop any hook so the no-hook
# checks below always exercise the skeleton path, offline.
export WHOSAID_OLLAMA="http://127.0.0.1:9"
unset WHOSAID_ACTION_ITEMS_HOOK

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

# assert helpers: every passing check increments PASS; any mismatch
# aborts (fail fast) with a message naming what and where.

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

assert_text() {  # assert_text <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    PASS=$((PASS + 1))
  else
    fail "$3 — pattern [$1] not found in text: [$2]"
  fi
}

assert_file() {  # assert_file <path> <what>
  if [ -s "$1" ]; then
    PASS=$((PASS + 1))
  else
    fail "$2 — missing or empty: $1"
  fi
}

# run_ws <args...>: run lib/workspace.py; leaves rc in RC, stdout in
# OUT, stderr text in ERR (never trips set -e).
run_ws() {
  set +e
  OUT="$(python3 "$WS_PY" "$@" 2> "$TMP/.last.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.last.err")"
}

echo "== workspace.py e2e: temp dir $TMP =="

# ---------------------------------------------------------------------------
# Guards + static checks. Missing tools are a SKIP (exit 0), a missing or
# unparsable module under test is a failure.
# ---------------------------------------------------------------------------
for tool in python3 ffmpeg ffprobe shasum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "SKIP: '$tool' not found on PATH — test/workspace_test.sh needs python3, ffmpeg, ffprobe, and shasum." >&2
    exit 0
  fi
done

[ -f "$WS_PY" ] || fail "required source file missing: $WS_PY"
bash -n "$SCRIPT_PATH" || fail "bash -n failed on test/workspace_test.sh"
python3 -m py_compile "$WS_PY" || fail "python3 -m py_compile failed on lib/workspace.py"

# make_m4a <path> <ISO8601-UTC-creation-time>: 1s silent aac fixture
# carrying a real container creation_time tag.
make_m4a() {
  ffmpeg -y -v error -f lavfi -i "anullsrc=r=16000:cl=mono" -t 1 \
    -metadata creation_time="$2" -c:a aac "$1" \
    || fail "ffmpeg failed to synthesize $1"
}

# make_meeting <ws> <folder> <creation-time>: dated folder with the m4a,
# plain transcript, and a two-speaker diarizer-format speakers file. The
# caller writes the meeting's own action-items.md.
make_meeting() {
  local dir="$1/$2"
  mkdir -p "$dir"
  make_m4a "$dir/meeting.m4a" "$3"
  printf 'monthly planning sync\n' > "$dir/meeting.txt"
  cat > "$dir/meeting.speakers.txt" <<'EOF'
# Speakers (2): Alice, Bob
[00:00:01] Alice: We should send the draft out to the team today.
[00:00:06] Bob: I will review the budget before Thursday.
EOF
}

# ---------------------------------------------------------------------------
# 1. folder-name: dated folder from the container creation_time tag, in
#    any IANA timezone; mtime fallback (with a stderr note) when the tag
#    is absent.
# ---------------------------------------------------------------------------
echo "-- folder-name --"

make_m4a "$TMP/tagged.m4a" "2026-09-16T14:03:17Z"

run_ws folder-name "$TMP/tagged.m4a" --tz America/Los_Angeles
assert_eq "$RC" 0 "folder-name (LA) exit code"
assert_text "creation_time=2026-09-16T14:03:17" "$ERR" "folder-name (LA) reads the container tag"
assert_eq "$OUT" "2026-09-16-0703" "folder-name --tz America/Los_Angeles (September = PDT, UTC-7)"

run_ws folder-name "$TMP/tagged.m4a" --tz UTC
assert_eq "$RC" 0 "folder-name (UTC) exit code"
assert_eq "$OUT" "2026-09-16-1403" "folder-name --tz UTC"

# Untagged file: no -metadata, so no container creation_time; pin the
# mtime (touch interprets in $TZ, the module renders in --tz, so the
# expectation is machine-TZ independent) and require the fallback note.
ffmpeg -y -v error -f lavfi -i "anullsrc=r=16000:cl=mono" -t 1 -c:a aac "$TMP/untagged.m4a" \
  || fail "ffmpeg failed to synthesize the untagged fixture m4a"
TAGCHECK="$(ffprobe -v error -show_entries format_tags=creation_time \
  -of default=noprint_wrappers=1:nokey=1 "$TMP/untagged.m4a")"
assert_eq "$TAGCHECK" "" "fixture m4a really carries no creation_time tag"

TZ=America/Los_Angeles touch -t 202609161403.17 "$TMP/untagged.m4a"
run_ws folder-name "$TMP/untagged.m4a" --tz America/Los_Angeles
assert_eq "$RC" 0 "folder-name (fallback) exit code"
assert_text "falling back to file mtime" "$ERR" "folder-name stderr mentions the mtime fallback"
assert_eq "$OUT" "2026-09-16-1403" "folder-name falls back to the pinned mtime"

# ---------------------------------------------------------------------------
# 2. hash: streamed sha256 must equal shasum -a 256.
# ---------------------------------------------------------------------------
echo "-- hash --"

EXPECT_SHA="$(shasum -a 256 "$TMP/tagged.m4a" | awk '{print $1}')"
run_ws hash "$TMP/tagged.m4a"
assert_eq "$RC" 0 "hash exit code"
assert_eq "$OUT" "$EXPECT_SHA" "hash equals shasum -a 256"

# ---------------------------------------------------------------------------
# 3. action-items: skeleton without a hook (exit 0, speakers listed);
#    hook plumbing (markdown from the hook's stdout lands in --md-out,
#    --json-out parses with source=hook, transcript on stdin, env vars);
#    failing hook (exit 7) degrades to the skeleton and still exits 0.
# ---------------------------------------------------------------------------
echo "-- action-items --"

TR="$TMP/ai/meeting.speakers.txt"
mkdir -p "$TMP/ai"
cat > "$TR" <<'EOF'
# Speakers (2): Alice, Bob
[00:00:01] Alice: We should send the draft out to the team today.
[00:00:06] Bob: I will review the budget before Thursday.
EOF

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/skel.md" --json-out "$TMP/ai/skel.json"
assert_eq "$RC" 0 "action-items (no hook) exit code"
assert_file "$TMP/ai/skel.md" "skeleton action-items.md written"
assert_grep "No summarizer hook" "$TMP/ai/skel.md" "skeleton says no hook is configured"
assert_grep "Speakers in this meeting: Alice, Bob" "$TMP/ai/skel.md" "skeleton lists the parsed speakers"
SKELSRC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"])' "$TMP/ai/skel.json")" \
  || fail "skeleton --json-out does not parse as JSON"
assert_eq "$SKELSRC" "skeleton" "skeleton --json-out reports source=skeleton"

# Fake hook: drains the transcript from stdin into a capture file, logs
# the WHOSAID_* env it was given, and cats fixed markdown on stdout.
HOOK="$TMP/hook.sh"
cat > "$HOOK" <<EOF
#!/bin/bash
cat > "$TMP/hook.stdin"
printf 'path=%s\nspeakers=%s\n' "\$WHOSAID_TRANSCRIPT_PATH" "\$WHOSAID_SPEAKERS" > "$TMP/hook.env"
cat <<'MD'
# Action items — fake hook

- **Alice:** prepare the slide deck
- **Bob:** circulate the meeting notes
MD
EOF
chmod +x "$HOOK"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/hook.md" --json-out "$TMP/ai/hook.json" --hook "$HOOK"
assert_eq "$RC" 0 "action-items (hook) exit code"
assert_grep "prepare the slide deck" "$TMP/ai/hook.md" "hook markdown landed in --md-out"
cmp -s "$TR" "$TMP/hook.stdin" \
  || fail "hook did not receive the transcript text on stdin"
PASS=$((PASS + 1))
assert_grep "speakers=Alice,Bob" "$TMP/hook.env" "hook received WHOSAID_SPEAKERS"
# The module exports the *resolved* transcript path (on macOS /var is a
# symlink to /private/var), so resolve the expectation the same way.
TR_RESOLVED="$(python3 -c 'import sys, pathlib; print(pathlib.Path(sys.argv[1]).resolve())' "$TR")"
assert_grep "path=$TR_RESOLVED" "$TMP/hook.env" "hook received WHOSAID_TRANSCRIPT_PATH"
HOOKCHECK="$(python3 - "$TMP/ai/hook.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["source"] == "hook", f"source={d['source']!r}, expected 'hook'"
items = d["items"]
assert len(items) == 2, items
assert items[0]["owner"] == "Alice" and items[0]["text"] == "prepare the slide deck", items[0]
assert items[1]["owner"] == "Bob" and items[1]["text"] == "circulate the meeting notes", items[1]
print("ok")
PY
)" || fail "hook --json-out failed python3 json.load checks"
assert_eq "$HOOKCHECK" "ok" "hook --json-out parses (json.load) with source=hook and parsed items"

# Failing hook: exits 7 with stderr noise -> skeleton markdown, exit 0.
HOOK7="$TMP/hook7.sh"
printf '#!/bin/bash\necho "summarizer exploded" >&2\nexit 7\n' > "$HOOK7"
chmod +x "$HOOK7"
run_ws action-items --transcript "$TR" --md-out "$TMP/ai/hook7.md" --hook "$HOOK7"
assert_eq "$RC" 0 "action-items (failing hook) still exits 0"
assert_text "hook exited 7" "$ERR" "failing hook is reported on stderr"
assert_file "$TMP/ai/hook7.md" "failing hook still writes action-items.md"
assert_grep "Speakers in this meeting: Alice, Bob" "$TMP/ai/hook7.md" "failing hook degrades to the skeleton"

# --engine (issue #14): the default (auto) keeps the hook/skeleton behaviour
# above; explicit engines are honoured; every failure degrades to the
# skeleton with exit 0. Ollama is unreachable here (WHOSAID_OLLAMA above).
echo "-- action-items --engine --"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/auto.md"
assert_eq "$RC" 0 "action-items (engine auto, nothing available) exit code"
assert_text "engine auto: no hook set and Ollama at http://127.0.0.1:9 is not answering" "$ERR" \
  "engine auto with no hook and no Ollama says so on stderr"
assert_grep "with --engine ollama" "$TMP/ai/auto.md" "skeleton mentions --engine ollama"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/none.md" --engine none --hook "$HOOK"
assert_eq "$RC" 0 "action-items --engine none exit code"
assert_text "action items \(skeleton\)" "$ERR" "--engine none writes the skeleton even with a hook given"
assert_grep "No summarizer hook" "$TMP/ai/none.md" "--engine none markdown is the skeleton"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/enghook.md" --engine hook --hook "$HOOK"
assert_eq "$RC" 0 "action-items --engine hook exit code"
assert_text "action items \(hook\)" "$ERR" "--engine hook runs the hook"
assert_grep "prepare the slide deck" "$TMP/ai/enghook.md" "--engine hook markdown came from the hook"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/enghook-none.md" --engine hook
assert_eq "$RC" 0 "action-items --engine hook without a hook exit code"
assert_text "no hook is set" "$ERR" "--engine hook without a hook warns"
assert_grep "No summarizer hook" "$TMP/ai/enghook-none.md" "--engine hook without a hook degrades to the skeleton"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/engollama.md" --json-out "$TMP/ai/engollama.json" --engine ollama
assert_eq "$RC" 0 "action-items --engine ollama (Ollama down) still exits 0"
assert_text "engine ollama failed" "$ERR" "--engine ollama failure is reported on stderr"
assert_grep "No summarizer hook" "$TMP/ai/engollama.md" "--engine ollama failure degrades to the skeleton"
OLLSRC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"])' "$TMP/ai/engollama.json")" \
  || fail "--engine ollama --json-out does not parse as JSON"
assert_eq "$OLLSRC" "skeleton" "--engine ollama failure reports source=skeleton in --json-out"

WHOSAID_ACTION_ITEMS_HOOK="$HOOK" run_ws action-items --transcript "$TR" --md-out "$TMP/ai/envhook.md"
assert_eq "$RC" 0 "action-items with WHOSAID_ACTION_ITEMS_HOOK exit code"
assert_text "action items \(hook\)" "$ERR" "engine auto picks the hook from the environment"

run_ws action-items --transcript "$TR" --md-out "$TMP/ai/badengine.md" --engine bogus
if [ "$RC" -eq 0 ]; then
  fail "--engine bogus must be rejected (argparse choices), got exit 0"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# 4. rollup basic: two complete dated meetings fold into the four
#    workspace artifacts.
# ---------------------------------------------------------------------------
echo "-- rollup basic --"

WS="$TMP/ws-basic"
make_meeting "$WS" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
- **Bob:** review the budget spreadsheet
EOF
make_meeting "$WS" "2026-09-17-0715" "2026-09-17T14:15:00Z"
cat > "$WS/2026-09-17-0715/action-items.md" <<'EOF'
# Action items — 2026-09-17-0715

- **Alice:** schedule the design review
- **Bob:** order the new laptops
- **Carol:** update the onboarding checklist
EOF

run_ws rollup "$WS" --action-items
assert_eq "$RC" 0 "rollup exit code"

assert_grep '^\| 2026-09-16-0703 \|.*\| yes \| yes \| yes \|$' "$WS/_INDEX.md" \
  "_INDEX.md row for meeting 1 is yes/yes/yes"
assert_grep '^\| 2026-09-17-0715 \|.*\| yes \| yes \| yes \|$' "$WS/_INDEX.md" \
  "_INDEX.md row for meeting 2 is yes/yes/yes"
assert_grep "nothing missing" "$WS/_INDEX.md" "_INDEX.md audit reports nothing missing"
assert_grep 'AI-001' "$WS/_ACTION-ITEMS.md" "_ACTION-ITEMS.md carries AI-001"
assert_grep 'AI-005' "$WS/_ACTION-ITEMS.md" "_ACTION-ITEMS.md carries AI-005 (5 distinct items)"

BASICCHECK="$(python3 - "$WS" <<'PY'
import json, sys
ws = sys.argv[1]
man = json.load(open(ws + "/_workspace.json"))
assert isinstance(man.get("meetings"), list), man
assert len(man["meetings"]) == 2, man["meetings"]
corpus = json.load(open(ws + "/_action-items.json"))
assert corpus["next_id"] == 6, corpus["next_id"]
assert [it["id"] for it in corpus["items"]] == [f"AI-00{i}" for i in range(1, 6)], corpus["items"]
assert sorted(corpus["folded_meetings"]) == ["2026-09-16-0703", "2026-09-17-0715"], corpus["folded_meetings"]
print("ok")
PY
)" || fail "basic rollup JSON state checks crashed"
assert_eq "$BASICCHECK" "ok" "_workspace.json has 2 entries; _action-items.json has 5 items, next_id=6"

# ---------------------------------------------------------------------------
# 5. Idempotence: re-running rollup with nothing new rewrites nothing —
#    all four artifacts stay byte-identical.
# ---------------------------------------------------------------------------
echo "-- rollup idempotence --"

SUMS_BEFORE="$(cat "$WS/_INDEX.md" "$WS/_workspace.json" "$WS/_ACTION-ITEMS.md" "$WS/_action-items.json" | shasum)"
run_ws rollup "$WS" --action-items
assert_eq "$RC" 0 "rollup re-run exit code"
SUMS_AFTER="$(cat "$WS/_INDEX.md" "$WS/_workspace.json" "$WS/_ACTION-ITEMS.md" "$WS/_action-items.json" | shasum)"
assert_eq "$SUMS_AFTER" "$SUMS_BEFORE" "re-run leaves all 4 artifacts byte-identical"

# ---------------------------------------------------------------------------
# 6. Dedupe: a near-paraphrase of an earlier item folds into ONE corpus
#    item (2 occurrences, first_seen != last_seen) while genuinely new
#    items keep getting fresh ids — no renumbering.
#
#    Note: "send the draft" vs "send the draft out to the team today"
#    scores only 0.56 difflib similarity — below the module's 0.82
#    dedupe threshold, so that pair folds as two DISTINCT items. The
#    paraphrase used here drops just "out" and scores 0.941.
# ---------------------------------------------------------------------------
echo "-- rollup dedupe --"

WS2="$TMP/ws-dedupe"
make_meeting "$WS2" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS2/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
- **Bob:** review the budget spreadsheet
EOF
make_meeting "$WS2" "2026-09-17-0715" "2026-09-17T14:15:00Z"
cat > "$WS2/2026-09-17-0715/action-items.md" <<'EOF'
# Action items — 2026-09-17-0715

- **Alice:** send the draft to the team today
- **Bob:** book the conference room for Friday
- **Carol:** update the onboarding checklist
EOF

SIMCHECK="$(python3 - <<'PY'
import difflib, re
norm = lambda t: " ".join(re.sub(r"[^0-9a-z\s]", " ", t.lower()).split())
r = difflib.SequenceMatcher(None, norm("send the draft out to the team today"),
                                  norm("send the draft to the team today")).ratio()
assert r >= 0.82, f"fixture paraphrase only scores {r}; dedupe would not fire"
print("ok")
PY
)" || fail "fixture sanity check failed"
assert_eq "$SIMCHECK" "ok" "fixture paraphrase pair really crosses the 0.82 threshold"

run_ws rollup "$WS2" --action-items
assert_eq "$RC" 0 "rollup (dedupe workspace) exit code"

DEDUPECHECK="$(python3 - "$WS2" <<'PY'
import json, sys
ws = sys.argv[1]
corpus = json.load(open(ws + "/_action-items.json"))
items = corpus["items"]
# ONE draft item with 2 occurrences spanning both meetings…
drafts = [it for it in items if "draft" in it["text"]]
assert len(drafts) == 1, [it["text"] for it in items]
d = drafts[0]
assert d["id"] == "AI-001", d
assert len(d["occurrences"]) == 2, d["occurrences"]
assert d["first_seen"] == "2026-09-16-0703" and d["last_seen"] == "2026-09-17-0715", d
assert d["first_seen"] != d["last_seen"]
# …new distinct items got fresh ids, nothing renumbered.
assert [it["id"] for it in items] == ["AI-001", "AI-002", "AI-003", "AI-004"], items
assert corpus["next_id"] == 5, corpus["next_id"]
print("ok")
PY
)" || fail "dedupe JSON checks crashed"
assert_eq "$DEDUPECHECK" "ok" "paraphrase folds into ONE item (2x, first!=last); fresh ids AI-003/AI-004, no renumber"
assert_grep 'AI-001.*2×' "$WS2/_ACTION-ITEMS.md" "_ACTION-ITEMS.md shows the deduped item with 2 occurrences"

# ---------------------------------------------------------------------------
# 7. Status preservation: hand-edited statuses survive plain re-runs;
#    --rebuild resets the corpus from scratch (statuses back to open).
# ---------------------------------------------------------------------------
echo "-- rollup status preservation --"

python3 - "$WS2/_action-items.json" <<'PY' || fail "failed to hand-edit _action-items.json"
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for it in d["items"]:
    if it["id"] == "AI-001":
        it["status"] = "resolved"
with open(p, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PY
PASS=$((PASS + 1))

run_ws rollup "$WS2" --action-items
assert_eq "$RC" 0 "rollup after hand-edit exit code"
STATUSCHECK="$(python3 - "$WS2/_action-items.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
ai1 = next(it for it in d["items"] if it["id"] == "AI-001")
assert ai1["status"] == "resolved", ai1
print("ok")
PY
)" || fail "status-preservation JSON check crashed"
assert_eq "$STATUSCHECK" "ok" "hand-set status=resolved survives a re-run"
assert_grep '\*\*AI-001\*\* \[resolved\]' "$WS2/_ACTION-ITEMS.md" "_ACTION-ITEMS.md renders the resolved status"

run_ws rollup "$WS2" --action-items --rebuild
assert_eq "$RC" 0 "rollup --rebuild exit code"
REBUILDCHECK="$(python3 - "$WS2/_action-items.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002", "AI-003", "AI-004"], items
assert all(it["status"] == "open" for it in items), items
ai1 = items[0]
assert len(ai1["occurrences"]) == 2, ai1
assert ai1["first_seen"] == "2026-09-16-0703" and ai1["last_seen"] == "2026-09-17-0715", ai1
assert d["next_id"] == 5, d["next_id"]
print("ok")
PY
)" || fail "rebuild JSON check crashed"
assert_eq "$REBUILDCHECK" "ok" "--rebuild resets every status to open (and re-dedupes to the same ids)"

# ---------------------------------------------------------------------------
# 8. Audit: a meeting missing its .speakers.txt is flagged in _INDEX.md;
#    a non-dated directory shows up as ORPHAN.
# ---------------------------------------------------------------------------
echo "-- rollup audit --"

rm "$WS2/2026-09-17-0715/meeting.speakers.txt"
run_ws rollup "$WS2" --action-items
assert_eq "$RC" 0 "rollup after deleting speakers.txt exit code"
assert_grep '^\| 2026-09-17-0715 \|.*\| yes \| NO \| yes \|$' "$WS2/_INDEX.md" \
  "_INDEX.md row flags the missing speakers file (Diarized = NO)"
assert_grep 'MISSING: speakers \.speakers\.txt' "$WS2/_INDEX.md" \
  "_INDEX.md audit names the missing speakers .speakers.txt"
if grep -q 'nothing missing' "$WS2/_INDEX.md"; then
  fail "audit still claims nothing missing after deleting a speakers file"
fi
PASS=$((PASS + 1))

mkdir "$WS2/random"
run_ws rollup "$WS2" --action-items
assert_eq "$RC" 0 "rollup after adding an orphan dir exit code"
assert_grep 'ORPHAN random/' "$WS2/_INDEX.md" "_INDEX.md audit reports ORPHAN random/"

# ---------------------------------------------------------------------------
# 9. Issue #3 — human status edit in _ACTION-ITEMS.md survives a re-run:
#    sed [open] -> [resolved] on AI-001's md line, re-run, and the edit
#    persists in BOTH _ACTION-ITEMS.md and _action-items.json, with ids
#    and next_id unchanged.
# ---------------------------------------------------------------------------
echo "-- rollup survives human status edit --"

WS3="$TMP/ws-status"
make_meeting "$WS3" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS3/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
EOF
make_meeting "$WS3" "2026-09-17-0715" "2026-09-17T14:15:00Z"
cat > "$WS3/2026-09-17-0715/action-items.md" <<'EOF'
# Action items — 2026-09-17-0715

- **Bob:** order the new laptops
EOF

run_ws rollup "$WS3" --action-items
assert_eq "$RC" 0 "rollup (status-edit workspace) exit code"
assert_grep '\*\*AI-001\*\* \[open\]' "$WS3/_ACTION-ITEMS.md" "AI-001 renders [open] before the hand edit"

sed -i '' 's/\*\*AI-001\*\* \[open\]/\*\*AI-001\*\* [resolved]/' "$WS3/_ACTION-ITEMS.md" \
  || fail "sed failed to hand-edit the status in _ACTION-ITEMS.md"
PASS=$((PASS + 1))

run_ws rollup "$WS3" --action-items
assert_eq "$RC" 0 "rollup after status hand-edit exit code"
assert_grep '\*\*AI-001\*\* \[resolved\]' "$WS3/_ACTION-ITEMS.md" \
  "human [resolved] status survives the re-run (_ACTION-ITEMS.md)"

STATUSEDITCHECK="$(python3 - "$WS3" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002"], items
ai1 = items[0]
assert ai1["status"] == "resolved", ai1
assert ai1["text"] == "send the draft out to the team today", ai1
assert d["next_id"] == 3, d["next_id"]
print("ok")
PY
)" || fail "status-edit JSON check crashed"
assert_eq "$STATUSEDITCHECK" "ok" \
  "hand-edited [resolved] persists in _action-items.json; id unchanged, next_id=3"

# ---------------------------------------------------------------------------
# 10. Issue #3 — human retitle: editing an item's text after the "): "
#     survives the re-run (md + JSON), without touching ids.
# ---------------------------------------------------------------------------
echo "-- rollup survives human retitle --"

WS4="$TMP/ws-retitle"
make_meeting "$WS4" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS4/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
EOF

run_ws rollup "$WS4" --action-items
assert_eq "$RC" 0 "rollup (retitle workspace) exit code"

sed -i '' 's/: send the draft out to the team today$/: circulate the final deck to the whole team/' \
  "$WS4/_ACTION-ITEMS.md" || fail "sed failed to hand-retitle AI-001 in _ACTION-ITEMS.md"
PASS=$((PASS + 1))

run_ws rollup "$WS4" --action-items
assert_eq "$RC" 0 "rollup after retitle exit code"
assert_grep ': circulate the final deck to the whole team$' "$WS4/_ACTION-ITEMS.md" \
  "retitled text survives the re-run (_ACTION-ITEMS.md)"
if grep -q 'send the draft out to the team today' "$WS4/_ACTION-ITEMS.md"; then
  fail "old title reappeared in _ACTION-ITEMS.md after the retitle survived a re-run"
fi
PASS=$((PASS + 1))

RETITLECHECK="$(python3 - "$WS4" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001"], items
ai1 = items[0]
assert ai1["text"] == "circulate the final deck to the whole team", ai1
assert len(ai1["occurrences"]) == 1, ai1
assert d["next_id"] == 2, d["next_id"]
print("ok")
PY
)" || fail "retitle JSON check crashed"
assert_eq "$RETITLECHECK" "ok" "retitled text persists in _action-items.json; id still AI-001"

# ---------------------------------------------------------------------------
# 11. Issue #3 — hand-merge: the human appends "(merged AI-002)" to
#     AI-001's line and deletes AI-002's line. After a re-run, AI-002's
#     status is "merged", its occurrences folded into AI-001, ids are
#     NOT renumbered, and the merged line renders as
#     "- **AI-002** [merged → AI-001] (N×): original text".
# ---------------------------------------------------------------------------
echo "-- rollup hand-merge --"

WS5="$TMP/ws-merge"
make_meeting "$WS5" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS5/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
EOF
make_meeting "$WS5" "2026-09-17-0715" "2026-09-17T14:15:00Z"
cat > "$WS5/2026-09-17-0715/action-items.md" <<'EOF'
# Action items — 2026-09-17-0715

- **Bob:** order the new laptops
EOF

run_ws rollup "$WS5" --action-items
assert_eq "$RC" 0 "rollup (hand-merge workspace) exit code"

sed -i '' 's/: send the draft out to the team today$/: send the draft out to the team today (merged AI-002)/' \
  "$WS5/_ACTION-ITEMS.md" || fail "sed failed to append the (merged AI-002) notation"
PASS=$((PASS + 1))
sed -i '' '/^- \*\*AI-002\*\*/d' "$WS5/_ACTION-ITEMS.md" \
  || fail "sed failed to delete AI-002's line"
PASS=$((PASS + 1))

run_ws rollup "$WS5" --action-items
assert_eq "$RC" 0 "rollup after hand-merge exit code"

MERGECHECK="$(python3 - "$WS5" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002"], items  # no renumbering
assert d["next_id"] == 3, d["next_id"]
ai1, ai2 = items
assert ai2["status"] == "merged", ai2
assert ai2["text"] == "order the new laptops", ai2
assert ai1["status"] == "open", ai1  # survivor keeps its own status
assert len(ai1["occurrences"]) == 2, ai1
assert {o["meeting"] for o in ai1["occurrences"]} == {"2026-09-16-0703", "2026-09-17-0715"}, ai1
print("ok")
PY
)" || fail "hand-merge JSON check crashed"
assert_eq "$MERGECHECK" "ok" \
  "AI-002 status=merged with occurrences folded into AI-001; ids not renumbered"
assert_grep '^- \*\*AI-002\*\* \[merged → AI-001\] .*: order the new laptops$' \
  "$WS5/_ACTION-ITEMS.md" "merged item renders as [merged → AI-001] with its original text"
assert_grep '^- \*\*AI-001\*\* .*\[open\].*\(2×\)' "$WS5/_ACTION-ITEMS.md" \
  "survivor AI-001 renders with the folded occurrences (2×)"

# ---------------------------------------------------------------------------
# 12. Issue #3 — a NEW meeting after human edits still folds: fresh items
#     get the next sequential id while the hand-edited status persists.
#     Continues the section-9 workspace (AI-001 was set to [resolved]).
# ---------------------------------------------------------------------------
echo "-- rollup new meeting folds after human edits --"

make_meeting "$WS3" "2026-09-18-0900" "2026-09-18T16:00:00Z"
cat > "$WS3/2026-09-18-0900/action-items.md" <<'EOF'
# Action items — 2026-09-18-0900

- **Carol:** update the onboarding checklist
EOF

run_ws rollup "$WS3" --action-items
assert_eq "$RC" 0 "rollup after adding a third meeting exit code"
assert_grep '\*\*AI-003\*\*' "$WS3/_ACTION-ITEMS.md" "new meeting's item gets the next id (AI-003)"
assert_grep '\*\*AI-001\*\* \[resolved\]' "$WS3/_ACTION-ITEMS.md" \
  "[resolved] from the hand edit still survives after folding the new meeting"

NEWFOLDCHECK="$(python3 - "$WS3" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002", "AI-003"], items
assert d["next_id"] == 4, d["next_id"]
ai1 = items[0]
assert ai1["status"] == "resolved", ai1
ai3 = items[2]
assert ai3["text"] == "update the onboarding checklist", ai3
assert ai3["owner"] == "Carol", ai3
assert [o["meeting"] for o in ai3["occurrences"]] == ["2026-09-18-0900"], ai3
assert "2026-09-18-0900" in d["folded_meetings"], d["folded_meetings"]
print("ok")
PY
)" || fail "new-meeting-after-edits JSON check crashed"
assert_eq "$NEWFOLDCHECK" "ok" \
  "third meeting folds as AI-003 (next_id=4) while AI-001 stays resolved in JSON"

# ---------------------------------------------------------------------------
# 13. Issue #3 — --similarity-threshold: the 0.941 paraphrase pair must
#     NOT merge at 0.99 but MUST merge at 0.5 (--rebuild between runs);
#     out-of-range 0.2 exits non-zero.
# ---------------------------------------------------------------------------
echo "-- rollup similarity-threshold flag --"

WS6="$TMP/ws-threshold"
make_meeting "$WS6" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS6/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
EOF
make_meeting "$WS6" "2026-09-17-0715" "2026-09-17T14:15:00Z"
cat > "$WS6/2026-09-17-0715/action-items.md" <<'EOF'
# Action items — 2026-09-17-0715

- **Alice:** send the draft to the team today
EOF

SIMCHECK2="$(python3 - <<'PY'
import difflib, re
norm = lambda t: " ".join(re.sub(r"[^0-9a-z\s]", " ", t.lower()).split())
r = difflib.SequenceMatcher(None, norm("send the draft out to the team today"),
                                   norm("send the draft to the team today")).ratio()
assert 0.82 <= r < 0.99, f"fixture pair scores {r}; expected in [0.82, 0.99)"
print("ok")
PY
)" || fail "threshold fixture sanity check failed"
assert_eq "$SIMCHECK2" "ok" "fixture pair scores in [0.82, 0.99): merges at default, not at 0.99"

run_ws rollup "$WS6" --action-items --similarity-threshold 0.99
assert_eq "$RC" 0 "rollup --similarity-threshold 0.99 exit code"
THR99CHECK="$(python3 - "$WS6" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002"], items
assert all("draft" in it["text"] for it in items), items
assert all(len(it["occurrences"]) == 1 for it in items), items
assert d["next_id"] == 3, d["next_id"]
print("ok")
PY
)" || fail "threshold 0.99 JSON check crashed"
assert_eq "$THR99CHECK" "ok" "at --similarity-threshold 0.99 the near-identical pair does NOT merge"

run_ws rollup "$WS6" --action-items --rebuild --similarity-threshold 0.5
assert_eq "$RC" 0 "rollup --rebuild --similarity-threshold 0.5 exit code"
THR50CHECK="$(python3 - "$WS6" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert len(items) == 1, [it["text"] for it in items]
ai1 = items[0]
assert ai1["id"] == "AI-001", ai1
assert len(ai1["occurrences"]) == 2, ai1
assert ai1["first_seen"] != ai1["last_seen"], ai1
assert d["next_id"] == 2, d["next_id"]
print("ok")
PY
)" || fail "threshold 0.5 JSON check crashed"
assert_eq "$THR50CHECK" "ok" "at --similarity-threshold 0.5 (with --rebuild) the pair DOES merge"

run_ws rollup "$WS6" --action-items --similarity-threshold 0.2
if [ "$RC" -eq 0 ]; then
  fail "--similarity-threshold 0.2 must exit non-zero (valid range 0.5-1.0), got 0"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# 14. Issue #3 — "Possible duplicates (review)": a pair landing just
#     under the default 0.82 threshold stays as two distinct items and
#     is surfaced together in a "## Possible duplicates" md section.
#     Fixture pair scores 0.800 ("schedule the design review" vs
#     "schedule the design sync").
# ---------------------------------------------------------------------------
echo "-- rollup possible duplicates section --"

WS7="$TMP/ws-dups"
make_meeting "$WS7" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS7/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** schedule the design review
EOF
make_meeting "$WS7" "2026-09-17-0715" "2026-09-17T14:15:00Z"
cat > "$WS7/2026-09-17-0715/action-items.md" <<'EOF'
# Action items — 2026-09-17-0715

- **Bob:** schedule the design sync
EOF

SIMCHECK3="$(python3 - <<'PY'
import difflib, re
norm = lambda t: " ".join(re.sub(r"[^0-9a-z\s]", " ", t.lower()).split())
r = difflib.SequenceMatcher(None, norm("schedule the design review"),
                                   norm("schedule the design sync")).ratio()
assert 0.70 <= r < 0.82, f"fixture pair scores {r}; expected in [0.70, 0.82)"
print("ok")
PY
)" || fail "duplicates fixture sanity check failed"
assert_eq "$SIMCHECK3" "ok" "near-miss fixture pair scores in [0.70, 0.82): under the default threshold"

run_ws rollup "$WS7" --action-items
assert_eq "$RC" 0 "rollup (duplicates workspace) exit code"
DUPCHECK="$(python3 - "$WS7" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002"], items
assert all(len(it["occurrences"]) == 1 for it in items), items
assert d["next_id"] == 3, d["next_id"]
print("ok")
PY
)" || fail "duplicates JSON check crashed"
assert_eq "$DUPCHECK" "ok" "the near-miss pair stays two distinct items at the default threshold"

assert_grep '^## Possible duplicates' "$WS7/_ACTION-ITEMS.md" \
  "_ACTION-ITEMS.md carries a '## Possible duplicates' section"
DUPSECTION="$(awk '/^## /{p=0} /^## Possible duplicates/{p=1} p' "$WS7/_ACTION-ITEMS.md")"
assert_text 'AI-001' "$DUPSECTION" "duplicates section names AI-001"
assert_text 'AI-002' "$DUPSECTION" "duplicates section names AI-002"

# ---------------------------------------------------------------------------
# 15. Issue #3 — per-item type field: a human adding "(my commitment)"
#     after the status bracket survives the re-run in md AND JSON.
# ---------------------------------------------------------------------------
echo "-- rollup type field survives --"

WS8="$TMP/ws-type"
make_meeting "$WS8" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS8/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
EOF

run_ws rollup "$WS8" --action-items
assert_eq "$RC" 0 "rollup (type workspace) exit code"

sed -i '' 's/\*\*AI-001\*\* \[open\] /\*\*AI-001\*\* [open] (my commitment) /' \
  "$WS8/_ACTION-ITEMS.md" || fail "sed failed to add the (my commitment) type"
PASS=$((PASS + 1))

run_ws rollup "$WS8" --action-items
assert_eq "$RC" 0 "rollup after type hand-edit exit code"
assert_grep '\*\*AI-001\*\* \[open\] \(my commitment\) ' "$WS8/_ACTION-ITEMS.md" \
  "type parenthetical survives the re-run (_ACTION-ITEMS.md)"

TYPECHECK="$(python3 - "$WS8" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
ai1 = d["items"][0]
assert ai1["id"] == "AI-001", ai1
assert ai1.get("type") == "my commitment", ai1
assert ai1["status"] == "open", ai1
print("ok")
PY
)" || fail "type-field JSON check crashed"
assert_eq "$TYPECHECK" "ok" "type persists as \"my commitment\" in _action-items.json"

# ---------------------------------------------------------------------------
# 16. Issue #3 — --rebuild drops curated state: after the section-15
#     type edit plus a status edit (both verified surviving first), a
#     --rebuild resets every status to open and clears every type.
# ---------------------------------------------------------------------------
echo "-- rollup rebuild drops curated state --"

sed -i '' 's/\*\*AI-001\*\* \[open\] (my commitment)/\*\*AI-001\*\* [resolved] (my commitment)/' \
  "$WS8/_ACTION-ITEMS.md" || fail "sed failed to hand-edit status + type together"
PASS=$((PASS + 1))

run_ws rollup "$WS8" --action-items
assert_eq "$RC" 0 "rollup after status+type hand-edit exit code"
assert_grep '\*\*AI-001\*\* \[resolved\] \(my commitment\)' "$WS8/_ACTION-ITEMS.md" \
  "status and type edits persist together before the rebuild"
CURATEDCHECK="$(python3 - "$WS8" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
ai1 = d["items"][0]
assert ai1["status"] == "resolved", ai1
assert ai1.get("type") == "my commitment", ai1
print("ok")
PY
)" || fail "curated-state JSON check crashed"
assert_eq "$CURATEDCHECK" "ok" "resolved status + my-commitment type persist together in JSON"

run_ws rollup "$WS8" --action-items --rebuild
assert_eq "$RC" 0 "rollup --rebuild (curated workspace) exit code"
REBUILDCURATEDCHECK="$(python3 - "$WS8" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001"], items
assert all(it["status"] == "open" for it in items), items
assert all(not it.get("type") for it in items), items
assert d["next_id"] == 2, d["next_id"]
print("ok")
PY
)" || fail "rebuild-curated JSON check crashed"
assert_eq "$REBUILDCURATEDCHECK" "ok" "--rebuild resets statuses to open and clears types"
assert_grep '^- \*\*AI-001\*\* \[open\] 2026' "$WS8/_ACTION-ITEMS.md" \
  "rebuilt line renders bare [open] with no type parenthetical"
if grep -q '\[resolved\]' "$WS8/_ACTION-ITEMS.md"; then
  fail "[resolved] survived --rebuild in _ACTION-ITEMS.md"
fi
PASS=$((PASS + 1))
if grep -q 'my commitment' "$WS8/_ACTION-ITEMS.md"; then
  fail "type parenthetical survived --rebuild in _ACTION-ITEMS.md"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# 17. Issue #14, section-aware folding: bullets under "## N. Heading" in a
#     meeting's action-items.md (the built-in summarizer's shape) give new
#     corpus items that heading as their type (minus "N. " and a trailing
#     parenthetical); "- none" placeholders and the <details> evidence block
#     are never folded; a hand-set type still wins on the next run.
# ---------------------------------------------------------------------------
echo "-- rollup section-typed items --"

WS9="$TMP/ws-sections"
make_meeting "$WS9" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS9/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

Speakers in this meeting: Alice_Example, Bob_Example, Carol_Example

_Auto-drafted 2026-09-16T07:30-07:00 by `qwen-stub` (local Ollama, offline) via `whosaid action-items --engine ollama`: 3 candidate turns (1 by Alice, 2 naming them, 0 added by the model over 1 slice), 2 kept as evidence, 2 items drafted, 0 flagged ⚠. A DRAFT: read the evidence and the transcript before trusting it._

## 1. Asks from leadership (Bob)
- **Alice_Example** [Bob 00:00:05] Send the vendor report by Thursday. Bob wants it first. "send the vendor report to me by Thursday"

## 2. Asks from team (Carol)
- none

## 3. Alice's own commitments
- **Alice_Example** [Alice 00:00:20] Refresh the dashboard data source. Before the demo. "I will refresh the data source"

## 4. Inferred next steps
- **Alice_Example** [inferred] Confirm the demo time with Carol.

<details><summary>Evidence turns the draft was built from (verbatim, 2)</summary>

- [00:00:05] Bob_Example: Alice, can you send the vendor report to me by Thursday?
- [00:00:20] Alice_Example: Yes, I will refresh the data source before the demo.

</details>
EOF

run_ws rollup "$WS9" --action-items
assert_eq "$RC" 0 "rollup (sections workspace) exit code"
SECTIONCHECK="$(python3 - "$WS9" <<'PY'
import json, sys
d = json.load(open(sys.argv[1] + "/_action-items.json"))
items = d["items"]
assert [it["id"] for it in items] == ["AI-001", "AI-002", "AI-003"], [it["text"] for it in items]
assert [it["type"] for it in items] == ["Asks from leadership", "Alice's own commitments", "Inferred next steps"], items
assert all(it["owner"] == "Alice_Example" for it in items), items
assert not any(it["text"].startswith("[00:") for it in items), "evidence turns were folded as items"
assert not any(it["text"] == "none" for it in items), "'- none' placeholder was folded as an item"
print("ok")
PY
)" || fail "section-type JSON check crashed"
assert_eq "$SECTIONCHECK" "ok" "3 items typed from their sections; evidence and '- none' not folded"
assert_grep '\*\*AI-001\*\* \[open\] \(Asks from leadership\) ' "$WS9/_ACTION-ITEMS.md" \
  "_ACTION-ITEMS.md renders the section as the type"

sed -i '' 's/\*\*AI-001\*\* \[open\] (Asks from leadership)/\*\*AI-001\*\* [open] (manager ask)/' \
  "$WS9/_ACTION-ITEMS.md" || fail "sed failed to hand-edit the section-derived type"
PASS=$((PASS + 1))
run_ws rollup "$WS9" --action-items
assert_eq "$RC" 0 "rollup after hand-editing a section-derived type exit code"
assert_grep '\*\*AI-001\*\* \[open\] \(manager ask\) ' "$WS9/_ACTION-ITEMS.md" \
  "hand-edited type wins over the section-derived one"

# ---------------------------------------------------------------------------
# 18. Regression guard (dev-commitments): a roll-up over a workspace with NO
#     commitments must not emit a Commitments section or commitments corpus.
#     (test/commitments_test.sh covers the with-commitments path.)
# ---------------------------------------------------------------------------
echo "-- rollup without commitments --"

WS10="$TMP/ws-no-commitments"
make_meeting "$WS10" "2026-09-16-0703" "2026-09-16T14:03:17Z"
cat > "$WS10/2026-09-16-0703/action-items.md" <<'EOF'
# Action items — 2026-09-16-0703

- **Alice:** send the draft out to the team today
EOF

run_ws rollup "$WS10" --action-items
assert_eq "$RC" 0 "rollup (no commitments) exit code"
if grep -q '^## Commitments' "$WS10/_INDEX.md"; then
  fail "_INDEX.md must not carry a Commitments section when no commitments exist"
fi
PASS=$((PASS + 1))
if [ -e "$WS10/_commitments.json" ]; then
  fail "_commitments.json must not be created when no commitments exist"
fi
PASS=$((PASS + 1))
if [ -e "$WS10/_COMMITMENTS.md" ]; then
  fail "_COMMITMENTS.md must not be created when no commitments exist"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

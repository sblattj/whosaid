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
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

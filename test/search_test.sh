#!/bin/bash
#
# test/search_test.sh: offline end-to-end test for lib/search.py
# (whosaid issue #14: workspace search).
#
# Self-contained: writes a synthetic workspace (two dated meeting folders and
# one hand-named folder, placeholder speakers, both "[HH:MM:SS] Name:" and
# "Name (MM:SS):" turn spellings, a continuation line) into a temp dir and
# drives every subcommand with the system python3:
#
#   1. missing-index paths (status/query/context exit 1 with the index hint)
#   2. build --no-embed, the summary line, WAL mode, the seg schema
#   3. exact queries: ranking, --speaker/--meeting filters, -k, snippets,
#      phrase and FTS5-unfriendly tokens (PR-42), --json shape, 0 hits
#   4. context windows (defaults, --before/--after, MM:SS, substring folder
#      match, --json, unknown folder)
#   5. speakers view, status, workspace from $WHOSAID_WORKSPACE
#   6. meaning/hybrid against a stub Ollama HTTP server (127.0.0.1, random
#      port) that returns deterministic bag-of-words vectors with a small
#      synonym table, so ranking, the exact/meaning/both tags, incremental
#      embedding, stale-vector pruning, --rebuild and both fallback messages
#      (no vectors yet; Ollama down) are asserted, never a real model.
#
# No Ollama, no network, no third-party packages. Skips (exit 0) only when
# python3 is missing. macOS/BSD: bash 3.2, BSD grep.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SEARCH_PY="$REPO/lib/search.py"

PASS=0
TEST_FAILED=0
TMP="$(mktemp -d)"
STUB_PID=""

cleanup() {
  if [ -n "$STUB_PID" ]; then kill "$STUB_PID" 2>/dev/null || true; fi
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

assert_text() {  # assert_text <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    PASS=$((PASS + 1))
  else
    fail "$3: pattern [$1] not found in text: [$2]"
  fi
}

assert_not_text() {  # assert_not_text <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    fail "$3: pattern [$1] unexpectedly found in text: [$2]"
  else
    PASS=$((PASS + 1))
  fi
}

# run <args...>: stdout -> OUT, stderr -> ERR, exit code -> RC (never aborts)
run() {
  set +e
  OUT="$(python3 "$SEARCH_PY" "$@" 2> "$TMP/.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.err")"
}

# jq-free JSON probes: py <expr> reads JSON from $OUT and prints the expression
py() {
  printf '%s' "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"
}

command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not found on PATH" >&2; exit 0; }
[ -f "$SEARCH_PY" ] || fail "required source file missing: $SEARCH_PY"
python3 -m py_compile "$SEARCH_PY" || fail "python3 -m py_compile failed on lib/search.py"
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# fixtures: 3 meetings, 4 placeholder speakers, 13 turns
WS="$TMP/ws"
mkdir -p "$WS/2026-01-05-0900" "$WS/weekly-sync-2" "$WS/2026-01-12-0900" "$WS/_private" "$WS/.hidden"
cat > "$WS/2026-01-05-0900/standup.speakers.txt" <<'EOF'
# Speaker-labeled transcript: standup
# Speakers (3): Alice_Example, Bob_Example, Carol_Example

[00:00:00] Alice_Example: morning everyone let's start with the deploy pipeline
[00:00:12] Bob_Example: the deploy pipeline is green again after the cache fix
[00:01:05] Carol_Example: i am blocked on the design review
[00:02:30] Alice_Example: the budget for the sprint is approved so we can hire a contractor
for the onboarding docs
[00:03:10] Bob_Example: PR-42 fixes the login bug and needs a review today
[00:04:00] Carol_Example: demo is thursday and the retro moves to friday
EOF
cat > "$WS/weekly-sync-2/notes.speakers.txt" <<'EOF'
# Weekly sync notes

Bob_Example (00:15): still waiting on access from the platform team
Dan_Example (01:40): the api cache tests are flaky on the release branch
Bob_Example (03:05): budget question for the dashboard vendor goes to alice
Dan_Example (05:20): onboarding docs are half done
EOF
cat > "$WS/2026-01-12-0900/standup.speakers.txt" <<'EOF'
[00:00:00] Alice_Example: budget update the vendor quote came in under the sprint budget
[00:00:45] Carol_Example: dashboard design review is done and the demo went well
[00:01:30] Alice_Example: login bug is closed and the release is tagged
EOF
# folders that must be ignored: underscore/dot prefixed, and one without transcripts
printf '[00:00:00] Ghost_Example: must not be indexed\n' > "$WS/_private/x.speakers.txt"
printf '[00:00:00] Ghost_Example: must not be indexed\n' > "$WS/.hidden/x.speakers.txt"
mkdir -p "$WS/empty-folder"

# stub Ollama: GET /api/tags -> 200; POST /api/embed -> deterministic vectors.
# Known words get one dimension each (synonyms fold together: blocked/stuck ->
# waiting, budget/cost -> money); unknown words contribute nothing; a text with
# no known word points at the last dimension. Every call appends one line
# "<n_inputs>" to the log file so the test can count embed calls.
STUB="$TMP/stub_ollama.py"
cat > "$STUB" <<'EOF'
import json, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VOCAB = ["waiting", "money", "access", "platform", "deploy", "pipeline", "cache", "review",
         "design", "dashboard", "sprint", "contractor", "onboarding", "docs", "login", "bug",
         "demo", "retro", "api", "tests", "release", "vendor", "quote", "team", "green"]
SYN = {"blocked": "waiting", "stuck": "waiting", "budget": "money", "cost": "money",
       "costs": "money", "spend": "money", "test": "tests"}
DIM = 512
PORT_FILE, LOG_FILE = sys.argv[1], sys.argv[2]


def embed(text):
    v = [0.0] * DIM
    hit = False
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        tok = SYN.get(tok, tok)
        if tok in VOCAB:
            v[VOCAB.index(tok)] += 1.0
            hit = True
    if not hit:
        v[DIM - 1] = 1.0
    return v


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({"models": [{"name": "stub-embed"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/api/embed":
            self._json({"error": "not found"}, 404)
            return
        if body.get("model") == "missing-model":
            self._json({"error": "model 'missing-model' not found"}, 404)
            return
        inp = body.get("input")
        if isinstance(inp, str):
            inp = [inp]
        with open(LOG_FILE, "a") as fh:
            fh.write(f"{len(inp)}\n")
        self._json({"model": body.get("model"), "embeddings": [embed(t) for t in inp]})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE, "w") as fh:
    fh.write(str(srv.server_address[1]))
srv.serve_forever()
EOF

# a closed port for the "Ollama down" cases: bind :0, read the port, release it
DOWN_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
cat > "$WS/whosaid.toml" <<EOF
[search]
ollama = "http://127.0.0.1:$DOWN_PORT"
embed_model = "stub-embed"
EOF

# ---------------------------------------------------------------------------
echo "== 1. missing index"
run status "$WS"
assert_eq "$RC" 1 "status exits 1 without an index"
assert_text "run: whosaid index " "$ERR" "status hint names whosaid index"
assert_text "\(missing\)" "$OUT" "status reports the db as missing"
run status "$WS" --json
assert_eq "$RC" 1 "status --json exits 1 without an index"
assert_eq "$(py 'd["exists"], d["indexed"]')" "False False" "status --json exists/indexed false"
run query "$WS" budget
assert_eq "$RC" 1 "query exits 1 without an index"
assert_text "run: whosaid index " "$ERR" "query hint names whosaid index"
run context "$WS" 2026-01-05-0900 00:01:00
assert_eq "$RC" 1 "context exits 1 without an index"
assert_text "run: whosaid index " "$ERR" "context hint names whosaid index"
run speakers "$WS"
assert_eq "$RC" 1 "speakers exits 1 without an index"

# ---------------------------------------------------------------------------
echo "== 2. build --no-embed"
run build "$WS" --no-embed
assert_eq "$RC" 0 "build --no-embed exits 0"
assert_text "^Indexed 13 segments · 3 meetings · 4 speakers → .*_search\.db; embeddings skipped \(--no-embed\), 0/13 stored$" "$OUT" \
  "build summary line"
[ -f "$WS/_search.db" ] || fail "_search.db not created"
PASS=$((PASS + 1))
SCHEMA="$(python3 - "$WS/_search.db" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("pragma journal_mode").fetchone()[0])
sql = c.execute("select sql from sqlite_master where name='seg'").fetchone()[0]
print("fts5" in sql.lower(), "porter unicode61" in sql, "line UNINDEXED" in sql, "t_sec UNINDEXED" in sql)
print([r[1] for r in c.execute("pragma table_info(emb)")])
print([r[1] for r in c.execute("pragma table_info(meta)")])
print(c.execute("select count(*) from seg where speaker='Ghost_Example'").fetchone()[0])
print(c.execute("select count(distinct meeting || '/' || source) from seg").fetchone()[0],
      sorted({r[0] for r in c.execute("select distinct source from seg")}))
meta = dict(c.execute("select key, value from meta"))
print(meta["segments"], meta["meetings"], meta["embedded"], meta["embed_model"], bool(meta.get("built_at")))
EOF
)"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 1p)" "wal" "journal_mode is WAL"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 2p)" "True True True True" "seg is FTS5 porter unicode61 with UNINDEXED t_sec/line"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 3p)" "['key', 'model', 'dim', 'vec']" "emb columns"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 4p)" "['key', 'value']" "meta columns"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 5p)" "0" "underscore/dot folders are not indexed"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 6p)" "3 ['notes.speakers.txt', 'standup.speakers.txt']" \
  "source column holds the speakers file names"
assert_eq "$(printf '%s\n' "$SCHEMA" | sed -n 7p)" "13 3 0 stub-embed True" "meta segments/meetings/embedded/model/built_at"

# ---------------------------------------------------------------------------
echo "== 3. exact queries"
run query "$WS" budget
assert_eq "$RC" 0 "exact query exits 0"
assert_text "^engine: exact$" "$(printf '%s' "$ERR" | sed 's/^whosaid: //')" "engine line on stderr"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "3 hit(s)." "budget: 3 hits"
assert_text "^\[2026-01-12-0900 @ 00:00:00\] Alice_Example: »budget« update" "$OUT" "hit line format with snippet markers"
assert_eq "$(printf '%s\n' "$OUT" | sed -n 1p | cut -c1-17)" "[2026-01-12-0900 " "two-occurrence turn ranks first (bm25)"
assert_text "\[weekly-sync-2 @ 03:05\] Bob_Example: »budget« question" "$OUT" "hand-named folder and MM:SS turn indexed"
run query "$WS" budget --speaker bob
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "1 hit(s)." "--speaker substring filter (case-insensitive)"
run query "$WS" budget --meeting 2026-01
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "2 hit(s)." "--meeting substring filter"
run query "$WS" budget -k 1
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "1 hit(s)." "-k limits hits"
run query "$WS" deployed
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "2 hit(s)." "porter stemming: deployed matches deploy"
run query "$WS" alice
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "1 hit(s)." "bare words match the text column only, not the speaker column"
run query "$WS" "speaker:alice budget"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "2 hit(s)." "raw FTS5 column filter syntax still works"
run query "$WS" '"onboarding docs"' --json
assert_eq "$RC" 0 "phrase query --json exits 0"
assert_eq "$(py 'len(d)')" "2" "phrase query: 2 hits"
assert_eq "$(py 'sorted(d[0].keys())')" "['file', 'line', 'meeting', 'score', 'snippet', 'source', 'speaker', 't_sec', 't_str', 'text']" \
  "--json hit keys"
assert_eq "$(py 'sorted(h["text"][-19:] for h in d)')" "[' docs are half done', 'the onboarding docs']" \
  "continuation line folded into the previous turn"
assert_eq "$(py 'set(h["source"] for h in d)')" "{'exact'}" "exact hits tagged source=exact"
assert_eq "$(py '[h["t_sec"] for h in d if h["meeting"]=="2026-01-05-0900"]')" "[150]" "t_sec is seconds"
run query "$WS" "PR-42"
assert_eq "$RC" 0 "PR-42 (FTS5-unfriendly token) exits 0"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "1 hit(s)." "PR-42 found via quoted-token retry"
assert_text "could not parse" "$ERR" "quoted-token retry is announced on stderr"
run query "$WS" "nothing-here-xyz"
assert_eq "$RC" 0 "zero-hit query exits 0"
assert_eq "$OUT" "0 hit(s)." "zero hits prints 0 hit(s)."
run query "$WS" ""
assert_eq "$OUT" "0 hit(s)." "empty query prints 0 hit(s)."
run query "$WS" budget --mode nope
assert_eq "$RC" 2 "bad --mode is a usage error (2)"
run query "$WS" budget -k 0
assert_eq "$RC" 2 "-k 0 is a usage error (2)"

# ---------------------------------------------------------------------------
echo "== 4. context"
run context "$WS" 2026-01-05-0900 00:02:30
assert_eq "$RC" 0 "context exits 0"
assert_eq "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" "3" "default window [-60,+120] holds 3 turns"
assert_eq "$(printf '%s\n' "$OUT" | sed -n 1p)" "[00:02:30] Alice_Example: the budget for the sprint is approved so we can hire a contractor for the onboarding docs" \
  "context line format, continuation folded"
assert_eq "$(printf '%s\n' "$OUT" | sed -n 3p | cut -c1-10)" "[00:04:00]" "context ordered by time"
run context "$WS" 2026-01-05-0900 02:30 --before 60 --after 60
assert_eq "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" "2" "MM:SS moment and --after narrows the window"
run context "$WS" 2026-01-05-0900 150 --before 0 --after 0 --json
assert_eq "$(py 'len(d), d[0]["t_sec"], d[0]["t_str"], d[0]["speaker"]')" "1 150 00:02:30 Alice_Example" "seconds moment, --json turn shape"
assert_eq "$(py 'sorted(d[0].keys())')" "['line', 'speaker', 't_sec', 't_str', 'text']" "--json turn keys"
run context "$WS" weekly 1:40 --before 0 --after 0
assert_eq "$OUT" "[01:40] Dan_Example: the api cache tests are flaky on the release branch" "unique substring resolves the folder"
assert_text "matched meeting weekly-sync-2" "$ERR" "substring match is announced"
run context "$WS" 2026-01 00:00:00
assert_eq "$RC" 1 "ambiguous substring exits 1"
assert_text "ambiguous: 2026-01-05-0900, 2026-01-12-0900" "$ERR" "ambiguous substring lists the candidates"
run context "$WS" nope 00:00:10
assert_eq "$RC" 1 "unknown meeting exits 1"
assert_text "unknown meeting 'nope'; known: 2026-01-05-0900, 2026-01-12-0900, weekly-sync-2" "$ERR" "unknown meeting lists the known folders"
run context "$WS" weekly-sync-2 abc
assert_eq "$RC" 2 "malformed moment is a usage error (2)"
run context "$WS" weekly-sync-2 59:00
assert_eq "$RC" 0 "window past the end exits 0"
assert_eq "$OUT" "" "window past the end prints nothing"

# ---------------------------------------------------------------------------
echo "== 5. speakers, status, workspace from the environment"
run speakers "$WS"
assert_eq "$RC" 0 "speakers exits 0"
assert_eq "$(printf '%s\n' "$OUT" | sed -n 1p | tr -s ' ')" " turns meetings speaker" "speakers header"
assert_text "^ +4 +2 +Bob_Example$" "$OUT" "Bob: 4 turns in 2 meetings"
assert_text "^ +2 +1 +Dan_Example$" "$OUT" "Dan: 2 turns in 1 meeting"
run speakers "$WS" --json
assert_eq "$(py '[(r["speaker"], r["turns"], r["meetings"]) for r in d if r["speaker"].startswith("D")]')" \
  "[('Dan_Example', 2, 1)]" "speakers --json rows"
run status "$WS"
assert_eq "$RC" 0 "status exits 0 with an index"
assert_text "^segments: +13$" "$OUT" "status segments"
assert_text "^embedded: +0/13 \(stub-embed\)$" "$OUT" "status embedded count and model"
assert_text "^ollama: +http://127.0.0.1:$DOWN_PORT \(not reachable\)$" "$OUT" "status reports Ollama down"
run status "$WS" --json
assert_eq "$(py 'd["indexed"], d["segments"], d["meetings"], d["speakers"], d["embedded"], d["ollama_up"], d["embed_enabled"]')" \
  "True 13 3 4 0 False True" "status --json fields"
run --help
assert_eq "$RC" 0 "--help exits 0"
OUT=""; ERR=""; RC=0
set +e
OUT="$(cd "$TMP" && WHOSAID_WORKSPACE="$WS" python3 "$SEARCH_PY" query budget -k 5 2>/dev/null)"; RC=$?
set -e
assert_eq "$RC" 0 "workspace omitted: \$WHOSAID_WORKSPACE applies"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "3 hit(s)." "single positional is the query, not the workspace"
set +e
OUT="$(cd "$WS" && python3 "$SEARCH_PY" speakers --json 2>/dev/null)"; RC=$?
set -e
assert_eq "$RC" 0 "workspace omitted: cwd with whosaid.toml applies"
assert_eq "$(py 'len(d)')" "4" "cwd workspace: 4 speakers"
set +e
OUT="$(cd "$TMP" && env -u WHOSAID_WORKSPACE python3 "$SEARCH_PY" speakers 2>"$TMP/.err")"; RC=$?
set -e
assert_eq "$RC" 1 "no workspace anywhere exits 1"
assert_text "no workspace given" "$(cat "$TMP/.err")" "no-workspace message"

# ---------------------------------------------------------------------------
echo "== 6. meaning/hybrid: fallback without vectors"
run query "$WS" blocked --mode meaning
assert_eq "$RC" 0 "meaning without vectors exits 0"
assert_text "^whosaid: engine: exact \(fallback: no embeddings for stub-embed" "$ERR" "fallback reason: no embeddings"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "1 hit(s)." "fallback ran the exact search"
run query "$WS" blocked --mode hybrid --json
assert_eq "$(py 'len(d), d[0]["source"]')" "1 exact" "hybrid falls back to exact too"

echo "== 7. build with the stub Ollama"
python3 "$STUB" "$TMP/stub.port" "$TMP/stub.log" &
STUB_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  [ -s "$TMP/stub.port" ] && break
  sleep 0.25
done
[ -s "$TMP/stub.port" ] || fail "stub Ollama did not start"
STUB_URL="http://127.0.0.1:$(cat "$TMP/stub.port")"
export WHOSAID_OLLAMA="$STUB_URL"   # env override beats whosaid.toml (wsconfig)
run status "$WS" --json
assert_eq "$(py 'd["ollama"] == "'"$STUB_URL"'", d["ollama_up"]')" "True True" "WHOSAID_OLLAMA override is reachable"
run build "$WS"
assert_eq "$RC" 0 "build with embeddings exits 0"
assert_text "; embedded 13 new \(13/13 total, stub-embed, [0-9.]+s\)$" "$OUT" "summary reports 13 newly embedded"
assert_eq "$(cat "$TMP/stub.log" | tr '\n' ' ')" "13 " "one /api/embed batch of 13 inputs"
VEC="$(python3 - "$WS/_search.db" <<'EOF'
import sqlite3, sys, struct, math
c = sqlite3.connect(sys.argv[1])
rows = c.execute("select key, model, dim, vec from emb").fetchall()
print(len(rows), {r[1] for r in rows}, {r[2] for r in rows}, {len(r[0]) for r in rows})
norms = []
for _k, _m, dim, blob in rows:
    v = struct.unpack(f"<{dim}f", blob)
    norms.append(round(math.sqrt(sum(x * x for x in v)), 4))
print(sorted(set(norms)))
meta = dict(c.execute("select key, value from meta"))
print(meta["embedded"], meta["dim"])
EOF
)"
assert_eq "$(printf '%s\n' "$VEC" | sed -n 1p)" "13 {'stub-embed'} {512} {40}" "13 vectors, model, dim 512, sha1 keys"
assert_eq "$(printf '%s\n' "$VEC" | sed -n 2p)" "[1.0]" "vectors are unit-normalized float32 little-endian"
assert_eq "$(printf '%s\n' "$VEC" | sed -n 3p)" "13 512" "meta embedded/dim"

run build "$WS"
assert_text "; embedded 0 new \(13/13 total" "$OUT" "second build embeds nothing (incremental)"
assert_eq "$(cat "$TMP/stub.log" | tr '\n' ' ')" "13 " "second build made no /api/embed call"
printf '[00:02:00] Dan_Example: the vendor quote is stuck in legal\n' >> "$WS/2026-01-12-0900/standup.speakers.txt"
run build "$WS"
assert_text "^Indexed 14 segments" "$OUT" "new turn indexed"
assert_text "; embedded 1 new \(14/14 total" "$OUT" "only the new turn is embedded"
assert_eq "$(cat "$TMP/stub.log" | tr '\n' ' ')" "13 1 " "third build embedded exactly one input"
run build "$WS" --rebuild
assert_text "; embedded 14 new \(14/14 total" "$OUT" "--rebuild re-embeds everything"
# drop the added turn again: its vector must be pruned as stale
python3 - "$WS/2026-01-12-0900/standup.speakers.txt" <<'EOF'
import sys
p = sys.argv[1]
lines = open(p).read().splitlines()
open(p, "w").write("\n".join(l for l in lines if "stuck in legal" not in l) + "\n")
EOF
run build "$WS"
assert_text "^Indexed 13 segments" "$OUT" "removed turn dropped from seg"
assert_eq "$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from emb").fetchone()[0])' "$WS/_search.db")" \
  "13" "stale vector pruned"
run status "$WS"
assert_text "^embedded: +13/13 \(stub-embed, dim 512\)$" "$OUT" "status shows embedded count and dim"

echo "== 8. meaning and hybrid ranking"
: > "$TMP/stub.log"
run query "$WS" blocked --mode meaning -k 3 --json
assert_eq "$RC" 0 "meaning query exits 0"
assert_text "^whosaid: engine: meaning$" "$ERR" "engine: meaning"
assert_eq "$(cat "$TMP/stub.log" | tr '\n' ' ')" "1 " "query embedded once"
assert_eq "$(py 'len(d)')" "2" "meaning -k 3 returns only the 2 turns with cosine > 0"
assert_eq "$(py 'set(h["source"] for h in d)')" "{'meaning'}" "meaning hits tagged source=meaning"
assert_eq "$(py 'sorted((h["speaker"], h["t_str"]) for h in d[:2])')" "[('Bob_Example', '00:15'), ('Carol_Example', '00:01:05')]" \
  "top-2: the literal 'blocked' turn and the 'waiting on access' turn (synonym, no shared word)"
assert_eq "$(py 'd[0]["score"] > d[1]["score"] > 0')" "True" "scores descend and stay positive"
assert_eq "$(py 'round(d[0]["score"], 3)')" "0.577" "cosine of the best hit (1/sqrt(3) for the stub)"
run query "$WS" blocked --mode meaning -k 2
assert_text "^\[0\.58\] \[2026-01-05-0900 @ 00:01:05\] Carol_Example: i am blocked on the design review" "$OUT" "meaning human line has a score column"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "2 hit(s)." "meaning hit count"
run query "$WS" blocked --mode meaning -k 5 --speaker bob --json
assert_eq "$(py 'set(h["speaker"] for h in d), d[0]["t_str"]')" "{'Bob_Example'} 00:15" "meaning --speaker filter"
run query "$WS" blocked --mode meaning -k 5 --meeting weekly --json
assert_eq "$(py 'set(h["meeting"] for h in d)')" "{'weekly-sync-2'}" "meaning --meeting filter"
run query "$WS" blocked --mode exact --json
assert_eq "$(py '[h["speaker"] for h in d]')" "['Carol_Example']" "exact finds only the literal word"
run query "$WS" blocked --mode hybrid -k 4 --json
assert_eq "$RC" 0 "hybrid exits 0"
assert_text "^whosaid: engine: hybrid$" "$ERR" "engine: hybrid"
assert_eq "$(py 'd[0]["speaker"], d[0]["source"], d[0]["snippet"]')" "Carol_Example both i am »blocked« on the design review" \
  "hybrid: literal+semantic hit is tagged both and keeps its snippet"
assert_eq "$(py '[h["source"] for h in d if h["speaker"]=="Bob_Example" and h["t_str"]=="00:15"]')" "['meaning']" \
  "hybrid: synonym-only hit is tagged meaning"
assert_eq "$(py 'd[0]["score"] > d[1]["score"]')" "True" "hybrid: fused score ranks the both-hit first"
run query "$WS" blocked --mode hybrid -k 2
assert_text "^\[both   \] \[2026-01-05-0900 @ 00:01:05\] Carol_Example: i am »blocked«" "$OUT" "hybrid human line has a tag column"
assert_text "^\[meaning\] \[weekly-sync-2 @ 00:15\] Bob_Example: still waiting on access" "$OUT" "hybrid human line for a meaning-only hit"
run query "$WS" "blocked OR thursday" --mode hybrid -k 6 --json
assert_eq "$(py '[(h["speaker"], h["t_str"], h["source"]) for h in d]')" \
  "[('Carol_Example', '00:01:05', 'both'), ('Carol_Example', '00:04:00', 'exact'), ('Bob_Example', '00:15', 'meaning')]" \
  "hybrid: both, exact-only (word the stub does not know) and meaning-only (synonym) in fused order"
# numpy-free path is forced here; when numpy is installed this run covers the pure-Python cosine too
set +e
OUT="$(WHOSAID_SEARCH_PURE=1 python3 "$SEARCH_PY" query "$WS" blocked --mode meaning -k 1 --json 2>/dev/null)"; RC=$?
set -e
assert_eq "$RC" 0 "pure-Python cosine path exits 0"
assert_eq "$(py 'd[0]["speaker"], round(d[0]["score"], 3)')" "Carol_Example 0.577" "pure-Python cosine agrees"

echo "== 9. embed errors and Ollama down"
run build "$WS" --rebuild --no-embed
assert_text "embeddings skipped \(--no-embed\), 0/13 stored" "$OUT" "--rebuild --no-embed drops the vectors"
WHOSAID_OLLAMA="$STUB_URL" run build "$WS"
assert_text "; embedded 13 new" "$OUT" "vectors restored"
: > "$TMP/stub.log"
cat > "$WS/whosaid.toml" <<EOF
[search]
embed_model = "missing-model"
EOF
run build "$WS"
assert_eq "$RC" 0 "build with a model Ollama lacks still exits 0"
assert_text "dropping 13 vector\(s\) from a different embed model" "$ERR" "vectors of the old model are dropped"
assert_text "embeddings stopped after 0/13: Ollama /api/embed failed: HTTP 404 model 'missing-model' not found \(run: ollama pull missing-model\)" "$ERR" \
  "missing model: pull hint on stderr"
assert_text "; embedded 0 new \(0/13 total, missing-model" "$OUT" "partial summary after the failure"
cat > "$WS/whosaid.toml" <<EOF
[search]
embed_model = "stub-embed"
embed = false
EOF
run build "$WS"
assert_text "embeddings skipped \(search.embed = false\)" "$OUT" "search.embed = false skips embedding"
cat > "$WS/whosaid.toml" <<EOF
[search]
embed_model = "stub-embed"
EOF
run build "$WS"
assert_text "; embedded 13 new" "$OUT" "vectors back for the fallback test"
kill "$STUB_PID"; wait "$STUB_PID" 2>/dev/null || true; STUB_PID=""
run query "$WS" blocked --mode meaning
assert_eq "$RC" 0 "meaning with Ollama down exits 0"
assert_text "^whosaid: engine: exact \(fallback: Ollama not reachable at $STUB_URL\)" "$ERR" "fallback reason: Ollama not reachable"
assert_eq "$(printf '%s\n' "$OUT" | tail -1)" "1 hit(s)." "fallback exact result"
run build "$WS"
assert_eq "$RC" 0 "build with Ollama down exits 0"
assert_text "embeddings skipped: Ollama not reachable at $STUB_URL" "$ERR" "build says why embeddings were skipped"
assert_text "embeddings skipped \(Ollama down\), 13/13 stored" "$OUT" "build keeps the stored vectors"
unset WHOSAID_OLLAMA

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"

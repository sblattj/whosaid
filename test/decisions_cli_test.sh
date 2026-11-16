#!/bin/bash
#
# test/decisions_cli_test.sh: `whosaid decisions` through the real launcher (issue #213).
# Runs the synthetic fixture call in test/fixtures/decisions (one topic decided twice,
# 28 minutes apart) and checks the help section, the text ledger, --json, a workspace
# from $WHOSAID_WORKSPACE, and the unknown-meeting exit. Local engine only: no model,
# no network. bash 3.2 / BSD tools.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHOSAID="$REPO/whosaid"
WS="$REPO/test/fixtures/decisions"
export WHOSAID_SPEAKER_DB="${TMPDIR:-/tmp}/whosaid-no-registry.json"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); echo "ok   $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL $1"; }
has()  { if grep -qF -- "$2" <<<"$3"; then ok "$1"; else bad "$1: missing '$2'"; printf '%s\n' "$3" | head -20; fi; }

out="$("$WHOSAID" decisions --help)"
has "help: DECISIONS section" "DECISIONS" "$out"
has "help: names the split flag" "split across visits" "$out"
out="$("$WHOSAID" help)"
has "full help lists decisions" "whosaid decisions [WS] <meeting>" "$out"

out="$("$WHOSAID" decisions "$WS" 2026-03-02-1000)"
has "text: the topic is split across visits" "split across visits" "$out"
has "text: first decision with its own proposal turn" "[00:11:20] Carol_Example (proposal): Let's keep the date parser in the service lib" "$out"
has "text: second decision with its own proposal turn" "[00:39:20] Carol_Example (proposal): Then let's move the auth client to a separate platform lib" "$out"
has "text: the second points back at the first" "split from D1" "$out"

json="$("$WHOSAID" decisions "$WS" 2026-03-02-1000 --json)"
n="$(printf '%s' "$json" | "${WHOSAID_PYTHON:-python3}" -c 'import json,sys; r=json.load(sys.stdin); t=[x for x in r["topics"] if "split across visits" in x["flags"]]; print(len(r["decisions"]), len(t), len(t[0]["decisions"]) if t else 0)')"
if [[ "$n" == "3 1 2" ]]; then ok "json: 3 decisions, 1 split topic holding 2"; else bad "json counts: $n"; fi

out="$(WHOSAID_WORKSPACE="$WS" "$WHOSAID" decisions 03-02)"
has "meeting by substring in \$WHOSAID_WORKSPACE" "2026-03-02-1000: 3 decision(s)" "$out"

set +e
err="$("$WHOSAID" decisions "$WS" nope-meeting 2>&1 >/dev/null)"; rc=$?
set -e
if [[ $rc -eq 1 ]]; then ok "unknown meeting exits 1"; else bad "unknown meeting exit $rc"; fi
has "unknown meeting names it" "nope-meeting" "$err"

echo "decisions cli: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

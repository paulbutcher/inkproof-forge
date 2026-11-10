#!/usr/bin/env bash
# Stage 6 acceptance: level 2.
#   - the gatekeeper approves a pull request once the Actions run for its head
#     commit succeeded, and with conformance's approval its scheduled merge
#     goes through; a new commit dismisses the approval, and the gatekeeper
#     approves the new head once its run succeeds;
#   - a status posted under CI's name on a failing build gets no approval, and
#     the pull request doesn't merge;
#   - a pull request changing API.lock waits for you even after the
#     gatekeeper's and conformance's approvals;
#   - while paused, the gatekeeper approves nothing.
# No Claude runs: it uses a scratch repository at level 2, stage6-scratch,
# with a CI of a few seconds, plays core with core's account, and has
# conformance review through its own forge command. It pauses and resumes the
# kit for a few minutes, and deletes the scratch repository at the end.
# About 20 minutes.
# Before it: scripts/bootstrap.sh (the gatekeeper's team) and scripts/up.sh.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
info() { echo "INFO: $*"; }

kit() { docker compose run --rm -T tools "$@"; }
phase() {
  local out
  out="$(kit test-stage6 "$@" || true)"
  printf '%s\n' "$out" | grep -E '^(PASS|FAIL|INFO)' || true
  printf '%s\n' "$out" | grep -q '^FAIL' && fails=$((fails + 1))
  printf '%s\n' "$out" | grep -Ev '^(PASS|FAIL|INFO|RESULT)' >&3 || true
}
keys="$(mktemp)"
exec 3>>"$keys"
key() { awk -v k="$1" '$1 == k { print $2 }' "$keys"; }
# conformance's review, through the command it always reviews with.
conformance_approves() {
  docker compose exec -T conformance sh -c 'cat > /tmp/stage6.json' <<'JSON'
{"summary": "The kit's stage 6 test: nothing to find.", "findings": []}
JSON
  docker compose exec -T conformance forge review stage6-scratch "$1" /tmp/stage6.json
}

echo "== Preconditions"
[ -n "$(docker compose ps -q --status running gatekeeper)" ] || { fail "the gatekeeper isn't running (scripts/up.sh)"; exit 1; }
[ -n "$(docker compose ps -q --status running conformance)" ] || { fail "conformance isn't running (scripts/up.sh)"; exit 1; }
[ ! -e "$STATE_DIR/control/paused" ] || { fail "the kit is paused; run scripts/resume.sh first"; exit 1; }
pass "the gatekeeper and conformance are running"

echo "== A scratch repository at level 2"
phase scratch

echo "== An ordinary pull request"
phase good
good="$(key GOOD)"
if out="$(conformance_approves "$good" 2>&1)"; then
  pass "conformance approved through forge review: $out"
else
  fail "conformance's review: $out"
fi
phase check-merged "$good"

echo "== A status posted under CI's name on a failing build"
phase spoof

echo "== An API.lock change"
phase apilock
api="$(key API)"
out="$(conformance_approves "$api" 2>&1)" && pass "conformance approved it" || fail "conformance's review: $out"
phase check-apilock "$api"

echo "== Paused"
"$(dirname "$0")/pause.sh" >/dev/null
phase paused-pr
"$(dirname "$0")/resume.sh" >/dev/null
phase resumed "$(key PAUSED)"

echo "== Cleanup"
[ "${KEEP:-}" = 1 ] || phase cleanup
rm -f "$keys"

echo
[ "$fails" = 0 ] && echo "Stage 6: all checks passed." || { echo "Stage 6: $fails check(s) failed."; exit 1; }

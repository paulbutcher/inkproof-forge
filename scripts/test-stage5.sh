#!/usr/bin/env bash
# Stage 5 acceptance: reviews.
#   - with two code owners, each one's review request blocks the merge, and
#     an approver's approval answers their own request;
#   - conformance's findings end in needs-human after the rounds limit;
#   - a pull request breaking CLAUDE.md gets a request for changes naming the
#     section; its author fixes it; conformance approves the new head; it
#     still waits for you at level 1;
#   - a blocking Lasm issue gets a Lasm review straight away;
#   - milestone-review brings an architecture, a process and a Lasm report,
#     each recommendation its own issue, one notice per report; a declined
#     recommendation isn't raised again; the process review cites runs.
# Real Claude runs: conformance and core (a few), and the three milestone
# reviews, which read every repository: expect a few dollars and up to two
# hours. It closes what it opens, recommendations included, and deletes its
# scratch repository, stage5-scratch.
# Before it: scripts/setup-repo.sh inkproof-core 1 reports the files right
# (conformance is a code owner from stage 5), and every agent is running.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
info() { echo "INFO: $*"; }

since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kit() { docker compose run --rm -T tools "$@"; }
jqt() { docker compose run --rm -T --no-deps --entrypoint jq tools "$@"; }
status() {
  docker compose exec -T dispatcher python3 -c \
    'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8080/status").read().decode())'
}
phase() {
  local out
  out="$(kit test-stage5 "$@" || true)"
  printf '%s\n' "$out" | grep -E '^(PASS|FAIL|INFO)' || true
  printf '%s\n' "$out" | grep -q '^FAIL' && fails=$((fails + 1))
  printf '%s\n' "$out" | grep -Ev '^(PASS|FAIL|INFO|RESULT)' >&3 || true
}
keys="$(mktemp)"
exec 3>>"$keys"
key() { awk -v k="$1" '$1 == k { print $2 }' "$keys"; }
# wait_runs PATTERN COUNT MINUTES: until COUNT runs whose triggers match have ended.
wait_runs() {
  local deadline=$(( $(date +%s) + $3 * 60 )) n
  while [ "$(date +%s)" -lt "$deadline" ]; do
    n="$(status | jqt -r --arg p "$1" '[.runs[] | select(.ended != null and (.triggers | any(test($p))))] | length')"
    [ "$n" -ge "$2" ] && return 0
    sleep 30
  done
  return 1
}

echo "== Preconditions"
if kit setup-repo inkproof-core 1 >/dev/null 2>&1; then
  pass "inkproof-core is set up for level 1, conformance a code owner"
else
  fail "inkproof-core needs its setup pull request merged: scripts/setup-repo.sh inkproof-core 1"
  exit 1
fi
for a in coordinator core conformance architecture process lasm; do
  [ -n "$(docker compose ps -q "$a")" ] || { fail "$a isn't running (scripts/up.sh)"; exit 1; }
done
pass "every agent is running"
[ ! -e "$STATE_DIR/control/paused" ] || { fail "the kit is paused; run scripts/resume.sh first"; exit 1; }

echo "== Two code owners"
phase scratch
phase two-owners

echo "== A disputed finding ends with you"
dispute="$(key DISPUTE)"
docker compose exec -T conformance sh -c 'cat > /tmp/findings.json' <<'EOF'
{"summary": "The kit's stage 5 test: a finding the author disputes.", "findings": [{"section": "Markdown", "finding": "The paragraph is hard-wrapped.", "status": "kept"}]}
EOF
for i in 1 2 3 4; do
  docker compose exec -T conformance forge review stage5-scratch "$dispute" /tmp/findings.json || true
done
phase check-dispute "$dispute"

echo "== The conformance loop on inkproof-core"
phase deviation
pr="$(key PR)"
info "core's pull request with a CLAUDE.md deviation: inkproof-core#$pr; waiting for CI, conformance and core"
if wait_runs "^review pull request inkproof-core#$pr at " 2 60; then
  pass "conformance reviewed inkproof-core#$pr twice"
else
  fail "conformance didn't review inkproof-core#$pr twice within an hour"
fi
phase check-loop "$pr"

echo "== A blocking Lasm issue"
lasm="$(kit test-stage5 lasm-issue | tail -1)"
wait_runs "^blocking Lasm issue inkproof-coordination#$lasm$" 1 45 || fail "no Lasm run for #$lasm within 45 minutes"
phase check-lasm "$lasm"

echo "== Milestone reviews"
kit test-stage5 declined | grep -E '^(INFO|PASS|FAIL)' || true
name="Stage 5 test $(date -u +%H%M%S)"
"$(dirname "$0")/milestone.sh" "$name" >/dev/null 2>&1
m="$(kit test-stage5 milestone-issue "$name")"
[ -n "$m" ] && pass "scripts/milestone.sh opened the milestone issue (#$m)" || { fail "no milestone issue"; exit 1; }
wait_runs "^milestone review requested on inkproof-coordination#$m$" 3 120 \
  || fail "the three milestone reviews didn't all finish within two hours"
sessions="$(status | jqt -r '[.runs[].session_id // empty | .[:8]] + [.sessions[].session_id // empty | .[:8]] | unique | join(" ")')"
phase check-milestone "$m" "$since" "$sessions"
notices="$(status | jqt -c --arg since "$since" '[.notices[] | select(.at >= $since)]')"
n="$(printf '%s' "$notices" | jqt -r --argjson m "$m" '[.[] | select(.kind == "report" and .number == $m)] | length')"
[ "$n" = 3 ] && pass "one notice per report on the milestone issue (3)" || fail "$n report notice(s) on the milestone issue; expected 3"
printf '%s' "$notices" | jqt -e '[.[] | select(.kind == "needs-human" and .repo == "inkproof-coordination")] | length == 0' >/dev/null \
  && pass "no notice per recommendation" || fail "recommendations made notices of their own"

echo "== Cleanup"
[ "${KEEP:-}" = 1 ] || phase cleanup "$since"
rm -f "$keys"

echo
[ "$fails" = 0 ] && echo "Stage 5: all checks passed." || { echo "Stage 5: $fails check(s) failed."; exit 1; }

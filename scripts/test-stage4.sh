#!/usr/bin/env bash
# Stage 4 acceptance: the dispatcher's triggers, reconciliation, caps, pause
# and resume, status and notifications, with the coordinator and every
# library agent running.
#   - pause: no run starts, a run's tool calls are refused, scheduled merges
#     are cancelled and recorded, only you may merge; resume undoes it all
#     and reschedules the merges in your name;
#   - while paused, each kind of event queues work for the right agent, and
#     untrusted authors, an agent's own comments and no-reply comments queue
#     nothing; only needs-human (not routine events) makes a notice;
#   - a request filed while the dispatcher is down is found by reconciliation;
#   - a burst of comments becomes one run, and a reached cap stops runs and
#     tells you.
# Most of it runs paused, so it starts only two short Claude runs. It leaves
# closed test issues and pull requests on inkproof-core, inkproof-scale,
# inkproof-consumer-metrics and inkproof-coordination.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
info() { echo "INFO: $*"; }

since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kit() { docker compose run --rm -T -e S4_SINCE="$since" tools "$@"; }
jqt() { docker compose run --rm -T --no-deps --entrypoint jq tools "$@"; }
status() {
  docker compose exec -T dispatcher python3 -c \
    'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8080/status").read().decode())'
}
# Run a phase, show its lines, count its failures; print its KEY lines on fd 3.
phase() {
  local out
  out="$(kit test-stage4 "$@" || true)"
  printf '%s\n' "$out" | grep -E '^(PASS|FAIL|INFO)' || true
  printf '%s\n' "$out" | grep -q '^FAIL' && fails=$((fails + 1))
  printf '%s\n' "$out" | grep -Ev '^(PASS|FAIL|INFO|RESULT)' >&3 || true
}
key() { awk -v k="$1" '$1 == k { print $2 }' "$keys"; }
keys="$(mktemp)"
exec 3>>"$keys"
runs_of() { status | jqt -r --arg a "$1" '[.runs[] | select(.agent == $a)] | length'; }

echo "== Preconditions"
for a in coordinator devtools float core scale consumer-metrics; do
  [ -n "$(docker compose ps -q "$a")" ] && pass "$a is running" || { fail "$a isn't running (scripts/up.sh)"; exit 1; }
done
[ ! -e "$STATE_DIR/control/paused" ] || { fail "the kit is already paused; run scripts/resume.sh first"; exit 1; }
first_notice="$(status | jqt -r '[.notices[].id] | max // 0')"
phase setup
pr="$(key PR)"

echo "== Pause"
bash "$(dirname "$0")/pause.sh" >/dev/null 2>&1
phase check-paused "$pr"
rm -f /tmp/inkproof-pause-probe 2>/dev/null || true
t0="$(date +%s)"
docker compose exec -T -e INKPROOF_RUN=1 -w /workspace/inkproof-core core timeout 300 \
  claude -p --permission-mode dontAsk --output-format json --max-turns 5 \
  "Use the Bash tool to run exactly: touch /tmp/inkproof-pause-probe" >/dev/null 2>&1 || true
took=$(( $(date +%s) - t0 ))
if docker compose exec -T core test -e /tmp/inkproof-pause-probe; then
  fail "while paused, a run's tool call ran"
else
  pass "while paused, a run's tool call was refused, and the run ended (${took}s)"
fi

echo "== Triggers, while paused"
phase events "$pr"
phase check-triggers "$pr" "$(key REQ)" "$(key NH)" "$(key UP)" "$(key BLOCKED)"

echo "== Reconciliation"
docker compose stop dispatcher >/dev/null 2>&1
missed="$(kit test-stage4 missed-request | tail -1)"
docker compose start dispatcher >/dev/null 2>&1
sleep 20
phase check-reconciled "$missed"

echo "== Notifications"
seen="$(mktemp)"; echo "$first_notice" > "$seen"
shown="$(NOTIFY_DRY_RUN=1 NOTIFY_SEEN_FILE="$seen" bash "$(dirname "$0")/notify.sh" 2>/dev/null || true)"
printf '%s\n' "$shown" | grep -q "needs-human inkproof-core#$(key NH)" \
  && pass "notify.sh shows the needs-human notice" || fail "notify.sh didn't show the needs-human notice: $shown"
[ "$(printf '%s\n' "$shown" | grep -c '^NOTICE')" = 1 ] && pass "and nothing else" \
  || fail "notify.sh showed more than the needs-human notice"

echo "== Status"
st="$(bash "$(dirname "$0")/status.sh" 2>/dev/null || true)"
for want in "PAUSED" "coordinator: paused" "waiting since" "Open requests:" "Open pull requests:" "Needs you (needs-human):" "Recommendations:"; do
  printf '%s\n' "$st" | grep -qF "$want" && pass "status shows \"$want\"" || fail "status doesn't show \"$want\""
done

echo "== Resume"
kit test-stage4 drop >/dev/null 2>&1   # what the test queued shouldn't start runs
bash "$(dirname "$0")/resume.sh" >/dev/null 2>&1
phase check-resumed "$pr"

echo "== A burst becomes one run; a cap stops runs and tells you"
before="$(runs_of scale)"
cap=$(( $(status | jqt -r '.agents.scale.runs_today') + 1 ))
# Put the dispatcher back to its own cap however the test ends.
trap 'docker compose up -d --no-deps dispatcher >/dev/null 2>&1' EXIT
MAX_RUNS_PER_DAY="$cap" docker compose up -d --no-deps dispatcher >/dev/null 2>&1
sleep 5
kit test-stage4 burst "$(key REQ)" >/dev/null || fail "couldn't make the burst"
deadline=$(( $(date +%s) + 1200 ))
until [ "$(status | jqt -r '[.runs[] | select(.agent == "scale" and .ended != null)] | length')" -gt "$before" ] \
      || [ "$(date +%s)" -gt "$deadline" ]; do sleep 15; done
[ "$(runs_of scale)" = $(( before + 1 )) ] && pass "three comments in a burst started one run" \
  || fail "the burst started $(( $(runs_of scale) - before )) runs"
kit test-stage4 poke "$(key REQ)" >/dev/null || fail "couldn't comment after the burst"
sleep 90
[ "$(runs_of scale)" = $(( before + 1 )) ] && pass "at its cap, scale started no more runs" || fail "scale went past its cap"
status | jqt -e '.agents.scale.state == "capped"' >/dev/null && pass "status shows scale capped" || fail "status doesn't show scale capped"
status | jqt -e '[.notices[] | select(.kind == "cap" and .repo == "inkproof-scale" and .posted == 1)] | length > 0' >/dev/null \
  && pass "the cap is a notice, posted to you on scale's Kit notices issue" || fail "no posted cap notice"
docker compose up -d --no-deps dispatcher >/dev/null 2>&1

echo "== Cleanup"
[ "${KEEP:-}" = 1 ] || phase cleanup
rm -f "$keys" "$seen"

echo
[ "$fails" = 0 ] && echo "Stage 4: all checks passed." || { echo "Stage 4: $fails check(s) failed."; exit 1; }

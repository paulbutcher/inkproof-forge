#!/usr/bin/env bash
# Stage 9 acceptance: supervision.
#   - a stall no rule catches (an accepted request labelled `blocked` by the
#     human, with no Blocked-by: line) wakes the supervisor, not the owner,
#     once the kit has been quiet;
#   - the supervisor's trigger names the item, why it is stalled and whose it is;
#   - the supervisor acts on the item (a comment, or taking the label off),
#     and the dispatcher doesn't comment a stall notice on it.
# It runs the dispatcher with IDLE_SECONDS=60 for its duration, and starts real
# Claude runs: the coordinator's check of the new request, the supervisor's
# run, and whatever the supervisor's nudge wakes. Up to an hour. It closes its
# issue at the end (KEEP=1 keeps it open).
# Before it: scripts/bootstrap.sh and scripts/up.sh, with `process` of kind
# supervisor in agents.conf.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
confirm "This runs the stage 9 acceptance tests, which file an issue on inkproof-core and start Claude runs"
export KIT_YES=1
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
info() { echo "INFO: $*"; }

kit() { docker compose run --rm -T tools "$@"; }
jqt() { docker compose run --rm -T --no-deps --entrypoint jq tools "$@"; }
status() {
  docker compose exec -T dispatcher python3 -c \
    'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8080/status").read().decode())'
}
REPO=inkproof-core
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== Preconditions"
[ -n "$(docker compose ps -q process)" ] && pass "process is running" || { fail "process isn't running (scripts/up.sh)"; exit 1; }
grep -Eq '^process[[:space:]]+supervisor[[:space:]]' agents.conf && pass "process is the supervisor in agents.conf" \
  || { fail "process isn't of kind supervisor in agents.conf"; exit 1; }
[ ! -e "$STATE_DIR/control/paused" ] && [ ! -e "$STATE_DIR/control/draining" ] && pass "the kit isn't paused or draining" \
  || { fail "the kit is paused or draining: scripts/resume.sh first"; exit 1; }
docker compose exec -T process test -d /var/lib/inkproof/archive-all/coordinator \
  && pass "process reads the whole run archive" || fail "process can't read /var/lib/inkproof/archive-all"

# A quiet spell after a minute, not twenty; put the dispatcher back however the test ends.
trap 'docker compose up -d --no-deps dispatcher >/dev/null 2>&1' EXIT
IDLE_SECONDS=60 docker compose up -d --no-deps dispatcher >/dev/null 2>&1
sleep 5

echo "== A stall no rule catches"
n="$(kit api POST "/repos/$FORGE_ORG/$REPO/issues" "$(jqt -nc '{title: "Stage 9 test: a stalled request",
  body: "(no reply needed)\n\nThe kit'"'"'s stage 9 test: an accepted request, labelled blocked by the human with no Blocked-by: line, so nothing the kit can see is moving it on."}')" \
  | jqt -r .number)"
kit api POST "/repos/$FORGE_ORG/$REPO/issues/$n/labels" '{"labels":["request","accepted","blocked"]}' >/dev/null
item="$REPO#$n"
info "filed $item"

echo "== The supervisor is woken for it"
deadline=$(( $(date +%s) + 2400 ))
run=""
until [ -n "$run" ] || [ "$(date +%s)" -gt "$deadline" ]; do
  sleep 30
  run="$(status | jqt -r --arg i "$item" '[.runs[] | select(.agent == "process" and any(.triggers[]; startswith("the kit is quiet") and contains($i)))] | first | .id // empty')"
done
if [ -z "$run" ]; then
  fail "no supervisor run for $item within 40 minutes"
else
  trigger="$(status | jqt -r --argjson r "$run" '.runs[] | select(.id == $r) | .triggers[] | select(startswith("the kit is quiet"))')"
  pass "supervisor run $run started for $item"
  grep -q "$item (it is labelled blocked, but no Blocked-by: line names an open, unreleased issue: core)" <<<"$trigger" \
    && pass "its trigger says why $item is stalled and whose it is" || fail "its trigger doesn't say why, or whose: $trigger"
  status | jqt -e --arg i "$item" '[.runs[] | select(.agent == "core" and any(.triggers[]; contains("nothing is moving") and contains($i)))] | length == 0' >/dev/null \
    && pass "core wasn't woken for it by the stall check" || fail "the stall check woke core as well"
  deadline=$(( $(date +%s) + 1800 ))
  until status | jqt -e --argjson r "$run" '.runs[] | select(.id == $r) | .ended != null' >/dev/null || [ "$(date +%s)" -gt "$deadline" ]; do sleep 30; done
  info "run $run: $(status | jqt -r --argjson r "$run" '.runs[] | select(.id == $r) | "\(.outcome); $\(.cost); \(.turns) turns; \(.forge_changes) Forgejo changes"')"
fi

echo "== It acted on the item"
comments="$(kit api GET "/repos/$FORGE_ORG/$REPO/issues/$n/comments")"
timeline="$(kit api GET "/repos/$FORGE_ORG/$REPO/issues/$n/timeline?limit=100")"
said="$(jqt -r '[.[] | select(.user.login == "process") | .body] | last // empty' <<<"$comments")"
unlabelled="$(jqt -r '[.[] | select(.type == "label" and .user.login == "process" and .label.name == "blocked" and .body != "1")] | length' <<<"$timeline")"
if [ -n "$said" ] || [ "$unlabelled" != 0 ]; then
  pass "the supervisor acted on $item"
  [ -n "$said" ] && info "it said: $(head -c 400 <<<"$said" | tr '\n' ' ')"
  [ "$unlabelled" != 0 ] && info "it took the blocked label off"
else
  fail "the supervisor neither commented on $item nor took its label off"
fi
jqt -e '[.[] | select(.user.login == "dispatcher" and (.body | contains("stall check")))] | length == 0' <<<"$comments" >/dev/null \
  && pass "the dispatcher commented no stall notice on it" || fail "the dispatcher commented a stall notice on it"
recs="$(kit api GET "/repos/$FORGE_ORG/inkproof-coordination/issues?state=all&type=issues&labels=review:process&since=$since&limit=50" \
  | jqt -r '[.[] | select(.user.login == "process")] | map("#\(.number) \(.title)") | join("; ")')"
info "recommendations the supervisor filed: ${recs:-none}"

echo "== Cleanup"
if [ "${KEEP:-}" != 1 ]; then
  kit api PATCH "/repos/$FORGE_ORG/$REPO/issues/$n" '{"state":"closed"}' >/dev/null && info "closed $item"
fi

echo
[ "$fails" = 0 ] && echo "Stage 9: all checks passed." || { echo "Stage 9: $fails check(s) failed."; exit 1; }

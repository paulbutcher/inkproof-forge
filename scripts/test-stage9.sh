#!/usr/bin/env bash
# Stage 9 acceptance: obligations and supervision.
#   - an accepted request labelled `blocked` by the human, with no Blocked-by:
#     line, is held "blocked without a reason", which its owner owes: core is
#     woken for it at once, and the wake says so;
#   - core is woken again while it stands, and either moves it on (takes the
#     label off, or adds a Blocked-by: line), or, once its tries are spent, it
#     becomes the human's `Stalled:` decision and the supervisor is woken for
#     it, and acts on it. Either way passes; the test says which happened.
# It runs the dispatcher with OBLIGATION_BACKOFF=0,60,60 for its duration, and
# starts real Claude runs: the coordinator's check of the new request, core's,
# and perhaps the supervisor's. Up to an hour. It closes its issue at the end
# (KEEP=1 keeps it open).
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

echo "== Preconditions"
[ -n "$(docker compose ps -q process)" ] && pass "process is running" || { fail "process isn't running (scripts/up.sh)"; exit 1; }
grep -Eq '^process[[:space:]]+supervisor[[:space:]]' agents.conf && pass "process is the supervisor in agents.conf" \
  || { fail "process isn't of kind supervisor in agents.conf"; exit 1; }
[ ! -e "$STATE_DIR/control/paused" ] && [ ! -e "$STATE_DIR/control/draining" ] && pass "the kit isn't paused or draining" \
  || { fail "the kit is paused or draining: scripts/resume.sh first"; exit 1; }
docker compose exec -T process test -d /var/lib/inkproof/archive-all/coordinator \
  && pass "process reads the whole run archive" || fail "process can't read /var/lib/inkproof/archive-all"

# Waits of a minute, not 15; put the dispatcher back however the test ends.
trap 'docker compose up -d --no-deps dispatcher >/dev/null 2>&1' EXIT
OBLIGATION_BACKOFF=0,60,60 docker compose up -d --no-deps dispatcher >/dev/null 2>&1
sleep 5

echo "== An item held without a reason"
n="$(kit api POST "/repos/$FORGE_ORG/$REPO/issues" "$(jqt -nc '{title: "Stage 9 test: blocked without a reason",
  body: "(no reply needed)\n\nThe kit'"'"'s stage 9 test: an accepted request, labelled blocked by the human with no Blocked-by: line, so its owner owes a line or the label off."}')" \
  | jqt -r .number)"
kit api POST "/repos/$FORGE_ORG/$REPO/issues/$n/labels" '{"labels":["request","accepted","blocked"]}' >/dev/null
item="$REPO#$n"
info "filed $item"

echo "== Its owner is woken for it"
deadline=$(( $(date +%s) + 1200 ))
run=""
until [ -n "$run" ] || [ "$(date +%s)" -gt "$deadline" ]; do
  sleep 20
  run="$(status | jqt -r --arg w "issue $item is blocked without a reason" \
    '[.runs[] | select(.agent == "core" and any(.triggers[]; contains($w)))] | first | .id // empty')"
done
[ -n "$run" ] && pass "core's run $run was woken for $item" || fail "core wasn't woken for $item within 20 minutes"

echo "== Moved on, or the human's and the supervisor's"
deadline=$(( $(date +%s) + 2700 ))
outcome=""
until [ -n "$outcome" ] || [ "$(date +%s)" -gt "$deadline" ]; do
  sleep 30
  s="$(status)"
  owed="$(jqt -r --arg i "$item" '[.obligations[] | select(.unit == $i and .agent == "core")] | first // empty | "\(.wakes) \(.escalated)"' <<<"$s")"
  if [ -z "$owed" ]; then
    outcome="moved"
  elif [ "${owed#* }" = 1 ]; then
    outcome="escalated"
  fi
done
case "$outcome" in
  moved)
    pass "core moved $item on: it owes nothing for it any more"
    timeline="$(kit api GET "/repos/$FORGE_ORG/$REPO/issues/$n/timeline?limit=100")"
    info "core's actions: $(jqt -r '[.[] | select(.user.login == "core") | .type] | join(", ")' <<<"$timeline")" ;;
  escalated)
    pass "core was woken $(cut -d' ' -f1 <<<"$owed") times without moving it on: it is the human's"
    kit api GET "/repos/$FORGE_ORG/inkproof-coordination/issues?state=open&type=issues&limit=50" \
      | jqt -e --arg t "Stalled: $item" '[.[] | select(.title == $t)] | length == 1' >/dev/null \
      && pass "a Stalled: decision is open for it" || fail "no Stalled: decision for it"
    deadline=$(( $(date +%s) + 1200 ))
    sup=""
    until [ -n "$sup" ] || [ "$(date +%s)" -gt "$deadline" ]; do
      sleep 30
      sup="$(status | jqt -r --arg i "$item has stalled" '[.runs[] | select(.agent == "process" and any(.triggers[]; startswith($i)) and .ended != null)] | first | .id // empty')"
    done
    if [ -n "$sup" ]; then
      pass "the supervisor's run $sup looked into it"
      said="$(kit api GET "/repos/$FORGE_ORG/$REPO/issues/$n/comments" | jqt -r '[.[] | select(.user.login == "process") | .body] | last // empty')"
      [ -n "$said" ] && info "it said: $(head -c 400 <<<"$said" | tr '\n' ' ')" || info "it said nothing on the item"
    else
      fail "the supervisor wasn't woken for it, or didn't finish, within 20 minutes"
    fi ;;
  *) fail "within 45 minutes, core neither moved $item on nor was it put to the human" ;;
esac

echo "== Cleanup"
if [ "${KEEP:-}" != 1 ]; then
  kit api PATCH "/repos/$FORGE_ORG/$REPO/issues/$n" '{"state":"closed"}' >/dev/null && info "closed $item"
fi

echo
[ "$fails" = 0 ] && echo "Stage 9: all checks passed." || { echo "Stage 9: $fails check(s) failed."; exit 1; }

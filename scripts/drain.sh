#!/usr/bin/env bash
# A clean stop, before restarting the Mac or the kit (scripts/up.sh): no run
# starts, while runs under way finish and CI and scheduled merges go on. It
# waits until nothing is under way, then says it is safe to stop. Work that
# arrives meanwhile is queued, and starts after scripts/resume.sh.
#   scripts/drain.sh            drain, and wait however long it takes
#   scripts/drain.sh --max N    after N minutes, pause instead (as
#                               scripts/pause.sh does), which stops the runs
#                               still going at their next tool call and queues
#                               their work again; then wait for them to end
set -euo pipefail
. "$(dirname "$0")/lib.sh"
die() { echo "$*" >&2; exit 1; }
max=""
case "${1:-}" in
  "") ;;
  --max) max="${2:-}"; case "$max" in ''|*[!0-9]*) die "usage: scripts/drain.sh [--max MINUTES]" ;; esac ;;
  *) die "usage: scripts/drain.sh [--max MINUTES]" ;;
esac

# One line from the dispatcher: OLD if it predates draining, DRAINED, or what
# is still under way.
state() {
  docker compose exec -T dispatcher python3 -c '
import json, urllib.request
s = json.load(urllib.request.urlopen("http://127.0.0.1:8080/status"))
if "draining" not in s:
    print("OLD")
elif not s["draining"]:
    print("not draining yet")
elif s["drained"]:
    print("DRAINED")
else:
    print("; ".join(w for w in s["under_way"] if not ('"$1"' and w == "CI")) or "DRAINED")'
}

s="$(state False)" || die "The dispatcher isn't answering; is the kit up (scripts/up.sh)?"
[ "$s" = OLD ] && die "This dispatcher predates draining. Bring in the new one first, which leaves runs alone:
  docker compose build tools && docker compose up -d --build dispatcher"
confirm "This stops agents starting runs until scripts/resume.sh; runs under way finish"
docker compose run --rm -T tools drain

start="$(date +%s)" last="" paused=False
while :; do
  s="$(state "$paused" 2>/dev/null)" || s="(the dispatcher isn't answering)"
  if [ "$s" = DRAINED ]; then
    if [ "$paused" = True ]; then
      echo "$(date +%H:%M) No run is under way: safe to stop. CI still running is cut short, and can be re-run."
    else
      echo "$(date +%H:%M) Nothing is under way: safe to stop."
    fi
    echo "Afterwards, scripts/resume.sh starts work again."
    exit 0
  fi
  [ "$s" = "$last" ] || echo "$(date +%H:%M) Waiting for: $s"
  last="$s"
  if [ -n "$max" ] && [ "$paused" = False ] && [ $(( $(date +%s) - start )) -ge $(( max * 60 )) ]; then
    echo "$(date +%H:%M) Still under way after $max minutes: pausing, so runs stop at their next tool call."
    KIT_YES=1 "$(dirname "$0")/pause.sh"
    paused=True
  fi
  sleep 30
done

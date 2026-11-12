#!/usr/bin/env bash
# Which reviews have taken place and their results, from Forgejo:
#   pull request reviews (conformance, the coordinator, you) on pull requests
#   updated in the last DAYS days (default 7), with each verdict and
#   conformance's round and open findings; and each milestone's architecture,
#   process and Lasm reviews, with what became of their recommendations.
#   --changes shows only the reviews that asked for more work: those that
#   requested changes, and milestone reviews that made recommendations.
#   scripts/reviews.sh [--changes] [DAYS]
set -euo pipefail
. "$(dirname "$0")/lib.sh"
days=7 changes=""
while [ $# -gt 0 ]; do
  case "$1" in
    --changes) changes=1; shift ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *[!0-9]*|"") echo "usage: scripts/reviews.sh [--changes] [DAYS]" >&2; exit 2 ;;
    *) days="$1"; shift ;;
  esac
done
# This machine's offset from UTC, for the times shown.
z="$(date +%z)"
tz=$(( (10#${z:1:2} * 3600 + 10#${z:3:2} * 60) * (${z:0:1}1) ))
docker compose run --rm -T -e KIT_TZ_OFFSET="$tz" -e KIT_WEB_URL="http://localhost:${FORGE_PORT:-3000}" tools reviews "$days" $changes

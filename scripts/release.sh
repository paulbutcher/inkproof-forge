#!/usr/bin/env bash
# Release a library yourself, at any autonomy level, even while paused:
#   scripts/release.sh <repository>                      show what the release would be
#   scripts/release.sh <repository> "<note>" [<issue>...]  release it
# The version is computed from API.lock's changes since the last release tag,
# exactly as the gatekeeper computes it for the coordinator at level 3. It
# refuses unless CHANGELOG.md on main has an entry for that version and CI
# passed on main's head. It tags main's head as you, publishes the release
# with the note and the changelog entry, and comments the release on each
# issue named, which must be open and labelled ready-for-release.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
[ $# -ge 1 ] || { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
repo="$1"; shift
run=(docker compose run --rm --no-deps -T)
if [ $# = 0 ]; then
  exec "${run[@]}" gatekeeper gatekeeper plan "$repo"
fi
# The gatekeeper's code, with your token instead of its own.
exec "${run[@]}" -e TOKEN_FILE=/run/secrets/human/token \
  -v "$(cd "$STATE_DIR" && pwd)/secrets/human:/run/secrets/human:ro" \
  gatekeeper gatekeeper release --human "$repo" "$@"

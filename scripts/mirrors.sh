#!/usr/bin/env bash
# Give every repository in agents.conf a push mirror to GitHub, synced on each
# push, using secrets/github/mirror-token, then show each mirror's state.
#   scripts/mirrors.sh            add missing mirrors
#   scripts/mirrors.sh --rotate   also replace every mirror's token
# Mirrors carry the branches MIRROR_BRANCHES names (default main) and every tag.
# Forgejo mirrors exactly: on GitHub, branches and tags outside that set are
# deleted at the first sync.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
docker compose run --rm tools mirrors "$@"
sleep 5
docker compose run --rm tools mirror-status

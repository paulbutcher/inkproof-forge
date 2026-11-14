#!/usr/bin/env bash
# Create a new library's repository in Forgejo with a seed that builds and
# passes CI, then give the agents' teams access and the labels, as
# scripts/bootstrap.sh does. Add the library's agent to agents.conf first.
# Safe to re-run.
#   scripts/new-library.sh inkproof-float Float
# Then scripts/mirrors.sh, for its push mirror to an empty GitHub repository
# of the same name, and scripts/setup-repo.sh for its autonomy level.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
[ $# = 2 ] || { echo "usage: scripts/new-library.sh <repository> <Name>" >&2; exit 2; }
docker compose run --rm tools new-library "$1" "$2"
docker compose run --rm tools bootstrap

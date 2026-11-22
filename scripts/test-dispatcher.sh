#!/usr/bin/env bash
# The dispatcher's unit tests (docker/dispatcher/test_dispatcher.py), with
# Forgejo stubbed out: quick, and nothing outside a throwaway container.
#   ORPHAN_WORLDS=50000 ORPHAN_SEED=1000 ORPHAN_VERBOSE=1 scripts/test-dispatcher.sh
# runs the search for orphaned items (NobodyOrphaned) wider, from another
# seed, listing every failure.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
image="$(sed -n 's/^FROM //p' docker/dispatcher/Dockerfile)"
docker run --rm --network none -v "$PWD/docker/dispatcher:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  -e ORPHAN_WORLDS -e ORPHAN_SEED -e ORPHAN_VERBOSE \
  "$image" python3 -m unittest test_dispatcher

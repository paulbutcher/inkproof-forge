#!/usr/bin/env bash
# The dispatcher's unit tests (docker/dispatcher/test_dispatcher.py), with
# Forgejo stubbed out, and the completeness checks of its states
# (test_states.py), over every combination of each unit's facts; and the
# gatekeeper's webhook handling (docker/gatekeeper/test_gatekeeper.py):
# quick, and nothing outside a throwaway container.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
image="$(sed -n 's/^FROM //p' docker/dispatcher/Dockerfile)"
docker run --rm --network none -v "$PWD/docker/dispatcher:/src:ro" -v "$PWD/docker/gatekeeper:/gatekeeper:ro" \
  -w /src -e PYTHONDONTWRITEBYTECODE=1 -e GATEKEEPER_PY=/gatekeeper/gatekeeper.py \
  "$image" sh -c 'python3 -m unittest test_dispatcher test_states && cd /gatekeeper && python3 -m unittest test_gatekeeper'

#!/usr/bin/env bash
# Unit tests for the agents' managed hook (docker/agent/bin/inkproof-hook):
# the guard that keeps the human's sessions from changing the checkouts runs
# use. Quick, and nothing outside a throwaway container.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
image="$(sed -n 's/^FROM //p' docker/dispatcher/Dockerfile)"
docker run --rm --network none -v "$PWD/docker/agent:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  "$image" python3 -m unittest test_inkproof_hook

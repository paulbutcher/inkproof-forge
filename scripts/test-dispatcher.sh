#!/usr/bin/env bash
# The dispatcher's unit tests (docker/dispatcher/test_dispatcher.py), with
# Forgejo stubbed out: quick, and nothing outside a throwaway container.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
image="$(sed -n 's/^FROM //p' docker/dispatcher/Dockerfile)"
docker run --rm --network none -v "$PWD/docker/dispatcher:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  "$image" python3 -m unittest test_dispatcher

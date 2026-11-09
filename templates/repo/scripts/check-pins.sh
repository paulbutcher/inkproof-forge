#!/usr/bin/env bash
# Fail if any of this project's dependencies (an inkproof- repository under
# one of GH_OWNERS, the GitHub owners it has lived under) is pinned to anything
# but a release tag (vX.Y.Z). Other dependencies are not checked.
set -euo pipefail
owners="${GH_OWNERS:?set GH_OWNERS to the GitHub owners of this project, space-separated}"

if [ ! -f lake-manifest.json ]; then
  echo "No lake-manifest.json; nothing to check."
  exit 0
fi

bad="$(jq -r --arg owners "$owners" '
  ($owners | ascii_downcase | split(" ") | map(select(. != "")
    | "github.com/" + . + "/inkproof-")) as $prefixes
  | .packages[]
  | select(.type == "git")
  | (.url | ascii_downcase) as $u
  | select(any($prefixes[]; . as $p | $u | contains($p)))
  | select((.inputRev // "") | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$") | not)
  | "\(.name) is pinned to \"\(.inputRev // "nothing")\""' lake-manifest.json)"

if [ -n "$bad" ]; then
  echo "$bad"
  echo "::error::Internal dependencies must be pinned to release tags (vX.Y.Z)"
  exit 1
fi
echo "All internal dependencies are pinned to release tags."

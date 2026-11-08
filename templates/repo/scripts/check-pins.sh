#!/usr/bin/env bash
# Fail if any dependency from our own GitHub owner is pinned to anything but
# a release tag (vX.Y.Z). External dependencies are not checked.
set -euo pipefail
owner="${GH_OWNER:-${GITHUB_REPOSITORY_OWNER:?set GH_OWNER}}"

if [ ! -f lake-manifest.json ]; then
  echo "No lake-manifest.json; nothing to check."
  exit 0
fi

bad="$(jq -r --arg owner "$owner" '
  .packages[]
  | select(.type == "git")
  | select(.url | ascii_downcase | contains("github.com/" + ($owner | ascii_downcase) + "/"))
  | select((.inputRev // "") | test("^v[0-9]+\\.[0-9]+\\.[0-9]+$") | not)
  | "\(.name) is pinned to \"\(.inputRev // "nothing")\""' lake-manifest.json)"

if [ -n "$bad" ]; then
  echo "$bad"
  echo "::error::Internal dependencies must be pinned to release tags (vX.Y.Z)"
  exit 1
fi
echo "All internal dependencies are pinned to release tags."

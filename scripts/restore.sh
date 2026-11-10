#!/usr/bin/env bash
# Replace the kit's Forgejo, run ledger and run archive with a backup:
#   scripts/restore.sh <backup folder>
# For when Docker's volumes are lost or broken, or on a new Mac. Everything
# since the backup is lost from Forgejo; GitHub's mirrors may be ahead of it
# by then. It asks you to confirm, keeps a raw copy of the current Forgejo data
# beside the backups, stops the kit, restores into fresh volumes, and starts
# the kit again with scripts/up.sh.
# secrets/ must be the kit's own (from Time Machine on a new Mac): Forgejo
# keeps the tokens' hashes, so the files have to match.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
umask 077
b="${1:-}"
[ -n "$b" ] && [ -s "$b/forgejo.tar.gz" ] || { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
b="$(cd "$b" && pwd)"
dir="${BACKUP_DIR:-$STATE_DIR/backups}"
p=inkproof-forge
vols="forgejo-data dispatcher-state run-archive"

echo "This replaces Forgejo, the run ledger and the run archive with the backup ${b##*/}."
echo "Anything since then is lost from Forgejo, and every agent is stopped meanwhile."
if [ "${RESTORE_CONFIRM:-}" != restore ]; then
  printf 'Type "restore" to go ahead: '
  read -r answer
  [ "$answer" = restore ] || { echo "Nothing changed."; exit 1; }
fi

if docker volume inspect "${p}_forgejo-data" >/dev/null 2>&1; then
  keep="$dir/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
  mkdir -p "$dir"
  docker run --rm --network none -v "${p}_forgejo-data:/d:ro" alpine:3.24 tar -C /d -czf - . > "$keep"
  echo "Kept a raw copy of the current Forgejo data: ${keep#./}"
fi

docker compose --profile tools down --remove-orphans
for v in $vols; do
  docker volume rm -f "${p}_$v" >/dev/null
  docker volume create --label com.docker.compose.project=$p --label com.docker.compose.volume=$v "${p}_$v" >/dev/null
done
docker compose --profile tools build --quiet tools >/dev/null
docker run --rm --user 0 --network none --entrypoint restore-volumes \
  -v "${p}_forgejo-data:/restore/forgejo" -v "${p}_dispatcher-state:/restore/dispatcher" \
  -v "${p}_run-archive:/restore/archive" -v "$b:/backup:ro" \
  inkproof-forge-tools /backup

"$(dirname "$0")/up.sh"
docker compose exec -T forgejo forgejo admin regenerate hooks >/dev/null
echo
echo "Restored ${b##*/}. If tokens were made since that backup, scripts/bootstrap.sh makes them right."

#!/usr/bin/env bash
# Back up the kit into BACKUP_DIR (default backups/ in the kit's folder), as
# one folder per backup named by its UTC time, keeping the newest BACKUP_KEEP
# (default 7). Time Machine, if it covers this folder, keeps their history.
# scripts/backup-install.sh runs this daily; scripts/restore-test.sh checks
# that the newest restores. Each backup holds:
#   forgejo.tar.gz      `forgejo dump`: repositories, the database as SQL,
#                       attachments and Forgejo's configuration
#   ledger.db           the dispatcher's run ledger (an SQLite online backup)
#   run-archive.tar.gz  every run's and session's log and transcript
#   before.json, after.json
#                       what Forgejo held just before and after the dump
#                       (kit snapshot), for scripts/restore-test.sh
# secrets/ and control/ aren't in it: they are files in the kit's folder.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
umask 077  # tokens' hashes, Forgejo's secrets, transcripts
dir="${BACKUP_DIR:-$STATE_DIR/backups}"
keep="${BACKUP_KEEP:-7}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dir/$stamp"
mkdir -p "$out.partial"
trap 'rm -rf "$out.partial"' EXIT

docker compose run --rm -T tools snapshot > "$out.partial/before.json"

docker compose exec -T forgejo sh -c 'rm -f /tmp/gitea/kit-dump.tar.gz && forgejo dump --quiet --type tar.gz --skip-index --skip-repo-archives --skip-log --file /tmp/gitea/kit-dump.tar.gz >&2'
docker compose exec -T forgejo cat /tmp/gitea/kit-dump.tar.gz > "$out.partial/forgejo.tar.gz"
docker compose exec -T forgejo rm -f /tmp/gitea/kit-dump.tar.gz

docker compose exec -T dispatcher python3 -c '
import sqlite3, sys, tempfile, os
f = tempfile.mktemp(suffix=".db")
src, dst = sqlite3.connect("/state/ledger.db"), sqlite3.connect(f)
src.backup(dst); dst.close()
sys.stdout.buffer.write(open(f, "rb").read()); os.remove(f)' > "$out.partial/ledger.db"

docker compose run --rm --no-deps -T --entrypoint tar archive-init -C /archive -czf - . > "$out.partial/run-archive.tar.gz"

docker compose run --rm -T tools snapshot > "$out.partial/after.json"

trap - EXIT
mv "$out.partial" "$out"
echo "Backed up to ${out#./} ($(du -sh "$out" | cut -f1))."

# The newest $keep backups stay; so does anything that isn't a backup.
ls -1d "$dir"/[0-9]*Z 2>/dev/null | sort -r | tail -n +"$((keep + 1))" | while read -r old; do
  rm -rf "$old"
  echo "Removed the old backup ${old#./}."
done

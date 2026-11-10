#!/usr/bin/env bash
# Install (or, with --remove, remove) the launchd job that runs
# scripts/backup.sh daily at 03:30, or when the Mac next wakes. Docker Desktop
# must be running then; if a backup fails you get a notification.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
label=com.inkproof-forge.backup
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
if [ "${1:-}" = --remove ]; then
  rm -f "$plist"
  echo "Removed the daily backup."
  exit 0
fi
kit="$(pwd)"
mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR/control"
logf="$kit/${STATE_DIR#./}/control/backup.log"
logf="${logf//\/.\//\/}"
sed -e "s|@LABEL@|$label|g" -e "s|@KIT@|$kit|g" -e "s|@PATH@|$PATH|g" -e "s|@LOG@|$logf|g" \
  launchd/backup.plist > "$plist"
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "The kit backs up daily at 03:30 into ${BACKUP_DIR:-$STATE_DIR/backups}; its log is $logf."

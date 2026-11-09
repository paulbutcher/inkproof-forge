#!/usr/bin/env bash
# Install (or, with --remove, remove) the launchd job that runs
# scripts/notify.sh every minute while you're logged in on this Mac.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
label=com.inkproof-forge.notify
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
if [ "${1:-}" = --remove ]; then
  rm -f "$plist"
  echo "Removed the notifier."
  exit 0
fi
kit="$(pwd)"
mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR/control"
sed -e "s|@LABEL@|$label|g" -e "s|@KIT@|$kit|g" -e "s|@PATH@|$PATH|g" launchd/notify.plist > "$plist"
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "The notifier runs every minute; its log is $kit/${STATE_DIR#./}/control/notify.log."

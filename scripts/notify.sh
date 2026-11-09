#!/usr/bin/env bash
# Show the dispatcher's new notices as macOS notifications: needs-human,
# blocking upstream issues, reached caps, and runs the extension doesn't list.
# Nothing routine is a notice. scripts/notify-install.sh runs this every
# minute from launchd. With NOTIFY_DRY_RUN=1 it prints them instead.
set -euo pipefail
KIT_NO_BUILD=1 . "$(dirname "$0")/lib.sh"
seen="${NOTIFY_SEEN_FILE:-$STATE_DIR/control/notified-id}"
last="$(cat "$seen" 2>/dev/null || echo 0)"
notices="$(curl -fsS --max-time 10 "http://127.0.0.1:${DISPATCHER_PORT:-3001}/notices.txt?after=$last")" || exit 0
[ -n "$notices" ] || exit 0
printf '%s\n' "$notices" | while IFS="$(printf '\t')" read -r id kind where text; do
  [ -n "$id" ] || continue
  if [ "${NOTIFY_DRY_RUN:-}" = 1 ] || ! command -v osascript >/dev/null; then
    echo "NOTICE $id $kind $where: $text"
  else
    esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
    osascript -e "display notification \"$(esc "$text")\" with title \"Inkproof: $(esc "$kind")\" subtitle \"$(esc "$where")\"" || true
  fi
  echo "$id" > "$seen"
done

#!/usr/bin/env bash
# What each agent is doing, from the dispatcher (never GitHub): its state,
# pending triggers, and recent runs; then notices for you.
# Then open requests, pull requests, recommendations and needs-human items
# across the project's repositories, from Forgejo.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
json="$(docker compose exec -T dispatcher python3 -c \
  'import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("http://127.0.0.1:8080/status").read().decode())')" \
  || { echo "The dispatcher isn't answering; is the kit up (scripts/up.sh)?" >&2; exit 1; }
docker compose run --rm -T --no-deps --entrypoint jq tools -r '
  def t: if . == null then "-" else . end;
  (if .paused then "PAUSED: no runs start until scripts/resume.sh.\n" else "" end),
  "Reconciled with Forgejo: \(.reconciled // "not yet")",
  "Agents:",
  (.agents | to_entries[]
    | "  \(.key): \(.value.state), \(.value.runs_today)/\(.value.cap) runs today, $\(.value.spend_today) spent"
      + (if (.value.pending | length) > 0 then "\n" + ([.value.pending[] | "    waiting since \(.since): \(.trigger)" + (if .after then " (not before \(.after))" else "" end)] | join("\n")) else "" end)),
  "",
  "Recent runs:",
  (.runs[:15][] | "  #\(.id) \(.agent) \(.started | t) -> \(.ended | t): \(.outcome // "running")"
      + "\n      triggers: \(.triggers | join("; "))"
      + "\n      cost $\((.cost // 0) * 100 | round / 100), \(.turns // 0) turns, \(.denied | length) denied, \(.forge_changes // 0) Forgejo change(s)"
      + "\n      session \(.session_id | t)" + (if .in_extension == 0 and .ended != null then " (NOT listed in the extension)" else "" end)
      + "\n      log \(.log | t)"),
  "",
  "Your sessions in agent containers:",
  (.sessions[:10][] | "  \(.agent) \(.session_id): \(.started | t) -> \(.ended | t)"),
  "",
  "Notices:",
  (.notices[:10][] | "  \(.at) \(.kind) \(.repo // "")#\(.number // "") \(if .posted == 1 then "(mentioned you in Forgejo)" else "" end)")
' <<<"$json"
echo
docker compose run --rm -T tools project-status

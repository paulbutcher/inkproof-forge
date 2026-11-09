#!/usr/bin/env bash
# Create your admin account and token if needed, then converge Forgejo on the
# organisation, the kit's accounts and tokens, teams and the dispatcher's
# webhook. Safe to re-run; run it again after adding repositories or agents.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
: "${HUMAN_USER:?set HUMAN_USER in $KIT_ENV}" "${HUMAN_EMAIL:?set HUMAN_EMAIL in $KIT_ENV}"

fj() { docker compose exec -T forgejo forgejo "$@"; }

if ! fj admin user list --admin | awk 'NR > 1 { print $2 }' | grep -qx "$HUMAN_USER"; then
  # Your password, typed here or given as HUMAN_PASSWORD for unattended setup.
  # The account is not marked "must change password": Forgejo refuses every
  # API call from an account in that state, the kit's token included.
  if [ -z "${HUMAN_PASSWORD:-}" ]; then
    [ -t 0 ] || { echo "Set HUMAN_PASSWORD, or run this in a terminal to be asked for it." >&2; exit 1; }
    while :; do
      printf 'Choose a password for your Forgejo account, %s: ' "$HUMAN_USER"
      read -rs HUMAN_PASSWORD; echo
      printf 'Again: '
      read -rs again; echo
      [ "$HUMAN_PASSWORD" = "$again" ] && [ ${#HUMAN_PASSWORD} -ge 8 ] && break
      echo "They differ, or are shorter than 8 characters; try again."
    done
  fi
  fj admin user create --admin --username "$HUMAN_USER" --email "$HUMAN_EMAIL" \
    --password "$HUMAN_PASSWORD" --must-change-password=false >/dev/null
  echo "Created your admin account, ${HUMAN_USER}."
fi

set +e
docker compose run --rm tools check-human
human=$?
set -e
if [ "$human" = 3 ]; then
  echo "Forgejo wants ${HUMAN_USER}'s password changed first. Sign in at http://localhost:${FORGE_PORT:-3000}/, change it, and run this again." >&2
  exit 1
elif [ "$human" != 0 ]; then
  echo "Creating an admin token for the kit's scripts in $STATE_DIR/secrets/human/token."
  tok="$(fj admin user generate-access-token --username "$HUMAN_USER" \
    --token-name "kit-admin-$(date +%Y%m%d%H%M%S)" --scopes all --raw | tr -d '[:space:]')"
  mkdir -p "$STATE_DIR/secrets/human"
  ( umask 077; printf '%s\n' "$tok" > "$STATE_DIR/secrets/human/token" )
fi

docker compose run --rm tools bootstrap >/dev/null

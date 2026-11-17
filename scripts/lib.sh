# Sourced by every host script: moves to the kit's folder and loads its
# environment.
#
# The same checkout can be used on your Mac and inside the kit's own dev
# container, each with its own Docker engine. On the Mac the environment is
# .env; inside a container it is .env.devcontainer. Each names its own
# STATE_DIR for secrets/, control/ and backups/, so the two never share
# settings or credentials. KIT_ENV overrides the choice of file.

cd "$(dirname "$0")/.."
if [ -z "${KIT_ENV:-}" ]; then
  if [ -f /.dockerenv ]; then KIT_ENV=.env.devcontainer; else KIT_ENV=.env; fi
fi
[ -f "$KIT_ENV" ] || { echo "Create $KIT_ENV from .env.example first." >&2; exit 1; }
set -a
case "$KIT_ENV" in
  /*) . "$KIT_ENV" ;;
  *) . "./$KIT_ENV" ;;
esac
set +a
STATE_DIR="${STATE_DIR:-.}"
# docker compose reads the same file, for these scripts and anything they run.
COMPOSE_ENV_FILES="$KIT_ENV"
# The dispatcher's definition carries agents.conf's checksum, so that
# `docker compose up -d` recreates it when the file changes.
AGENTS_CONF_HASH="$(cksum < agents.conf | awk '{ print $1 }')"
export KIT_ENV STATE_DIR COMPOSE_ENV_FILES AGENTS_CONF_HASH

# Asks before a script changes the kit, naming which kit it would change:
#   confirm "This pauses every agent"
# KIT_YES=1 answers yes, for a script that runs others; without it, and with
# no terminal to ask on, the script stops having changed nothing.
confirm() {
  local kit
  case "$KIT_ENV" in
    .env.devcontainer|*/.env.devcontainer) kit="the dev container's kit" ;;
    .env|*/.env) kit="the kit on this Mac" ;;
    *) kit="the kit configured by $KIT_ENV" ;;
  esac
  [ "${KIT_YES:-}" = 1 ] && return 0
  [ -t 0 ] || { echo "$1, on $kit. Set KIT_YES=1 to go ahead without being asked." >&2; exit 1; }
  printf '%s, on %s (%s). Go ahead? [y/N] ' "$1" "$kit" "$KIT_ENV" >&2
  local answer
  read -r answer
  case "$answer" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Nothing changed." >&2; exit 1 ;;
  esac
}

# compose.yaml includes the agents' services, generated from agents.conf.
# Rewritten only when it changes, so its timestamp means something.
awk -f scripts/compose-agents.awk agents.conf docker/agent/compose.base.yaml agents.conf > compose.agents.yaml.new \
  || { echo "Couldn't generate compose.agents.yaml from agents.conf" >&2; rm -f compose.agents.yaml.new; exit 1; }
if cmp -s compose.agents.yaml.new compose.agents.yaml; then rm -f compose.agents.yaml.new; else mv compose.agents.yaml.new compose.agents.yaml; fi
# Every agent, in agents.conf's order.
AGENTS="$(awk '!/^[[:space:]]*(#|$)/ { print $1 }' agents.conf | tr '\n' ' ')"

# The admin tools run from their image, so rebuild it whenever the kit's
# copy of them changes; Docker's cache makes this quick when nothing has.
[ -n "${KIT_NO_BUILD:-}" ] || docker compose --profile tools build --quiet tools >/dev/null \
  || { echo "Couldn't build the tools image; see: docker compose --profile tools build tools" >&2; exit 1; }

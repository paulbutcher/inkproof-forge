# kit bootstrap: converge Forgejo on the organisation, accounts, tokens, teams
# and webhook the kit needs. Safe to re-run; it reports what it changed.
# Teams are given the repositories that exist, so run it again after
# migrating repositories in.

# Team permissions, per repository unit. Units not listed are none.
units() {
  local code="$1" issues="$2" pulls="$3"
  jq -nc --arg c "$code" --arg i "$issues" --arg p "$pulls" '{
    "repo.code": $c, "repo.issues": $i, "repo.pulls": $p,
    "repo.releases": "read", "repo.actions": "read", "repo.wiki": "read",
    "repo.projects": "none", "repo.packages": "none",
    "repo.ext_issues": "none", "repo.ext_wiki": "none"}'
}
U_OWN="$(units write write write)"
U_DEPS="$(units read write read)"
U_COORD_DEPS="$(units read write write)"
U_REVIEW="$(units read write write)"
U_READ="$(units read read read)"

SCOPES_WRITE='["write:repository","write:issue","read:user","read:organization"]'
SCOPES_ISSUES='["read:repository","write:issue","read:user","read:organization"]'

ensure_org() {
  if [ "$(api GET "/orgs/$ORG")" = 200 ]; then return; fi
  call 201 POST /orgs "$(jq -nc --arg o "$ORG" \
    '{username: $o, visibility: "private", repo_admin_change_team_access: false}')" >/dev/null
  changed "created organisation $ORG"
}

ensure_user() {
  local u="$1"
  if [ "$(api GET "/users/$u")" != 200 ]; then
    call 201 POST /admin/users "$(jq -nc --arg u "$u" --arg p "$(random_hex 24)" \
      '{username: $u, email: "\($u)@noreply.localhost", password: $p,
        must_change_password: false, visibility: "limited"}')" >/dev/null
    changed "created account $u"
  fi
  # Kit accounts own nothing: an account that could create a repository, or
  # fork one, would administer it.
  call 200 PATCH "/admin/users/$u" "$(jq -nc --arg u "$u" \
    '{login_name: $u, source_id: 0, max_repo_creation: 0, allow_create_organization: false}')" >/dev/null
}

ensure_token() {
  local u="$1" scopes="$2" f="/secrets/$1/token" pw tok
  if [ -s "$f" ] && [ "$(TOKEN="$(token_of "$u")" api GET /user)" = 200 ] \
     && [ "$(jq -r .login "$RESP")" = "$u" ]; then
    return
  fi
  # Tokens are created with the account's own password, so set a fresh one
  # that is used once and never stored.
  pw="$(random_hex 24)"
  call 200 PATCH "/admin/users/$u" "$(jq -nc --arg u "$u" --arg p "$pw" \
    '{login_name: $u, source_id: 0, password: $p, must_change_password: false}')" >/dev/null
  BASIC="$u:$pw" call 204,404 DELETE "/users/$u/tokens/kit" >/dev/null
  tok="$(BASIC="$u:$pw" call 201 POST "/users/$u/tokens" \
    "$(jq -nc --argjson s "$scopes" '{name: "kit", scopes: $s}')" | jq -r .sha1)"
  mkdir -p "/secrets/$u"
  ( umask 077; printf '%s\n' "$tok" > "$f" )
  changed "created a token for $u in secrets/$u/token"
  FRESH_TOKEN=1
}

team_id() {
  call 200 GET "/orgs/$ORG/teams?limit=50" | jq -r --arg n "$1" '.[] | select(.name == $n) | .id'
}

# ensure_team NAME UNITS MEMBER REPO...: the team exists with these unit
# permissions, MEMBER as its only member, and exactly those of REPO... that
# exist; REPO "*" alone means every repository in the organisation, present
# and future.
ensure_team() {
  local name="$1" units="$2" member="$3" id r current all=false
  shift 3
  [ "${1:-}" = "*" ] && all=true
  id="$(team_id "$name")"
  local body
  body="$(jq -nc --arg n "$name" --argjson u "$units" --argjson all "$all" \
    '{name: $n, description: "Kit-managed; edit agents.conf instead", permission: "read",
      includes_all_repositories: $all, can_create_org_repo: false, units_map: $u}')"
  if [ -z "$id" ]; then
    id="$(call 201 POST "/orgs/$ORG/teams" "$body" | jq -r .id)"
    changed "created team $name"
  else
    current="$(call 200 GET "/teams/$id" | jq -c --argjson u "$units" '.units_map | with_entries(select(.key | in($u)))')"
    [ "$(jq -r .includes_all_repositories "$RESP")" = "$all" ] || current='"stale"'
    if [ "$(jq -cS . <<<"$current")" != "$(jq -cS . <<<"$units")" ]; then
      call 200 PATCH "/teams/$id" "$body" >/dev/null
      changed "updated team $name's permissions"
    fi
  fi

  local members
  members="$(call 200 GET "/teams/$id/members" | jq -r '.[].login')"
  grep -qx "$member" <<<"$members" || { call 204 PUT "/teams/$id/members/$member" >/dev/null; changed "added $member to $name"; }
  for r in $members; do
    [ "$r" = "$member" ] || { call 204 DELETE "/teams/$id/members/$r" >/dev/null; changed "removed $r from $name"; }
  done

  $all && return 0
  local have want=""
  have="$(call 200 GET "/teams/$id/repos?limit=50" | jq -r '.[].name')"
  for r in "$@"; do
    [ "$(api GET "/repos/$ORG/$r")" = 200 ] || continue
    want+="$r"$'\n'
    grep -qx "$r" <<<"$have" || { call 204 PUT "/teams/$id/repos/$ORG/$r" >/dev/null; changed "gave $name $r"; }
  done
  for r in $have; do
    grep -qx "$r" <<<"$want" || { call 204 DELETE "/teams/$id/repos/$ORG/$r" >/dev/null; changed "took $r from $name"; }
  done
}

WEBHOOK_URL="http://dispatcher:8080/hook"
WEBHOOK_EVENTS='["create","delete","push","issues","issue_assign","issue_label","issue_milestone","issue_comment","pull_request","pull_request_assign","pull_request_label","pull_request_milestone","pull_request_comment","pull_request_review_approved","pull_request_review_rejected","pull_request_review_comment","pull_request_sync","pull_request_review_request","release","action_run_failure","action_run_success"]'

ensure_webhook() {
  local hooks id secret body
  secret="$(tr -d '[:space:]' < /secrets/dispatcher/webhook-secret)"
  hooks="$(call 200 GET "/orgs/$ORG/hooks")"
  id="$(jq -r --arg u "$WEBHOOK_URL" '.[] | select(.config.url == $u) | .id' <<<"$hooks" | head -1)"
  body="$(jq -nc --arg u "$WEBHOOK_URL" --arg s "$secret" --argjson e "$WEBHOOK_EVENTS" \
    '{type: "forgejo", active: true, events: $e, branch_filter: "*",
      config: {url: $u, content_type: "json", http_method: "post", secret: $s}}')"
  if [ -z "$id" ]; then
    call 201 POST "/orgs/$ORG/hooks" "$body" >/dev/null
    changed "created the organisation webhook to the dispatcher"
    return
  fi
  if [ "$(jq -c --arg u "$WEBHOOK_URL" '.[] | select(.config.url == $u) | [.active, (.events | sort)]' <<<"$hooks")" \
       != "$(jq -c '[true, sort]' <<<"$WEBHOOK_EVENTS")" ]; then
    call 200 PATCH "/orgs/$ORG/hooks/$id" "$body" >/dev/null
    changed "updated the organisation webhook"
  fi
}

# Every repository named in agents.conf, one per line.
all_repos() {
  grep -v '^[[:space:]]*\(#\|$\)' /etc/kit/agents.conf | awk '$3 != "-" { print $3 }' | sort -u
}

# expand READS OWN: the repositories an agent reads, one per line.
expand_reads() {
  case "$1" in
    -) ;;
    '*') all_repos | grep -vx -- "$2" || true ;;
    *) tr ',' '\n' <<<"$1" ;;
  esac
}

cmd_bootstrap() {
  TOKEN="$(token_of human)"
  log "Converging $FORGE_URL:"
  ensure_org

  local name kind own reads
  local accounts=(gatekeeper dispatcher ci-reader)
  while read -r name kind own reads; do
    accounts+=("$name")
  done < <(grep -v '^[[:space:]]*\(#\|$\)' /etc/kit/agents.conf)

  for name in "${accounts[@]}"; do
    ensure_user "$name"
  done

  while read -r name kind own reads; do
    local r_list
    mapfile -t r_list < <(expand_reads "$reads" "$own")
    case "$kind" in
      library)
        ensure_token "$name" "$SCOPES_WRITE"
        ensure_team "$name-own" "$U_OWN" "$name" "$own"
        ensure_team "$name-deps" "$U_DEPS" "$name" "${r_list[@]}" ;;
      coordinator)
        ensure_token "$name" "$SCOPES_WRITE"
        ensure_team "$name-own" "$U_OWN" "$name" "$own"
        ensure_team "$name-deps" "$U_COORD_DEPS" "$name" "${r_list[@]}" ;;
      pr-reviewer)
        ensure_token "$name" "$SCOPES_WRITE"
        ensure_team "$name-review" "$U_REVIEW" "$name" "${r_list[@]}" ;;
      milestone-reviewer)
        ensure_token "$name" "$SCOPES_ISSUES"
        local others=() r
        for r in "${r_list[@]}"; do [ "$r" = "$COORDINATION" ] || others+=("$r"); done
        ensure_team "$name-read" "$U_READ" "$name" "${others[@]}"
        ensure_team "$name-coord" "$U_DEPS" "$name" "$COORDINATION" ;;
      *) die "agents.conf: $name has unknown kind $kind" ;;
    esac
  done < <(grep -v '^[[:space:]]*\(#\|$\)' /etc/kit/agents.conf)

  ensure_token gatekeeper "$SCOPES_WRITE"
  # The gatekeeper approves pull requests at levels 2 and 3, and at level 3
  # tags and publishes releases and comments on the issues they close. Branch
  # protection keeps it out of main, and tag protection keeps its tags to
  # level-3 repositories.
  ensure_team gatekeeper-review "$(units write write write | jq -c '."repo.releases" = "write"')" gatekeeper "*"
  ensure_token dispatcher "$SCOPES_ISSUES"
  # The dispatcher comments on issues to notify the human, on any repository.
  ensure_team dispatcher-notify "$(units read write read)" dispatcher "*"
  # CI reads this project's repositories to fetch dependencies; the job's own
  # token reads only the repository it runs for.
  FRESH_TOKEN=0
  ensure_token ci-reader '["read:repository","read:user"]'
  ensure_team ci-read "$(units read none none)" ci-reader "*"
  ensure_ci_secret "$FRESH_TOKEN"
  ensure_ci_variable DEPS_GITHUB_OWNERS "$GH_OWNERS"
  ensure_webhook
  # The protocol's labels, on every repository that exists (setup-repo also
  # makes them, but upstream issues go on inkproof-coordination whatever its
  # level).
  local r
  for r in $(all_repos); do
    [ "$(api GET "/repos/$ORG/$r")" = 200 ] && ensure_labels "$r"
  done
  log "Bootstrap made $CHANGES change(s)."
  echo "$CHANGES"
}

# CI fetches dependencies with ci-reader's token, from the organisation's
# DEPS_READ_TOKEN Actions secret. Forgejo never shows a secret's value, so it
# is set whenever the token is new or the secret is missing.
ensure_ci_secret() {
  if [ "$1" = 1 ] || ! call 200 GET "/orgs/$ORG/actions/secrets" | jq -e '.[] | select(.name == "DEPS_READ_TOKEN")' >/dev/null; then
    call 201,204 PUT "/orgs/$ORG/actions/secrets/DEPS_READ_TOKEN" \
      "$(jq -nc --arg d "$(token_of ci-reader)" '{data: $d}')" >/dev/null
    changed "stored ci-reader's token as the organisation's DEPS_READ_TOKEN Actions secret"
  fi
}

# ensure_ci_variable NAME VALUE: an organisation Actions variable, which
# workflows read as vars.NAME.
ensure_ci_variable() {
  local body
  body="$(jq -nc --arg v "$2" '{value: $v}')"
  case "$(api GET "/orgs/$ORG/actions/variables/$1")" in
    200) [ "$(jq -r .data "$RESP")" = "$2" ] && return
         call 201,204 PUT "/orgs/$ORG/actions/variables/$1" "$body" >/dev/null ;;
    404) call 201,204 POST "/orgs/$ORG/actions/variables/$1" "$body" >/dev/null ;;
    *) die "reading the $1 Actions variable: $(head -c 300 "$RESP")" ;;
  esac
  changed "set the organisation's $1 Actions variable to \"$2\""
}

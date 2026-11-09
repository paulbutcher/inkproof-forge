# kit migrate, kit mirrors: bring the repositories in agents.conf over from
# GitHub, then push-mirror them back. The kit never calls GitHub itself:
# Forgejo does, through forge-proxy, with tokens of the human's own.

GITHUB_IMPORT_TOKEN=/secrets/github/import-token
GITHUB_MIRROR_TOKEN=/secrets/github/mirror-token

# kit migrate: import every repository in agents.conf that Forgejo doesn't
# have yet, with history, issues, labels, milestones, pull requests and
# releases. Without an import token the import is anonymous, which works for
# public repositories within GitHub's lower rate limit.
cmd_migrate() {
  TOKEN="$(token_of human)"
  local auth="" r s
  [ -s "$GITHUB_IMPORT_TOKEN" ] && auth="$(tr -d '[:space:]' < "$GITHUB_IMPORT_TOKEN")"
  [ -n "$auth" ] || log "No secrets/github/import-token: importing anonymously (public repositories only)."
  for r in $(all_repos); do
    if [ "$(api GET "/repos/$ORG/$r")" = 200 ]; then
      log "$ORG/$r is already in Forgejo; skipping."
      continue
    fi
    log "Importing $GH_OWNER/$r from GitHub into $ORG/$r..."
    s="$(curl -sS -m 7200 -X POST -H "Authorization: token $TOKEN" -H 'Content-Type: application/json' \
          -o "$RESP" -w '%{http_code}' "$FORGE_URL/api/v1/repos/migrate" \
          --data-binary "$(jq -nc --arg g "$GH_OWNER" --arg o "$ORG" --arg r "$r" --arg t "$auth" '{
            clone_addr: "https://github.com/\($g)/\($r).git", service: "github",
            repo_owner: $o, repo_name: $r, private: true, mirror: false,
            issues: true, labels: true, milestones: true, pull_requests: true,
            releases: true, wiki: false, lfs: false}
            + (if $t == "" then {} else {auth_token: $t} end)')")"
    if [ "$s" != 201 ]; then
      log "kit: importing $r failed ($s):"; head -c 2000 "$RESP" >&2; log ""
      return 1
    fi
    changed "imported $ORG/$r"
  done
}

# kit mirrors: give every repository in agents.conf a push mirror to its GitHub
# namesake, synced on every push, carrying MIRROR_BRANCHES (a Forgejo branch
# filter; empty for every branch) and every tag. With --rotate, replaces each
# mirror's credential with the current secrets/github/mirror-token.
cmd_mirrors() {
  TOKEN="$(token_of human)"
  [ -s "$GITHUB_MIRROR_TOKEN" ] || die "put a GitHub token with Contents read and write on the repositories in secrets/github/mirror-token"
  local rotate="${1:-}" pw r url existing name
  pw="$(tr -d '[:space:]' < "$GITHUB_MIRROR_TOKEN")"
  for r in $(all_repos); do
    [ "$(api GET "/repos/$ORG/$r")" = 200 ] || { log "$ORG/$r isn't in Forgejo; skipping."; continue; }
    url="https://github.com/$GH_OWNER/$r.git"
    existing="$(call 200 GET "/repos/$ORG/$r/push_mirrors" \
      | jq -c --arg u "$url" '[.[] | select(.remote_address == $u)][0] // empty')"
    if [ -n "$existing" ]; then
      name="$(jq -r .remote_name <<<"$existing")"
      if [ "$rotate" != --rotate ] && [ "$(jq -r '.branch_filter // ""' <<<"$existing")" = "${MIRROR_BRANCHES-main}" ]; then
        continue
      fi
      call 204 DELETE "/repos/$ORG/$r/push_mirrors/$name" >/dev/null
    fi
    call 200,201 POST "/repos/$ORG/$r/push_mirrors" "$(jq -nc --arg u "$url" --arg h "$HUMAN_USER" \
      --arg p "$pw" --arg b "${MIRROR_BRANCHES-main}" '{
        remote_address: $u, remote_username: $h, remote_password: $p,
        sync_on_commit: true, interval: "24h", branch_filter: $b, use_ssh: false}')" >/dev/null
    changed "push mirror $ORG/$r -> $url (branches: ${MIRROR_BRANCHES-main}; tags: all)"
    call 200 POST "/repos/$ORG/$r/push_mirrors-sync" >/dev/null
  done
}

# kit mirror-status: each repository's push mirrors, with their last sync and error.
cmd_mirror_status() {
  TOKEN="$(token_of human)"
  local r
  for r in $(all_repos); do
    [ "$(api GET "/repos/$ORG/$r")" = 200 ] || continue
    call 200 GET "/repos/$ORG/$r/push_mirrors" \
      | jq -r --arg r "$r" '.[] | "\($r)\t\(.remote_address)\tbranches=\(.branch_filter // "")\tlast=\(.last_update)\terror=\(.last_error // "")"'
  done
}

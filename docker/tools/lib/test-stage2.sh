# kit test-stage2: the stage 2 checks that run against Forgejo's API.
# scripts/test-stage2.sh compares refs with GitHub and reads forge-proxy's log.
#
# Prints PASS/FAIL/INFO lines, plus:
#   PROBE_WINDOW <start> <end>   epoch seconds around the push-mirror probe
#   MIRROR_TEST <repo> <main sha> <tag> <unmirrored branch>   when a scratch
#                                mirror round trip ran, for the host to check

S2_FAILS=0
s2_pass() { echo "PASS: $*"; }
s2_fail() { echo "FAIL: $*"; S2_FAILS=$((S2_FAILS + 1)); }
s2_info() { echo "INFO: $*"; }

# Status of the Actions run for a commit in a repository, once finished.
s2_wait_run() {
  local repo="$1" sha="$2" deadline=$(( $(date +%s) + ${3:-1800} )) s
  while [ "$(date +%s)" -lt "$deadline" ]; do
    s="$(call 200 GET "/repos/$ORG/$repo/actions/runs?limit=50" \
      | jq -r --arg sha "$sha" '(.workflow_runs // .)[]? | select((.commit_sha // .head_sha) == $sha) | .status' | head -1)"
    case "$s" in success|failure|cancelled|skipped|blocked) echo "$s"; return ;; esac
    sleep 10
  done
  echo timeout
}

# Pushes a scratch repository's main to a push mirror and waits for a sync.
s2_mirror_sync() {
  local repo="$1" before after i
  before="$(call 200 GET "/repos/$ORG/$repo/push_mirrors" | jq -r '.[0].last_update // ""')"
  # Forgejo answers 500 when the push fails; the mirror records why.
  api POST "/repos/$ORG/$repo/push_mirrors-sync" >/dev/null
  for i in $(seq 60); do
    after="$(call 200 GET "/repos/$ORG/$repo/push_mirrors" | jq -r '.[0].last_update // ""')"
    [ -n "$after" ] && [ "$after" != "$before" ] && break
    sleep 2
  done
  call 200 GET "/repos/$ORG/$repo/push_mirrors" | jq -r '.[0].last_error // ""'
}

s2_scratch_repo() {
  api DELETE "/repos/$ORG/$1" >/dev/null
  call 201 POST "/orgs/$ORG/repos" "$(jq -nc --arg n "$1" '{name: $n, private: true, default_branch: "main"}')" >/dev/null
}

cmd_test_stage2() {
  TOKEN="$(token_of human)"
  local mirror_test="${1:-}" r n

  # --- Imported repositories -------------------------------------------------
  for r in $(all_repos); do
    if [ "$(api GET "/repos/$ORG/$r")" != 200 ]; then s2_fail "$ORG/$r is not in Forgejo"; continue; fi
    jq -e '.private and .default_branch == "main"' "$RESP" >/dev/null \
      && s2_pass "$ORG/$r is in Forgejo, private, default branch main" \
      || s2_fail "$ORG/$r: $(jq -c '{private, default_branch}' "$RESP")"
    local issues pulls labels releases milestones branches
    issues="$(call 200 GET "/repos/$ORG/$r/issues?state=all&type=issues&limit=50" | jq length)"
    pulls="$(call 200 GET "/repos/$ORG/$r/pulls?state=all&limit=50" | jq length)"
    labels="$(call 200 GET "/repos/$ORG/$r/labels?limit=50" | jq length)"
    releases="$(call 200 GET "/repos/$ORG/$r/releases?limit=50" | jq length)"
    milestones="$(call 200 GET "/repos/$ORG/$r/milestones?state=all&limit=50" | jq length)"
    branches="$(call 200 GET "/repos/$ORG/$r/branches?limit=50" | jq length)"
    s2_info "$r: $issues issues, $pulls pull requests, $labels labels, $releases releases, $milestones milestones, $branches branches (first page of 50 each)"

    # Migrated content is attributed to its GitHub author, never a Forgejo account.
    local all_items unattributed
    all_items="$( { call 200 GET "/repos/$ORG/$r/issues?state=all&type=all&limit=50"; \
                    call 200 GET "/repos/$ORG/$r/issues/comments?limit=50"; } | jq -s 'add')"
    n="$(jq length <<<"$all_items")"
    unattributed="$(jq -r '[.[] | select((.original_author // "") == "")] | map("\(.user.login)") | unique | join(", ")' <<<"$all_items")"
    if [ "$n" = 0 ]; then
      s2_info "$r has no issues, pull requests or comments to check for attribution"
    elif [ -z "$unattributed" ]; then
      s2_pass "$r: all $n migrated issues, pull requests and comments carry an original author ($(jq -r '[.[].original_author] | unique | join(", ")' <<<"$all_items")); Forgejo shows them as $(jq -r '[.[].user.login] | unique | join(", ")' <<<"$all_items")"
    else
      s2_fail "$r: some migrated content has no original author, posted as: $unattributed"
    fi
  done

  # --- Push mirrors of the imported repositories -----------------------------
  local status
  status="$(cmd_mirror_status)"
  if [ -z "$status" ]; then
    s2_info "no push mirrors yet (scripts/mirrors.sh)"
  else
    while IFS=$'\t' read -r r url branches last err; do
      if [ "$err" = "error=" ]; then s2_pass "mirror $r -> $url ($branches, $last)"; else s2_fail "mirror $r -> $url: $err"; fi
    done <<<"$status"
  fi

  # --- Push mirror round trip -------------------------------------------------
  local work probe=stage2-mirror-probe
  work="$(mktemp -d)"
  ( cd "$work" && git init -q -b main && git_as commit -q --allow-empty -m "Stage 2 mirror test" \
    && git_as tag v0.0.1 && git_as checkout -q -b not-mirrored && git_as commit -q --allow-empty -m "Not for GitHub" \
    && git_as checkout -q main )

  if [ -n "$mirror_test" ]; then
    # A real round trip, to a scratch GitHub repository the human created.
    [ -s "$GITHUB_MIRROR_TOKEN" ] || die "a mirror test needs secrets/github/mirror-token"
    s2_scratch_repo "$mirror_test"
    call 200,201 POST "/repos/$ORG/$mirror_test/push_mirrors" "$(jq -nc --arg u "https://github.com/$GH_OWNER/$mirror_test.git" \
      --arg h "$HUMAN_USER" --arg p "$(tr -d '[:space:]' < "$GITHUB_MIRROR_TOKEN")" \
      '{remote_address: $u, remote_username: $h, remote_password: $p, sync_on_commit: true, interval: "24h", branch_filter: "main", use_ssh: false}')" >/dev/null
    ( cd "$work" && git_as push -q "$FORGE_URL/$ORG/$mirror_test.git" main not-mirrored v0.0.1 )
    local err
    err="$(s2_mirror_sync "$mirror_test")"
    [ -z "$err" ] && s2_pass "pushed to $mirror_test and its mirror synced" || s2_fail "mirror sync of $mirror_test: $err"
    echo "MIRROR_TEST $mirror_test $(git -C "$work" rev-parse main) v0.0.1 not-mirrored"
    [ "${KEEP:-}" = 1 ] || api DELETE "/repos/$ORG/$mirror_test" >/dev/null
  fi

  # A mirror to a GitHub repository that doesn't exist, with no valid
  # credential: shows the push goes through forge-proxy, and changes nothing.
  s2_scratch_repo "$probe"
  call 200,201 POST "/repos/$ORG/$probe/push_mirrors" "$(jq -nc --arg u "https://github.com/$GH_OWNER/inkproof-mirror-probe-does-not-exist.git" \
    '{remote_address: $u, remote_username: "nobody", remote_password: "not-a-token", sync_on_commit: false, interval: "24h", branch_filter: "main", use_ssh: false}')" >/dev/null
  ( cd "$work" && git_as push -q "$FORGE_URL/$ORG/$probe.git" main )
  local t0 err
  t0="$(date +%s)"
  err="$(s2_mirror_sync "$probe")"
  echo "PROBE_WINDOW $t0 $(date +%s)"
  # Refused by GitHub, not by a failure to get there.
  case "$err" in
    *"Could not resolve host"*|*"Failed to connect"*|*"proxy"*|"")
      s2_fail "the probe mirror didn't reach GitHub: ${err:-no error recorded}" ;;
    *) s2_pass "a push mirror reached GitHub and was refused there without a valid token" ;;
  esac
  [ "${KEEP:-}" = 1 ] || api DELETE "/repos/$ORG/$probe" >/dev/null

  # --- A consumer resolves its dependencies in CI ------------------------------
  local consumer=stage2-consumer sha s
  s2_scratch_repo "$consumer"
  cp -r /etc/kit/tests/fixtures/stage2-consumer/. "$work/consumer"
  sed -i "s/@GH_OWNER@/$GH_OWNER/g" "$work/consumer/lakefile.lean"
  ( cd "$work/consumer" && git init -q -b main && git add -A && git_as commit -qm "Stage 2 consumer" \
    && git_as push -q "$FORGE_URL/$ORG/$consumer.git" main )
  sha="$(git -C "$work/consumer" rev-parse HEAD)"
  s="$(s2_wait_run "$consumer" "$sha")"
  if [ "$s" = success ]; then
    s2_pass "in CI, a consumer fetched inkproof-core and inkproof-scale from Forgejo, under current and former GitHub URLs, and plausible from GitHub; its manifest kept the GitHub URLs"
  else
    s2_fail "the consumer's CI run ended: $s (see $ORG/$consumer's Actions; rerun with KEEP=1 to keep it)"
  fi
  [ "${KEEP:-}" = 1 ] || api DELETE "/repos/$ORG/$consumer" >/dev/null

  echo "RESULT: $S2_FAILS failure(s)"
  [ "$S2_FAILS" = 0 ]
}

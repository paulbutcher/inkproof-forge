# kit test-stage1: the stage 1 checks that run against Forgejo's API.
# scripts/test-stage1.sh adds the network checks and reads the dispatcher.
# Uses a scratch repository, stage1-ci, deleted at the end unless KEEP=1.

FAILS=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
info() { echo "INFO: $*"; }

REPO=stage1-ci
PROBE=stage1-probe

# The Actions run for a commit: prints "status conclusion" once finished.
run_for() {
  call 200 GET "/repos/$ORG/$REPO/actions/runs?limit=50" \
    | jq -r --arg sha "$1" '(.workflow_runs // .)[]? | select((.commit_sha // .head_sha) == $sha) | .status' | head -1
}

wait_run() {
  local sha="$1" deadline=$(( $(date +%s) + ${2:-1800} )) s
  while [ "$(date +%s)" -lt "$deadline" ]; do
    s="$(run_for "$sha")"
    case "$s" in
      success|failure|cancelled|skipped|blocked) echo "$s"; return ;;
    esac
    sleep 10
  done
  echo timeout
}

cleanup() {
  TOKEN="$(token_of human)"
  [ "${KEEP:-}" = 1 ] && { info "kept $ORG/$REPO"; return; }
  api DELETE "/repos/$ORG/$REPO" >/dev/null
  local id
  id="$(team_id "$PROBE")"
  [ -n "$id" ] && api DELETE "/teams/$id" >/dev/null
  api DELETE "/admin/users/$PROBE?purge=true" >/dev/null
}

cmd_test_stage1() {
  TOKEN="$(token_of human)"
  trap cleanup EXIT
  local work sha_ok sha_bad s pr

  # Registration
  s="$(curl -sS -o "$RESP" -w '%{http_code}' "$FORGE_URL/user/sign_up")"
  if [ "$s" = 404 ] || [ "$s" = 403 ] || grep -qi 'registration is disabled' "$RESP"; then
    pass "registration is disabled ($s)"
  else
    fail "registration page answered $s"
  fi

  # A scratch repository with the CI template and a tiny Lean package.
  api DELETE "/repos/$ORG/$REPO" >/dev/null
  call 201 POST "/orgs/$ORG/repos" "$(jq -nc --arg n "$REPO" '{name: $n, private: true, default_branch: "main"}')" >/dev/null
  work="$(mktemp -d)"
  cp -r /etc/kit/templates/repo/. "$work/"
  cp /etc/kit/tests/fixtures/stage1/{lakefile.toml,lean-toolchain,Stage1.lean,Tests.lean} "$work/"
  (
    cd "$work"
    git init -q -b main && git add -A
    git_as commit -qm "Stage 1 CI fixture"
    git_as push -q "$FORGE_URL/$ORG/$REPO.git" main
  ) || { fail "couldn't push the fixture"; return; }
  sha_ok="$(git -C "$work" rev-parse HEAD)"

  info "waiting for CI on main ($sha_ok); the first run downloads a Lean toolchain"
  local t0 t1
  t0="$(date +%s)"
  s="$(wait_run "$sha_ok")"
  t1="$(date +%s)"
  [ "$s" = success ] && pass "CI built main in $((t1 - t0))s" || fail "CI on main ended: $s"

  # A pull request with a sorry must fail.
  (
    cd "$work"
    git checkout -q -b broken
    cp /etc/kit/tests/fixtures/stage1/Stage1.broken.lean Stage1.lean
    git_as commit -qam "Use sorry"
    git_as push -q "$FORGE_URL/$ORG/$REPO.git" broken
  )
  sha_bad="$(git -C "$work" rev-parse HEAD)"
  pr="$(call 201 POST "/repos/$ORG/$REPO/pulls" '{"head":"broken","base":"main","title":"Stage 1: sorry"}' | jq -r .number)"
  t0="$(date +%s)"
  s="$(wait_run "$sha_bad")"
  t1="$(date +%s)"
  [ "$s" = failure ] && pass "CI failed the pull request with a sorry (#$pr), in $((t1 - t0))s with the cached toolchain" \
                     || fail "CI on the sorry pull request ended: $s"
  echo "SHAS: $sha_ok $sha_bad"

  # Agents' team permissions: core gets this repository as it would its own.
  local core_team
  core_team="$(team_id core-own)"
  call 204 PUT "/teams/$core_team/repos/$ORG/$REPO" >/dev/null
  call 201 POST "/repos/$ORG/$REPO/tag_protections" \
    "$(jq -nc --arg h "$HUMAN_USER" '{name_pattern: "*", whitelist_usernames: [$h]}')" >/dev/null

  s="$(TOKEN="$(token_of core)" api POST "/repos/$ORG/$REPO/releases" '{"tag_name":"v9.9.9","target_commitish":"main","name":"v9.9.9"}')"
  [ "$s" = 403 ] && pass "core can't create a release (403)" || fail "core creating a release got $s: $(head -c 300 "$RESP")"

  if (cd "$work" && TOKEN="$(token_of core)" git_as push -q "$FORGE_URL/$ORG/$REPO.git" "HEAD:refs/heads/agent/core/probe" 2>/dev/null); then
    pass "core can push a branch"
  else
    fail "core can't push a branch"
  fi
  if (cd "$work" && git tag v9.9.9 && TOKEN="$(token_of core)" git_as push -q "$FORGE_URL/$ORG/$REPO.git" v9.9.9 2>/dev/null); then
    fail "core pushed a tag"
  else
    pass "core can't push a tag"
  fi

  # Tag protection also covers a release that creates its tag. The probe
  # account may write releases; only tag protection stands in its way.
  call 201 POST /admin/users "$(jq -nc --arg u "$PROBE" --arg p "$(random_hex 24)" \
    '{username: $u, email: "\($u)@noreply.localhost", password: $p, must_change_password: false}')" >/dev/null
  local probe_team probe_pw probe_tok
  probe_team="$(call 201 POST "/orgs/$ORG/teams" "$(jq -nc --arg n "$PROBE" \
    '{name: $n, permission: "write", includes_all_repositories: false,
      units_map: {"repo.code": "write", "repo.releases": "write"}}')" | jq -r .id)"
  call 204 PUT "/teams/$probe_team/members/$PROBE" >/dev/null
  call 204 PUT "/teams/$probe_team/repos/$ORG/$REPO" >/dev/null
  probe_pw="$(random_hex 24)"
  call 200 PATCH "/admin/users/$PROBE" "$(jq -nc --arg u "$PROBE" --arg p "$probe_pw" '{login_name: $u, source_id: 0, password: $p}')" >/dev/null
  probe_tok="$(BASIC="$PROBE:$probe_pw" call 201 POST "/users/$PROBE/tokens" '{"name":"probe","scopes":["write:repository"]}' | jq -r .sha1)"

  s="$(TOKEN="$probe_tok" api POST "/repos/$ORG/$REPO/releases" '{"tag_name":"v9.9.8","target_commitish":"main","name":"v9.9.8"}')"
  if [ "$s" = 201 ]; then fail "a release created a protected tag"; else pass "tag protection refused a release creating its tag ($s)"; fi
  local tp
  tp="$(call 200 GET "/repos/$ORG/$REPO/tag_protections" | jq -r '.[0].id')"
  call 204 DELETE "/repos/$ORG/$REPO/tag_protections/$tp" >/dev/null
  s="$(TOKEN="$probe_tok" api POST "/repos/$ORG/$REPO/releases" '{"tag_name":"v9.9.7","target_commitish":"main","name":"v9.9.7"}')"
  [ "$s" = 201 ] && pass "control: without tag protection the same release is created" || fail "control release got $s: $(head -c 300 "$RESP")"

  echo "RESULT: $FAILS failure(s)"
  [ "$FAILS" = 0 ]
}

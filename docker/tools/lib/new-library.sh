# kit new-library REPO NAME: create a library's repository in Forgejo and
# commit its seed to main, as the human: a Lake package for Inkproof.NAME, with
# a test package, that builds and passes CI. REPO must be a library's own
# repository in agents.conf.
#
# The seed is templates/library/ with REPO, NAME and the source library's
# copyright header filled in, and from the source library's main (SEED_FROM,
# default inkproof-core), the files every library shares: CLAUDE.md, LICENSE,
# .gitignore and lean-toolchain, and the Lake manifests' format.
#
# Safe to re-run: a repository already seeded is left alone, and an empty one
# (from a run that stopped part way) is seeded. One with other content is
# refused, so the seed never lands on real work.

SEED_TEMPLATES=/etc/kit/templates/library

cmd_new_library() {
  local repo="${1:-}" name="${2:-}" src lib header ops
  [ -n "$repo" ] && [ -n "$name" ] || die "usage: new-library <repository> <Name>, e.g. new-library inkproof-float Float"
  [[ "$name" =~ ^[A-Z][A-Za-z0-9]*$ ]] || die "the name is a Lean namespace under Inkproof, such as Float"
  grep -v '^[[:space:]]*\(#\|$\)' /etc/kit/agents.conf | awk -v r="$repo" '$2 == "library" && $3 == r { f = 1 } END { exit !f }' \
    || die "$repo isn't a library's own repository in agents.conf; add its agent there first"
  TOKEN="$(token_of human)"
  src="${SEED_FROM:-${REPO_PREFIX:-inkproof-}core}"
  lib="Inkproof$name"

  if [ "$(api GET "/repos/$ORG/$repo")" = 200 ]; then
    if [ "$(jq -r .empty "$RESP")" != true ]; then
      if [ "$(api GET "/repos/$ORG/$repo/contents/lakefile.lean?ref=main")" = 200 ]; then
        log "$ORG/$repo is already seeded; nothing to do."
        return 0
      fi
      die "$ORG/$repo already has content but no lakefile.lean on main; not seeding over it"
    fi
    log "$ORG/$repo exists and is empty; seeding it."
  else
    call 201 POST "/orgs/$ORG/repos" "$(jq -nc --arg n "$repo" '{name: $n, private: true, default_branch: "main"}')" >/dev/null
    changed "created $ORG/$repo"
  fi

  header="$(src_file "$src" lakefile.lean)"
  header="$(awk 'NR == 1 && !/^\/-/ { exit } { print } /^-\/$/ { exit }' <<<"$header")"
  [ -n "$header" ] || die "$src's lakefile.lean doesn't start with a /- ... -/ copyright header"

  ops='[]'
  add_op() { ops="$(jq -c --arg p "$1" --arg c "$(printf '%s\n' "$2" | base64 -w0)" '. + [{operation: "create", path: $p, content: $c}]' <<<"$ops")"; }
  local f path text
  while IFS= read -r f; do
    path="${f#"$SEED_TEMPLATES"/}"
    path="${path//NAME/$name}"
    text="$(cat "$f")"
    text="${text//\{\{HEADER\}\}/$header}"
    text="${text//\{\{REPO\}\}/$repo}"
    text="${text//\{\{NAME\}\}/$name}"
    text="${text//\{\{LIB\}\}/$lib}"
    add_op "$path" "$text"
  done < <(find "$SEED_TEMPLATES" -type f | sort)
  for f in CLAUDE.md LICENSE .gitignore lean-toolchain; do
    text="$(src_file "$src" "$f")"
    add_op "$f" "$text"
  done
  add_op test/lean-toolchain "$text"
  # The manifests in the source's format: no dependencies at the root, and the
  # library itself, by path, in the test package.
  text="$(src_file "$src" lake-manifest.json)"
  text="$(jq --arg n "«$repo»" '.packages = [] | .name = $n' <<<"$text")"
  add_op lake-manifest.json "$text"
  text="$(src_file "$src" test/lake-manifest.json)"
  text="$(jq --arg n "«$repo»" --arg t "«$repo-test»" '
        ([.packages[] | select(.type == "path" and .dir == "..")][0]
          // error("no path dependency on .. in the source test manifest")) as $self
        | .packages = [$self | .name = $n] | .name = $t' <<<"$text")"
  add_op test/lake-manifest.json "$text"

  call 201 POST "/repos/$ORG/$repo/contents" "$(jq -nc --argjson f "$ops" --arg s "$src" --arg n "$name" '
    {branch: "main", new_branch: "main", files: $f,
     message: "Seed Inkproof.\($n) (scripts/new-library.sh, shared files from \($s))"}')" >/dev/null
  changed "$repo: committed the seed to main ($(jq length <<<"$ops") files)"
}

# src_file REPO PATH: a file's contents on REPO's main.
src_file() {
  [ "$(api GET "/repos/$ORG/$1/raw/$2?ref=main")" = 200 ] || die "can't read $2 on $1's main"
  cat "$RESP"
}

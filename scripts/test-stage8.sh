#!/usr/bin/env bash
# Stage 8 acceptance: backups.
#   - scripts/backup.sh makes a backup, and keeps only the newest
#     BACKUP_KEEP: three backups with BACKUP_KEEP=2 leave the newest two;
#   - scripts/restore-test.sh restores the newest into a fresh volume and
#     finds it matches what Forgejo held;
#   - on the Mac, the daily backup job is installed (scripts/backup-install.sh).
# It backs up into a temporary folder beside your backups and removes it.
# About 5 minutes; no Claude runs.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }

test_dir="$STATE_DIR/backups/stage8-test"
rm -rf "$test_dir"
trap 'rm -rf "$test_dir"' EXIT

echo "== Backups and pruning"
made=()
for i in 1 2 3; do
  if out="$(BACKUP_DIR="$test_dir" BACKUP_KEEP=2 "$(dirname "$0")/backup.sh" 2>&1)"; then
    made+=("$(sed -n 's|^Backed up to .*/\([0-9TZ]*\) .*|\1|p' <<<"$out")")
  else
    fail "backup $i: $out"
  fi
  sleep 1
done
left="$(ls -1 "$test_dir" | grep -E '^[0-9]+T[0-9]+Z$' | sort | tr '\n' ' ')"
[ "$left" = "${made[1]} ${made[2]} " ] && pass "three backups with BACKUP_KEEP=2 left the newest two ($left)" \
  || fail "left: $left; made: ${made[*]}"
newest="$test_dir/${made[2]}"
for f in forgejo.tar.gz ledger.db run-archive.tar.gz before.json after.json; do
  [ -s "$newest/$f" ] || fail "the backup has no $f"
done
[ "$(stat -c %a "$newest/forgejo.tar.gz" 2>/dev/null || stat -f %Lp "$newest/forgejo.tar.gz")" = 600 ] \
  && pass "the backup's files are readable by you only" || fail "the backup's files are readable by others"

echo "== The newest restores"
if out="$(BACKUP_DIR="$test_dir" "$(dirname "$0")/restore-test.sh" 2>&1)"; then
  pass "$(grep '^Compared' <<<"$out")"
else
  fail "restore test: $out"
fi

echo "== The daily job"
if [ "$(uname)" = Darwin ]; then
  launchctl print "gui/$(id -u)/com.inkproof-forge.backup" >/dev/null 2>&1 \
    && pass "the daily backup job is installed" || fail "the daily backup job isn't installed: scripts/backup-install.sh"
else
  echo "INFO: not on a Mac: the launchd job is checked there"
fi

echo
[ "$fails" = 0 ] && echo "Stage 8: all checks passed." || { echo "Stage 8: $fails check(s) failed."; exit 1; }

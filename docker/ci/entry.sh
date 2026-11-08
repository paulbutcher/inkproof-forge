# Registers the runner on first start, from the shared secret that
# scripts/up.sh also gives Forgejo, then runs it.
set -eu
if [ ! -s /data/.runner ]; then
  forgejo-runner create-runner-file --instance http://forgejo:3000 \
    --secret "$(cat /run/ci/runner-secret)" --name ci
fi
exec forgejo-runner daemon --config /etc/runner/config.yml

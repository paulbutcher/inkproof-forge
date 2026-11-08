# Inkproof forge

A local forge for the Inkproof agents: Forgejo is canonical, GitHub is a push mirror that nothing polls, and each agent is a Claude Code agent with its own Forgejo account. [requirements.md](requirements.md) sets out what the kit does and how we'll know it works; this README covers running it.

The kit is built in stages. Stage 1 is in place: Forgejo, CI, the egress proxies and the bootstrap. Later stages add the GitHub migration and mirrors, the agents, the dispatcher's work queues, reviews, autonomy levels 2 and 3, and backups.

## Layout

```
compose.yaml            Forgejo, port forwarder, proxies, CI runner and engine, dispatcher
.env.example            organisation, your Forgejo username and email, host UID/GID, port
agents.conf             every agent: kind, own repository, repositories it reads
docker/proxy/           squid config and one allowlist each for agents, CI and Forgejo
docker/ci/              runner config and entry point; image/ is the image CI jobs run in
docker/dispatcher/      receives Forgejo's webhooks
docker/tools/           admin tools (bootstrap, tests) run in a container, not on the host
docker/ports/           forwards 127.0.0.1:FORGE_PORT to Forgejo
templates/repo/         copied into every repository: CI workflow, pin check
scripts/                up, down, bootstrap, test-stage1
tests/fixtures/         a tiny Lean package for the CI test
secrets/                tokens and secrets, written by the scripts (never committed)
```

## Setup

You need Docker Desktop. The scripts are bash 3.2-compatible and run from the kit's folder; run them with `bash` if a copy lost their executable bits.

1. `cp .env.example .env` and edit it: your Forgejo username (use your GitHub username, so `CODEOWNERS` entries carry over), your email, and your UID and GID (`id -u`, `id -g`).
2. `bash scripts/up.sh` builds the images, generates the CI runner's and webhook's secrets, starts everything, and loads the CI image into CI's own Docker engine.
3. `bash scripts/bootstrap.sh` asks you to choose a password for your Forgejo admin account, then creates an admin token for the kit's scripts (`secrets/human/token`), the organisation, every account in `agents.conf` plus `gatekeeper` and `dispatcher`, their tokens and teams, and the webhook to the dispatcher. It reports what it changed; re-running it changes nothing unless something drifted.
4. Sign in at <http://localhost:3000/> as yourself.
5. Optionally, `bash scripts/test-stage1.sh` checks the networks, CI, permissions and webhooks. It takes a few minutes the first time, while CI downloads a Lean toolchain.

`bash scripts/down.sh` stops everything and keeps the volumes.

### Bringing the repositories over from GitHub

Forgejo imports each repository in `agents.conf` once, with its history, issues, labels, milestones, pull requests and releases, then pushes `main` and every tag back to GitHub on each commit. Both use GitHub tokens of your own; nothing uses a machine user, and content the suspended account wrote is hidden on GitHub, so it doesn't import.

1. On GitHub, create a fine-grained token with resource owner `tentenths`, the four `inkproof-` repositories, and read-only Contents, Issues, Pull requests and Metadata. Save it as `secrets/github/import-token` (`chmod 600`).
2. Create a second fine-grained token on the same repositories with Contents read and write and nothing else. Save it as `secrets/github/mirror-token` (`chmod 600`).
3. `bash scripts/migrate.sh` imports whatever Forgejo doesn't have yet, then gives the agents' teams their repositories. Forgejo's proxy allows GitHub's API only while it runs. You can delete the import token afterwards.
4. Decide what the mirrors carry. Forgejo mirrors exactly: with `MIRROR_BRANCHES=main`, the default, the first sync deletes every other branch on GitHub (the old `agent/*` branches; tags, `main` and closed pull requests' diffs are untouched). With `MIRROR_BRANCHES=` (empty), every Forgejo branch is mirrored, including agents' future work branches.
5. `bash scripts/mirrors.sh` adds the push mirrors and shows each one's last sync and error. To rotate the mirror token, replace the file and run `bash scripts/mirrors.sh --rotate`.
6. `bash scripts/test-stage2.sh` checks the import against GitHub, the mirrors, and that Forgejo's proxy is closed to GitHub's API again. To check a full round trip without touching the project's repositories, create an empty private repository `tentenths/inkproof-mirror-test` on GitHub, add it to the mirror token, and run `MIRROR_TEST_REPO=inkproof-mirror-test bash scripts/test-stage2.sh`; delete the repository afterwards.

### The same checkout in the dev container

The kit's dev container mounts this checkout and has a Docker engine of its own, for developing and testing the kit. Inside a container the scripts read `.env.devcontainer` instead of `.env`. That file sets its own `STATE_DIR` (`./.devcontainer-state`) for `secrets/`, `control/` and `backups/`, and its own `FORGE_PORT` (3300), so neither environment overwrites the other's credentials or takes the other's port. `KIT_ENV` names a different file for either.

## What runs where

- **Forgejo** is on internal networks only. The host reaches it through a port forwarder bound to `127.0.0.1`; it reaches the outside only through `forge-proxy`, which allows `github.com` and nothing else, so `docker compose exec forge-proxy cat /var/log/squid/access.log` is the record of everything it sent out.
- **CI** jobs run in `ci-docker`, a Docker engine of their own, never your Docker Desktop's. It is privileged, so jobs must not reach its API: it accepts only TLS connections with a client certificate that only the runner holds. Jobs reach Forgejo directly and the outside only through `ci-proxy`. Lean toolchains are kept in a volume inside `ci-docker`, so each is downloaded once.
- **Workflows** use no `uses:` actions, so CI fetches no code from outside; `templates/repo/.forgejo/workflows/checks.yml` checks out with plain git. They fetch this project's repositories with `ci-reader`'s token, the organisation's `DEPS_READ_TOKEN` Actions secret, because a job's own token reads only its own repository. A workflow an agent changes on its branch runs before review, so an agent could read that token and with it every repository in the organisation; it can't write anything with it.
- **Agents** (from stage 3) reach Forgejo and the dispatcher directly and the outside only through `proxy`. No allowlist includes `api.github.com`.

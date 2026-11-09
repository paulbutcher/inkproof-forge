# Inkproof forge

A local forge for the Inkproof agents: Forgejo is canonical, GitHub is a push mirror that nothing polls, and each agent is a Claude Code agent with its own Forgejo account. [requirements.md](requirements.md) sets out what the kit does and how we'll know it works; this README covers running it.

The kit is built in stages. In place so far: Forgejo, CI, the egress proxies and the bootstrap (stage 1); the migration from GitHub and the mirrors back (stage 2); and the first agent, `core`, at autonomy level 1, with its runner, the dispatcher's queue and run ledger, and VS Code (stage 3); every library agent and the coordinator, every trigger, caps, pause and resume, status and notifications (stage 4). Later stages add reviews, autonomy levels 2 and 3, and backups.

## Layout

```
compose.yaml            Forgejo, port forwarder, proxies, CI runner and engine, dispatcher
.env.example            organisation, your Forgejo username and email, host UID/GID, port
agents.conf             every agent: kind, own repository, repositories it reads
docker/proxy/           squid config and one allowlist each for agents, CI and Forgejo
docker/ci/              runner config and entry point; image/ is the image CI jobs run in
docker/dispatcher/      receives Forgejo's webhooks, queues work for agents, keeps the run ledger
docker/agent/           the agent image: runner, hooks, forge command, managed settings, PROTOCOL.md
roles/                  each agent's role, which comes before the protocol in its CLAUDE.md
docker/tools/           admin tools (bootstrap, tests) run in a container, not on the host
docker/ports/           forwards 127.0.0.1:FORGE_PORT to Forgejo, DISPATCHER_PORT to the dispatcher
templates/repo/         what every repository needs: CI workflow, request template, pin check
scripts/                up, down, bootstrap, migrate, mirrors, setup-repo, open, status,
                        stop-run, pause, resume, notify, notify-install, kit, and each
                        stage's test
launchd/                the notifier's launchd job, installed by scripts/notify-install.sh
tests/fixtures/         small Lean packages for the CI tests
secrets/                tokens and secrets, written by the scripts (never committed)
```

## Setup

You need Docker Desktop. The scripts are bash 3.2-compatible: run them from the kit's folder as `scripts/<name>.sh`, or with `bash scripts/<name>.sh` if a copy has lost their executable bits.

1. `cp .env.example .env` and edit it: your Forgejo username (use your GitHub username, so `CODEOWNERS` entries carry over), your email, and your UID and GID (`id -u`, `id -g`).
2. `scripts/up.sh` builds the images, generates the CI runner's and webhook's secrets, starts everything, and loads the CI image into CI's own Docker engine.
3. `scripts/bootstrap.sh` asks you to choose a password for your Forgejo admin account, then creates an admin token for the kit's scripts (`secrets/human/token`), the organisation, every account in `agents.conf` plus `gatekeeper` and `dispatcher`, their tokens and teams, and the webhook to the dispatcher. It reports what it changed; re-running it changes nothing unless something drifted.
4. Sign in at <http://localhost:3000/> as yourself.
5. Optionally, `scripts/test-stage1.sh` checks the networks, CI, permissions and webhooks. It takes a few minutes the first time, while CI downloads a Lean toolchain.

`scripts/down.sh` stops everything and keeps the volumes.

### Bringing the repositories over from GitHub

Forgejo imports each repository in `agents.conf` once, with its history, issues, labels, milestones, pull requests and releases, then pushes `main` and every tag back to GitHub on each commit. Both use GitHub tokens of your own; nothing uses a machine user, Imported issues, pull requests and comments are posted by Ghost. Forgejo keeps each one's GitHub author, but its API doesn't return it, and agents never trust them.

On GitHub the repositories are under `GH_OWNER` (`paulbutcher`); in Forgejo they're in the organisation `FORGE_ORG` (`tentenths`). They used to be under `tentenths` on GitHub, and old tags' lakefiles still say so, so `GH_FORMER_OWNERS` lists that owner too. CI and agents fetch `inkproof-` repositories under every one of those owners from Forgejo.

1. On GitHub, create a fine-grained token with resource owner `paulbutcher`, the four `inkproof-` repositories, and read-only Contents, Issues, Pull requests and Metadata. Save it as `secrets/github/import-token` (`chmod 600`).
2. Create a second fine-grained token on the same repositories with Contents read and write and nothing else. Save it as `secrets/github/mirror-token` (`chmod 600`).
3. `scripts/migrate.sh` imports whatever Forgejo doesn't have yet, then gives the agents' teams their repositories. Forgejo's proxy allows GitHub's API only while it runs. You can delete the import token afterwards.
4. Decide what the mirrors carry. Forgejo mirrors exactly: with `MIRROR_BRANCHES=main`, the default, the first sync deletes every other branch on GitHub (the old `agent/*` branches; tags, `main` and closed pull requests' diffs are untouched). With `MIRROR_BRANCHES=` (empty), every Forgejo branch is mirrored, including agents' future work branches.
5. `scripts/mirrors.sh` adds the push mirrors and shows each one's last sync and error. To rotate the mirror token, replace the file and run `scripts/mirrors.sh --rotate`.
6. `scripts/test-stage2.sh` checks the import against GitHub, the mirrors, and that Forgejo's proxy is closed to GitHub's API again. To check a full round trip without touching the project's repositories, create an empty private repository `paulbutcher/inkproof-mirror-test` on GitHub, add it to the mirror token, and run `MIRROR_TEST_REPO=inkproof-mirror-test scripts/test-stage2.sh`; delete the repository afterwards.

### The first agent: core at level 1

Each agent runs in its own container, `core` first. Its runner claims work from the dispatcher and starts a fresh, unattended `claude -p` run for it, never while you're working in that container. At level 1 everything an agent writes waits for your approval before it merges.

1. In a terminal on your Mac, run `claude setup-token` and sign in with the Claude account the agents should use. Save the token it prints as `secrets/claude/oauth-token`, then `chmod 600 secrets/claude/oauth-token`. The token lasts a year; repeat this step when it expires.
2. `scripts/up.sh` builds the agent image and starts `core`. It also writes `secrets/claude/agent.env`, which passes the token to agent containers and nowhere else.
3. `scripts/setup-repo.sh inkproof-core 1` sets labels, merge settings, `main`'s protection and tag protection for level 1, then checks the repository's own files against `templates/repo/`. Those files are code-owned by you, so where they differ it opens a pull request in your name rather than committing them, and prints its link:
   - `.github/` goes, because Forgejo ignores GitHub's `CODEOWNERS` and would try to run GitHub's workflow;
   - `.forgejo/workflows/checks.yml`, `.forgejo/issue_template/request.md` and `scripts/check-pins.sh` come from `templates/repo/`;
   - `.forgejo/CODEOWNERS` is generated for the level. Forgejo reads each of its lines as a regular expression.

   Review the pull request and merge it once CI passes; as an admin you can merge without an approval. Then run the script again: it reports the files right and exits 0. Until then it exits 3, which Docker Desktop reports with a hint about a "Compose error"; there is none.
4. In VS Code on your Mac, open **Settings**, and turn off **Dev › Containers: Copy Git Config**, then set **Dev › Containers: Git Credential Helper Config Location** to `none`. Otherwise VS Code copies your own git identity and credentials into agent containers.
5. `scripts/open.sh core` opens VS Code attached to `core`'s container, at `/workspace/inkproof-core`. The first time, VS Code installs its server and the Claude and Lean extensions there. Open `.inkproof/current.md` to follow the current run live.
6. Optionally, `scripts/test-stage3.sh` checks the agent end to end. It costs a few dollars of Claude usage, takes about half an hour, and leaves closed test issues and pull requests on `inkproof-core` and `inkproof-coordination`.

Day to day:

- **Work starts itself.** A request issue (labels `request` and `new`), or a comment or review on one of `core`'s issues or pull requests, from you or another kit account, queues work. A run starts once the events have settled for a minute (`DEBOUNCE_SECONDS`), within 15 minutes at most (`MAX_DELAY_SECONDS`).
- **You come first.** While you're using Claude in `core`'s container, through the extension or a terminal, no run starts there. Queued work starts 10 minutes after your last prompt finishes (`HUMAN_GRACE_SECONDS`). A run already going carries on.
- **Every run is a conversation you can continue.** It's logged to `.inkproof/runs/` in the workspace, and listed in the Claude extension's past sessions, where you can open and continue it. `scripts/stop-run.sh core` interrupts a run; it's recorded and can be continued like any other.
- **What happened:** `scripts/status.sh` shows each agent's state and pending work, recent runs with their outcome, cost, session and log, your own sessions in agent containers, and notices. Every run's log and transcript, and your sessions' transcripts, are kept in the `run-archive` volume.
- **Bounds:** one run at a time per agent, at most 20 runs per agent per UTC day (`MAX_RUNS_PER_DAY`), each at most 240 minutes (`MAX_RUN_MINUTES`). Set these in `.env` and rerun `scripts/up.sh`.

### Every agent, the brake and notifications

`scripts/up.sh` starts the coordinator and every library agent once each has its tokens. Each library's repository needs `scripts/setup-repo.sh <repository> 1` and its pull request merged, as for `inkproof-core`; so does `inkproof-coordination`, which gets code owners and the request template but no CI.

- **What starts work.** Only you and the kit's accounts; never an agent's own actions, an untrusted account, or a comment whose first line is `(no reply needed)`.
  - Every agent, on its own repository: a request (labels `request` and `new`), a comment or review on one of its open issues or pull requests, a failed Actions run on one of its pull requests, one of its pull requests falling behind `main`, and a release, a closed upstream issue or your comment on one, when one of its `blocked` issues names it in a `Blocked-by:` line.
  - The coordinator, across the project: a request filed, a pull request opened or merged, `ready-for-release` added, a release published, `needs-human` added or removed, and a review requested from it.
- **Missed webhooks** delay work rather than lose it: on start and every 15 minutes (`RECONCILE_SECONDS`), the dispatcher looks in Forgejo for untriaged requests, failed Actions runs on agents' pull requests, and their pull requests behind `main`.
- **Caps.** Each agent starts at most 20 runs per UTC day (`MAX_RUNS_PER_DAY`), and optionally spends at most `MAX_SPEND_PER_DAY` dollars. A reached cap is a notice: the dispatcher comments on that agent's "Kit notices" issue, mentioning you, and reuses the issue until you close it.
- **Pause.** `scripts/pause.sh` stops everything: no run starts, a run under way is refused its next tool call and ends, every scheduled merge is cancelled and recorded in `control/paused-merges.json`, and only you may merge. Your own sessions in agent containers still work. `scripts/resume.sh` restores the merge lists, reschedules those merges in your name, and lets agents start again.
- **Notifications.** Forgejo shows the dispatcher's mentions in its notifications. For macOS notifications too, run `scripts/notify-install.sh` once: a launchd job then shows each new notice every minute (`needs-human`, a `blocking` upstream issue, a reached cap, a run the extension doesn't list), and nothing routine. `scripts/notify-install.sh --remove` takes it out.
- **Status.** `scripts/status.sh` shows each agent's state (idle, running, human active, paused or capped), its waiting work and spend today, recent runs, your sessions and notices, then open requests, pull requests, recommendations and `needs-human` items across the project.

### The same checkout in the dev container

The kit's dev container mounts this checkout and has a Docker engine of its own, for developing and testing the kit. Inside a container the scripts read `.env.devcontainer` instead of `.env`. That file sets its own `STATE_DIR` (`./.devcontainer-state`) for `secrets/`, `control/` and `backups/`, and its own `FORGE_PORT` (3300), so neither environment overwrites the other's credentials or takes the other's port. `KIT_ENV` names a different file for either.

## What runs where

- **Forgejo** is on internal networks only. The host reaches it through a port forwarder bound to `127.0.0.1`; it reaches the outside only through `forge-proxy`, which allows `github.com` and nothing else, so `docker compose exec forge-proxy cat /var/log/squid/access.log` is the record of everything it sent out.
- **CI** jobs run in `ci-docker`, a Docker engine of their own, never your Docker Desktop's. It is privileged, so jobs must not reach its API: it accepts only TLS connections with a client certificate that only the runner holds. Jobs reach Forgejo directly and the outside only through `ci-proxy`. Lean toolchains are kept in a volume inside `ci-docker`, so each is downloaded once.
- **Workflows** use no `uses:` actions, so CI fetches no code from outside; `templates/repo/.forgejo/workflows/checks.yml` checks out with plain git. They fetch this project's repositories with `ci-reader`'s token, the organisation's `DEPS_READ_TOKEN` Actions secret, because a job's own token reads only its own repository. A workflow an agent changes on its branch runs before review, so an agent could read that token and with it every repository in the organisation; it can't write anything with it.
- **Agents** reach Forgejo and the dispatcher directly and the outside only through `proxy`. No allowlist includes `api.github.com`. Each container holds only that agent's Forgejo token and runner key, plus the Claude token. Its policy (managed settings, managed `CLAUDE.md`, git configuration and hooks) is root-owned in the image, so the agent can't change it. In unattended runs, anything the managed settings don't allow is refused rather than waiting for a prompt.
- **Claude.** Unattended runs, and your own sessions in agent containers, use the token from `claude setup-token`, so they count against that Claude account's plan and usage limits. `claude setup-token` makes a long-lived token for non-interactive use such as this; check that running agents this way fits your plan's terms. The cost of each run is in `scripts/status.sh`, and the caps above bound it.
- **The dispatcher** keeps the queues and the run ledger in its own volume. Its status is on `127.0.0.1:DISPATCHER_PORT` (3001); claiming work needs a runner key, which each agent's container holds for itself only.

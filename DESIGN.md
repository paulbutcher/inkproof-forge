# Design

What the kit enforces, how, and why. This is the specification the kit is built and changed against: a change to the kit's behaviour changes this document in the same commit. [README.md](README.md) is the overview, and [RUNBOOK.md](RUNBOOK.md) covers operating the kit.

## Constraints

- Host scripts are bash 3.2 and run on Docker Desktop for macOS.
- Services and the agent runner use only the Python 3 standard library.
- Images bake their scripts in with `COPY --chmod`, and mounted scripts are run with `bash` or `sh`, so lost executable bits don't matter.
- No container relies on jq 1.6 features that it rejects (`end`, `label` as keys).

## Components

| Component | Runs as | Holds | Reaches |
| --- | --- | --- | --- |
| `forgejo` (Forgejo 17) | compose service, `127.0.0.1:FORGE_PORT` on the host through the port forwarder | repositories, issues, pull requests; the human's GitHub mirror credential | internal networks; GitHub only through `forge-proxy`, for mirror pushes |
| `ci-runner` + `ci-docker` (`docker:dind`) | compose services | runner registration; CI's TLS client certificate | Forgejo; the outside only through `ci-proxy` |
| `proxy`, `ci-proxy` (squid) | compose services | the agents' and CI's allowlists | the internet |
| `forge-proxy` (squid) | compose service | Forgejo's allowlist: `github.com` only | GitHub, for git pushes (and its API while `scripts/migrate.sh` runs) |
| `dispatcher` | compose service, status on `127.0.0.1:DISPATCHER_PORT` | dispatcher account token, webhook secret, per-agent runner keys; the run ledger, queues and caps | Forgejo |
| one container per agent in `agents.conf`: the coordinator, each library, each reviewer | compose service, generated from `agents.conf` | that agent's Forgejo token and runner key, and the Claude token | Forgejo, dispatcher, `proxy`; the coordinator also reaches `gatekeeper` |
| `agent-runner` (inside each agent container) | the agent's user | nothing beyond the container's | dispatcher (claims work, reports runs); writes the run archive |
| `run-archive` volume | | every agent's run logs and transcripts, the human's sessions included | read-only to the process reviewer |
| `gatekeeper` | compose service | gatekeeper account token and webhook secret, which no agent can read | Forgejo; it also answers Forgejo's webhooks and the coordinator |
| host scripts | the human, on macOS | the human's Forgejo admin token (`secrets/human/token`) | Forgejo and dispatcher on `127.0.0.1` |

### What runs where

- **Forgejo** is on internal networks only. The host reaches it through a port forwarder bound to `127.0.0.1`. It reaches the outside only through `forge-proxy`, which allows `github.com` and nothing else, so `forge-proxy`'s access log is the record of everything it sent out.
- **CI** jobs run in `ci-docker`, a Docker engine of their own, never the host's. That engine is privileged, so jobs must not reach its API: it accepts only TLS connections with a client certificate that only the runner holds. Jobs reach Forgejo directly and the outside only through `ci-proxy`. Lean toolchains are kept in a volume inside `ci-docker`, so each is downloaded once.
- **Workflows** use no `uses:` actions, so CI fetches no code from outside; `checks.yml` checks out with plain git. Jobs fetch this project's repositories with `ci-reader`'s token, the organisation's `DEPS_READ_TOKEN` Actions secret, because a job's own token reads only its own repository. A workflow an agent changes on its branch runs before review, so an agent could read that token, and with it every repository in the organisation. It can't write anything with it.
- **Agents** reach Forgejo and the dispatcher directly, and the outside only through `proxy`. No allowlist includes `api.github.com`. Agents' containers set `NODE_USE_ENV_PROXY=1`, since Node's built-in `fetch` otherwise ignores the proxy variables and finds no route out. Each container holds only that agent's Forgejo token and runner key, plus the Claude token. Its policy (managed settings, managed `CLAUDE.md`, git configuration and hooks) is root-owned in the image, so the agent can't change it. In unattended runs, anything the managed settings don't allow is refused rather than waiting for a prompt.
- **The gatekeeper** shares one network with Forgejo and another with the coordinator only, so no other agent can reach it. Its Forgejo token is mounted only into its own container. Its account reviews, tags, publishes releases and comments. Branch protection keeps it out of `main`, and tag protection lets it tag only libraries at level 3.
- **The dispatcher** keeps the queues and the run ledger in its own volume. Claiming work needs a runner key, which each agent's container holds for itself only.
- **Resources.** Each agent's container has a memory limit with no swap beyond it, a process and thread limit and a CPU limit (`AGENT_MEMORY`, `AGENT_PIDS`, `AGENT_CPUS`). `ci-docker` has one memory and CPU limit for all its jobs together (`CI_MEMORY`, `CI_CPUS`). A run that exceeds its share fails in its own container. Without these, one agent's run took 90 GiB and 15,000 threads, and the kernel's out-of-memory killer took Forgejo down instead. The dispatcher has a memory limit too (`DISPATCHER_MEMORY`, default 2 GiB), past which it is restarted with its ledger intact: it once reached 82 GiB, paging forever through a list Forgejo doesn't page. The agents and `ci-docker` are also the first the out-of-memory killer picks, and Forgejo, the dispatcher and the gatekeeper the last.

## Accounts and permissions

- **Accounts.** They are:
  - the human, as Forgejo admin (`HUMAN_USER`);
  - one per agent in `agents.conf`: the coordinator, each library, and each reviewer (`conformance`, `architecture`, `process`, `lasm`);
  - `gatekeeper`;
  - `dispatcher`;
  - `ci-reader`, which reads every repository in the organisation and writes nothing.

  Registration is disabled, and `scripts/bootstrap.sh` creates the kit's accounts and tokens.

  The GitHub account `inkproof-tentenths`, which the agents shared before the kit, is never used or recreated.
- **Organisation and teams.** The repositories live in the Forgejo organisation `FORGE_ORG`. It can't be the human's username, because Forgejo users and organisations share one namespace. Each library agent and the coordinator get teams with per-unit access. A site's agent (kind `site`) is a library's in everything here and below; only its deployment differs ([Sites](#sites)).
  - **Its own repository:** Code, Issues, Pull requests and Actions write; Releases and Wiki read; Packages none. Actions write lets it re-run a CI run that failed for the runner's sake (`forge ci --rerun`); secrets, variables and runners need the repository's admin, which no agent is (checked by `scripts/test-stage1.sh`).
  - **Its dependencies (all libraries, for the coordinator):** Code read, Issues write, Pull requests read. The coordinator's team has Pull requests write, so it can review and approve.
  - **Every repository, for each library agent:** Code, Issues and Pull requests read, so it can reuse before it builds. Forgejo lets anyone who can read a repository open an issue on it, so filing requests only on dependencies is the protocol's rule, not a permission.
- **Reviewers own no repository.**
  - `conformance` has Code read and Pull requests write on every `inkproof-` repository, so it can review.
  - `architecture` and `lasm` have Code read everywhere, and Issues write on `inkproof-coordination` only.
  - `process`, the supervisor (kind `supervisor` in `agents.conf`), has Code read and Issues and Pull requests write everywhere, so that it can comment on any item it supervises, reopen one closed by mistake and take off a stale label. Its token has no repository write scope, so it can't approve, merge or push. It alone reads the run ledger and the whole run archive.

  No reviewer can push.
- **Agents never administer.** No agent account is a repository or organisation admin, so none can change branch protection, tag protection, webhooks, mirrors or workflow settings. `.forgejo/`, `.github/` and `CLAUDE.md` are code-owned by the human.
- **Trusted authors** are the kit's accounts and the human. Content migrated from GitHub is never trusted. Forgejo posts it as Ghost (user id -1) and keeps its GitHub author only in the database, because Forgejo 17's API leaves `original_author` empty. A migrated "Go ahead" from the human is therefore history, not a decision; decisions that still stand are recorded in the coordination repository. Comments from `dispatcher` and `gatekeeper` always mark themselves as needing no reply.
- **Tokens.** There is one Forgejo token per account, in `secrets/<account>/token`, `chmod 600`. Each agent container mounts only its own, and the `dispatcher` and `gatekeeper` tokens are mounted only into those services.

## Trust boundaries

| Rule | Mechanism |
| --- | --- |
| Agent writes only its own repository | Team permissions: write on its own repository, read and issue write elsewhere |
| No force pushes, deleting `main`, or direct pushes to `main` | `main` branch protection: no pushers, merges through pull requests only |
| Nothing merges without someone checking CI | Approvers confirm the Actions run (`/repos/{owner}/{repo}/actions/runs`) for the head commit, never commit statuses, which any account with write access can post under CI's name |
| Nothing merges that deviates from `CLAUDE.md` unless the human agrees | `conformance` is code owner of every path, so its review is requested and blocks; "block on rejected reviews" holds its findings |
| Agents can't change what they're checked against | `CLAUDE.md` code-owned by the human; the reviewer checks against the base branch's copy |
| No agent creates a tag or release | Protected tags `*`: the human, and at level 3 `gatekeeper`; Releases read-only for agent teams |
| At level 3, releases only through the gatekeeper's computed version and `CHANGELOG.md` check | Only `gatekeeper` may push tags; its token is only in its container; it answers only the coordinator |
| `API.lock` needs the human (levels 1, 2) or the coordinator (level 3) | `CODEOWNERS` plus "block on official review requests", with the code owner on the approvals list |
| `.forgejo/`, `.github/`, `lean-toolchain`, `REQUIREMENTS.md`, `CLAUDE.md` need the human | `CODEOWNERS`, at every level |
| Agents can't weaken CI | Workflows in `.forgejo/workflows/` are code-owned by the human; CI jobs run in `ci-docker`, never the host's Docker |
| No agent runs code on GitHub except a site's build | `.github/` is code-owned by the human, and `setup-repo.sh` removes it everywhere but a site's Pages workflow; that workflow gives the repository's code read access to its own contents and nothing else |
| Every API change still reaches the human once | `scripts/milestone-diff.sh`, against Forgejo, in each milestone issue |
| Recommendations change nothing until the human decides | Reviewers write only issues on `inkproof-coordination` (the supervisor also comments on items that have stalled, and its mentions wake agents); none can push, and policy files are read-only to every agent |
| Untrusted issues can't steer agents | Dispatcher and Stop hook filter on authorship, and never trust content with an original (migrated) author; trigger descriptions carry only numbers, names and links, never issue text |
| No internet except essentials | Internal networks; the proxies' allowlists are the only exits, and list only tooling |
| Nothing in the kit calls GitHub's API | No GitHub credential in any container except Forgejo's mirror credential; `api.github.com` is on no allowlist, and on Forgejo's only while `scripts/migrate.sh` runs |
| Agents can't loosen their own rules | Managed settings, managed `CLAUDE.md`, git config and hooks are root-owned in the image or mounted read-only |
| Caps and the run record are outside the agent's reach | The dispatcher keeps the ledger and refuses claims over a cap or while paused; agents can't write the run archive except their own runner's entries |
| Only the human pauses and resumes | `control/` is mounted read-only; pause also restricts who may merge |

## Autonomy levels

`scripts/setup-repo.sh <repo> <level>` applies these settings, using the human's token. Re-running it with a lower level falls back.

`CODEOWNERS` and the other files a level needs live in the repository and are code-owned by the human. So the script doesn't commit them: it opens a pull request in the human's name that makes them right, and updates it on later runs. The human reviews and merges it.

| Setting on `main` | Level 1 | Level 2 | Level 3 |
| --- | --- | --- | --- |
| Required approvals | 1 | 1 | 1 |
| Approvals list | human, `conformance` | human, `gatekeeper`, `conformance` | human, `gatekeeper`, `conformance`, `coordinator` |
| `CODEOWNERS` for every path | human, `conformance` | `gatekeeper`, `conformance` | `gatekeeper`, `conformance` |
| `CODEOWNERS` for `API.lock` | human | human | `coordinator` |
| An ordinary pull request merges once approved by | human and `conformance` | `gatekeeper`, after the Actions run for the head commit succeeded, and `conformance` | as level 2 |
| Required status checks | `checks*` (a convenience, not the gate) | same | same |
| Protected tags `*` | human | human | human, `gatekeeper` |
| Who releases | human (`scripts/release.sh`) | human | coordinator, through `gatekeeper` |

These settings are on at every level:
- block on official review requests;
- block on rejected reviews;
- dismiss stale approvals.

Pull requests are not required to be up to date with `main`. A pull request behind `main` merges as it is, and CI on `main` catches what two pull requests break together.

Every code owner's review request blocks the merge until that owner approves, so the single required approval is a floor, not the gate. Agents schedule "merge when checks succeed" on their own pull requests at every level, and the scheduled merge waits for the approvals the level requires.

### Forgejo behaviour the levels rely on

- **Review requests.** Every pending code owner's review request blocks the merge. An approver's official approval answers their own request, though Forgejo's API goes on listing them under `requested_reviewers` (verified 2026-11-09, and by `scripts/test-stage5.sh`).
- **Finding `CODEOWNERS`.** Forgejo 17 reads `CODEOWNERS` from the default branch: from the first that exists of `CODEOWNERS`, `docs/CODEOWNERS`, `.gitea/CODEOWNERS` and `.forgejo/CODEOWNERS`. It never reads `.github/`.
- **Matching `CODEOWNERS`.** Each line is a Go regular expression anchored to a whole path without a leading slash (`API\.lock`, `\.forgejo/.*`). Every matching line adds its owners, so the catch-all line and the `API.lock` line combine. This was read from Forgejo's source on 2026-11-09.
- **No pushes.** Branch protection's "no pushes" applies to the human too, so the human's own changes, such as the `.forgejo/` files `setup-repo.sh` asks for, also go through pull requests. An admin can merge one without the approval.
- **Scheduled merges.** Forgejo acts on a scheduled merge only when a review or CI status arrives after it was scheduled. A merge scheduled once everything has already passed would never fire, so `forge pr automerge` merges at once in that case.
- **Actions webhooks.** Forgejo 17 sends a webhook for a finished Actions run, whether it failed or succeeded (verified 2026-11-08). The kit subscribes to no event for a run starting.
- **Actions write.** A team's Actions write on a repository lets its members re-run a run (`POST /repos/{owner}/{repo}/actions/runs/{id}/rerun`, by the run's database ID), but not write secrets or variables or get a runner registration token, which need the repository's admin (verified 2026-11-20, and by `scripts/test-stage1.sh`).

### The gatekeeper

The gatekeeper is a Python program, not a Claude agent, so it costs nothing per pull request.

Forgejo's webhooks tell it where to look: a finished Actions run, a pull request opened or pushed to, or a push to `main`, which may change a repository's level. They come over the `gate` network, which it shares only with Forgejo, signed with its own secret (`secrets/gatekeeper/webhook-secret`). It reads nothing from a webhook but where to look, and looks again 30 seconds after a finished run, in case the runs API lags the event.

On start, when the kit is resumed, and every 15 minutes (`GATEKEEPER_SWEEP_SECONDS`), it looks at every open pull request. So a lost delivery delays an approval rather than losing it, and nothing polls Forgejo in between.

It acts only:
- in repositories whose `CODEOWNERS` on `main` names `@gatekeeper`, which is how `setup-repo.sh` marks levels 2 and 3;
- on pull requests into `main`.

It approves the head commit when Forgejo's Actions runs for that commit include `checks.yml` and all of them succeeded. It never reads commit statuses. Its approval starts with `(no reply needed)`, so it wakes no one, and while the kit is paused it approves nothing. Its account's team reads code and reviews pull requests on every repository, and can do nothing else. Branch protection dismisses its approval when a new commit arrives, and the gatekeeper approves the new head once that head's run succeeds.

### Releases

At level 3 the gatekeeper also releases, when the coordinator asks with `forge release`. The request comes over a network that only the coordinator and the gatekeeper share, and carries a key that only the coordinator's container holds (`secrets/gatekeeper/coordinator-key`). The gatekeeper refuses while the kit is paused. It also refuses for a repository below level 3, which it recognises by `CODEOWNERS` not giving `API.lock` to `@coordinator`.

The gatekeeper and `scripts/release.sh` run the same code, so a release follows the same steps whoever makes it:
1. It refuses a repository that doesn't exist, naming any whose name ends with the one given (`viewport` for `inkproof-viewport`), before anything else: otherwise the missing `CODEOWNERS` would make it look below level 3. Then it takes the highest `vX.Y.Z` tag as the last release.
2. It computes the next version from the diff of `API.lock`'s lines since then:
   - before 1.0, minor if `API.lock` changed and patch otherwise;
   - from 1.0, major if a line was removed or changed, minor if lines were only added, and patch otherwise.
3. It requires a `CHANGELOG.md` entry headed with that version, in either form the libraries use (`## [X.Y.Z]` or `## vX.Y.Z`), and a successful Actions run on `main`'s head.
4. It publishes the release, which tags `main`'s head.
5. It comments the release on each `ready-for-release` issue named, and removes the label. It closes each one that isn't a request, such as an owner's task for itself: no requester will verify it, so nothing else would end it.

With no earlier tag, the first release is v0.1.0. A new major line, such as 1.0, starts with a tag the human makes. A level-3 library may depend on one below level 3: that one's releases stay the human's, and its consumers wait on them with a `Blocked-by:` line.

## Runs

- **Hand-off.** The dispatcher queues triggers per agent. Each container's `agent-runner` claims a batch over HTTP with its runner key. The dispatcher grants a claim only when the agent has no run, is under its caps, and the kit is neither paused nor draining. The runner reports the run's start and end, and the dispatcher records it.
- **Priority.** An issue labelled `priority` comes before anything new. The human sets the label, or the coordinator; an agent adds it only to an issue that holds up one of its own priority items. In a claimed batch, the triggers whose items are labelled `priority` come first, marked so, followed by a line naming any other open priority items on the agent's own repository. The protocol says what to do with them: keep what's open moving, but start nothing new before them.
  - A claim no runner will start would keep the agent from every other run, so it is closed unstarted and its work queued again as it was: at once if its reply doesn't reach the runner (which gave up waiting), and otherwise once it has gone unstarted for `CLAIM_START_SECONDS` (default 900, above the ten minutes the runner may spend cloning new repositories before it starts).
  - The dispatcher records when each runner last called in. Queued work for an agent with no run open, whose runner has been silent for three minutes (an idle runner calls in every 15 seconds; one running a run doesn't), and a claim unstarted for two, make the status line say "Kit: stuck".
  - At most `FORGE_CONCURRENCY` (default 8) of the dispatcher's requests to Forgejo run at once, so that a slow Forgejo isn't given more work. Lists are read page by page, except issue comments, which Forgejo serves all at once and doesn't page; a list that serves the same page again ends there.
- **Starting.** The runner starts a run whenever the dispatcher grants one, whether or not the human has a session open in the container (Sessions with the human). It runs `claude -p --permission-mode dontAsk --output-format stream-json --name "<agent>: <triggers>" --session-id <uuid>` in the agent's workspace. The prompt lists the triggers as pointers, and says that no one can approve anything for it.
- **First-run prompts.** `agent-init` marks each agent's folder as trusted in `.claude.json`. The managed `CLAUDE.md` is generated as one file (role followed by protocol), so there is no external include to approve.
- **Logging.** The stream becomes a Markdown log under `.inkproof/runs/` in the workspace, plus `current.md`, which updates live in a VS Code tab. `.inkproof/` is excluded from git.
- **Ending.** When a run ends, the runner records its outcome, then rewrites `"entrypoint":"sdk-cli"` to `"entrypoint":"claude-vscode"` in the run's transcript, so that the Claude extension lists it, and the human can open it and ask about it. If the rewrite matched nothing, the run record says so and the human is notified; the run can still be resumed by ID.
- **Retries.** A run that fails, times out or exits before finishing a reply may have left its work undone, and most triggers are queued only once. So its triggers are queued again after `RETRY_DELAY_SECONDS` (default 300) times the attempt number, at most `MAX_RUN_RETRIES` times (default 2). After that, the dispatcher opens a decision for the human with the run and its work.
  - A run that failed before it got going (one turn at most, nothing spent, no change in Forgejo) failed for its environment: an outage, the proxy, the API or the account's usage limit. It doesn't count as an attempt, or against the daily run cap. Its work is queued again after 5 minutes, then 15, then every hour (`ENV_RETRY_DELAYS`), until one of the agent's runs gets going, so it outlasts an outage without filling the human's queue with decisions. A rule queuing that work again doesn't cut its wait short. The agents share most of their environment (the Claude credential and its usage limit, the proxies), so when any agent's run gets going, every agent's work held back this way is due at once.
  - A run whose error names when the account's usage limit resets ("You've hit your session limit · resets 4:40pm (UTC)") is retried that way too, however far it got before the limit cut it short, since the limit is no fault of its work. Its work, and every agent's work held back this way, is due 2 minutes after the reset (`LIMIT_RESET_MARGIN_SECONDS`), rather than at the next of `ENV_RETRY_DELAYS`. A reset time without a time zone, or more than 8 days off, isn't trusted, and the delays apply.
  - The human gets one notice while runs keep failing this way (`env-failing`), and the status line says "Kit: failing" until they stop.
  - A failed run's outcome is the result's subtype (`error_max_turns`), or for an API error, whose subtype is `success`, the error's own text (a usage limit, with when it resets).
  - A run the human stops isn't retried.
  - A run that ends while the kit is paused is queued again as it was, without counting as an attempt.
  - So is a run abandoned by a restarting runner.
- **The record.** For each run, the ledger keeps:
  - the trigger, start and end, and outcome;
  - cost, turns and denied commands;
  - session ID and log path;
  - whether the run shows in the extension;
  - whether it changed anything in Forgejo, from the webhooks its account caused.

  It also records the human's sessions in each container: start, end and session ID.
- **Retention.** The runner copies each run's log and transcript, and each of the human's session transcripts, to `run-archive`, where they are kept for at least two milestones. Managed settings raise `cleanupPeriodDays` to 365, so Claude Code doesn't delete transcripts that runs and the human's questions about them still need.
- **Bounds.**
  - At most one run per agent at a time.
  - Configurable caps per agent per UTC day on runs (default 20; the coordinator 40, because every other agent's work wakes it), and optionally on spend, from the stream's reported cost.
  - A maximum run length (default 240 minutes).
  - A burst of triggers within the debounce window (default 60 seconds) becomes one run.
  - A run starts within a configurable delay of its event, at most 15 minutes by default.

  A reached cap is a decision for the human.
- **Interrupting.** `scripts/stop-run.sh <agent>` interrupts a run, which is then recorded and rewritten like any other.

## Sessions with the human

The human talks to an agent directly in a session of their own in its container, to ask about its work or have it look into something. Runs go on beside such a session, in the same checkouts, so the session only consults: it looks, tries things in a clone of its own, and advises. What should change goes to the agent's runs as an issue.

- **Which sessions.** A session is the human's when `CLAUDE_CODE_ENTRYPOINT` is `claude-vscode` or `cli` and the runner didn't start it (`INKPROOF_RUN`). Runs, whose entrypoint is `sdk-cli`, aren't, and neither are extension processes. A run's transcript opened in the extension becomes one of the human's sessions.
- **Told at the start.** The managed `SessionStart` hook records the session and tells the agent that it is in one, which points it at the protocol's "Sessions with the human": read anything, build and test only in a clone outside `/workspace`, file issues and comment in Forgejo, nothing else.
- **A guard.** The managed `PreToolUse` hook refuses, in the human's sessions only: edits under `/workspace`; git commands that change a checkout there (`checkout`, `switch`, `commit`, `stash`, `reset`, `pull` and the like) or its branches; `git push` anywhere; builds there (`lake build`, `npm`, `npx`, `build-site.sh`, `lean_build`); file writes and redirects into it; and in Forgejo anything but filing issues and commenting (`tea issues create`, `tea comment`), and every `forge` command but those that read (`ci` without `--rerun`, `reviews`, `ledger`). Each refusal says what to do instead. It catches mistakes, not intent: it is no sandbox, and the Lean language server may still build what a file it opens imports.
- **No waiting.** Nothing holds runs back for the human: the runner claims work while their session is open, and the idle checks don't wait for it. The agent's comments in the session wake no one, so a decision or go-ahead it needs from the human is a comment the human posts in their own name.
- **Recorded.** The runner reports each session's start and end to the dispatcher, on a thread of its own so that it does so during a run, and archives its transcript.
- `scripts/open.sh <agent>` attaches VS Code to the running container. `scripts/up.sh` builds every image, and Dev Containers never builds one. Managed settings and hooks apply to the human's sessions too. Host credential, git config and SSH agent forwarding are turned off.
- **From the Claude app.** `scripts/remote.sh` starts, in each agent's container, a session of the human's through Remote Control: an interactive `claude --remote-control <agent>` in tmux (`agent-remote`), which the human drives from the Claude desktop app, claude.ai/code or the mobile app, signed in to the agents' Claude account. Its entrypoint is `cli`, so it is one of the human's sessions, and the guard applies. Remote Control refuses the setup token runs use, so the session goes without `CLAUDE_CODE_OAUTH_TOKEN` and uses a claude.ai login of the agents' account, kept in the agent's own Claude volume, which the human makes once per agent (`scripts/remote.sh --attach <agent>`). Runs keep using the setup token. If Claude exits, the session starts again after ten seconds. It ends when the container is recreated.

## Pause and resume

`scripts/pause.sh` does three things:
- it writes `control/paused`;
- it cancels scheduled merges, recording them;
- it narrows the merge list on every protected branch to the human.

While paused:
- the dispatcher grants no claims;
- the gatekeeper approves and releases nothing;
- a managed hook refuses every tool call in a run, so a running run stops at its next step.

`scripts/resume.sh` restores the merge lists. In the human's name, it merges each recorded pull request that nothing now stands in the way of, and reschedules the others. While the kit is paused, nothing changes in Forgejo except by the human.

### Draining

`scripts/drain.sh` is a clean stop, for restarting the Mac or the kit. It writes `control/draining`, and while that is there the dispatcher grants no claims. Everything else goes on: runs under way finish, CI runs, scheduled merges land, and the work they bring is queued. The script waits until no run is under way and no CI is running, then says it is safe to stop. With `--max N` it pauses after N minutes instead, which ends the runs still going at their next tool call and queues their work again.

- The flag is a file, so it survives a restart: nothing starts until `scripts/resume.sh` removes it. After a drain, `resume.sh` does only that, since no merge was cancelled.
- Draining looks quiet, but only because nothing may start. So, as while paused, the idle summary is skipped, since the coordinator couldn't run to write it. Obligations wait too: their runs are held back, so no wake is counted.
- The dispatcher can be replaced on its own without disturbing a run (`docker compose up -d --build dispatcher`): a run talks to Forgejo, and its runner talks to the dispatcher only to claim work and to report the run's end, which it retries for five minutes. That is how a dispatcher without draining is replaced by one with it, before the first drain (verified 2026-11-20 in the dev container: a run's end was recorded although the dispatcher was replaced while it ran).

## Triggers

What an agent owes comes from the work as it is now, never from the event that changed it ([proposals/explicit-state.md](proposals/explicit-state.md)). There are three kinds:
- **States.** Every issue, pull request, repository's release, milestone and the kit itself is a unit with a state. A pure function (`docker/dispatcher/states.py`) derives it from a few facts the dispatcher reads from Forgejo. Each state that isn't finished has holders, who owe its next step: agents, the human, the world outside, a timer, or other units.
- **Asks.** Something asked of an agent on an item, which it owes an answer to until it acts there.
- **Notices.** Something an agent should look at once. A notice is never what work waits on.

States and asks are obligations, which wake their agents until they go away and then become the human's (Obligations). A wake reads `<unit> (<state>): <step>`, such as `issue inkproof-core#5 (merged): label it ready-for-release, with its CHANGELOG.md entry`. Notices are queued once for each version of what they look at. The numbers below are those of the asks and notices in the dispatcher's code. The states replaced the rules once numbered 1, 3 to 7, 9, 10, 14, 15, 17, 19 to 21, 23 to 25, 27, 29 and 30 on open items, and the stall check.

Webhooks only say which units to evaluate again:
- the item an event names;
- the requests a pull request refers to with `Refs`;
- the issues whose `Blocked-by:` lines name it;
- its repository's release, for a push to `main`, CI on `main`, a release, or an issue labelled, closed or reopened;
- the milestones and the kit, for an issue opened, closed, reopened or moved between milestones.

On start, and every 15 minutes (`RECONCILE_SECONDS`), a sweep evaluates every repository's release and open items, the issues closed lately, and the milestones and the kit. So a missed delivery delays work rather than losing it. A state held by a timer is evaluated again when the timer runs out. Before a claim is granted, the dispatcher evaluates again each unit behind the agent's work, and drops what is no longer owed.

The status page (`scripts/status.sh`, and `scripts/watch.sh`, which asks every 15 seconds) reads nothing from Forgejo:
- **What waits on the human.** The dispatcher keeps an index of the open items assigned to the human or requesting their review. Every evaluation of an item updates the item's entry, and the sweep drops entries for items that closed without a webhook.
- **Whether CI is running.** No webhook says that an Actions run has started. So any push, branch or tag creation, pull request event, or Actions run event marks its repository. Once a minute the dispatcher lists the Actions runs of the marked repositories, and of those it last found busy. The sweep lists every repository's runs. A repository that is marked but not yet checked counts as running, so a drain waits for it.

Only trusted authors count: an agent's own actions, and comments whose first line is `(no reply needed)`, ask nothing.

### States

The facts are booleans and small enums: a label, whether a line exists, whether work merged since a time. The dispatcher reduces timestamps and prose to them before the state function sees them. Each table below lists the states in the order they are tested: the first that applies wins.

**What holds an issue or pull request**, whatever its kind, comes first, in this order:
1. **with the human**: `needs-human` that the human hasn't commented on since it went on, or the item assigned to them by hand;
2. **upstream**: labelled `upstream`, held by the world outside;
3. **waiting**: the latest `Waiting-on: <agent>` line from a trusted account, at the start of a line, held by the agent it names until it acts there. A later line naming someone else or `none` replaces it, and a line naming the human or its writer says nothing: the human is reached with `needs-human`;
4. **invalid**: a `Blocked-by:` line naming the item itself while it is the newest such line, or one that closes a loop of items each blocked by the next. Held by whoever wrote it;
5. **blocked**: a `Blocked-by: <owner>/<repo>#<n>` line naming an item that is open, or closed while still labelled `ready-for-release` and never released (its work isn't out yet, whoever closed it). Held by the items it names. A line may name a pull request, which Forgejo closes when it merges;
6. **blocked without a reason**: `blocked`, with no such line, held by the owner.

**An issue** is one of these kinds:
- a request: labelled `request` and filed by someone other than the owner;
- a milestone issue: on `inkproof-coordination`, labelled `milestone-review`;
- a recommendation, or a decision the dispatcher filed: the human's to close;
- a `Release <repo>` decision the coordinator filed: the human's, or the coordinator's once a release has overtaken it;
- otherwise, a task: the owner's own, the human's, or another agent's that isn't a request.

Its states:
- **in review**: a milestone issue, open or closed, that a milestone review hasn't reported on since `milestone-review` went on. Held by those reviewers, since the coordinator may accept a milestone before its reviews report;
- **closed unreleased**: a request or task closed while labelled `ready-for-release` and never released. Held by the coordinator, which reopens and releases it, or takes the label off and says why;
- **closed**: finished;
- **overtaken**: a `Release <repo>` decision after which the library published a release, whoever published it, before the coordinator acted on it again. Held by the coordinator, to close it or update it;
- **with the human**: a `Release <repo>` decision otherwise, a recommendation, or a decision the dispatcher filed;
- the holds above;
- **reviewed**: a milestone issue every review has reported on. Held by the coordinator: it accepts the milestone, or says what it waits for with a `Blocked-by:` line;
- **untriaged**: labelled `new`, or filed by someone other than the owner, never touched by the owner and without `accepted` or `counter-proposal`. Forgejo drops the labels of anyone without write access to issues, so a request can arrive unlabelled. Held by the owner;
- **counter-proposed**: a request labelled `counter-proposal`, held by its requester;
- **verified**: a request labelled `verified`, held by its requester, to close it;
- **in progress**: an open pull request refers to it with `Refs` (at the start of a line, or followed directly by the references in the middle of one) or "part of" (in its title or description). Held by that pull request;
- **ready**: labelled `ready-for-release`, held by its repository's release;
- **released**: a `Released in vX.Y.Z` comment, from the gatekeeper, the human or the owner, with no `not-verified` added since. A request is held by its requester, to verify it against the release, add `not-verified` saying what falls short, or name what it must do first with a `Blocked-by:` line. Another issue is held by its owner, to close it;
- **merged**: one of the owner's pull requests that refers to it merged since it was last unblocked (or at that moment), accepted or released. Held by the owner, to add `ready-for-release` with its `CHANGELOG.md` entry. Not on `inkproof-coordination`, which has no releases;
- **to do**: otherwise, held by the owner. It is quiet: it waits while the owner has anything else to do.

A request's requester, or a pull request's author, that is neither an agent nor the human is replaced by the owner, to take it on or close it.

**A pull request**'s states:
- **closed**: merged or closed, and finished;
- the holds above;
- **in draft**: a title starting `WIP`, `[WIP]` or `Draft`. Held by the author, quietly;
- **conflicting**: Forgejo finds a conflict with `main`. One merely behind merges as it is;
- **CI failing** on the head;
- **no CI**: no CI run at all for the head `CI_MISSING_SECONDS` (default 10 minutes) after the push, as on a branch without the workflow;
- **CI stuck**: CI still running `CI_LATE_SECONDS` (default an hour) after its last change;
- **changes requested**: a request for changes at the head, standing (not dismissed). All of these are held by the author;
- **author replied**: the author has answered every request for changes at the head, since the reviewer's latest review and comment, without pushing. Held by those reviewers;
- **CI running**, or not yet started: a timer;
- **in review**: held by every reviewer owed at once:
  - conformance, which hasn't reviewed the head, once it has settled. It has settled at once when the author is an agent with no run under way, when that run ends, and otherwise `CONFORMANCE_SETTLE_SECONDS` (default 10 minutes) after the last push. Until then a timer holds it with them;
  - a reviewer that requested changes on an earlier head;
  - and everyone else whose review is requested and not given at the head, in the timeline or by Forgejo, code owners included: the coordinator at level 3, or the human. The coordinator's wake says whether its review is the one the pull request waits for, read from CODEOWNERS on `main` in Forgejo, since a clone made before the setup pull request merged has no CODEOWNERS;
- **settling**: the head settling, with no other review owed: a timer;
- **unapproved**: no approval at the head, and none requested. Held by the author;
- **checking**: Forgejo hasn't yet worked out whether it conflicts: a timer;
- **unscheduled**: no merge scheduled, once nothing else stands in its way. Held by the author, quietly;
- **stuck**: a scheduled merge that hasn't gone through `MERGE_STUCK_SECONDS` (default 10 minutes) after nothing stood in its way. Forgejo acts on a schedule only when a review or status arrives after it. Held by the author, to merge it now;
- **merging**: a timer.

**A repository's release** (each library and site):
- **main failing**: CI failed on `main`'s head, whatever else. Held by the owner;
- **up to date**: nothing to release, so finished. There is something to release when an open issue is labelled `ready-for-release`, or `CHANGELOG.md`'s newest heading on `main` is above the last release: the highest `vX.Y.Z` tag, as the gatekeeper finds it, since a repository's tags may predate its releases in Forgejo. The changelog is written only when preparing a release, so its entry asks for one even with nothing labelled (demos#105);
- **held**: a `ready-for-release` issue that something holds (the human, a wait, a blocker). Held by that issue. So when the coordinator can't release, it says why on the issue with a `Waiting-on:` line for the owner, or a `Blocked-by:` line;
- **main CI running**, or not yet started: a timer; **main CI stuck** after `CI_LATE_SECONDS`, held by the owner;
- **no entry**: work labelled, with no heading above the last release. Held by the owner. When the newest heading is the last release's, the wake says so, and if lines were added under it after its tag, that they belong under a new heading (an entry written before the tag and merged after it lands there: demos, 2026-11-23);
- **wrong entry**: a heading other than the version the gatekeeper would compute from `API.lock` since the last release (`states.next_version`, which a test holds to the gatekeeper's). Held by the owner, to make it the version `forge version` gives;
- **releasable**: held by the coordinator. At level 3 it releases with `forge release`, naming every `ready-for-release` issue. Below level 3 it puts the release to the human as a `Release <repo>` decision with the exact command. Its wake names the issues and gives the command;
- **with the human**: below level 3, once that decision is open.

**A milestone**, by title, with its namesakes on every repository:
- **closed**: closed on every repository, so finished;
- **in progress**: an open issue on any of them, its milestone issue included. Held by those issues;
- **empty**: no issue at all, so finished;
- **finished**: its work done, and no milestone issue yet. Held by the coordinator, to open it, labelled `milestone` and `milestone-review`, which starts the reviews;
- **accepted**: its milestone issue closed. Held by the coordinator, to close it on every repository.

**The kit**:
- **in progress**: a milestone has open work;
- **finished**: `ROADMAP.md` on `inkproof-coordination`'s `main` has a line `Status: finished`;
- **not started**: no milestone has ever had work. Finished, until the human starts one;
- **planning**: a pull request open on `inkproof-coordination`, held by it;
- **with the human**: a decision the coordinator filed there is open and assigned to the human;
- **what next**: otherwise, held by the coordinator, quietly. It closes out any milestone left open, then drafts the next one, puts to the human what it needs, or records in `ROADMAP.md` that the project is finished.

### Asks

- **(2)** On the owner's open issue or pull request: the newest comment, or review that isn't an approval, by someone else, that wants a reply. A request for changes always wants a reply. It is owed by the owner until it does anything there after it. What it does in a run that started before the comment doesn't count, since that run was briefed without it. The wake names who wrote it and links the comment (or, for a review, gives the `forge reviews` command), so the run reads that one rather than looking for it.
- **(26)** A comment by the human that @-mentions an agent, on any issue or pull request, owed by that agent until it acts there. This is how the human reaches an agent other than the repository's owner, whom any comment reaches (2). The supervisor's @-mentions count too, the owner's included, since its comments start `(no reply needed)`; at most `SUPERVISOR_NUDGES` (default 3) of its comments a day on one item count. On a request, the owner's comment that wants a reply and @-mentions the requester counts too. It is the owner's way to ask what only the requester knows, such as its reproduction (gpu#59). Any other agent's @-mentions ask nothing. Only the newest mention of each agent on an item is owed: acting there answers them all.
- **(30)** On a closed item, a `Waiting-on: <agent>` line, as for the hold: a verified request is where the talk after it goes on (gpu#59: the coordinator asked demos there for the release of its fix). On an open item, it is a hold.

### Notices

- **(8)** The human's comment on an open upstream issue that an item's `Blocked-by:` line names, for the item's owner.
- **(11)** A request closed without `verified` by someone other than its requester: the requester hears it was rejected.
- **(12)** For the coordinator, an open request filed by someone else, to check it is well formed and well placed.
- **(13)** For the coordinator, a pull request opened by another agent, open or merged, unless it changes only `CHANGELOG.md`, which the coordinator checks when it releases. The human's pull requests, such as `setup-repo.sh`'s, don't count. The changed files are read again at each evaluation, so a pull request that grows beyond `CHANGELOG.md` counts then. It waits until CI finishes on the pull request, or for `PR_OPENED_HOLD_SECONDS` at most (default 10 minutes), so that a review asked of the coordinator shares the run.
- **(16)** For the coordinator, `needs-human` added or removed by anyone but itself and the dispatcher, if it hasn't commented or labelled since. Not on a process or Lasm review's recommendation: those change the kit or go upstream, which is never within its remit.
- **(18)** For the coordinator, a release published by anyone but the gatekeeper and itself. It is the only notice that comes from an event, not a state.
- **(22)** For `lasm`, an open issue on `inkproof-coordination` labelled `upstream`, `upstream:lasm` and `blocking`.
- **(28)** For the owner, an open issue labelled `priority` by someone else, once each time the label goes on.

The dispatcher keeps each issue's `blocked` label in step with its `Blocked-by:` lines: on while an item they name blocks it, and off once no line is left, if the dispatcher put it on. It keeps an upstream issue's `blocking` label in step with them too: on while an open item's lines name it. Agents write only the `Blocked-by:` lines.

### Obligations

An agent owes each state it holds, and each ask it hasn't answered: one obligation per unit, agent and state or ask.
- **Discharged** as soon as the unit no longer owes it: when the state, its holders or the ask change. Its queued work is dropped at the claim. If the same state comes back later, it starts afresh.
- **At once.** A new obligation wakes its agent at once. A quiet one waits while the agent has anything else queued or running.
- **Again.** Each time a run it woke ends with the obligation still standing, it wakes the agent again after the next of `OBLIGATION_BACKOFF` (default at once, then 15 minutes, then 15 minutes), its unit first evaluated afresh. The wake says it is woken again, and how to say what it waits for. A run can stop short, and an agent that can't act yet must say why in a way that changes the state: a `Blocked-by:` line, `Waiting-on:`, `needs-human` with a decision, or `not-verified`. A comment that only says what it will do changes nothing, so it is woken again.
- **Then the human's.** Once as many runs have ended with it standing as there are waits, and the last wait is over, it becomes the decision `Stalled: <unit>`, with what the agent owed. If there is a supervisor, it is woken to find out why. The agent isn't woken for it again until what it owes changes. Once the agent no longer owes it, the dispatcher says so on the decision and closes it, unless the human has closed it already. The decision links to the unit as the human's browser reaches Forgejo (`FORGE_WEB_URL`).
- A wake counts once its run has ended, so an outage, a cap or a pause, which holds the run back, uses up nothing. A run that never got going is retried without counting (Retries). Work dropped by hand (`/admin/drop`) comes back after the next wait.
- **Dropped once.** On a ledger from before the obligations, the old rules' work that no run has taken is dropped once, at start, as obligations take its place.

`test_states.py` checks the states without sampling:
- every combination of every unit kind's facts has a state, and every state that isn't finished has holders;
- only the states it lists as finished are finished;
- each holder's step moves the unit to another state or other holders;
- from every combination, a finished state can be reached.

So no state waits for nothing. What the checks can't cover is whether the facts are read right from Forgejo, which `test_dispatcher.py` tests against a fake Forgejo.

### The idle summary

The coordinator writes the human a summary when the kit goes quiet, once per quiet spell. Quiet means:
- no run;
- no queued work, except what a cap holds;
- the human active nowhere;
- no CI;
- nothing new for `IDLE_SECONDS` (default 20 minutes). The dispatcher starting counts as new.

It comes only when the set of items waiting on the human differs from the set at the last idle summary. Otherwise it would say again what they already know, while releases and verifications in between keep ending one quiet spell and starting the next. When the project finishes, it comes once, since nothing waiting on them is a change.

The run's last message, which the Claude extension lists in its container, tells the human what finished and what it needs from them.

### The first sweep

On a ledger from before the rules existed, the first sweep records the notices it finds as handled, since the old event-driven triggers covered them: upstream comments, rejections, filings, pull requests opened, `needs-human` changes and blocking Lasm issues. Obligations are woken as usual. Webhooks start nothing until this sweep has run.

## What waits on the human

Everything waiting on the human is in Forgejo's own dashboards: open issues and pull requests assigned to them, and pull requests requesting their review. No file or other list repeats it.

- `needs-human` means the human must decide. Something waiting on an agent never carries it.
- A `needs-human` item states its decision, in the description or one comment, under these headings:
  - Question;
  - Options;
  - Recommendation;
  - If you say go ahead: what the agent will do if the human accepts its recommendation;
  - Blocked on this.

  The dispatcher takes the label off anything missing them, says which are missing, and wakes whoever added the label.
- The dispatcher assigns the human to anything labelled `needs-human`, and to `blocking` upstream issues, and unassigns them when that ends. The dispatcher account has issues and pull requests write on every repository for this.
- The kit's own decisions are `needs-human` issues of their own, assigned to the human:
  - a reached cap;
  - a run the extension doesn't list;
  - an obligation an agent was woken for as many times as `OBLIGATION_BACKOFF` allows without it going away (`Stalled: <unit>`), a released request nobody verifies among them.

  A stalled item is a problem for the human to resolve or fix in the kit. No agent verifies another's request.
- Below level 3, the coordinator puts each library's ready release to the human as a decision, `Release <repo>`, with the exact `scripts/release.sh` command for the human to run. Its "If you say go ahead" says only what the coordinator does: close it once the release is published. A "go ahead" comment releases nothing.
- The coordinator's idle summary says whether the project is waiting on the human, stalled or finished, and how many items wait on them. It comes only when what waits on the human has changed since the last one.

## Notifications

Forgejo has no email without SMTP. The dispatcher records a notice for each of these, and for nothing else routine:
- every `needs-human` label added, with a milestone's recommendations grouped into one notice per report;
- every reached cap;
- every `blocking` upstream issue;
- every obligation that becomes the human's (`Stalled:`), as its decision;
- every run whose transcript rewrite failed;
- runs failing before they get going, once until they get going again.

For an existing issue, the dispatcher comments as `dispatcher`, mentioning the human, so the notice shows in Forgejo's notifications. A cap or a failed transcript rewrite gets an issue of its own, assigned to the human. A host launchd job, `scripts/notify.sh`, reads new notices from the dispatcher on `127.0.0.1` and shows each as a macOS notification. SMTP with the human's own mail account is optional, for being told while away from the machine.

## Reviews

### Conformance, on every pull request

- **What.** `conformance` checks the pull request's diff against the base branch's `CLAUDE.md` and the protocol, and reports each deviation with the section it breaks. Under the protocol's "Reuse before you build", it also checks two things for a pull request that adds declarations: that it says what prior art it looked for and where, outside this project (Lean core, Std, Batteries, Mathlib, Reservoir) as well as in it, and that nothing it adds repeats a declaration in another library, by what the declaration does as well as by its name. Under "Working in your repository", it checks that a pull request changing what the public API does updates the `## What it does` list in the repository's `README.md`, which is how outside users and the agents alike find what each library does. It reviews craft against stated rules only; scope stays with the coordinator.
- **Outcome.** It approves, or requests changes with its findings. A request for changes wakes the author through the review trigger, and blocks the merge until `conformance` approves a later head.
- **Answering findings.** The author fixes each finding or replies with a reason. `conformance` accepts the reason or keeps the finding. After `CONFORMANCE_ROUNDS` rounds on one pull request (default 3), the findings the author disputes go to the human: it labels the pull request `needs-human` instead of reviewing again, and the human decides. That happens once per head, and only for disputes: a finding the author has agreed to fix, or hasn't seen yet, gets an ordinary request for changes.
- **For the process review.** `forge review` is the only way `conformance` reviews. It ends every review with a machine-readable `conformance-findings` record: the section and status of each finding (fixed, disputed or escalated), the round, and whether the review was escalated. So repeated deviations can be found. `forge review` also enforces the rounds limit and the CI check in code.

### At the end of each milestone

- **The milestone.** A Forgejo milestone on `inkproof-coordination` gives each milestone its start and end. Forgejo milestones belong to one repository, so a milestone has a namesake on each library it involves, holding its briefs.
  - As soon as a milestone's work is merged, the coordinator drafts the next one and files its briefs as requests, without waiting for releases, verification or acceptance.
  - When the last issue in it closes, the coordinator (or the human, with `scripts/milestone.sh`) opens a milestone issue and adds `milestone-review`. The `API.lock` diff from `scripts/milestone-diff.sh` is part of the milestone issue.
  - The coordinator accepts the milestone itself once every brief's acceptance criteria are met on `main` and no fork is open.
  - The human decides only real forks (a choice between designs or trade-offs that changes what gets built), and the reviews' recommendations, which the acceptance doesn't wait for.
- **Architecture** (`architecture`). It asks:
  - Are the boundaries between libraries in the right place?
  - Is there too much coupling between libraries, or too little cohesion within one?
  - Are libraries working around limitations in dependencies, including ones we don't control, and should those be fixed instead?
  - Is code duplicated between libraries, or does something general, in a library or an application such as the demos site, belong lower down?
  - Does each library's `README.md` list everything its `API.lock` does, by job?
  - Does a library build what an existing library already provides (`PRIOR-ART.md`, Lean core, Std, Batteries, Mathlib, Reservoir) without recording why?

  It reads every repository, the milestone's `API.lock` diff, requests and how they were resolved, and the upstream tracker.
- **Process** (`process`). It looks at how agents work: the protocol, the roles, `CLAUDE.md`, prompts, and what wakes each agent. It proposes changes that stop a mistake caught in review from happening again, or that remove work nobody needs, whether by agents or by the human. That includes runs the kit started for nothing:
  - triggers whose runs repeatedly change nothing;
  - runs that one run could have done;
  - runs woken by something that asked nothing;
  - the runs behind a reached cap.

  It reads the run ledger, the run archive (the human's own sessions included), conformance findings, review comments, failed CI runs, denied commands, caps reached, runs that changed nothing, and everything the human had to do.
- **Lasm** (`lasm`). Lasm bugs and possible improvements found this milestone, each with a minimal reproduction, from the upstream tracker and from what the libraries had to do to work with Lasm. A `blocking` Lasm issue gets this review straight away, for that issue only.
- **Recommendations.** Each review files one report on the milestone issue, and one issue per recommendation on `inkproof-coordination`, labelled `recommendation`, `review:<kind>` and `needs-human`. The human gets one notice per report, not per recommendation.
  - **The human's decision.** The human closes each recommendation: accepted as it is, or rejected by first adding `declined` and a comment saying why (Forgejo records no reason for closing an issue).
  - **The coordinator.** It may adopt a recommendation within its own remit (a brief, a request, moving shared code under ARCH-1, its own files), saying why it is its to decide. It never declines one. It closes each recommendation it acts on, the human's included, once the work it filed for it has closed (a `Blocked-by:` line on each request).
  - **Reviewers.** They read earlier decisions, and don't raise a rejected recommendation again without new evidence. They propose text in the issue, and never edit policy.
  - **What an accepted recommendation becomes.** An accepted architecture or Lasm recommendation becomes a brief through the coordinator, or upstream work for the human. An accepted process recommendation is a change the human makes to the kit or to `CLAUDE.md`.

## Dependencies we don't control

- **The upstream tracker.** When an agent finds a bug or limitation in a dependency outside the `inkproof-` repositories (Lasm, Lean, Mathlib or any other), it doesn't work around it. It files an issue on `inkproof-coordination` labelled `upstream` and `upstream:<project>`, with a reproduction. If it can't continue, it marks its own task `blocked` with a `Blocked-by:` line naming that issue.
- **Lasm's missing bindings don't block.** We don't control Lasm, so a browser API it has no typed binding for is bound in Lean by the library that needs it (Lasm.JS's `@[js]` attribute, or `Value.get` and `callMethod`). A shim is used only for what Lean can't express, such as a class. The upstream issue stays as a request for Lasm.
- **The tracker is our record.** The human takes upstream issues to their projects when they choose: for Lasm, once something running on it shows why they matter. Until then they stay open, and nothing in the kit reports them.
- **Blocking.** An open upstream issue that an open item's `Blocked-by:` line names waits on the human. The dispatcher keeps its `blocking` label in step with those lines, which assigns it to the human and notifies them; agents never set the label.
- **Unblocking.** Nothing polls GitHub, so an upstream fix can't wake blocked agents by itself. The human closes the upstream issue, or comments on it, once a fix is available, and that is the trigger.
- **Fetching.** Third-party Lean packages are fetched by Lake from GitHub through the proxies, as git reads. Lasm is an npm package (`@lasm/cli`), which repositories pin in `test/package.json` and fetch from npm, also through the proxies; its browser bindings (`Lasm.Web`, `Lasm.UI`, `Lasm.JS`) come with it, not from Lake. None of these is in Forgejo.
- **Browsers.** The agent and CI images include headless Chromium for browser tests, installed by Playwright when the images are built, at one pinned version (`PLAYWRIGHT_VERSION`, 1.64.1). Neither agents nor CI can download browsers, since no proxy allows Playwright's download host, so a repository pins `playwright` at exactly that version, and an upgrade changes the images and the pins together. Its WebGL is software, which is enough for functional tests, not for golden images across real GPUs (VAL-7).

## Agents' command line

Agents get `tea` for issues, labels, comments and pull requests, and a kit command, `forge`, for what `tea` lacks:

- `forge pr automerge <n>`: merge now if nothing stands in the way, otherwise schedule "merge when checks succeed" (see Forgejo behaviour);
- `forge pr merge <n>`: merge now, or say why Forgejo won't;
- `forge ci <repo> <pr|main>`: the Actions run's result for the pull request's head commit, or for `main`'s. With `--log`, it adds the log of each failed job in that run (the last 200 lines, or `--log all`), so an agent can find out why CI failed without the human. With `--rerun`, it runs a failed run again, in the agent's own repository, for a failure that is the runner's rather than the code's;
- `forge pr approve <repo> <n>` (coordinator): approve, but only if the Actions run for the head commit succeeded;
- `forge review <repo> <n> <findings>` (`conformance`): approve or request changes from structured findings, only once CI has passed. After the rounds limit, it labels the pull request `needs-human` instead for the findings the author disputes, once per head;
- `forge reviews <repo> <n>`: a pull request's reviews and their findings, which `tea` doesn't show;
- `forge ledger [days]` (`process`): the run ledger;
- `forge version`, in a checkout: the next release's version, from `API.lock` in the working tree against the highest `vX.Y.Z` tag, by the gatekeeper's rule (Releases), so an owner heads its `CHANGELOG.md` entry with the version the release will check for. `test_forge.py` holds it to the gatekeeper's own function;
- `forge release <repo> '<note>' <issue>...` (coordinator, level 3): ask the gatekeeper to release.

Managed settings deny these, with every alias:
- `tea api`, `tea login`, and `tea`'s release and approval commands;
- `curl` and `wget`;
- `git config`;
- force pushes.

`tea api` is the agent's whole account, including posting a commit status under CI's name, which is the spoof an early trial demonstrated. Denying it and offering `forge` keeps runs to reviewed operations, and puts the rule "approve only after checking the Actions run" in code instead of a prompt. The deny list can be got round with a script; the real boundary is the account's permissions and branch protection, and the list is defence in depth.

WebFetch is allowed, so agents can look for existing libraries before building (the protocol's "Reuse before you build"). It goes through the agents' proxy, so it reaches only the allowlist (GitHub's pages, and Reservoir and Loogle on `lean-lang.org`).

Denying `curl` doesn't stop a repository's own scripts from downloading, as a build does when it fetches its packages. The agent runs them exactly as someone building the repository from GitHub would. That is the build working as intended, not a way round the denial, and the allowlist still limits what they reach.

## GitHub mirror

- **Migration.** `scripts/migrate.sh` uses Forgejo's migration to import each repository from GitHub once, with history, issues, labels and pull requests, using a GitHub token of the human's own. Imported content is posted by Ghost.
- **Mirrors.** After import, each repository push-mirrors to GitHub on every commit, with a credential of the human's own: contents and workflows write on the `inkproof-` repositories, and no machine user. GitHub refuses a push that adds or changes a file in `.github/workflows/` without workflows write, and a site's Pages workflow is one.
  - Mirrors carry `main` (`MIRROR_BRANCHES`) and every tag. Issues and pull requests live only in Forgejo, which is why Forgejo itself is backed up.
  - Forgejo pushes with `git push --mirror`, so branches and tags on GitHub outside that set are deleted at the first sync. `refs/pull/*` is never pushed.
  - Git ignores Forgejo's `[proxy]` setting, so Forgejo's container sets the standard proxy variables for it.
- **New libraries** start in Forgejo from a seed (`scripts/new-library.sh`), and the push mirror fills an empty GitHub repository; there is no import.
- **Dependencies.** Agents and CI fetch `inkproof-` dependencies from Forgejo, while `lakefile` and `lake-manifest.json` keep their GitHub URLs. Third-party packages are unaffected, and still come from GitHub.
  - There is one `url."http://forgejo:3000/<FORGE_ORG>/inkproof-".insteadOf` value for each GitHub owner the repositories have lived under (`GH_OWNER` and `GH_FORMER_OWNERS`), because old tags' lakefiles name the former owner.
  - The values are set in the root-owned system git config, and in CI, which reads the owners from the organisation's `DEPS_GITHUB_OWNERS` Actions variable.
  - Each value is scoped to `https://github.com/<owner>/inkproof-`. A rule for all of `https://github.com/` would send Mathlib, Batteries, Plausible and Lasm to Forgejo, where they don't exist.

### Sites

A site is an application whose pages are public: `inkproof-demos`. It has kind `site` in `agents.conf` and is otherwise a library, with the same accounts, teams, autonomy levels and releases. Each release tag is deployed to GitHub Pages.

- **GitHub deploys, not the kit.** The mirror carries the tag to GitHub, where the site's `.github/workflows/pages.yml` builds and deploys it. Nothing in the kit gains a GitHub credential or an allowlist entry. Forgejo ignores the workflow, because it reads `.github/workflows/` only when `.forgejo/workflows/` doesn't exist.
- **The deployment is a Pages artifact, not a branch.** Mirroring deletes any branch on GitHub that Forgejo doesn't have, so a `gh-pages` branch would be deleted at the next sync.
- **The workflow is the human's, and the build is the agent's.** The workflow comes from `templates/site/`: `new-library.sh` seeds it and `setup-repo.sh` checks it, and `.github/` is code-owned by the human. It runs the repository's `scripts/build-site.sh`, which builds the whole site into `_site/`.
  - Only the build job runs that script, with `contents: read` and nothing else.
  - The deploy job, which holds `pages: write`, runs only GitHub's `deploy-pages` action.
  - The repository's code therefore runs on GitHub's runners, but it can't push, tag or change settings there.
- **CI builds the site first.** `checks.yml` runs `scripts/build-site.sh` wherever it exists, so a site that doesn't build fails its pull request rather than its deployment.
- **Only tags deploy.** GitHub's `github-pages` environment allows only the default branch until the human adds `v*` (RUNBOOK.md, Adding a site), so each deployed page is a released version.

## Backups

`scripts/backup.sh` runs daily from a host launchd job. It makes:
- a `forgejo dump`;
- a copy of the dispatcher's ledger;
- a copy of the run archive.

They go into `BACKUP_DIR` (default `backups/`, outside every volume), which keeps the last `BACKUP_KEEP` (default 7). Time Machine covers the kit's folder and keeps their history, so `BACKUP_KEEP` only bounds the disk they take.

Bind-mounting the live volumes for Time Machine was rejected, for three reasons:
- SQLite over Docker Desktop's file sharing is slow and has a history of locking trouble;
- a crash-consistent copy is never checked;
- a dump is portable to another machine or a later Forgejo.

- **The dump.** `forgejo dump` holds the database twice: as SQL, written consistently, and as a raw copy of the live SQLite file, which may be mid-write. A restore rebuilds the database from the SQL. It leaves out queues, indexes and temporary files, which Forgejo rebuilds. The ledger is copied with SQLite's online backup. Backups are readable by the human only, because they hold tokens' hashes, Forgejo's own secrets and transcripts.
- **What "compares" means.** `scripts/backup.sh` records a snapshot of Forgejo (`kit snapshot`) just before and just after the dump. A snapshot is every repository, every branch and tag with its commit, and every issue and pull request with its title, state and comment count. `scripts/restore-test.sh` takes the same snapshot of the restored copy. Everything that stood still between the two live snapshots must match exactly; anything that changed during the backup is skipped and counted.
- **The throwaway copy can reach nothing.** It is on an internal network, with mirrors, webhooks and Actions off, so it can't push the backup's old refs to GitHub. Its own port, `RESTORE_TEST_PORT` (3002), is for the human to look at it.
- **Restoring for real.** `scripts/restore.sh` asks for confirmation, keeps a raw copy of the current Forgejo data, and restores into fresh volumes. It needs the kit's own `secrets/`, because Forgejo keeps only the tokens' hashes.

## Invariants

The kit is working when these hold.

- **End to end.** On a level-3 library, a brief filed by the coordinator goes through every step with no human action: implemented, reviewed for conformance, approved, merged, released, picked up by its consumer and marked `verified`. Every step shows in Forgejo, and each run's log and conversation open in VS Code.
- **Milestones.** At a milestone's end, the three reviews report with recommendations the human can accept or reject one by one, and the next milestone's reviews respect those decisions.
- **Consulting.** The human can talk to any agent, and open any finished or interrupted run in the extension and ask about it, while that agent's runs go on. Their session changes no checkout and, in Forgejo, only files issues and comments.
- **Failing safely.** Each of these fails safely:
  - an `API.lock` change without the required approval doesn't merge;
  - a status posted under CI's name doesn't get a broken build approved or merged;
  - a pull request with an unresolved `CLAUDE.md` finding doesn't merge;
  - an agent's attempt to create a tag or release is refused;
  - an issue or comment from an untrusted account starts no run;
  - a burst of comments between two agents, or between an author and `conformance`, stays within its bound and ends;
  - while paused, nothing starts, merges or releases.
- **Off GitHub.** The proxies' logs show no GitHub API calls. GitHub receives only:
  - mirror pushes;
  - unauthenticated git reads;
  - downloads of Lean toolchains and third-party packages.

  Every repository's `main` and tags arrive there.
- **Recovery.** Forgejo restored from a backup into a fresh volume has its issues, pull requests and history intact.

## Decisions

- **Each agent has its own account.** So agents approve each other's pull requests, and authorship identifies the writer. Comments don't carry a `From:` line, which could be forged. Comments that need no reply start with `(no reply needed)`.
- **The dispatcher uses webhooks, not polling.** Nothing polls GitHub. The dispatcher reconciles against Forgejo, which is local, to catch missed deliveries.
- **Each run is a fresh headless session.** Each run is a new `claude -p` conversation, logged, and listed in the extension, where the human can open it and ask about it. There's no long-lived terminal session to attach to.
- **Approvals are the merge gate, not required statuses.** Any account with push access can post a commit status under CI's name.
- **Protected files aren't used.** In the trial they blocked every merge. `CODEOWNERS` does the job instead.
- **Autonomy levels are per repository.** So the human can raise one library at a time, and fall back.
- **The gatekeeper approves on CI from level 2.** So no Claude run is spent per pull request on checking CI.
- **Notifications are Forgejo mentions plus a macOS notifier.** SMTP is optional.
- **Each reviewer is its own agent and account.**
- **The process review may read the human's own sessions**, through the run archive.
- **The human's sessions consult; they don't take over.** Runs once waited while the human was active in a container, and the human could continue a run themselves. In practice taking over wasn't used, the waiting held the kit up, and a run already under way shared its checkout with the human's session, which changed it beneath the run (inkproof-web#95's follow-up, 2026-11-22). So runs no longer wait, and the human's sessions change nothing but issues and comments.
- **Lasm stays outside Forgejo**, fetched from GitHub. Its issues go through the upstream tracker, and the human unblocks.

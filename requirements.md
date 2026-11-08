From: Claude (building inkproof-forge)
Building against: a new repository, `inkproof-forge`. `tentenths/inkproof-agents` is reference only.
Priority: high

## Use case

The Inkproof agents keep working as they did on GitHub, but on the human's own machine. A local Forgejo is the canonical forge, GitHub is a push mirror that nothing polls, and the human reaches agents through VS Code and the Claude extension. Every requirement in `inkproof-agents/yolo.md` still applies, translated from GitHub to Forgejo: agents start themselves, runs are bounded, the human can pause, autonomy levels 1 to 3 hold, `API.lock` is gated, releases are computed and checked, escalation reaches the human, and milestone review shows every `API.lock` change once.

The kit also adds automated reviews: a check of every pull request against `CLAUDE.md` by an agent other than its author, and architecture, process and Lasm reviews at the end of each milestone, which make recommendations for the human to decide.

This document fixes the components, the trust boundaries, how each autonomy level maps to Forgejo settings, and how we'll know it works. Decisions taken in review are recorded at the end.

## What changes from the existing kit

- **No shared identity.** Each agent has its own Forgejo account, so agents approve each other's pull requests and authorship identifies the writer. Comments no longer start with `From: <agent>`; authorship replaces it, and unlike a `From:` line it can't be forged.
- **Events, not polling.** A dispatcher receives Forgejo's webhooks. Nothing polls GitHub; the dispatcher reconciles against Forgejo, which is local, to catch missed deliveries.
- **Fresh headless runs.** Each run is a new `claude -p` conversation, logged to a Markdown file and resumable in the extension. There is no tmux and no attach script.
- **Approvals are the merge gate.** Any account with push access can post a commit status with CI's name, so required statuses are not a gate on their own. A merge needs an approval from an account that checked the Actions run.
- **Reviews.** Every pull request needs the conformance reviewer's approval, and each milestone ends with three reviews whose recommendations come to the human.

## Components

| Component | Runs as | Holds | Reaches |
| --- | --- | --- | --- |
| `forgejo` (Forgejo 17) | compose service, `127.0.0.1:3000` on the host | repositories, issues, pull requests; the human's GitHub mirror credential | agents' network; GitHub only through `forge-proxy`, for mirror pushes |
| `ci-runner` + `ci-docker` (`docker:dind`) | compose services | runner registration | Forgejo; the outside only through `proxy` |
| `proxy` (squid) | compose service | agent and CI allowlists | the internet |
| `forge-proxy` (squid) | compose service | Forgejo's allowlist: `github.com` only | GitHub, for git pushes |
| `dispatcher` | compose service, status on `127.0.0.1:3001` | dispatcher account token, webhook secret, per-agent runner keys; the run ledger, queues and caps | Forgejo |
| one container per agent: the coordinator, each library, each reviewer | compose service | that agent's Forgejo token and runner key | Forgejo, dispatcher, `proxy`; the coordinator also reaches `gatekeeper` |
| `agent-runner` (inside each agent container) | the agent's user | nothing beyond the container's | dispatcher (claims work, reports runs); writes the run archive |
| `run-archive` volume | | every agent's run logs and transcripts, the human's sessions included | read-only to the process reviewer |
| `gatekeeper` (levels 2 and 3) | compose service | gatekeeper account token, which no agent can read | Forgejo; answers only the coordinator |
| host scripts | the human, on macOS | the human's Forgejo admin token (`secrets/human/token`) | Forgejo and dispatcher on `127.0.0.1` |

Networks keep these apart: library agents share no network with `gatekeeper`; `gatekeeper` reaches Forgejo through a network no agent joins; `ci-docker` has no route out except `proxy`. The gatekeeper also checks a key that only the coordinator's container has.

Host scripts are bash 3.2 and run on Docker Desktop. Services and the agent runner are Python 3 standard library. Images bake their scripts in with `COPY --chmod`, and mounted scripts are run with `bash` or `sh`, so lost executable bits don't matter. No container relies on jq 1.6 features that it rejects (`end`, `label` as keys).

## Accounts and permissions

- **Accounts.** The human (Forgejo admin, `HUMAN_USER`); the coordinator and one per library (`coordinator`, `core`, `scale`, `consumer-metrics`); one per review (`conformance`, `architecture`, `process`, `lasm`); `gatekeeper`; `dispatcher`. Registration is disabled, and `scripts/bootstrap.sh` creates the kit's accounts and tokens. The suspended GitHub account `inkproof-tentenths` is never used or recreated.
- **Organisation and teams.** Repositories stay under `tentenths` with their GitHub names. Each library agent and the coordinator get two teams with per-unit access:
  - its own repository: Code, Issues and Pull requests write; Releases, Actions and Wiki read; Packages none;
  - its dependencies (all libraries, for the coordinator): Code read, Issues write, Pull requests read. The coordinator's has Pull requests write, so it can review and approve.
- **Reviewers own no repository.** `conformance` has Code read and Pull requests write on every `inkproof-` repository, so it can review. `architecture`, `process` and `lasm` have Code read everywhere and Issues write on `inkproof-coordination` only. No reviewer can push.
- **Agents never administer.** No agent account is a repository or organisation admin, so none can change branch protection, tag protection, webhooks, mirrors or workflows' settings. `.forgejo/` and `CLAUDE.md` are code-owned by the human.
- **Trusted authors** are the kit's accounts. Content migrated from GitHub (it carries an original author) is never trusted. Comments from `dispatcher` and `gatekeeper` always mark themselves as needing no reply.
- **Tokens.** One Forgejo token per account, in `secrets/<account>/token`, `chmod 600`. Each agent container mounts only its own; `dispatcher` and `gatekeeper` tokens are mounted only into those services.

## Trust boundaries

| Rule | Mechanism |
| --- | --- |
| Agent writes only its own repository | Team permissions: write on its own repository, read and issue write elsewhere |
| No force pushes, deleting `main`, or direct pushes to `main` | `main` branch protection: no pushers, merges through pull requests only |
| Nothing merges without someone checking CI | Approvers confirm the Actions run (`/repos/{owner}/{repo}/actions/runs`) for the head commit, never commit statuses |
| Nothing merges that deviates from `CLAUDE.md` unless the human agrees | `conformance` is code owner of every path, so its review is requested and blocks; "block on rejected reviews" holds its findings |
| Agents can't change what they're checked against | `CLAUDE.md` code-owned by the human; the reviewer checks against the base branch's copy |
| No agent creates a tag or release | Protected tags `*`: the human, and at level 3 `gatekeeper`; Releases read-only for agent teams |
| At level 3, releases only through the gatekeeper's computed version and `CHANGELOG.md` check | Only `gatekeeper` may push tags; its token is only in its container; it answers only the coordinator |
| `API.lock` needs the human (levels 1, 2) or the coordinator (level 3) | `CODEOWNERS` plus "block on official review requests", with the code owner on the approvals list |
| `.forgejo/`, `lean-toolchain`, `REQUIREMENTS.md`, `CLAUDE.md` need the human | `CODEOWNERS`, at every level |
| Agents can't weaken CI | Workflows in `.forgejo/workflows/` are code-owned by the human; CI jobs run in `ci-docker`, never the host's Docker |
| Every API change still reaches the human once | `scripts/milestone-diff.sh`, against Forgejo |
| Recommendations change nothing until the human decides | Reviewers write only issues on `inkproof-coordination`; none can push, and policy files are read-only to every agent |
| Untrusted issues can't steer agents | Dispatcher and Stop hook filter on authorship; trigger descriptions carry only numbers and names, never issue text |
| No internet except essentials | Internal networks; `proxy` allowlist is the only exit for agents and CI, `forge-proxy` for Forgejo |
| Nothing in the kit calls GitHub's API | No GitHub credential in any container; `api.github.com` is on no allowlist |
| Agents can't loosen their own rules | Managed settings, managed `CLAUDE.md`, git config and hooks are root-owned in the image or mounted read-only |
| Caps and the run record are outside the agent's reach | The dispatcher keeps the ledger and refuses claims over a cap or while paused; agents can't write the run archive except their own runner's entries |
| Only the human pauses and resumes | `control/` is mounted read-only; pause also restricts who may merge |

## Autonomy levels in Forgejo

`scripts/setup-repo.sh <repo> <level>` applies these, using the human's token, and re-running it with a lower level falls back. `CODEOWNERS` lives in the repository, so the script checks it and says what to change.

| Setting on `main` | Level 1 | Level 2 | Level 3 |
| --- | --- | --- | --- |
| Required approvals | 1 | 1 | 1 |
| Approvals list | human, `conformance` | human, `gatekeeper`, `conformance` | human, `gatekeeper`, `conformance`, `coordinator` |
| `CODEOWNERS` for every path | human, `conformance` | `gatekeeper`, `conformance` | `gatekeeper`, `conformance` |
| `CODEOWNERS` for `API.lock` | human | human | `coordinator` |
| An ordinary pull request merges once approved by | human and `conformance` | `gatekeeper`, after the Actions run for the head commit succeeded, and `conformance` | as level 2 |
| Required status checks | `checks*` (a convenience, not the gate) | same | same |
| Always on | block on official review requests, on rejected reviews and on an outdated branch; dismiss stale approvals | | |
| Protected tags `*` | human | human | human, `gatekeeper` |
| Who releases | human (`scripts/release.sh`) | human | coordinator, through `gatekeeper` |

Every code owner's review request blocks the merge until that owner approves, so the single required approval is a floor, not the gate. Agents schedule "merge when checks succeed" on their own pull requests at every level; it waits for the approvals the level requires. Protected files are not used: in the trial they blocked every merge.

## Runs

- **Hand-off.** The dispatcher queues triggers per agent. Each container's `agent-runner` claims a batch over HTTP with its runner key; the dispatcher grants a claim only when the agent has no run, is under its caps, and the kit is not paused. The runner reports the run's start and end, and the dispatcher records it.
- **Starting.** The runner starts a run only when no human session is active. It runs `claude -p --permission-mode dontAsk --output-format stream-json --name "<agent>: <triggers>" --session-id <uuid>` in the agent's workspace, with a prompt listing the triggers as pointers and saying that no one can approve anything for it.
- **Logging.** The stream becomes a Markdown log under `.inkproof/runs/` in the workspace, plus `current.md`, which updates live in a VS Code tab. `.inkproof/` is excluded from git.
- **Ending.** When a run ends, the runner records its outcome, then rewrites `"entrypoint":"sdk-cli"` to `"entrypoint":"claude-vscode"` in the run's transcript. If the rewrite matched nothing, the run record says so and the human is notified; the run can still be resumed by ID.
- **The record.** For each run the ledger keeps the trigger, start, end, outcome, cost, turns, denied commands, session ID, log path, whether it shows in the extension, and whether it changed anything in Forgejo (from the webhooks its account caused). It also records the human's sessions in each container: start, end and session ID.
- **Retention.** The runner copies each run's log and transcript, and each of the human's session transcripts, to `run-archive`, kept for at least two milestones. Managed settings raise `cleanupPeriodDays` above the default 30, so Claude Code doesn't delete transcripts that runs and take-overs still need.
- **Bounds.** At most one run per agent. Configurable caps per agent per day on runs (default 20) and optionally on spend (from the stream's reported cost), and a maximum run length (default 240 minutes). A burst of triggers within a debounce window (default 60 seconds) becomes one run. A run starts within a configurable delay of its event, at most 15 minutes by default.
- **Interrupting.** `scripts/stop-run.sh <agent>` interrupts a run, which is then recorded and rewritten like any other, so the human can continue it.

## Stepping aside for the human

- Managed hooks record human activity in the agent's container whenever `CLAUDE_CODE_ENTRYPOINT` is `claude-vscode` or `cli`: busy from a prompt until Claude stops, then a grace period (default 10 minutes). Runs, whose entrypoint is `sdk-cli`, don't count. Extension processes are ignored.
- While the human is active, the runner starts nothing and pending work waits. A run already under way when the human arrives continues, unless the human interrupts it.
- A busy mark with no stop is treated as stale after the maximum run length, so an interrupted session can't block runs for ever.

## The human's interface

- **VS Code.** `scripts/up.sh` builds and starts everything; Dev Containers never builds an image. `scripts/open.sh <agent>` opens VS Code attached to that agent's running container at its workspace. Managed settings and hooks apply to the human's sessions there too. Host credential, git config and SSH agent forwarding are turned off.
- **Forgejo web UI** at `http://localhost:3000` for issues, pull requests, reviews, Actions and recommendations.
- **First-run prompts.** `agent-init` marks each agent's folder as trusted in `.claude.json`. The managed `CLAUDE.md` is generated as one file (role followed by protocol), so there is no external include to approve. If the trust key proves unreliable, setup lists accepting the prompt once per agent as a step.
- **Status.** `scripts/status.sh` shows, without touching GitHub: each agent's state (idle, running, human active, paused, capped), its pending triggers, and recent runs with start, trigger, outcome, cost, session ID, log path and whether it shows in the extension; then open requests, pull requests, recommendations and `needs-human` items across the `inkproof-` repositories.
- **Pause and resume.** `scripts/pause.sh` writes `control/paused`, cancels scheduled merges (recording them), and narrows the merge list on every protected branch to the human. The dispatcher grants no claims, the gatekeeper approves and releases nothing, and a managed hook refuses every tool call in a run, so a running run stops at its next step. `scripts/resume.sh` restores the merge lists and reschedules the recorded merges in the human's name. While paused, nothing changes in Forgejo except by the human.

## Triggers

From `watch-work`, now driven by webhooks. Only trusted authors count; an agent's own actions and comments whose first line is `(no reply needed)` start nothing.

- **Every library agent and the coordinator, on its own repository:** a request opened, or labelled `request` and `new`; a comment or review on one of its open issues or pull requests; a failed Actions run on one of its pull requests; one of its pull requests falling behind `main`; a release of a repository named in a `Blocked-by: <owner>/<repo>#<n>` line on one of its `blocked` issues.
- **The coordinator, across the `inkproof-` repositories:** a request filed; a pull request opened or merged; `ready-for-release` added; a release published; `needs-human` added or removed; a review requested from it.
- **`conformance`:** a pull request's Actions run succeeding on a head commit it hasn't reviewed, once no new commit has arrived for a settling period (default 10 minutes). Pull requests whose title marks them as work in progress are skipped.
- **`architecture`, `process` and `lasm`:** `milestone-review` added to a milestone issue on `inkproof-coordination`.
- **`lasm`, also:** `blocking` added to an `upstream:lasm` issue.
- **Reconciliation.** On start, and every 15 minutes, the dispatcher checks Forgejo for untriaged requests, failed and unreviewed runs, and pull requests behind `main`, so a missed delivery delays work rather than losing it. If Forgejo sends no event for a finished Actions run, this check is how runs are found, at a shorter interval.

## Reviews

### Conformance, on every pull request

- **What.** `conformance` checks the pull request's diff against the base branch's `CLAUDE.md` and the protocol, and reports each deviation with the section it breaks. It reviews craft against stated rules only; scope stays with the coordinator.
- **Outcome.** It approves, or requests changes with its findings. A request for changes wakes the author through the review trigger, and blocks the merge until `conformance` approves a later head.
- **Answering findings.** The author fixes each finding or replies with its reason. `conformance` accepts a reason or keeps the finding. After a configurable number of rounds on one pull request (default 3), it labels the pull request `needs-human` instead of reviewing again, and the human decides.
- **For the process review.** Each finding is recorded with its repository, pull request, section and whether it was fixed, disputed or escalated, so repeated deviations can be found.

### At the end of each milestone

- **The milestone.** A Forgejo milestone on `inkproof-coordination` gives each milestone its start and end. When its work is done, the coordinator (or the human, with `scripts/milestone.sh`) opens a milestone issue and adds `milestone-review`. The three reviews run before the human accepts the milestone, and feed that decision. The `API.lock` diff from `scripts/milestone-diff.sh` is part of the milestone issue.
- **Architecture** (`architecture`). Are boundaries between libraries in the right place; is there too much coupling between libraries or too little cohesion within one; are libraries working around limitations in dependencies, including ones we don't control, and should those be fixed instead. It reads every repository, the milestone's `API.lock` diff, requests and how they were resolved, and the upstream tracker.
- **Process** (`process`). How agents work: the protocol, roles, `CLAUDE.md` and prompts. It proposes changes that stop a mistake caught in review from happening again, or remove work that isn't needed, by agents or by the human. It reads the run ledger, the run archive (the human's own sessions included), conformance findings, review comments, failed CI runs, denied commands, caps reached, runs that changed nothing, and everything the human had to do.
- **Lasm** (`lasm`). Lasm bugs and possible improvements found this milestone, each with a minimal reproduction, from the upstream tracker and from what the libraries had to do to work with Lasm. A `blocking` Lasm issue gets this review straight away, for that issue only.
- **Recommendations.** Each review files one report on the milestone issue, and one issue per recommendation on `inkproof-coordination`, labelled `recommendation`, `review:<kind>` and `needs-human`. The human closes each as completed (accepted) or as not planned with a reason. Reviewers read earlier decisions and don't raise a rejected recommendation again without new evidence. An accepted architecture or Lasm recommendation becomes a brief through the coordinator, or upstream work for the human; an accepted process recommendation is a change the human makes to the kit or to `CLAUDE.md`. Reviewers propose text in the issue; they never edit policy.
- **Notification.** The human is notified once per report, not per recommendation.

## Dependencies we don't control

- **The upstream tracker.** When an agent finds a bug or limitation in a dependency outside the `inkproof-` repositories (Lasm, Lean, Mathlib or any other), it doesn't work around it. It files an issue on `inkproof-coordination` labelled `upstream` and `upstream:<project>`, with a reproduction, and marks its own task `blocked` with a `Blocked-by:` line naming that issue if it can't continue. A blocker also gets `blocking`, which notifies the human.
- **Unblocking.** Nothing polls GitHub, so an upstream fix can't wake blocked agents by itself. The human closes the upstream issue, or comments on it, once a fix is available; that is the trigger.
- **Fetching.** Lasm and other third-party packages are fetched by Lake from GitHub through `proxy`, as git reads; they are not in Forgejo.

## Agents' command line

Agents get `tea` for issues, labels, comments and pull requests, and a kit command, `forge`, for what `tea` lacks:

- `forge pr automerge <n>`: schedule "merge when checks succeed";
- `forge pr update <n>`: bring a pull request up to date with `main`;
- `forge ci <repo> <pr>`: the Actions run's result for the pull request's head commit;
- `forge pr approve <repo> <n>` (coordinator, `conformance`): approve only if the Actions run for the head commit succeeded;
- `forge release <repo> '<note>' <issue>...` (coordinator, level 3): ask the gatekeeper to release.

`tea api`, `tea login`, `tea` release commands and `tea` approval commands are denied in managed settings, with every alias, as are `curl`, `wget`, `git tag`, `git config` and force pushes. `tea api` is the agent's whole account, including posting a commit status with CI's name, which is the spoof the trial demonstrated. Denying it and offering `forge` keeps runs to reviewed operations, and puts the rule "approve only after checking the Actions run" in code instead of a prompt. The deny list can be got round with a script, as before; the boundary is the account's permissions and branch protection, and the list is defence in depth.

## Notifications

Forgejo has no email without SMTP. The dispatcher records a notice for every `needs-human` label added (a milestone's recommendations are grouped into one notice per report), every reached cap, every `blocking` upstream issue, and every run whose transcript rewrite failed, and nothing else routine. For each, it comments on the issue as `dispatcher`, mentioning the human, so the notice shows in Forgejo's notifications; caps go on one `needs-human` issue per agent, reused until the human closes it. A host launchd job, `scripts/notify.sh`, reads new notices from the dispatcher on `127.0.0.1` and shows each as a macOS notification. SMTP with the human's own mail account is optional, for being told while away from the machine.

## GitHub mirror and migration

- **Migration.** `scripts/migrate.sh` uses Forgejo's migration to import each repository from GitHub with history, issues, labels and pull requests, using a GitHub token of the human's own, once. Content written by the suspended account doesn't import and isn't recreated.
- **Mirrors.** After import, each repository push-mirrors to GitHub on every commit, with a credential of the human's own (contents write on the `inkproof-` repositories). No machine user. Mirrors carry code and tags; issues and pull requests live only in Forgejo, which is why Forgejo itself is backed up.
- **Dependencies.** Agents and CI fetch `inkproof-` dependencies from Forgejo with `url."http://forgejo:3000/tentenths/inkproof-".insteadOf "https://github.com/tentenths/inkproof-"`, set in the root-owned system git config and in CI, while `lakefile` and `lake-manifest.json` keep their GitHub URLs. Third-party packages are unaffected and still come from GitHub.

## Backups

`scripts/backup.sh` runs `forgejo dump` daily from a host launchd job, plus copies of the dispatcher's ledger and the run archive, into `BACKUP_DIR` (default `backups/`, outside every volume), keeping the last N. `scripts/restore.sh` restores a dump into a fresh volume. `scripts/restore-test.sh` restores the latest dump into a throwaway compose project on another port and compares repositories, refs, issues, pull requests and comments with the live instance.

## Repository layout

```
compose.yaml, .env.example, README.md, requirements.md
docker/forgejo/      app.ini overrides
docker/ci/           runner config (container.network: host, Lean volume)
docker/proxy/        squid configs and allowlists: agents, CI, Forgejo
docker/agent/        image, PROTOCOL.md, managed-settings.json, hooks, agent-init,
                     agent-runner, forge, git credential helper, gitconfig
docker/dispatcher/   dispatcher.py
docker/gatekeeper/   gatekeeper.py
roles/               one role per agent, reviewers included
templates/repo/      CLAUDE.md, .forgejo/workflows/checks.yml, .forgejo/CODEOWNERS,
                     request issue template, scripts/check-pins.sh
scripts/             up, down, bootstrap, migrate, mirrors, setup-repo, open, status,
                     pause, resume, stop-run, release, milestone, notify, backup,
                     restore, restore-test, milestone-diff
launchd/             plists for notify and backup
secrets/, control/, backups/   not committed
```

## Acceptance criteria

Each stage is shown working before the next starts. The brief's seven stages gain one for reviews (stage 5); later stages move down by one.

**Stage 1: forge, CI, proxy and bootstrap.**

- `scripts/up.sh` starts Forgejo on `127.0.0.1:3000` with registration disabled, and `scripts/bootstrap.sh` creates the organisation, teams, the kit's accounts and their tokens, and the dispatcher's organisation webhook. Re-running it changes nothing.
- A test repository's workflow in `.forgejo/workflows/` runs `lake build` in `ci-docker`, reusing Lean from a named volume, with no route out except `proxy`.
- From an agent container, an allowed domain is reachable through `proxy`, `api.github.com` and an unlisted domain are refused, and Forgejo is reachable directly.
- Whether Forgejo 17 sends a webhook for a finished Actions run, failed or successful, is answered.

**Stage 2: migration and mirrors.**

- The four repositories import with history, issues, labels and pull requests, under their GitHub names.
- Each push-mirrors to GitHub on commit; a push to `main` and a tag arrive there, and `forge-proxy`'s log shows only git traffic to `github.com`.
- A consumer fetches an `inkproof-` dependency from Forgejo, and a third-party one from GitHub, and its manifest keeps the GitHub URLs.

**Stage 3: one library agent at level 1.**

- `core` runs at level 1. A trusted request starts a run; the run opens a pull request as `core` and schedules its merge, which waits for the human's approval.
- The live log updates in a VS Code tab; afterwards the run is listed in the extension and continues from its history. An interrupted run does too.
- While the human is active in `core`'s container, no run starts; work waits and starts after the grace period.
- An unattended run never waits on a prompt, and the deny rules hold in `dontAsk`.
- A fresh `claude-agents` volume runs unattended without first-run prompts, or the README lists the step that makes it so.
- The ledger records everything listed under The record, the run archive holds the run's and the human's transcripts, and `cleanupPeriodDays` is raised.
- An agent's change to `CLAUDE.md` waits for the human. An upstream issue with `blocking` reaches the human.

**Stage 4: the dispatcher and the coordinator.**

- Every trigger above starts the right agent, within the configured delay; untrusted authors, an agent's own comments and no-reply comments start nothing.
- Caps hold, are shown in status, and notify the human; a comment burst between two agents stays within the cap and ends.
- Pause and resume behave as described; `scripts/status.sh` shows everything listed under Status.
- `needs-human` produces a macOS notification, and nothing routine does.

**Stage 5: reviews.**

- A pull request with a `CLAUDE.md` deviation gets a request for changes naming the section; its author is woken, fixes it, and the pull request merges only after `conformance` approves the new head.
- A disputed finding ends in `needs-human` after the configured number of rounds, not in an endless exchange.
- `milestone-review` on a milestone issue produces architecture, process and Lasm reports, each recommendation as its own issue, and one notification per report. A recommendation closed as not planned is not raised again at the next milestone without new evidence.
- The process review cites runs from the ledger and the archive, the human's sessions included. A `blocking` Lasm issue gets a Lasm review straight away.

**Stage 6: level 2.**

- `gatekeeper` approves a pull request once its Actions run succeeded, and with `conformance`'s approval it merges itself. A status posted with CI's name on a failing build gets no approval, and it doesn't merge.
- A pull request changing `API.lock` waits for the human even after the gatekeeper's and `conformance`'s approvals.

**Stage 7: level 3 and releases.**

- A pull request changing `API.lock` merges only with the coordinator's approval; a new push dismisses it.
- The coordinator releases through the gatekeeper, which computes the version from the `API.lock` diff since the last tag and refuses without the matching `CHANGELOG.md` entry. The policy text (`PROTOCOL.md`, `roles/`, `templates/repo/`) is updated and proposed for review.
- A level-3 library can depend on a lower-level one and waits for its human-made releases.

**Stage 8: backups.**

- The daily dump runs, old dumps are pruned, and `scripts/restore-test.sh` passes against a fresh volume.

**Every stage.** The README states, for each level, what the human sets up, what runs where and what it costs (reviewer runs included), and which Claude credential unattended runs use and that this use is within its terms. Anything done by hand is a numbered step.

## How we'll know it works

- **End to end.** On a level-3 library, a brief filed by the coordinator is implemented, reviewed for conformance, approved, merged, released, picked up by its consumer and marked `verified`, with no human action. Every step shows in Forgejo, and each run's log and conversation open in VS Code.
- **Milestone.** At a milestone's end, the three reviews report with recommendations the human can accept or reject one by one, and the next milestone's reviews respect those decisions.
- **Taking over.** The human opens any finished or interrupted run in the extension and continues it. While they are active, no run starts for that agent.
- **Negative tests.** Each fails safely:
  - an `API.lock` change without the required approval doesn't merge;
  - a status posted with CI's name doesn't get a broken build approved or merged;
  - a pull request with an unresolved `CLAUDE.md` finding doesn't merge;
  - an agent's attempt to create a tag or release is refused;
  - an issue or comment from an untrusted account starts no run;
  - a burst of comments between two agents, or between an author and `conformance`, stays within its bound and ends;
  - while paused, nothing starts, merges or releases.
- **Off GitHub.** Over a day of activity, the proxies' logs show no GitHub API calls. GitHub receives only mirror pushes, plus unauthenticated git reads and downloads for Lean toolchains and third-party packages; every repository's `main` and tags arrive there.
- **Recovery.** Forgejo restored from a backup into a fresh volume has its issues, pull requests and history intact.

## Check before relying on it

Each is verified in the stage that first needs it, then removed from this list.

- [ ] Forgejo team unit permissions allow Releases read with Code write, and that blocks release creation by API (stage 1).
- [ ] Creating a release through the API with a new tag is subject to tag protection (stage 1).
- [ ] Forgejo sends webhooks for finished Actions runs (stage 1).
- [ ] A push mirror sends branches and tags but not `refs/pull/*`, which GitHub rejects (stage 2).
- [ ] Forgejo's `[proxy]` setting covers push mirrors and leaves webhooks to the dispatcher direct (stage 2).
- [ ] Migrated issues and comments carry an original author the dispatcher can see (stage 2).
- [ ] Setting `hasTrustDialogAccepted` for a folder in `.claude.json` is enough for headless runs and the extension (stage 3).
- [ ] VS Code can attach to a running compose container with a kit-provided attach config, without Dev Containers building anything (stage 3).
- [ ] `cleanupPeriodDays` in managed settings governs transcript deletion for every session in the container (stage 3).
- [ ] An approval from an approvals-list member satisfies the review count and their own official review request (stage 3).
- [ ] Forgejo's `CODEOWNERS` syntax and location (`.forgejo/CODEOWNERS`), and that every matching line requests its owners, so the catch-all owners add to the `API.lock` owner rather than being replaced (stage 3).
- [ ] Every pending official review request blocks the merge, not only the first (stage 3).
- [ ] Narrowing a branch's merge list cancels nothing silently, and scheduled merges can be cancelled and rescheduled by API (stage 4).
- [ ] `PreToolUse` hooks fire in `-p` runs and a deny there ends the run's work promptly (stage 4).

## Decided in review (2026-11-08)

- **Autonomy levels stay**, set per repository, so the human can raise one library at a time and fall back.
- **`insteadOf` is scoped** to `https://github.com/tentenths/inkproof-`. A rule for all of `https://github.com/` would send Mathlib, Batteries, Plausible and Lasm to Forgejo, where they don't exist.
- **`gatekeeper` approves on CI from level 2**, so no Claude run is spent per pull request on checking CI. It arrives with level 2 (stage 6); level 3 (stage 7) adds releases to it.
- **`From:` lines are dropped.** Authorship identifies the writer; comments that need no reply start with `(no reply needed)`.
- **Notifications** are Forgejo mentions plus a macOS notifier on the host, with SMTP optional.
- **Reviews** are added: conformance on every pull request, and architecture, process and Lasm at each milestone's end. Each reviewer is its own agent and account.
- **The process review may read the human's own sessions**, through the run archive.
- **Lasm stays outside Forgejo**, fetched from GitHub; its issues go through the upstream tracker, and the human unblocks.

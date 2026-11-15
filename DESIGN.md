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
| `gatekeeper` | compose service | gatekeeper account token, which no agent can read | Forgejo, by polling; it also answers the coordinator |
| host scripts | the human, on macOS | the human's Forgejo admin token (`secrets/human/token`) | Forgejo and dispatcher on `127.0.0.1` |

### What runs where

- **Forgejo** is on internal networks only. The host reaches it through a port forwarder bound to `127.0.0.1`. It reaches the outside only through `forge-proxy`, which allows `github.com` and nothing else, so `forge-proxy`'s access log is the record of everything it sent out.
- **CI** jobs run in `ci-docker`, a Docker engine of their own, never the host's. That engine is privileged, so jobs must not reach its API: it accepts only TLS connections with a client certificate that only the runner holds. Jobs reach Forgejo directly and the outside only through `ci-proxy`. Lean toolchains are kept in a volume inside `ci-docker`, so each is downloaded once.
- **Workflows** use no `uses:` actions, so CI fetches no code from outside; `checks.yml` checks out with plain git. Jobs fetch this project's repositories with `ci-reader`'s token, the organisation's `DEPS_READ_TOKEN` Actions secret, because a job's own token reads only its own repository. A workflow an agent changes on its branch runs before review, so an agent could read that token, and with it every repository in the organisation. It can't write anything with it.
- **Agents** reach Forgejo and the dispatcher directly, and the outside only through `proxy`. No allowlist includes `api.github.com`. Each container holds only that agent's Forgejo token and runner key, plus the Claude token. Its policy (managed settings, managed `CLAUDE.md`, git configuration and hooks) is root-owned in the image, so the agent can't change it. In unattended runs, anything the managed settings don't allow is refused rather than waiting for a prompt.
- **The gatekeeper** shares one network with Forgejo and another with the coordinator only, so no other agent can reach it. Its Forgejo token is mounted only into its own container. Its account reviews, tags, publishes releases and comments. Branch protection keeps it out of `main`, and tag protection lets it tag only libraries at level 3.
- **The dispatcher** keeps the queues and the run ledger in its own volume. Claiming work needs a runner key, which each agent's container holds for itself only.

## Accounts and permissions

- **Accounts.** They are:
  - the human, as Forgejo admin (`HUMAN_USER`);
  - one per agent in `agents.conf`: the coordinator, each library, and each reviewer (`conformance`, `architecture`, `process`, `lasm`);
  - `gatekeeper`;
  - `dispatcher`;
  - `ci-reader`, which reads every repository in the organisation and writes nothing.

  Registration is disabled, and `scripts/bootstrap.sh` creates the kit's accounts and tokens.

  The GitHub account `inkproof-tentenths`, which the agents shared before the kit, is never used or recreated.
- **Organisation and teams.** The repositories live in the Forgejo organisation `FORGE_ORG`. It can't be the human's username, because Forgejo users and organisations share one namespace. Each library agent and the coordinator get teams with per-unit access:
  - **Its own repository:** Code, Issues and Pull requests write; Releases, Actions and Wiki read; Packages none.
  - **Its dependencies (all libraries, for the coordinator):** Code read, Issues write, Pull requests read. The coordinator's team has Pull requests write, so it can review and approve.
  - **Every repository, for each library agent:** Code, Issues and Pull requests read, so it can reuse before it builds. Forgejo lets anyone who can read a repository open an issue on it, so filing requests only on dependencies is the protocol's rule, not a permission.
- **Reviewers own no repository.**
  - `conformance` has Code read and Pull requests write on every `inkproof-` repository, so it can review.
  - `architecture`, `process` and `lasm` have Code read everywhere, and Issues write on `inkproof-coordination` only.

  No reviewer can push.
- **Agents never administer.** No agent account is a repository or organisation admin, so none can change branch protection, tag protection, webhooks, mirrors or workflow settings. `.forgejo/` and `CLAUDE.md` are code-owned by the human.
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
| `.forgejo/`, `lean-toolchain`, `REQUIREMENTS.md`, `CLAUDE.md` need the human | `CODEOWNERS`, at every level |
| Agents can't weaken CI | Workflows in `.forgejo/workflows/` are code-owned by the human; CI jobs run in `ci-docker`, never the host's Docker |
| Every API change still reaches the human once | `scripts/milestone-diff.sh`, against Forgejo, in each milestone issue |
| Recommendations change nothing until the human decides | Reviewers write only issues on `inkproof-coordination`; none can push, and policy files are read-only to every agent |
| Untrusted issues can't steer agents | Dispatcher and Stop hook filter on authorship, and never trust content with an original (migrated) author; trigger descriptions carry only numbers and names, never issue text |
| No internet except essentials | Internal networks; the proxies' allowlists are the only exits |
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
- **Actions webhooks.** Forgejo 17 sends a webhook for a finished Actions run, whether it failed or succeeded (verified 2026-11-08).

### The gatekeeper

The gatekeeper is a Python program, not a Claude agent, so it costs nothing per pull request.

It polls Forgejo (`GATEKEEPER_POLL_SECONDS`, default 30) rather than taking webhooks. That way nothing needs to reach it except the coordinator, and a lost delivery can't lose an approval.

It acts only:
- in repositories whose `CODEOWNERS` on `main` names `@gatekeeper`, which is how `setup-repo.sh` marks levels 2 and 3;
- on pull requests into `main`.

It approves the head commit when Forgejo's Actions runs for that commit include `checks.yml` and all of them succeeded. It never reads commit statuses. Its approval starts with `(no reply needed)`, so it wakes no one, and while the kit is paused it approves nothing. Its account's team reads code and reviews pull requests on every repository, and can do nothing else. Branch protection dismisses its approval when a new commit arrives, and the gatekeeper approves the new head once that head's run succeeds.

### Releases

At level 3 the gatekeeper also releases, when the coordinator asks with `forge release`. The request comes over a network that only the coordinator and the gatekeeper share, and carries a key that only the coordinator's container holds (`secrets/gatekeeper/coordinator-key`). The gatekeeper refuses while the kit is paused. It also refuses for a repository below level 3, which it recognises by `CODEOWNERS` not giving `API.lock` to `@coordinator`.

The gatekeeper and `scripts/release.sh` run the same code, so a release follows the same steps whoever makes it:
1. It takes the highest `vX.Y.Z` tag as the last release.
2. It computes the next version from the diff of `API.lock`'s lines since then:
   - before 1.0, minor if `API.lock` changed and patch otherwise;
   - from 1.0, major if a line was removed or changed, minor if lines were only added, and patch otherwise.
3. It requires a `CHANGELOG.md` entry headed with that version, in either form the libraries use (`## [X.Y.Z]` or `## vX.Y.Z`), and a successful Actions run on `main`'s head.
4. It publishes the release, which tags `main`'s head.
5. It comments the release on each `ready-for-release` issue named, and removes the label.

With no earlier tag, the first release is v0.1.0. A new major line, such as 1.0, starts with a tag the human makes. A level-3 library may depend on one below level 3: that one's releases stay the human's, and its consumers wait on them with a `Blocked-by:` line.

## Runs

- **Hand-off.** The dispatcher queues triggers per agent. Each container's `agent-runner` claims a batch over HTTP with its runner key. The dispatcher grants a claim only when the agent has no run, is under its caps, and the kit is not paused. The runner reports the run's start and end, and the dispatcher records it.
- **Starting.** The runner starts a run only when no human session is active. It runs `claude -p --permission-mode dontAsk --output-format stream-json --name "<agent>: <triggers>" --session-id <uuid>` in the agent's workspace. The prompt lists the triggers as pointers, and says that no one can approve anything for it.
- **First-run prompts.** `agent-init` marks each agent's folder as trusted in `.claude.json`. The managed `CLAUDE.md` is generated as one file (role followed by protocol), so there is no external include to approve.
- **Logging.** The stream becomes a Markdown log under `.inkproof/runs/` in the workspace, plus `current.md`, which updates live in a VS Code tab. `.inkproof/` is excluded from git.
- **Ending.** When a run ends, the runner records its outcome, then rewrites `"entrypoint":"sdk-cli"` to `"entrypoint":"claude-vscode"` in the run's transcript, so that the Claude extension lists it. If the rewrite matched nothing, the run record says so and the human is notified; the run can still be resumed by ID.
- **Retries.** A run that fails, times out or exits before finishing a reply may have left its work undone, and most triggers are queued only once. So its triggers are queued again after `RETRY_DELAY_SECONDS` (default 300) times the attempt number, at most `MAX_RUN_RETRIES` times (default 2). After that, the dispatcher opens a decision for the human with the run and its work.
  - A run the human stops isn't retried: they may continue it.
  - A run that ends while the kit is paused is queued again as it was, without counting as an attempt.
  - So is a run abandoned by a restarting runner.
- **The record.** For each run, the ledger keeps:
  - the trigger, start and end, and outcome;
  - cost, turns and denied commands;
  - session ID and log path;
  - whether the run shows in the extension;
  - whether it changed anything in Forgejo, from the webhooks its account caused.

  It also records the human's sessions in each container: start, end and session ID.
- **Retention.** The runner copies each run's log and transcript, and each of the human's session transcripts, to `run-archive`, where they are kept for at least two milestones. Managed settings raise `cleanupPeriodDays` to 365, so Claude Code doesn't delete transcripts that runs and take-overs still need.
- **Bounds.**
  - At most one run per agent at a time.
  - Configurable caps per agent per UTC day on runs (default 20; the coordinator 40, because every other agent's work wakes it), and optionally on spend, from the stream's reported cost.
  - A maximum run length (default 240 minutes).
  - A burst of triggers within the debounce window (default 60 seconds) becomes one run.
  - A run starts within a configurable delay of its event, at most 15 minutes by default.

  A reached cap is a decision for the human.
- **Interrupting.** `scripts/stop-run.sh <agent>` interrupts a run, which is then recorded and rewritten like any other, so the human can continue it.

## Stepping aside for the human

- Managed hooks record human activity in the agent's container whenever `CLAUDE_CODE_ENTRYPOINT` is `claude-vscode` or `cli`. The container is busy from a prompt until Claude stops, then for a grace period (default 10 minutes). Runs, whose entrypoint is `sdk-cli`, don't count, and extension processes are ignored.
- While the human is active, the runner starts nothing and pending work waits. A run already under way when the human arrives continues, unless the human interrupts it.
- A busy mark with no stop is treated as stale after the maximum run length, so an interrupted session can't block runs for ever.
- `scripts/open.sh <agent>` attaches VS Code to the running container. `scripts/up.sh` builds every image, and Dev Containers never builds one. Managed settings and hooks apply to the human's sessions too. Host credential, git config and SSH agent forwarding are turned off.

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

## Triggers

Every wake-up is a rule over an issue or pull request as it is now, or over a repository (its `main`, its milestones), never over the event that changed it. The numbers below are the rules' numbers in the dispatcher's code.

Webhooks only say which items to evaluate again:
- the item an event names;
- the requests a pull request refers to with `Refs`;
- the issues whose `Blocked-by:` lines name it.

On start, and every 15 minutes (`RECONCILE_SECONDS`), a sweep evaluates every open item and the issues closed lately, so a missed delivery delays work rather than losing it.

A rule queues its trigger once for each version of the state it looks at (a head commit, a comment, a time). So an agent that looks and leaves things as they are isn't woken again until something changes. A run that fails gives its triggers back (Retries). Before a claim is granted, the dispatcher evaluates again each item behind the agent's triggers, and drops those no longer owed.

Only trusted authors count. An agent's own actions, and comments whose first line is `(no reply needed)`, start nothing. The only wake-up that is an event, not a state, is the coordinator's for a release it didn't ask for (18).

### Every library agent and the coordinator, on its own repository

- **(1)** An open request labelled `request` and `new` by someone else, or an open issue by someone else with no labels at all that it hasn't touched. (Forgejo drops the labels of anyone without write access to the repository's issues.)
- **(2)** A comment, or a review that isn't an approval, by someone else on one of its open issues or pull requests, that wants a reply and is newer than anything it has done there. A request for changes always wants a reply.
- On one of its open pull requests:
  - **(3)** CI failing on the head.
  - **(25)** No CI run at all for the head `CI_MISSING_SECONDS` (default 10 minutes) after it was pushed, as on a branch without the workflow.
  - **(4)** A conflict with `main`.
  - **(5)** No merge scheduled, once the owner's run has ended (it is looked at again then). Or a merge scheduled that hasn't gone through `MERGE_STUCK_SECONDS` (default 10 minutes) after nothing stood in its way: CI passed on the head, there's no conflict and no request for changes standing, and everyone a review was requested from has approved the head.
- **(6)** An open request it hasn't labelled `ready-for-release`, not blocked, that one of its pull requests, merged since the request was last unblocked, refers to with `Refs`.
- **(7)** An accepted request that:
  - isn't blocked;
  - has no open pull request referring to it, and nothing merged for it since it was last unblocked;
  - isn't ready for release or released.

  This rule applies only while the agent has nothing running or queued, and fires once each time the request is unblocked.
- **(8)** An open issue whose `Blocked-by: <owner>/<repo>#<n>` lines name only issues now closed or released (a `Released in` comment), once each time. Or the human's comment on an open upstream issue that one of those lines names.
- **(9)** CI failing on `main`.

The dispatcher keeps each issue's `blocked` label in step with its `Blocked-by:` lines: on while an issue they name is open and unreleased. Agents write only the `Blocked-by:` lines.

### Every agent, on a request it filed

- **(10)** A `Released in vX.Y.Z` comment, from the gatekeeper, the human or the repository's owner, while the request is open and not verified, and again a day after the release (`UNVERIFIED_NUDGE_SECONDS`). The exception is the gatekeeper's comment on the coordinator's own requests, which the coordinator verifies in the run that asked for the release.
- **(11)** On someone else's repository, a counter-proposal, or the request being closed without `verified` by someone other than the agent itself.

### The coordinator, across the `inkproof-` repositories

- **(12)** An open request filed by someone else.
- **(13)** A pull request opened by someone else, open or merged. The trigger is held until CI finishes on the pull request, or for `PR_OPENED_HOLD_SECONDS` at most (default 10 minutes), so that a review requested from the coordinator shares the run.
- **(14)** A pull request requesting its review, whose head passed CI and which it hasn't approved at that head. The trigger says whether the coordinator's is the review the pull request waits for: it is when CODEOWNERS on the repository's `main` gives `API.lock` to the coordinator (level 3), read from Forgejo, since a clone made before the setup pull request merged has no CODEOWNERS.
- **(15)** An item labelled `ready-for-release`, once CI on `main` has finished and passed, that it hasn't commented on or labelled since the label went on, since `main` last changed, or since CI on `main` finished.
- **(16)** `needs-human` added or removed by anyone but itself and the dispatcher, if it hasn't commented or labelled since.
- **(17)** A milestone with closed issues and none open on any repository it spans (namesakes are matched by title), once per milestone rather than once per repository.
- **(23)** No milestone on any repository with an open issue, so a milestone left open once its work is done doesn't hold it back. It fires once for the newest milestone that had work (by creation, so closing leftover or empty milestones doesn't fire it again), and not while the coordinator runs or has work queued, so that what comes next is always someone's work: the next milestone drafted, what it needs put to the human, or the project recorded as finished.
- **(24)** An open milestone issue on which every milestone review has reported since `milestone-review` went on, if it hasn't commented since the last report.
- **(18)** A release published by anyone but the gatekeeper and itself.
- **The idle summary.** The kit going quiet, once per quiet spell, with nothing stalled (see Stalls). Quiet means:
  - no run;
  - no queued work, except what a cap holds;
  - the human active nowhere;
  - no CI;
  - nothing new for `IDLE_SECONDS` (default 20 minutes).

  The run's last message, which the Claude extension lists in its container, tells the human what finished and what it needs from them.

### `conformance`

- **(19)** An open pull request, not marked as work in progress, whose head passed CI (or, in `inkproof-coordination`, which has no CI, any head), and which it hasn't reviewed. It reviews once the head has settled:
  - at once when the author is an agent with no run under way and the human isn't active in its container;
  - when that run ends;
  - otherwise, `CONFORMANCE_SETTLE_SECONDS` (default 10 minutes) after the last push.
- **(20)** The author's reply, after its request for changes at the current head.

### `architecture`, `process` and `lasm`

- **(21)** An issue on `inkproof-coordination` labelled `milestone-review` that the reviewer hasn't commented on since the label went on, whether open or closed. The coordinator may accept the milestone and close its issue before the reviews run.

### `lasm`, also

- **(22)** An open issue on `inkproof-coordination` labelled `upstream`, `upstream:lasm` and `blocking`.

### Any agent, when the human mentions it

- **(26)** A comment by the human that @-mentions the agent, on any issue or pull request, if the agent hasn't commented or acted there since. This is how the human reaches an agent other than the repository's owner, whom any comment wakes (2). An agent's @-mentions wake no one.

### Stalls

The rules can't foresee every way work stops. So once the kit has gone quiet (as for the idle summary), every open issue and pull request must be waiting for something the kit can see:
- the human: `needs-human`, assigned to them, or their review requested and not given on the head;
- an open, unreleased issue its `Blocked-by:` lines name;
- an upstream issue;
- a reviewer asked for and not yet answered, on a head whose CI passed (until CI passes, only the author can move a pull request);
- a released request's requester (rule 10);
- for an issue, an open pull request referring to it.

An item that isn't waiting for any of these wakes whoever should move it on, once per quiet spell:
- the reviewer;
- the author of a pull request;
- the coordinator, for `ready-for-release`;
- the requester, for a counter-proposal;
- otherwise, the repository's owner.

An item still stalled after `STALL_ROUNDS` quiet spells (default 2) becomes a decision for the human instead, `Stalled: <repo>#<n>`. A capped agent isn't woken, and the count starts again once the item waits visibly.

Each catch is a gap in the rules. So the human hears of it once (a `stall` notice, on the item), and the process review looks for why no rule fired and recommends the change that would have caught it. The coordinator's idle summary comes only in a quiet spell with nothing stalled.

### The first sweep

On a ledger from before the rules existed, the first sweep records some of what it finds as handled. These are the kinds the old event-driven triggers covered, and those that only report what happened: replies, merges, unblocking, filings, pull requests opened, `needs-human` changes, milestone reviews and milestones done. It queues the rest, which is work the old triggers may have missed. Webhooks start nothing until this sweep has run.

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
  - a request released `UNVERIFIED_STALL_SECONDS` ago (default two days) that nobody has verified;
  - an item still stalled after `STALL_ROUNDS` quiet spells.

  A stalled request is a problem for the human to resolve or fix in the kit. No agent verifies another's request.
- Below level 3, the coordinator puts each library's ready release to the human as a decision, `Release <repo>`, with the exact `scripts/release.sh` command.
- The coordinator's idle summary says whether the project is waiting on the human, stalled or finished, and how many items wait on them. It comes only in a quiet spell in which no item was found stalled, so "stalled" there means a stall the kit's check can't see.

## Notifications

Forgejo has no email without SMTP. The dispatcher records a notice for each of these, and for nothing else routine:
- every `needs-human` label added, with a milestone's recommendations grouped into one notice per report;
- every reached cap;
- every `blocking` upstream issue;
- every item the stall check catches, once each time it stalls;
- every run whose transcript rewrite failed.

For an existing issue, the dispatcher comments as `dispatcher`, mentioning the human, so the notice shows in Forgejo's notifications. A cap or a failed transcript rewrite gets an issue of its own, assigned to the human. A host launchd job, `scripts/notify.sh`, reads new notices from the dispatcher on `127.0.0.1` and shows each as a macOS notification. SMTP with the human's own mail account is optional, for being told while away from the machine.

## Reviews

### Conformance, on every pull request

- **What.** `conformance` checks the pull request's diff against the base branch's `CLAUDE.md` and the protocol, and reports each deviation with the section it breaks. Under the protocol's "Reuse before you build", it also checks two things for a pull request that adds declarations: that it says what prior art it looked for, and that nothing it adds repeats a declaration in another library. It reviews craft against stated rules only; scope stays with the coordinator.
- **Outcome.** It approves, or requests changes with its findings. A request for changes wakes the author through the review trigger, and blocks the merge until `conformance` approves a later head.
- **Answering findings.** The author fixes each finding or replies with a reason. `conformance` accepts the reason or keeps the finding. After `CONFORMANCE_ROUNDS` rounds on one pull request (default 3), it labels the pull request `needs-human` instead of reviewing again, and the human decides.
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
  - Is code duplicated between libraries, or does something general belong lower down?
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

- **The upstream tracker.** When an agent finds a bug or limitation in a dependency outside the `inkproof-` repositories (Lasm, Lean, Mathlib or any other), it doesn't work around it. It files an issue on `inkproof-coordination` labelled `upstream` and `upstream:<project>`, with a reproduction. If it can't continue, it marks its own task `blocked` with a `Blocked-by:` line naming that issue. A blocker also gets `blocking`, which notifies the human.
- **Unblocking.** Nothing polls GitHub, so an upstream fix can't wake blocked agents by itself. The human closes the upstream issue, or comments on it, once a fix is available, and that is the trigger.
- **Fetching.** Third-party Lean packages are fetched by Lake from GitHub through the proxies, as git reads. Lasm is an npm package (`@lasm/cli`), which repositories pin in `test/package.json` and fetch from npm, also through the proxies; its browser bindings (`Lasm.Web`, `Lasm.UI`, `Lasm.JS`) come with it, not from Lake. None of these is in Forgejo.
- **Browsers.** The agent and CI images include headless Chromium for browser tests, installed by Playwright when the images are built, at one pinned version (`PLAYWRIGHT_VERSION`, 1.64.1). Neither agents nor CI can download browsers, since no proxy allows Playwright's download host, so a repository pins `playwright` at exactly that version, and an upgrade changes the images and the pins together. Its WebGL is software, which is enough for functional tests, not for golden images across real GPUs (VAL-7).

## Agents' command line

Agents get `tea` for issues, labels, comments and pull requests, and a kit command, `forge`, for what `tea` lacks:

- `forge pr automerge <n>`: merge now if nothing stands in the way, otherwise schedule "merge when checks succeed" (see Forgejo behaviour);
- `forge pr merge <n>`: merge now, or say why Forgejo won't;
- `forge ci <repo> <pr|main>`: the Actions run's result for the pull request's head commit, or for `main`'s. With `--log`, it adds the log of each failed job in that run (the last 200 lines, or `--log all`), so an agent can find out why CI failed without the human;
- `forge pr approve <repo> <n>` (coordinator): approve, but only if the Actions run for the head commit succeeded;
- `forge review <repo> <n> <findings>` (`conformance`): approve or request changes from structured findings, only once CI has passed. After the rounds limit, it labels the pull request `needs-human` instead;
- `forge reviews <repo> <n>`: a pull request's reviews and their findings, which `tea` doesn't show;
- `forge ledger [days]` (`process`): the run ledger;
- `forge release <repo> '<note>' <issue>...` (coordinator, level 3): ask the gatekeeper to release.

Managed settings deny these, with every alias:
- `tea api`, `tea login`, and `tea`'s release and approval commands;
- `curl` and `wget`;
- `git tag` and `git config`;
- force pushes.

`tea api` is the agent's whole account, including posting a commit status under CI's name, which is the spoof an early trial demonstrated. Denying it and offering `forge` keeps runs to reviewed operations, and puts the rule "approve only after checking the Actions run" in code instead of a prompt. The deny list can be got round with a script; the real boundary is the account's permissions and branch protection, and the list is defence in depth.

WebFetch is allowed, so agents can look for existing libraries before building (the protocol's "Reuse before you build"). It goes through the agents' proxy, so it reaches only the allowlist (GitHub's pages, and Reservoir and Loogle on `lean-lang.org`). It sends no credential, so it can't act as the agent in Forgejo.

## GitHub mirror

- **Migration.** `scripts/migrate.sh` uses Forgejo's migration to import each repository from GitHub once, with history, issues, labels and pull requests, using a GitHub token of the human's own. Imported content is posted by Ghost.
- **Mirrors.** After import, each repository push-mirrors to GitHub on every commit, with a credential of the human's own: contents write on the `inkproof-` repositories, and no machine user.
  - Mirrors carry `main` (`MIRROR_BRANCHES`) and every tag. Issues and pull requests live only in Forgejo, which is why Forgejo itself is backed up.
  - Forgejo pushes with `git push --mirror`, so branches and tags on GitHub outside that set are deleted at the first sync. `refs/pull/*` is never pushed.
  - Git ignores Forgejo's `[proxy]` setting, so Forgejo's container sets the standard proxy variables for it.
- **New libraries** start in Forgejo from a seed (`scripts/new-library.sh`), and the push mirror fills an empty GitHub repository; there is no import.
- **Dependencies.** Agents and CI fetch `inkproof-` dependencies from Forgejo, while `lakefile` and `lake-manifest.json` keep their GitHub URLs. Third-party packages are unaffected, and still come from GitHub.
  - There is one `url."http://forgejo:3000/<FORGE_ORG>/inkproof-".insteadOf` value for each GitHub owner the repositories have lived under (`GH_OWNER` and `GH_FORMER_OWNERS`), because old tags' lakefiles name the former owner.
  - The values are set in the root-owned system git config, and in CI, which reads the owners from the organisation's `DEPS_GITHUB_OWNERS` Actions variable.
  - Each value is scoped to `https://github.com/<owner>/inkproof-`. A rule for all of `https://github.com/` would send Mathlib, Batteries, Plausible and Lasm to Forgejo, where they don't exist.

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
- **Taking over.** The human can open any finished or interrupted run in the extension and continue it. While they are active, no run starts for that agent.
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
- **Each run is a fresh headless session.** Each run is a new `claude -p` conversation, logged, and resumable in the extension. There's no long-lived terminal session to attach to.
- **Approvals are the merge gate, not required statuses.** Any account with push access can post a commit status under CI's name.
- **Protected files aren't used.** In the trial they blocked every merge. `CODEOWNERS` does the job instead.
- **Autonomy levels are per repository.** So the human can raise one library at a time, and fall back.
- **The gatekeeper approves on CI from level 2.** So no Claude run is spent per pull request on checking CI.
- **Notifications are Forgejo mentions plus a macOS notifier.** SMTP is optional.
- **Each reviewer is its own agent and account.**
- **The process review may read the human's own sessions**, through the run archive.
- **Lasm stays outside Forgejo**, fetched from GitHub. Its issues go through the upstream tracker, and the human unblocks.

# Runbook

How to set up and run the kit. [README.md](README.md) says what it is; [DESIGN.md](DESIGN.md) says exactly what it enforces and why.

You need Docker Desktop on a Mac. The scripts are bash 3.2-compatible: run them from the kit's folder as `scripts/<name>.sh`, or with `bash scripts/<name>.sh` if a copy has lost their executable bits. Every setting is in `.env`, described in [.env.example](.env.example). After changing one, rerun `scripts/up.sh`.

## First-time setup

### The forge

1. `cp .env.example .env` and edit it:
   - your Forgejo username: use your GitHub username, so `CODEOWNERS` entries carry over;
   - your email;
   - your UID and GID (`id -u`, `id -g`).
2. `scripts/up.sh` builds the images, generates the CI runner's and webhook's secrets, starts everything, and loads the CI image into CI's own Docker engine.
3. `scripts/bootstrap.sh` asks you to choose a password for your Forgejo admin account. It then creates:
   - an admin token for the kit's scripts (`secrets/human/token`);
   - the organisation;
   - every account in `agents.conf`, plus `gatekeeper`, `dispatcher` and `ci-reader`, with their tokens and teams;
   - the webhook to the dispatcher.

   It reports what it changed. Re-running it changes nothing unless something has drifted.
4. Sign in at <http://localhost:3000/> as yourself.

`scripts/down.sh` stops everything and keeps the volumes.

### Bringing the repositories over from GitHub

This happens once. Forgejo imports each repository in `agents.conf` with its history, issues, labels, milestones, pull requests and releases, then pushes `main` and every tag back to GitHub on each commit. Both steps use GitHub tokens of your own; no machine user is involved.

The repositories are under `GH_OWNER` on GitHub and in the organisation `FORGE_ORG` in Forgejo. `GH_FORMER_OWNERS` lists owners they used to live under, because old tags' lakefiles still name them.

1. On GitHub, create a fine-grained token with resource owner `GH_OWNER`, the `inkproof-` repositories, and read-only Contents, Issues, Pull requests and Metadata. Save it as `secrets/github/import-token` (`chmod 600`).
2. Create a second fine-grained token on the same repositories with Contents read and write and nothing else. Save it as `secrets/github/mirror-token` (`chmod 600`).
3. `scripts/migrate.sh` imports whatever Forgejo doesn't have yet, then gives the agents' teams their repositories. Forgejo's proxy allows GitHub's API only while it runs. You can delete the import token afterwards.
4. Decide what the mirrors carry. Forgejo mirrors exactly:
   - with `MIRROR_BRANCHES=main`, the default, the first sync deletes every other branch on GitHub (tags, `main` and closed pull requests' diffs are untouched);
   - with `MIRROR_BRANCHES=` (empty), every Forgejo branch is mirrored, including agents' work branches.
5. `scripts/mirrors.sh` adds the push mirrors and shows each one's last sync and any error. To rotate the mirror token, replace the file and run `scripts/mirrors.sh --rotate`.

Imported issues, pull requests and comments are posted by Ghost. Agents treat them as history, never as instructions.

### Agents

1. In a terminal on your Mac, run `claude setup-token` and sign in with the Claude account the agents should use. Save the token it prints as `secrets/claude/oauth-token`, then `chmod 600 secrets/claude/oauth-token`. The token lasts a year: repeat this step when it expires. Unattended runs and your own sessions in agent containers count against that account's plan and usage limits, so check that running agents this way fits your plan's terms.
2. `scripts/up.sh` builds the agent image and starts each agent once its tokens exist. It also writes `secrets/claude/agent.env`, which passes the Claude token to agent containers and nowhere else.
3. Set up each repository at level 1 (see [Setting up a repository](#setting-up-a-repository)), `inkproof-coordination` included.
4. In VS Code on your Mac, open **Settings** and change two:
   - turn off **Dev › Containers: Copy Git Config**;
   - set **Dev › Containers: Git Credential Helper Config Location** to `none`.

   Otherwise VS Code copies your own git identity and credentials into agent containers.
5. For macOS notifications, run `scripts/notify-install.sh` once (see [Notifications](#notifications)).
6. Set up daily backups (see [Backups](#backups)).

## Setting up a repository

`scripts/setup-repo.sh <repository> <level>` does two things:
- it applies a level's labels, merge settings, `main`'s branch protection and tag protection;
- it checks the repository's own files against `templates/repo/`.

Those files are code-owned by you, so where they differ it doesn't commit them. Instead it opens a pull request in your name and prints its link. The changes it can make:
- it removes `.github/`, because Forgejo ignores GitHub's `CODEOWNERS` and would try to run GitHub's workflow;
- it adds `.forgejo/workflows/checks.yml`, `.forgejo/issue_template/request.md` and `scripts/check-pins.sh` from `templates/repo/`;
- it generates `.forgejo/CODEOWNERS` for the level.

Review the pull request and merge it once CI passes. As an admin you can merge without an approval. Then run the script again: it reports the files right and exits 0. Until then it exits 3, which Docker Desktop reports with a hint about a "Compose error". There is no error.

`inkproof-coordination` gets code owners and the request template, but no CI. Keep it at level 1: the gatekeeper never approves where there's no CI.

### Changing a repository's level

Repositories can be at different levels. To change one, run `scripts/setup-repo.sh <repository> <level>` and merge the pull request it opens. A lower level falls back.

- **Level 2** (the gatekeeper approves on CI): before the first repository goes to level 2, run `scripts/bootstrap.sh` once, which gives the gatekeeper its access, then `scripts/up.sh`, which starts it.
- **Level 3** (the coordinator approves `API.lock` changes and releases through the gatekeeper): a level-3 library can depend on one below level 3. That library's releases stay yours, and its consumers wait for them.

## Adding a library

A new library starts in Forgejo, not on GitHub: `scripts/new-library.sh` creates its repository with a seed that builds and passes CI, and the push mirror then fills an empty GitHub repository.

1. On GitHub, create an empty public repository `<GH_OWNER>/<repository>`, with no README, and add it to the mirror token.
2. Add the library's agent:
   - a line in `agents.conf`;
   - the new repository in the dependencies of every library that will depend on it;
   - its role in `roles/<agent>.md`.

   Its compose service is generated from `agents.conf`.
3. `scripts/new-library.sh <repository> <Name>`, for example `scripts/new-library.sh inkproof-float Float`, creates the repository and commits its seed to `main`:
   - a Lake package for `Inkproof.<Name>`, with a test package, from `templates/library/`;
   - the CI workflow, `scripts/check-pins.sh` and the request template, from `templates/repo/`, so that every branch has CI from its first commit;
   - `CLAUDE.md`, `LICENSE`, `.gitignore`, `lean-toolchain` and the manifests' format, from `inkproof-core` (`SEED_FROM` names another repository to copy them from).

   It then gives the teams the repository and creates its labels. It never seeds over a repository that has content.
4. `scripts/mirrors.sh`, then `scripts/up.sh`, which starts the agent. Every agent clones the new repository before its next run.
5. `scripts/setup-repo.sh <repository> <level>`, and merge its pull request.

## Day to day

### Work starts itself

You don't start runs. A request issue (labels `request` and `new`), or a comment or review on an agent's issue or pull request, from you or another kit account, queues work for the agent concerned. So do CI failures, merge conflicts, releases and the other rules listed in [DESIGN.md](DESIGN.md#triggers).

A comment wakes the agent that owns the repository. To reach any other agent, such as the coordinator about a review it owes, @-mention it in your comment (`@coordinator`): your mentions wake the agents they name, once per comment. Agents' mentions wake no one.

A run starts once the events have settled for a minute (`DEBOUNCE_SECONDS`), and within 15 minutes at most (`MAX_DELAY_SECONDS`). A comment whose first line is `(no reply needed)` starts nothing.

### You come first

While you're using Claude in an agent's container, through the extension or a terminal, no run starts there. Queued work starts 10 minutes after your last prompt finishes (`HUMAN_GRACE_SECONDS`), or as soon as you close the session, unless another of your sessions there was in use in those 10 minutes. `/clear` doesn't count as closing. A run already under way carries on.

### Following and taking over runs

- `scripts/open.sh <agent>` opens VS Code attached to that agent's container, at its workspace. The first time, VS Code installs its server and the Claude and Lean extensions there.
- To follow the current run live, open `.inkproof/current.md`.
- Every run is logged to `.inkproof/runs/` in the workspace. It's also listed in the Claude extension's past sessions, where you can open it and continue the conversation.
- `scripts/stop-run.sh <agent>` interrupts a run. The run is recorded, and you can continue it like any other.
- Every run's log and transcript, and your own sessions' transcripts, are kept in the `run-archive` volume.

### What waits on you

Everything that waits on you is in Forgejo's dashboards: issues assigned to you, and pull requests requesting your review.

The dispatcher assigns you to anything labelled `needs-human`, and to blocking upstream issues. Each such item states its decision under the headings Question, Options, Recommendation, If you say go ahead, and Blocked on this.

The kit files its own decisions the same way:
- a reached cap, saying what's waiting and how to raise the cap;
- a run the extension doesn't list;
- a request released two days ago that nobody has verified;
- an item that stays stalled (`Stalled: <repo>#<n>`).

A stall is usually a gap in the kit's rules, worth finding with `scripts/activity.sh <repo>#<n>`.

Below level 3, the coordinator puts each library's ready release to you as a decision, `Release <repo>`, with the exact command to run.

### Notifications

Forgejo shows the dispatcher's mentions in its notifications. For macOS notifications too, run `scripts/notify-install.sh` once. A launchd job then shows each new notice every minute, and nothing routine. Notices are:
- `needs-human`;
- a `blocking` upstream issue;
- a reached cap;
- a run the extension doesn't list;
- an item the stall check caught.

`scripts/notify-install.sh --remove` takes the job out.

### Summaries

When the kit goes quiet with nothing stalled, the coordinator writes you a summary:
- whether things are waiting on you, stalled or finished;
- what has finished since its last summary;
- what it needs from you, most important first, with links;
- what everything else is waiting on.

Open it with `scripts/open.sh coordinator`. It's the session named like "coordinator: the kit is idle; 2 items wait on you" in the Claude extension's list. Reply there and the conversation carries on with you present.

### Seeing what's happening

- **`scripts/status.sh`** shows, for each agent:
  - its state: idle, running, human active, paused or capped;
  - its waiting work and its spend today;
  - its recent runs, with outcome, cost, session and log.

  It then shows your sessions, the notices, and the open requests, pull requests, recommendations and `needs-human` items across the project. It also shows whether the gatekeeper is running and its latest approvals.

  Its last line says whether the kit will make progress by itself:
  - "Kit: working", with what's running, queued or in CI;
  - "Kit: quiet, nothing happens until you act", with what waits on you;
  - quiet with nothing waiting on you, in which case a stalled item or the coordinator's summary comes next.
- **`scripts/activity.sh`** follows what's happening, one line per entry, in your time zone. It covers pushes, pull requests, reviews, labels, CI results and releases, with who did them; work queued and why; runs starting and ending, with outcome and cost; your sessions; and notices.

  It shows the last 40 entries and then follows until Ctrl-C. `-n 200` shows more first, `--once` doesn't follow, and a word filters the lines: `scripts/activity.sh scale`. Issue references such as `inkproof-core#24` link to Forgejo in terminals that support hyperlinks; `--urls` prints the addresses instead.
- **`scripts/reviews.sh`** lists reviews and their results.
  - First, the reviews on pull requests updated in the last 7 days (`scripts/reviews.sh 30` for 30), whether by conformance, the coordinator, the gatekeeper or you. For each it shows who, when, the verdict, conformance's round and open findings, and whether the review is stale or was dismissed.
  - Then each milestone's architecture, process and Lasm reviews: whether each has reported, and each recommendation's state.

  `--changes` shows only the reviews that asked for more work.
- **`docker compose logs gatekeeper`** shows each pull request the gatekeeper looked at, and what it found in that pull request's CI run.

### Pausing

`scripts/pause.sh` stops everything:
- no run starts;
- a run under way is refused its next tool call, and ends;
- every scheduled merge is cancelled and recorded in `control/paused-merges.json`;
- only you may merge.

Your own sessions in agent containers still work.

`scripts/resume.sh` undoes it:
- it restores the merge lists;
- it merges in your name each recorded pull request that nothing now stands in the way of;
- it reschedules the rest;
- it lets agents start again.

### Bounds

| Bound | Default | Setting |
| --- | --- | --- |
| Runs per agent at a time | 1 | none |
| Runs per agent per UTC day | 20 | `MAX_RUNS_PER_DAY` |
| Coordinator runs per UTC day | 40 | `MAX_COORDINATOR_RUNS_PER_DAY` |
| Spend per agent per day | no limit | `MAX_SPEND_PER_DAY` |
| Run length | 240 minutes | `MAX_RUN_MINUTES` |
| Retries of a failed run | 2, after 5 then 10 minutes | none: `MAX_RUN_RETRIES` and `RETRY_DELAY_SECONDS` are read by the dispatcher but not passed from `.env` |

The coordinator's cap is higher because every other agent's work wakes it.

A failed run that has used up its retries, or a reached cap, becomes a decision for you. A run you stop isn't retried. A run cut short by a pause is queued again as it was.

## Milestones and reviews

Milestones are the coordinator's to run:
- it drafts the next milestone as soon as the current one's work is merged;
- it opens the milestone issue that starts the reviews;
- it accepts the milestone once its acceptance criteria are met.

You decide only real forks, and the recommendations outside its remit. To start the reviews yourself, run `scripts/milestone.sh "<name>"`. It opens a milestone issue on `inkproof-coordination` with every `API.lock` change since the last one, labelled `milestone-review`. `scripts/milestone-diff.sh` shows the same changes without opening anything.

Recommendations are issues on `inkproof-coordination` labelled `recommendation`, `review:<kind>` and `needs-human`. You get one notice per review report, not one per recommendation.
- **To accept a recommendation**, close it. An accepted process recommendation is a change you make to the kit or to `CLAUDE.md`.
- **To decline one**, add `declined`, say why in a comment, then close it. Reviewers don't raise a declined recommendation again without new evidence.

The coordinator may adopt a recommendation within its own remit, and says why on it.

## Releases

At any level, you can release a library yourself:
- `scripts/release.sh <repository>` shows what the next release would be;
- `scripts/release.sh <repository> "<note>" [<issue>...]` makes it, tagging `main`'s head as you.

It refuses unless `CHANGELOG.md` has an entry for the computed version and CI passed on `main`. Each issue you name must be open and labelled `ready-for-release`: it gets a comment with the release, and loses the label. To start a new major line, such as 1.0, tag it yourself in Forgejo. [DESIGN.md](DESIGN.md#releases) describes how the version is computed.

At level 3, the coordinator releases through the gatekeeper, and you don't need to do anything.

## Backups

Forgejo, the run ledger and the run archive live in Docker volumes, inside Docker Desktop's disk image, which Time Machine doesn't usefully back up. `scripts/backup.sh` copies them into the kit's folder, so Time Machine picks the copies up with everything else here, `secrets/` included.

1. Check that Time Machine covers this folder, that is, that it isn't in Time Machine's "Exclude from Backups" list.
2. Run `scripts/backup-install.sh` once. A launchd job then runs `scripts/backup.sh` daily at 03:30, or when the Mac next wakes, as long as Docker Desktop is running. If a backup fails, you get a notification, and the log is `control/backup.log`. `scripts/backup-install.sh --remove` takes the job out.
3. Now and then, run `scripts/restore-test.sh`. It restores the newest backup into a throwaway Forgejo that can reach nothing, and compares it with what the live Forgejo held during the backup. With `KEEP=1` it leaves the copy running on `127.0.0.1:3002` for you to look at.

Each backup is a folder in `backups/` (`BACKUP_DIR`), named by its UTC time and readable only by you. It holds:
- `forgejo.tar.gz`, from `forgejo dump`;
- `ledger.db`;
- `run-archive.tar.gz`.

The newest 7 are kept (`BACKUP_KEEP`). Time Machine keeps the history beyond that.

### Restoring

`scripts/restore.sh backups/<time>` replaces Forgejo, the ledger and the run archive with that backup, after you type `restore` to confirm, then starts the kit again. It first keeps a raw copy of the current Forgejo data beside the backups.

Use it when Docker's volumes are lost, after a Docker Desktop reset for example, or on a new Mac. `secrets/` must be the kit's own (from Time Machine, on a new Mac), because Forgejo keeps only the tokens' hashes. Anything since the backup is lost from Forgejo, though GitHub's mirrors may have the latest commits.

## Developing the kit

The kit's dev container mounts this checkout and has a Docker engine of its own, for developing and testing the kit. Inside a container the scripts read `.env.devcontainer` instead of `.env`. That file sets its own `STATE_DIR` (`./.devcontainer-state`) for `secrets/`, `control/` and `backups/`, and its own `FORGE_PORT` (3300), so neither environment overwrites the other's credentials or takes the other's port. `KIT_ENV` names a different file for either. In the container, run ad-hoc commands with `scripts/kit.sh`, never a bare `docker compose`, which would read the Mac's `.env`.

### Tests

**Run the stage tests in the dev container only, never against the live kit.** They pause and resume the whole kit, file issues, open pull requests, start milestone reviews and attempt releases. Each tests one part of the kit:

| Test | Checks |
| --- | --- |
| `scripts/test-dispatcher.sh` | The dispatcher's unit tests, with Forgejo stubbed out. Quick, isolated, and safe anywhere. |
| `scripts/test-stage1.sh` | Bootstrap, networks and proxies, CI in its own engine, that agents can't tag or release, and Actions webhooks. |
| `scripts/test-stage2.sh` | Migration from GitHub, attribution, mirrors, and dependencies fetched from Forgejo. With `MIRROR_TEST_REPO`, a round trip through a scratch GitHub repository. |
| `scripts/test-stage3.sh` | One agent at level 1: unattended runs, deny rules, the ledger and archive, the transcript rewrite, and stepping aside for you. Starts real Claude runs, costing a few dollars. |
| `scripts/test-stage4.sh` | The dispatcher's triggers, reconciliation, caps, pause and resume, status and notifications. |
| `scripts/test-stage5.sh` | Conformance reviews and their rounds limit, milestone reviews, and code owners blocking merges. |
| `scripts/test-stage6.sh` | Level 2: the gatekeeper approves only on a real Actions run. |
| `scripts/test-stage7.sh` | Level 3: the coordinator gates `API.lock`, and releases go through the gatekeeper. |
| `scripts/test-stage8.sh` | Backups, pruning and restore-test. |

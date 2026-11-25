# A GitHub kit: what the suspension tells us and what GitHub's policies require

This is research for a possible GitHub version of the kit. Nothing in it changes the kit. It assumes GitHub will never answer the support ticket about the suspended machine user: as of 2026-11-23 the ticket had gone two weeks without a reply.

Labels used below:
- **[policy]** is GitHub's own documentation or terms.
- **[anecdote]** is someone else's report.
- **[inference]** is our reasoning.
- **[experience]** is from running [lean-bump](https://github.com/paulbutcher/lean-bump)'s GitHub App, which fans out across about 35 repositories, measured from its logs in November 2026.

The sources were gathered on 2026-11-23. Treat the anecdotes as unverified.

## 1. What the old kit did

The old kit is [tentenths/inkproof-agents](https://github.com/tentenths/inkproof-agents). On 7 November 2026 its machine user was suspended, some time after 15:46 UTC.

### Identity

- Four agent containers acted as the same machine user, created on 3 November. Each used two fine-grained PATs: one for writing, one for reading.
- From 12:07 a GitHub App, the gatekeeper, posted the `api-lock` check, labels, comments and releases. Its private key was kept outside the agents' containers.
- **The App was not affected by the suspension.**

### Polling

Each container ran `watch-work`, which polled every `POLL_INTERVAL`. The deployment set this to **60 s**; the code's default was 300 s. Every request was an unconditional `gh` call, with no ETags.

| Who | Requests per poll |
|---|---|
| Each of the three library agents | about 6: a GraphQL `issue list` (twice), a GraphQL `pr list` with `statusCheckRollup` and reviews, open issues, issue comments, review comments, and one release list per blocked dependency |
| The coordinator | about 22: its own 6, a listing of the organisation's repos, and 4 per project repo (issues, pulls, issue events, releases) |
| **Total** | **about 40 per minute, or about 2,500 an hour** |

All of that came out of the single 5,000-an-hour budget that a user shares across all its tokens. On top of it came:
- the Stop hook, which ran `gh issue list` every time Claude stopped
- the agents' own reads and writes during runs
- the gatekeeper App, which polled every 60 s on its own, separate budget

### Timeline on 7 November (UTC)

| Time | Event |
|---|---|
| 10:38 | Unattended runs added |
| about 11:00 | Unattended runs started |
| 12:07 | Level 3 and the gatekeeper App |
| 15:51 | Remote Control |
| after 15:46 | Suspended |

The account was suspended about 4 to 5 hours after it started running unattended, four days after it was created. Some pushes were also refused by the organisation's rulesets: to protected `main`, of `v*` tags, and of branches containing workflow files.

### Most likely trigger [inference]

The most likely trigger was a sudden change from light, human-paced use to continuous automated traffic, at about half the user's primary limit, on a brand-new account. Some facts point that way:
- GitHub's docs warn that continuing to make requests while rate limited "may result in the banning of your integration" **[policy]**. If the agents' own activity pushed the account over its limit, the kit had no backoff.
- No reports link refused pushes, or the `Agent/claude-code` user agent, to suspensions.
- The App survived at the same polling rate from the same machine. That is one example, not proof, but it points at the account rather than the traffic as what was flagged.

## 2. What GitHub's terms say

### Machine accounts

The [ToS §B.3](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) allows machine accounts **[policy]**:
- A machine account must be "set up by an individual human … used exclusively for performing automated tasks."
- "the owner of the Account is ultimately responsible for the machine's actions."
- You may have one free machine account per person.

### Automation and the API

- The [AUP §4](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) prohibits "excessive automated bulk activity" and "undue burden on our servers through automated means" **[policy]**.
- [ToS §H](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) lets GitHub suspend API access for "excessively frequent requests", "in our sole discretion" **[policy]**.
- **No thresholds are published.**

### AI agents

- Neither the ToS nor the AUP has rules specific to AI agents.
- GitHub's own third-party agents ([Agent HQ](https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents)) act as **GitHub Apps** **[policy]**.

### Appeals

- Appeals are reviewed by humans and have no stated turnaround **[policy]**.
- Reported waits run from days to months, and many get no answer at all. One collection of 80 cases has 46 unresolved **[anecdote]**.
- One user's replacement account was suspended as **ban evasion**. Don't create a new machine user while the appeal is open **[anecdote]**.

## 3. Rate limits

Source: [GitHub's REST rate-limit docs](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) **[policy]**.

### Primary limits

| Identity | Limit |
|---|---|
| User | 5,000 requests an hour, shared across all its tokens |
| App installation | 5,000 an hour, plus 50 for each repo or user above 20, up to 12,500 |
| GraphQL | 5,000 points an hour, a separate budget from REST |
| `GITHUB_TOKEN` in Actions | 1,000 an hour per repository, separate again |

By that formula this kit gets the base 5,000 an hour. It has 13 repositories in `agents.conf`, and one App acting for every agent leaves the organisation with one or two members.

GitHub doesn't keep to its own formula **[experience]**:
- lean-bump's App, on about 35 repositories, read 12,500 from `GET /rate_limit`. The formula gives 5,750.
- Its readings fell into two counts that dropped independently. One was mostly runs using all-repository tokens, the other mostly single-repository tokens, but not consistently.

So plan for 5,000, and read the real budget. `GET /rate_limit` costs nothing against any limit. Log `remaining/limit` and the reset time for `core` and `graphql` regularly, and warn when either falls below about 25%. Read the logs before acting on a suspected rate limit: lean-bump's suspected one turned out to be something else (section 10).

### Secondary limits

These apply to **every** identity, Apps included.

| Limit | Value |
|---|---|
| Concurrent requests | at most 100 |
| Points per minute, REST | at most 900 per endpoint (a GET costs 1, a write costs 5) |
| Points per minute, GraphQL | at most 2,000 |
| CPU time | at most 90 s per 60 s |
| Requests that create content | **80 a minute and 500 an hour**, web UI included |

The secondary limits can't be queried and can change without notice. GitHub sends no headers for them, so the first sign of one is a 403 or 429. For a kit whose agents write a lot, they, not the hourly budget, are the real constraint **[experience]**. Overlapping work adds up: three of lean-bump's runs at once made about 90 writes in one minute, more than the documented 80, without being throttled. Don't count on that.

### GitHub's recommendations

- Use webhooks rather than polling.
- Make requests one at a time, not concurrently.
- Wait at least 1 s between writes.
- Honour `retry-after` and `x-ratelimit-reset`. Without them, wait a minute, then back off exponentially.
- Make conditional requests: a **304 doesn't count** against the primary limit.

From experience **[experience]**:
- On a 403 or 429, wait for `retry-after` if it's given. If `x-ratelimit-remaining` is 0, wait until `x-ratelimit-reset`. If the message names a secondary limit, wait at least 60 s and back off exponentially. Give up after a few tries.
- On a 502, 503 or 504, retry a read with backoff, but **never retry a write**, because it may already have taken effect.
- Events caused by `GITHUB_TOKEN`, including tags it pushes, start no other workflows. Anything that must trigger downstream workflows needs the App's token.
- Reads can cost more than the writes they save. lean-bump's filter on which repositories need an event cut writes per event from about 33 to 5–8, but raised reads from about 35 to about 135, so a burst ran at about 9,000 an hour. GraphQL batches reads: one search returns 100 repositories, and `object(expression: "HEAD:path")` reads a file in the same query.

## 4. Getting webhooks to a Mac behind NAT

| Option | Verdict |
|---|---|
| smee.io, `gh webhook forward` | GitHub says these are for development and testing, not production **[policy]** |
| Cloudflare Tunnel, Tailscale Funnel, ngrok | Fine; verify the HMAC signature |
| Hookdeck, Webhook Relay | Add queueing and replay of missed deliveries |
| A self-hosted Actions runner in the private repos | Needs only outbound HTTPS. Costs Actions usage, and the Actions terms forbid disproportionate "serverless"-style use **[policy]** |

How delivery works:
- GitHub **does not redeliver** failed deliveries automatically. They can be resent by API for 3 days.
- Each delivery must be answered within 10 s **[policy]**.

The current dispatcher already treats webhooks as hints and runs a reconciliation sweep, which fits this model well.

## 5. Surviving a suspension

### Effects

- Reports say a suspended user's content becomes hidden: comments, issues and PRs return 404 **[anecdote]**. So a suspended agent identity could hide the project's history.
- Organisations have been flagged too **[anecdote]**.
- No reports were found of GitHub's abuse systems suspending an App. An installation can be suspended, but the documented mechanism is the App's owner or the installing account doing it **[policy]**.

### Backups

- Use `git clone --mirror`, plus an export of issues and PRs, for example with [python-github-backup](https://github.com/josegonzalez/python-github-backup). The kit has no Python (`lean-core.md`, Decision), so the export would be written in Lean or shell, or run outside the kit.
- The org migrations API produces archives that can't be restored on github.com, so it is for archiving only **[policy]**.
- Run backups as an identity separate from the agents', and store them off GitHub.
- A push-mirror to a Forgejo you control gives somewhere to carry on working.

## 6. What a GitHub kit would need

These are ranked by how well the evidence supports them.

1. **One client, one queue.**
   - Only the dispatcher talks to GitHub, and its requests are serial.
   - Writes are at least 1 s apart, with a budget well under 80 a minute and 500 an hour, counting every agent together.
   - It honours the rate-limit headers and stops outright while limited, and retries as in section 3: reads after a 5xx, never writes.
   - It reads its real budget from `GET /rate_limit` and logs it.
   - Agents reach GitHub only through it. Today's `forge` wrapper is the natural place for this.

   The current `FORGE_CONCURRENCY=8` would become 1 for writes **[policy]**.
2. **Webhooks, plus a reconciliation sweep using conditional requests.**
   - Webhooks arrive through a production-grade tunnel or relay.
   - The sweep uses ETags and `since=`, so that sweeps which find nothing new cost nothing. Since 2026-11-24 the sweep re-reads only items whose `updated_at` has changed, plus each open PR (section 8). On GitHub those reads could also be conditional **[policy]**.
   - Better still, the sweep's listings could be a few GraphQL queries: one can return every open issue and PR across the repositories, with their labels and `updatedAt`, instead of one REST listing per repository per kind **[experience]**.
   - Where the kit can't tell whether something needs doing, because a read failed or a list was cut short, it acts as if it does. A wasted check costs less than a missed one **[experience]**. The CI flags already do this: a repository that can't be checked counts as busy.
3. **An org-owned GitHub App, not a machine user.**
   - It uses short-lived installation tokens, with no password and no user account to suspend.
   - Its budget grows with the org in principle, though not for this kit's size (section 3).
   - It is the identity GitHub's own agents use **[policy]**. That Apps face less abuse risk is **[inference]**, backed by the gatekeeper having survived.
   - One App can't serve every agent. Required approvals need distinct identities: the gatekeeper's, conformance's and the coordinator's, at least. The other agents could share one App, named in each comment's `From:` line as now **[inference]**.
4. **Continuous backup and a mirror forge**, run as a separate identity. **Keeping the forge swappable** (question 1 of the overall investigation) makes "carry on in Forgejo" the plan for a suspension.
5. **Ramp up gradually.** Start a new installation slowly, add jitter to scheduled work, cap PRs and issues per hour, and batch commits **[anecdote/inference]**.
6. **Avoid refusals in the first place.** Check rulesets and protected refs locally before pushing **[inference]**. This is cheap.
7. **If suspended:** file one ticket, stop all automation for that identity, and don't create a replacement account. Fall back to the mirror forge.
8. **Keep the human's own account out of the agents' path**, so that nothing flagged can spread to the organisation's owner **[inference]**.

## 7. How much the current kit calls its forge

These figures were measured on the Mac's kit on 2026-11-23, with 18 agents.

**Sources:**
- **Reads:** Forgejo's request log, about 15 hours (02:36 to 17:59 UTC). It covered a quiet night with no writes from 02:00 to 11:00, then an active afternoon.
- **Writes:** Forgejo's database, the last 7 days.

**Caveats:**
- The callers were identified by IP address, using the addresses after the containers were rebuilt at about 17:00.
- The dispatcher and the gatekeeper are clear from what they request. The attribution of the agents' own calls before the rebuild is less reliable.
- Forgejo's calls don't map one-to-one onto GitHub's.

### Reads: far over GitHub's limits

| | Quiet hours | Active hours |
|---|---|---|
| Dispatcher | about 12,600 an hour | 13,000 to 21,000 an hour |
| Gatekeeper | about 3,400 an hour | similar |
| Agents' own calls, all 18 together | negligible | a few hundred an hour |
| **Total** | **about 15,700 an hour** | **20,000 to 26,000 an hour** |

Even when quiet, that is about three times a user's 5,000 an hour, and above an App installation's ceiling of 12,500. The busiest minute had 1,644 requests.

Where the reads come from:

- **The status page.** Almost all of the dispatcher's quiet traffic is `kit_summary()`, which `scripts/watch.sh` calls every 15 s by default. Each call runs `waiting_for_human()`, which lists open issues assigned to the human and open PRs in every repo, and `ci_running()`, which lists running and waiting Actions runs in every repo. That is about 54 requests per redraw, about 234 redraws an hour. The idle check repeats `ci_running()` once a minute.
- **The gatekeeper's 30 s poll.** Each poll lists the org's repos, then every repo's open PRs and `main` branch.
- **Bursts of evaluation after webhooks.** In the busiest minute the dispatcher read about 380 issues, 245 comment lists and 130 timelines. Re-evaluating whole repos re-reads everything, with no caching across evaluations.

### Writes: within GitHub's limits, but not by much at peak

These counts include issues, PRs, comments, reviews, review requests, label, assignee and milestone changes, closes and releases. Rows Forgejo creates by itself, such as commit references and pushes to a PR, are excluded.

| | Measured | GitHub's limit for one identity |
|---|---|---|
| Per day | 470 to 1,460 | none |
| Per hour, peak | 351 (17 Nov 13:00); usually under 250 | 500 |
| Per minute, peak | 53 API writes (23 Nov 16:27); 118 rows of any kind on 17 Nov | 80 |

On top of these come about 50 merges a day, whose retries on Forgejo's 405 response add more calls.

The busiest writers were the coordinator, the gatekeeper, demos, conformance, web and the dispatcher.

### What this means

**Reads are the problem, not writes.** Writes need pacing and a budget, not redesign: a queue that sends at most one write a second keeps them under 80 a minute. An hourly budget of about 400 covers the observed peak of 351, as long as every agent shares one App identity.

## 8. What has been fixed (2026-11-24)

Three changes take out the read traffic measured above. None of them depends on GitHub, so they apply to the kit on Forgejo now. They were tested in the dev container's kit, with `scripts/test-dispatcher.sh`, stage 6 and stage 4, then deployed to the Mac's kit and measured there ("Measured on the Mac" below).

| | Before | After |
|---|---|---|
| Status page (`status.sh`, `watch.sh` every 15 s) | about 54 requests per redraw, about 12,600 an hour | none |
| Gatekeeper | a poll every 30 s, about 3,400 an hour | only what webhooks name, plus a sweep every 15 minutes |
| Sweep, every 15 minutes | every open item read in full | one read per open PR, plus the listings |

What each change does:

- **The status page reads nothing from Forgejo.**
  - What waits on the human is an index in the dispatcher's database. Every evaluation of an item updates it, and the sweep prunes it.
  - Whether CI is running is a per-repository flag. A webhook that can start or end a run marks the repository, and the dispatcher checks only the marked repositories and those it last found busy, once a minute. The sweep checks them all.
  - When the kit is quiet, the dispatcher asks Forgejo nothing about either.
- **The gatekeeper takes webhooks.**
  - Forgejo sends signed webhooks over the gatekeeper's own network, for a finished Actions run, a PR opened, reopened or pushed to, and a push to `main`. The gatekeeper checks only the PR or repository named.
  - A sweep every 15 minutes (`GATEKEEPER_SWEEP_SECONDS`), on start and on resume, means a missed webhook delays an approval rather than losing it.
  - Stage 6 passes.
- **The dispatcher reads an item only when it has changed.**
  - It keeps its last read of each item: the issue, timeline and reviews, and a PR's files for each head.
  - It drops an item's entry when a webhook names the item, when the kit writes to it, when a listing shows a newer `updated_at`, or after `ITEM_CACHE_SECONDS` (default an hour).
  - A PR itself is re-read every time, because Forgejo doesn't bump `updated_at` when `main` moves or a review is requested.
  - An evaluation for a webhook, and the check before a claim, read the item and its blockers fresh.
  - In the dev container's kit, a warm sweep read no unchanged items.

### Measured on the Mac (2026-11-24)

This comes from Forgejo's request log for 12:07 to 15:46 UTC, after the deploy, with `scripts/watch.sh` open throughout. The kit was moderately active, with 19 to 45 webhooks an hour. Requests through `ports`, which came from this investigation reading Forgejo from the dev container, are left out.

| | Before (section 7) | After |
|---|---|---|
| Dispatcher | 13,000–21,000 an hour active, about 12,600 quiet | 970–1,580 an hour |
| Gatekeeper | about 3,400 an hour | 97–165 an hour |
| Both together | 16,400–24,400 an hour | about 1,070–1,750 an hour |
| Busiest minute | 1,644 requests | about 300, a sweep |
| Writes | up to 351 an hour, at a peak | 11–32 an hour, in this window |

Read traffic is down by about 93%, and under GitHub's base 5,000 an hour for an App, with room to spare. What's left of the dispatcher's 5,394 reads in the window:
- **Issue listings: 1,243,** from the sweep and from release evaluations. GraphQL could batch these.
- **Actions-run checks: 520,** for repositories a webhook marked as possibly running CI.
- **Fresh reads of changed items: about 930,** issues and timelines.
- **`main`, `CHANGELOG.md`, `CODEOWNERS` and tags: about 1,000.**

The window had no quiet hour and no peak like the afternoon of the first measurement.

### What this means for a GitHub version

- **The design carries over, but GitHub will cost more requests than Forgejo for the same work.** The dispatcher and gatekeeper now work the way GitHub recommends: webhooks first, a slow reconciliation sweep, and a cache of what was last read. But the cache relies on `updated_at`, and GitHub may bump it for different changes than Forgejo does, so that would need checking again on GitHub. GitHub does support conditional requests, and a 304 doesn't count against the hourly limit. So on GitHub, the PR re-read and the sweep's listings could be conditional, and would then cost nothing when unchanged.
- **Still to do:**
  - measure a quiet hour and a busy one, which the second measurement didn't include;
  - the write queue and budget;
  - webhook delivery from GitHub to the Mac, through a relay or tunnel;
  - the generic/specific split, with the forge as a swappable part.

## 9. Contributions from outside the project

A major reason to support GitHub is to find out whether the kit can accept issues and PRs from people outside the project. It isn't a hard requirement if the problems prove insurmountable. Forgejo mode can't do it, because its Forgejo is private to the Mac, so this is about GitHub mode.

### How trust works today

- Only trusted authors count: the kit's accounts and the human.
- An untrusted account's request goes to its repository's owner to triage, with a description that carries only its number. Its comments start nothing.
- Imported content is never trusted.

The kit has never had to act on what an outsider wrote, beyond deciding whether to triage it.

### Issues

Accepting outsiders' issues is feasible with gates. The risks:
- **Prompt injection.** An agent that reads an outsider's text can be steered by it. The kit's guardrails bound what a steered agent can do: it can't merge without the gatekeeper and conformance, can't release, can't change files only the human may change, and reaches the internet only through the allowlist. But it can still comment with its own token, and its container holds its forge token and Claude token.
- **Cost.** Every outside request costs runs and rate limit, and on a public repository volume and spam are outside the kit's control.

A design that addresses both:
1. **A quarantine agent.** One agent, with a token that can only comment and label, reads outsiders' issues. It writes each up as a request in its own words, and never quotes instructions. Owners act on that write-up, not on the original.
2. **A human gate.** An outsider's request reaches an owner only once the human, or a rule the human sets, accepts it. Before then it costs one quarantine run at most.
3. **Caps.** Per outside author, and per day, using GitHub's interaction limits for new accounts.

### PRs

Accepting outsiders' PRs is harder, because of code execution.
- **Building a Lean PR runs its code.** `lakefile.lean` is a Lean program, and macros, `run_cmd` and `#eval` run during elaboration. An agent that checks out and builds an outsider's PR gives its author code execution inside the agent's container, with the agent's tokens. So **agents must never build an outsider's PR.**
- **CI is safe on GitHub's runners.** A `pull_request` workflow from a fork runs with a read-only token and no secrets **[policy]**. A self-hosted runner on the Mac should never run them: GitHub says self-hosted runners "should almost never be used for public repositories" **[policy]**. GitHub can also require a maintainer's approval before a fork's workflows run.
- **Review is text.** Conformance and the coordinator can review the diff without building it. The diff is still untrusted text, with the same prompt-injection risk as an issue.
- **Merging needs a human.** An outsider's PR merges only with the human's approval, on top of CI and the agents' reviews. Alternatively, an owner agent reimplements the change from the reviewed diff, in a PR of its own.

### Verdict so far [inference]

- **Issues:** feasible, with a quarantine agent, a human gate and caps.
- **PRs:** feasible only under strict rules: never built by agents, CI on GitHub's runners alone, and the human approving every merge. Without those, they are the insurmountable case.

**Decision (2026-11-24): issues only, at first.** Outsiders' PRs are closed automatically, with a note asking for an issue instead. The quarantine agent, the human gate and the caps are the first things to build. PRs can come later on the same machinery, with the extra rules above: agents never build them, CI runs on GitHub's runners, merges need the human, and contribution terms are decided.

## 10. Open questions

- **Local testing without Forgejo standing in for GitHub.** The stage tests need either a fake GitHub API, which drifts from the real one, or a test org on real GitHub, which carries suspension risk of its own. Possibly the forge seam can be tested against Forgejo, with only a thin GitHub adapter tested live. The adapter's retry logic, at least, can be tested without GitHub: a fake `curl` on `PATH` serving canned responses, such as a 429 then a success, makes retries quick and deterministic **[experience]**.
- **Stalls that look like rate limiting.** These are input for the Lean investigation's list of stalls more than for the GitHub question. lean-bump's pipeline stopped mid-cascade, and none of the causes was a rate limit **[experience]**:
  - a "needs a human" label that nothing removed once its cause went away;
  - a "handled already" marker that recorded only the commit, so a successful re-run on that commit was skipped;
  - an update with no changes, which pushed nothing, so CI never re-ran and nothing checked the item again;
  - a validator stricter than the instructions it enforced;
  - configuration missing in one repository.

  This kit has had the same kind of bug, which is why `unstall` exists and why `needs-human` without a written decision is refused. A model of the states could rule out the first three by construction.
- **How many Apps.** At least one per reviewing role, for distinct approvals (section 6). Whether the library agents also need one each depends on whether anything must tell them apart on GitHub, beyond the `From:` line.
- **Adopting existing projects.** The kit must be able to adopt a project already on GitHub, owned by a single account, either working in place on GitHub or by importing it to Forgejo and mirroring back. `generic-specific-split.md` ("Forges and adoption") covers this. Two points belong here:
  - Repositories can stay on a personal account. A private App can be installed only on the account that owns it, but a public App can be installed on other accounts **[policy]**. So the App can be owned by a separate organisation, made public, and installed on the personal account, which then never owns an App. No repositories move.
  - On GitHub Free, a personal account's private repositories get no branch protection or rulesets. They need GitHub Pro **[policy]**. Public repositories have both on Free.
  - Outside contributors' issues and PRs: section 9.
- **Does an App's approval count as a required review?** GitHub's docs say only that approvals must come from those with write access. A community thread says a dedicated App's approval counts, while `github-actions[bot]`'s doesn't **[anecdote]**. Test it on a real repository. If it doesn't count, the kit falls back to what the old kit did: a required status check pinned to a specific App.

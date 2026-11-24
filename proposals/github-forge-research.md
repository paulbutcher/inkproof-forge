# A GitHub kit: what the suspension tells us and what GitHub's policies require

This is research for a possible GitHub version of the kit. Nothing in it changes the kit. It assumes GitHub will never answer the support ticket about the suspended machine user: as of 2026-11-23 the ticket had gone two weeks without a reply.

Labels used below:
- **[policy]** is GitHub's own documentation or terms.
- **[anecdote]** is someone else's report.
- **[inference]** is our reasoning.

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
| GraphQL | 5,000 points an hour |

### Secondary limits

These apply to **every** identity, Apps included.

| Limit | Value |
|---|---|
| Concurrent requests | at most 100 |
| Points per minute, REST | at most 900 per endpoint (a GET costs 1, a write costs 5) |
| Points per minute, GraphQL | at most 2,000 |
| CPU time | at most 90 s per 60 s |
| Requests that create content | **80 a minute and 500 an hour**, web UI included |

The secondary limits can't be queried and can change without notice.

### GitHub's recommendations

- Use webhooks rather than polling.
- Make requests one at a time, not concurrently.
- Wait at least 1 s between writes.
- Honour `retry-after` and `x-ratelimit-reset`. Without them, wait a minute, then back off exponentially.
- Make conditional requests: a **304 doesn't count** against the primary limit.

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

- Use `git clone --mirror`, plus an export of issues and PRs, for example with [python-github-backup](https://github.com/josegonzalez/python-github-backup).
- The org migrations API produces archives that can't be restored on github.com, so it is for archiving only **[policy]**.
- Run backups as an identity separate from the agents', and store them off GitHub.
- A push-mirror to a Forgejo you control gives somewhere to carry on working.

## 6. What a GitHub kit would need

These are ranked by how well the evidence supports them.

1. **One client, one queue.**
   - Only the dispatcher talks to GitHub, and its requests are serial.
   - Writes are at least 1 s apart, with a budget well under 80 a minute and 500 an hour, counting every agent together.
   - It honours the rate-limit headers and stops outright while limited.
   - Agents reach GitHub only through it. Today's `forge` wrapper is the natural place for this.

   The current `FORGE_CONCURRENCY=8` would become 1 for writes **[policy]**.
2. **Webhooks, plus a reconciliation sweep using conditional requests.**
   - Webhooks arrive through a production-grade tunnel or relay.
   - The sweep uses ETags and `since=`, so that sweeps which find nothing new cost nothing. Today's sweep reads all of every repo's open items, so this is a change **[policy]**.
3. **An org-owned GitHub App, not a machine user.**
   - It uses short-lived installation tokens, with no password and no user account to suspend.
   - Its budget grows with the org.
   - It is the identity GitHub's own agents use **[policy]**. That Apps face less abuse risk is **[inference]**, backed by the gatekeeper having survived.
   - One App can serve every agent, with the agent named in each comment's `From:` line, as now. Several Apps would make agents distinct to GitHub, for approvals for example, but cost more to set up.
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

**Reads are the problem, not writes.** A GitHub version needs:
- **A status page that reads the dispatcher's own state**, or a cache, instead of polling every repo. This alone removes about 80% of the quiet traffic.
- **A gatekeeper driven by webhooks**, or one sharing the dispatcher's view of the forge, instead of a 30 s poll.
- **Evaluation from a local cache of issues and PRs**, refreshed by webhook payloads and conditional requests, so that a burst re-reads only what changed.

**Writes need pacing and a budget, not redesign.** A queue that sends at most one write a second keeps them under 80 a minute. An hourly budget of about 400 covers the observed peak of 351, as long as every agent shares one App identity.

## 8. Open questions

- **Local testing without Forgejo standing in for GitHub.** The stage tests need either a fake GitHub API, which drifts from the real one, or a test org on real GitHub, which carries suspension risk of its own. Possibly the forge seam can be tested against Forgejo, with only a thin GitHub adapter tested live.
- **One App or one per agent.** This depends on whether the design needs agents to approve each other's PRs.

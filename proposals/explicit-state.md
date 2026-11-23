# Proposal: explicit state, one holder, one wake policy

Status: adopted and built on 2026-11-23. The human chose to switch at once rather than run shadow mode first (see Built so far). DESIGN.md's Triggers section is now the specification; this records why. It replaces DESIGN.md's Triggers and Stalls sections and the rule layer of `docker/dispatcher/dispatcher.py`.

## Why

The dispatcher already works from state, not events: a webhook only says which items to evaluate, and the sweep evaluates everything. Its weakness is in how it decides what is owed.

- **The state is implicit.** Where a request has got to is inferred from combinations of labels, comments, `Refs` lines, release comments and timestamp comparisons, read by 30 overlapping rules (about 400 lines). No single place says "this request is in state X, so Y owes Z", so nothing can check that every state has a next step. Each gap found on 2026-11-22 was a state no rule covered: unreleased work on `main`, a wait on another agent, a consumer action, a released request labelled `ready-for-release` again, and a request on a closed issue.
- **Two mechanisms do one job.** A rule fires once per version of the state it reads, so an agent that is woken and doesn't move the item isn't woken again. The stall check (about 200 lines), rule 7's follow-ups, and rule 10's day-later nudge each exist to compensate, and each has gaps of its own. The rollout that never started a quiet spell was one of them.
- **Tests can only sample.** The randomised `NobodyOrphaned` test checks the stall check on generated worlds, but its generator and oracle share the dispatcher's blind spots. That is regression testing, not completeness.

## The design in one paragraph

Each unit of work (a request, an own task, a pull request, a repository's unreleased work, a decision for the human, a milestone) has a **state**, derived by one pure function from a small set of **facts** read from Forgejo. Each non-terminal state has exactly one **holder** (an agent, the human, or another unit it waits on) and one **step** for that holder. While a unit's state and holder are unchanged, the holder **owes** the step: it is woken, woken again with backoff if its run ends without the state changing, and after a few tries the unit becomes a decision for the human. Because facts are finite and the state function is total, completeness can be checked exhaustively, not sampled.

## Facts, not prose

The state function reads only facts. Most already exist in Forgejo; a few that today live in prose become structured.

| Fact | Source | Today |
| --- | --- | --- |
| open or closed | Forgejo | same |
| labels: `request`, `new`, `accepted`, `counter-proposal`, `needs-human`, `ready-for-release`, `verified`, `upstream`, `priority` | Forgejo | same |
| `not-verified`: the requester checked the latest release and it falls short | **new label**, added by the requester with a comment saying what fell short | prose ("Not verified: …"), which only rule 2 picks up, and only for the owner |
| blockers: the units its `Blocked-by:` lines name, and whether each is finished | lines, as now | same, but a line naming the unit itself, or forming a cycle, is an invalid fact (below) |
| waiting on: the agent its latest unanswered `Waiting-on:` line names | line, as now | same |
| pull requests referring to it with `Refs`, open and merged, with merge times | Forgejo | same |
| released: its latest `Released in vX.Y.Z` comment | comment, as now | same |
| the repository's last tag, `CHANGELOG.md`'s newest heading on `main`, `main`'s CI | Forgejo | same, read by `unreleased_entry` |
| for a pull request: author, head, CI on the head, reviews at the head, requested reviewers, merge scheduled, mergeable, WIP title | Forgejo | same |

Facts are read into a plain, typed snapshot (one dataclass per unit kind) by an I/O layer that does nothing else. The state function never calls Forgejo and never compares timestamps except through facts already reduced to booleans, such as "merged since its latest release".

An **invalid fact** is not ignored silently, as a self-referencing `Blocked-by:` is today. It is a state of its own, held by whoever wrote it, with the step "fix this line": for example, "your `Blocked-by:` names this issue itself".

## The units and their states

Each table lists the states in the order the function tests them: the first that matches wins. Every unit first checks the common states at the top.

### Common to every open unit

| State | When | Holder | Step |
| --- | --- | --- | --- |
| with the human | `needs-human`, or assigned to the human | human | decide; once they comment, the holder is the unit's next holder below, which acts on their answer |
| upstream | `upstream` | the world outside; the human sees `blocking` | none |
| invalid | an invalid fact | whoever wrote it | fix it |
| blocked | a blocker isn't finished | the blocker (a unit) | none here: the blocker has its own holder |
| waiting | an unanswered `Waiting-on: <agent>` | that agent | answer or act here |

"Blocked" makes the holder another unit, so holders form a graph. The dispatcher follows it and refuses cycles by marking the line that closes one invalid. That makes "everything ends at an agent, the human or the world" a runtime check, not a hope.

### A request (an issue labelled `request` on someone else's repository)

| State | When | Holder | Step |
| --- | --- | --- | --- |
| untriaged | `new` | owner | accept, counter-propose or reject |
| counter-proposed | `counter-proposal` | requester | answer |
| in progress | an open pull request refers to it | that pull request (a unit) | none here |
| to do | accepted; nothing merged for it since its latest release (or ever); not released, or `not-verified` since the latest release | owner | do the work: a pull request with `Refs` |
| merged | accepted; work merged since its latest release (or ever); not `ready-for-release` | owner | label `ready-for-release`, with any `Consumer action:` line |
| ready | `ready-for-release` | its repository's release (a unit) | none here |
| released | released; neither `verified` nor `not-verified` since then | requester | verify against the release; or file the consumer action as its own task and add `Blocked-by:` naming it; or add `not-verified` and say why |
| done | `verified` and closed | none | terminal |
| rejected | closed without `verified` by the owner | requester, once | acknowledge (the only one-shot step) |

`not-verified` after a release sends the request back to "to do", so the gpu#59 loop (fix, release, still broken, fix again) is a cycle through explicit states rather than a comment the owner has to notice.

### An own task (an issue its repository's owner filed, without `request`)

The same as a request, without the triage, counter-proposal and verification states: "released" is terminal, and the release closes it as now.

### A pull request

| State | When | Holder | Step |
| --- | --- | --- | --- |
| in draft | WIP title | author | finish it |
| CI failing | CI failed on the head, or never ran | author | fix it, or merge `main` in for a missing workflow |
| changes requested | a request for changes at the head | author | fix, or reply to the reviewer |
| conflicting | not mergeable | author | resolve it |
| in review | an agent's review requested at the head, CI passed | that reviewer | review |
| with the human | the human's review requested at the head | human | review |
| unscheduled | approved and passing, merge not scheduled | author | schedule the merge |
| stuck | scheduled, ready for longer than `MERGE_STUCK_SECONDS` | author | merge it now |
| merging | scheduled, not yet ready (CI running, settling) | Forgejo | none: a timer, not a holder |
| done | merged or closed | none | terminal |

### A repository's release

This replaces both rule 15 and the `main` check added for demos#105, and makes `ready-for-release` a pointer to this unit rather than its only trigger.

| State | When | Holder | Step |
| --- | --- | --- | --- |
| up to date | nothing unreleased: no issue labelled `ready-for-release`, and `CHANGELOG.md`'s newest heading is not above the last tag | none | terminal (until something merges) |
| CI failing on main | `main`'s CI failed | owner | fix it before anything else |
| waiting for CI | `main`'s CI is running | Forgejo | none |
| heading wrong | something is unreleased, and `CHANGELOG.md` has no heading for the version `forge version` would compute | owner | fix the heading |
| releasable | something is unreleased, CI passed, the heading is right | coordinator (level 3) or human (below) | release, naming every `ready-for-release` issue |

Since the protocol says `CHANGELOG.md` is updated only when preparing a release, an entry above the last tag is itself a request to release. Owners still label the issues the release finishes, so the release can comment on them, but forgetting to do so no longer stalls the release.

### A decision for the human, a milestone, an upstream issue

These are already explicit in today's design and carry over as small state tables: a decision is with the human until they answer; a milestone moves through open, finished, in review, reported and accepted (rules 17, 21, 23, 24); an upstream issue is with the world, `blocking` when an agent's line names it. Writing them as tables is mechanical.

## One wake policy

Every unit's holder, if it is an agent, **owes** the unit's step. The dispatcher keeps one row per (unit, state, holder): the **obligation**.

1. **First wake.** A new obligation queues a run for the holder with a message built from the state: "inkproof-gpu#59 is *merged*: your #67 merged after its release v0.17.0, so label it `ready-for-release`". This replaces 30 hand-written descriptions with one per state.
2. **Discharged** when the unit's state or holder changes. A claim checks this, as `no_longer_owed` does today.
3. **Again, with backoff**, if the holder's run ends and the obligation stands: once at once (the run may have stopped short), then 15 minutes after that run ends, and 15 minutes after the next (`OBLIGATION_BACKOFF`). Waiting longer buys nothing: a change in Forgejo makes a new obligation anyway, and failed runs are retried separately. 15 minutes is no shorter than the kit's settle windows (10 minutes) and its reconcile sweep (15), so a state about to change on its own isn't held against the holder. Only wakes whose runs actually ran count, so an outage doesn't use up tries. A holder that can't act yet says why in a way that changes state: `Blocked-by:`, `Waiting-on:`, `needs-human` with a decision, or `not-verified`. A comment alone doesn't, so a run that says "I'll do it later" is woken again.
4. **To the human** after `OBLIGATION_TRIES` wakes (3) with the obligation unchanged, about 30 minutes after the first run ends: a `Stalled:` decision, as now.
5. **Not while busy.** Quiet obligations (an own task, the next milestone) wait while the holder has other work, as rule 7's do today.

This one policy replaces the once-per-version rule keys, rule 7's follow-ups, rule 10's nudge and two-day escalation, the whole stall check, the main check, and the idle check's role in finding stalls. The idle summary stays, for telling the human what waits on them.

Timers are holders too. "Waiting for CI" and "merging" name Forgejo, with a deadline (`CI_MISSING_SECONDS`, `MERGE_STUCK_SECONDS`) after which the state becomes one an agent holds. So no state can wait for nothing.

## Checking it is complete

Two checks, neither sampled.

1. **The state function is total and has no orphans.** Each unit kind's facts reduce to a finite set of booleans and small enums (a request has about 13). A test enumerates every combination, calls the state function, and asserts that each combination gives exactly one state, and that every non-terminal state has a holder that is an agent, the human, a unit or a timer. With a few thousand combinations per kind, this runs in well under a second.
2. **Every state can reach the end.** A second test explores the abstract machine. From each state, an obedient holder's step produces the next facts, and the environment's moves are nondeterministic: CI passes or fails, a reviewer approves or asks for changes, a requester finds the release short. Breadth-first search over the finite space asserts that from every reachable state a terminal state (or "with the human") is reachable, and that no step leaves the state unchanged. This is model checking in plain Python. If wanted later, the same tables port to Lean, and the two properties become theorems.

What these don't cover is the I/O layer: whether the facts read from Forgejo are right. That stays tested as now, against the fake Forgejo, and the end-to-end simulation (started on 2026-11-22, paused for this proposal) becomes much smaller, because the agents' policies are one step per state.

## What agents change

The protocol gets shorter.

- **Do the step your wake names.** If you can't, record why in one of the four ways that change state (`Blocked-by:`, `Waiting-on:`, `needs-human` with a decision, `not-verified`). Anything else, and you will be woken again, then it goes to the human.
- **Requesters** add `not-verified`, with a comment saying what fell short, instead of writing it only in prose. They remove it with `verified`.
- **Owners** no longer need to remember to label `ready-for-release` for the release to happen. They still should, so the release names the issue.
- Every sentence in the protocol that explains which rule wakes whom can go. The wake message says what is owed and why.

## What stays

Webhooks as hints, the sweep, claims, debounce, runs, retries, caps, pause and drain, priorities, decision formatting and the section check, notices, keeping the `blocked` and `blocking` labels in step, the gatekeeper, the human-session guard and `forge`. The run ledger keeps its tables; `triggers` gains an obligation key in place of the rule key.

## Migration

No item needs editing, because state is derived from facts that already exist. The exception is `not-verified`, which is new; requests whose requester said "not verified" in prose before the switch are found by a one-off sweep and reported to the human to label.

1. **Build the state layer beside the rules.** It includes the fact snapshot, the state function, the holder graph, and both completeness tests. Nothing changes at run time.
2. **Shadow mode.** Run both for a few days in the dev container, then on the Mac. Log every unit where the new holder differs from who the rules woke, or where the new design would wake and the rules didn't. Each difference is either a gap in the rules or a mistake in the tables, and either way is worth knowing.
3. **Switch.** Obligations queue the runs, and the rule functions, the stall check and their settings are deleted. DESIGN.md's Triggers and Stalls sections become the state tables, and project CLAUDE.md's note about keeping rule numbers in step becomes one about keeping the tables in step with the code.
4. **Protocol.** Make the protocol changes above in the same rollout (`scripts/up.sh`), since the wake messages change with them.

## Cost

- About 600 lines of rules and stall checking are replaced by roughly 300 lines of fact reading, 300 lines of state tables and the wake policy, and the two completeness tests.
- Most of `test_dispatcher.py`'s rule tests become fact-reading tests or are subsumed by the exhaustive check. Its tests of claims, runs, retries, caps and idle summaries stay as they are.
- Shadow mode costs nothing but log lines.
- The risk is in the I/O layer, where facts are read wrongly. Shadow mode is there to catch that before anything depends on it.

## Built so far

On 2026-11-23:
- `docker/dispatcher/states.py` holds the facts and states, and `test_states.py` the two checks;
- the dispatcher reads the facts, keeps the obligations and wakes by them (DESIGN.md: Triggers);
- the rule functions for progress, the stall check, rule 7's follow-ups, rule 10's nudges and their settings are gone.

Shadow mode was built, then dropped before it ran: the human chose to switch at once and find what is wrong as it shows. A tally over the old random-world generator's worlds, run while it existed, found the differences expected of the design, and one gap of the old rules it fixes: two issues whose `Blocked-by:` lines name each other woke no one.

Building it settled these, which differ from the design above:

- **Conversation and notices stay as rules.** Only progress becomes states. The rest keeps today's code at the switch:
  - replies to the owner (2), mentions (26) and a `Waiting-on:` line on a closed item (30) are obligations to answer;
  - the rest are one-run notices: 8's upstream comment, 11's rejection, 12, 13, 16, 18, 22 and 28.

  Each needs an agent's look, never its action, so none can leave work without a holder. The wake policy's backoff still applies to the obligations to answer, since an unanswered question stalls work too.
- **One hold for issues and pull requests.** The common states are one fact, `hold`, read in a fixed order: the human, upstream, waiting, an invalid line, blocked, then a bare `blocked` label. It applies to pull requests too, which can carry `Blocked-by:` and `Waiting-on:` lines and `needs-human` (demo#35). Folding them into one fact also keeps the space small enough to enumerate.
- **A state may have several holders.** Reviewers review at once: conformance, the coordinator at level 3, any other reviewer asked for, and the milestone reviewers. Each holder owes the step until it is no longer among them, so the obligation key is (unit, state, holder).
- **What a pull request's author must fix comes first.** That is conflicts, CI failing, CI missing or stuck, and changes requested at the head. An unscheduled merge comes last, once nothing else stands in its way: putting it earlier held up the reviews, which run alongside it.
- **Untriaged** means labelled `new`, or filed by someone else and never touched by the owner, without `accepted` or `counter-proposal` (rule 1 and its unlabelled case).
- **Invalid lines.** A `Blocked-by:` line naming the item itself is invalid only while it is the newest such line, since a comment can't be taken back. A loop of items each blocked by the next is invalid too, which the rules can't see (`loops_back`).
- **The release** compares `CHANGELOG.md`'s heading with the version the gatekeeper would compute (`states.next_version`, held to the gatekeeper's by a test), so a heading it would refuse is the owner's before the coordinator is woken. A `ready-for-release` issue with a hold holds the release. Below level 3, the coordinator holds it until a `Release <repo>` decision is open, then the human does.
- **The kit's own unit** carries rule 23. It is finished when `ROADMAP.md` on the coordination repository has a line `Status: finished`. That is a fact the coordinator must write at the switch, since today it writes the finish in prose.
- **Lateness.** CI running longer than `CI_LATE_SECONDS` (one hour) on a head or on `main` is the author's or owner's to look into, so no timer waits for ever.

Done at the switch:
- the wake policy, with its `obligations` table (`OBLIGATION_BACKOFF`, default `0,900,900`);
- the `not-verified` label, which `setup-repo` and `bootstrap` create;
- the protocol and the coordinator's and supervisor's roles: do the step a wake names or record why where the kit can see it, `not-verified`, `Status: finished`, and a `Waiting-on:` line to hold a release the coordinator can't make;
- the old rules' queued work dropped once at start (`retire_rule_triggers`);
- escalation: the `Stalled:` decision, with the supervisor woken alongside rather than before the human.

## Decisions

Made on 2026-11-23:

- **Backoff and tries.** Three wakes: at once, then 15 minutes later, then 15 more, then the human. The first-cut defaults were one hour then four.
- **Releasing without a label.** A `CHANGELOG.md` entry above the last tag is enough to release. The release doesn't wait for an owner's `ready-for-release`.
- **Milestones** are in the first cut. Rule 23 is the only thing that gives the kit a next step once everything else is finished, so without them the checks couldn't show that the kit as a whole never goes quiet with nothing owed, and the old trigger machinery couldn't be deleted at the switch.
- **Lean** is set aside for now.

# Building the kit's core in Lean

## Decision (2026-11-24)

**The kit's core will be written in Lean, and the kit will have no Python.** The dispatcher, the gatekeeper, the agents' tools and their tests are all to be rewritten in Lean. The rest of this document is the investigation that preceded the decision. Its recommendation, to keep Python with a Lean model beside it, is superseded. Its findings still say where proofs pay off, and what still needs testing ("What the decision means" below).

---

The investigation asked whether to rebuild the dispatcher's core in Lean and prove liveness properties, to avoid the stalls the kit has had. It rests on two pieces of evidence:
- a taxonomy of the kit's real stalls, from the git history and the issues in the human's Forgejo;
- a prototype in Lean, in [lean-model/](lean-model/).

## Summary

- **Proofs would have prevented about a third of the historical stalls.** Of 51 stalls, 34 were mistakes in the state logic, the kind a model can catch. But most of those were states nobody had thought of yet, and a model written at the time would have shared that blind spot. The realistic estimate is 30–45%.
- **Since the state rewrite on 11-23, most of that class is already covered.** [docker/dispatcher/states.py](../docker/dispatcher/states.py) is a pure function over small finite facts, and `test_states.py` checks it exhaustively. Over a finite space, an exhaustive test is as strong as a proof. Porting it to Lean, which the prototype does for releases, adds nothing.
- **Where Lean adds something is in properties of infinite executions,** which an exhaustive check over a snapshot of facts can't state. The prototype proves three things about the obligation scheduler:
  - an ordinary obligation is discharged or escalated within a bound, whatever happens;
  - a **quiet** obligation can starve forever under today's rule, with no one told;
  - a small fix rules that out.

  That starvation is a latent stall the tests can't see.
- **The next stalls are likely to come from places Lean can't reach.** These are the reading of facts from Forgejo, Forgejo's own behaviour, infrastructure, and agents. None of the stalls recorded since the rewrite would a proof have prevented.
- **The investigation recommended** keeping the dispatcher in Python, with a Lean model beside it. That recommendation is superseded by the decision above.

## The stall history

51 stalls, from 11-13 to 11-23. Each comes from a commit fixing it, a `Stalled:` decision, a dispatcher stall-check comment, or a process-review recommendation, found by reading every commit and every issue in Forgejo.

| Category | Count | Typical causes |
|---|---|---|
| A. State logic | 34 | a state nobody had thought of (14), a wake used up by a once-per-version rule (6), a fact derived wrongly in code (7), a marker never cleared (3), the wrong holder (2), a loop of waits (1) |
| B. Forgejo or CI behaving unexpectedly | 6 | a scheduled merge that never fires, labels silently dropped, `page` ignored on comments (the dispatcher's loop to 82 GiB), CI that never ran on a branch |
| C. Infrastructure | 3 | the internet outage, usage limits, a lost claim reply |
| D. Agent behaviour | 7 | an agent waiting in prose instead of with a fact the kit reads, misreading `API\.lock`, misleading tool output |

Would a Lean model have caught them?

| Verdict | Count | Meaning |
|---|---|---|
| Yes | 25 | a model of the states, with explicit fairness, would plausibly have shown it |
| Partly | 14 | only if the model also captured Forgejo's or CI's behaviour, or the code that reads facts |
| No | 7 | parsing, Forgejo quirks, infrastructure |
| Bounded | 5 | agent failures no proof prevents, but a proved bound on escalation limits them |

The "yes" figure is an upper bound. Many of the state-logic stalls were states that didn't exist yet:
- the kit as a unit (stall 9);
- an owner's own tasks (32 and 40);
- a repository's unreleased work (47);
- waits from one agent to another (33 and 44).

A model only proves things about the states it has, so it would have been as complete as the people writing it, and no more. `proposals/explicit-state.md` says the same about the earlier test generator. What a model catches reliably is the structural class: a wake spent once per version, markers never cleared, loops of waits, and steps that change nothing the state depends on. That's roughly 12 to 15 stalls.

The properties that would have caught the most, ranked:
1. Every unfinished state has a holder who is woken, and woken again while the state stands: about 17 stalls.
2. Every step a holder may take, "I can't yet" included, changes a fact the state depends on: about 9.
3. No marker outlives its cause: 4.
4. Every state held by Forgejo or CI has a deadline that hands it to an agent: 3.
5. The holder is right, and can act: 3.
6. An unresolved obligation reaches the human within a bounded number of wakes: bounds every agent-behaviour stall.
7. The waits between units have no loops: 1.

The rewrite on 11-23 addressed most of these:
- `states.py` and `test_states.py` cover 1 to 3 and 5 for each unit, exhaustively.
- The obligation layer's backoff and escalation is 6.

Since then, two stalls are recorded: usage limits (infrastructure), and one agent stall that escalated as designed. A proof would have prevented neither.

## The prototype

In [lean-model/](lean-model/), built with Lean 4.36.0 and core Lean only, no Mathlib. Every theorem depends only on Lean's standard axioms (`propext`, `Quot.sound`): no `sorry` and no `native_decide`. Building it takes about a second.

**[Release.lean](lean-model/Kitmodel/Release.lean): the release states, ported line for line.** It ports `release_state` and `test_states.py`'s `release_steps`, and proves the three properties `test_states.py` checks:
1. A state is finished exactly when it is "up to date".
2. Every step moves the unit on.
3. Every combination can finish.

All three are proved by kernel computation over the 384 combinations of facts. The port was mechanical. The one thing it surfaced is that finishing takes up to 12 steps: a first attempt at depth 6 failed for 12 combinations. Otherwise this shows what was expected: over finite facts, Lean proves what the exhaustive Python test already establishes.

**[Obligation.lean](lean-model/Kitmodel/Obligation.lean): one obligation and its agent.** This abstracts `dispatcher.advance()`. The environment chooses at every step whether the agent is busy with other work, and whether a run discharged the obligation. The theorems are about every infinite execution:
- **`ordinary_settles`.** However the agents behave, an ordinary obligation is discharged, or becomes a `Stalled:` decision, within `tries + 1` steps.
- **`quiet_starves`.** Under today's rule, a **quiet** obligation ("to do", "in draft", "unscheduled", "what next") waits while its agent is busy, and the busy check comes before escalation in `advance()`. So for an agent that always has other work queued or running, the obligation is never woken and never counts a wake. It never becomes the human's. The kit's other safety nets don't fire either, because the supervisor and the idle summary both wait for the kit to go quiet. A busy coordinator, which every PR and release gives notices, is the likeliest case. It isn't in the stall history, but the rule only arrived on 11-23.
- **`patient_settles`.** If a quiet obligation waits for a busy agent at most `patience` times, it settles within `(tries + 1) × (patience + 1)` steps, however busy its agent is kept.

The model is an abstraction: one obligation, one call of `advance()` per step. Its theorems hold of the model, and it's the fidelity of the model to `advance()` that makes them say something about the kit. Here the correspondence is close: a few dozen lines of `advance()` and `busy()`.

## What a Lean core would and wouldn't buy

| Where stalls come from | What helps | Does Lean help? |
|---|---|---|
| The states of one unit | exhaustive checks (already in place) | no more than the checks |
| The scheduler over time: wakes, backoff, escalation, quiet obligations, caps, pause | proofs over infinite executions | **yes**, and nothing else does |
| Waits between units (`unit:blockers`, `unit:release`, `unit:held` and so on) | a model of all the units together | **yes**: today they are checked one unit at a time |
| Reading facts from Forgejo (about 3,000 lines of Python) | tests against a real Forgejo, property tests of the parsers | only if that code were in Lean and specified against Forgejo, which can't be done |
| Forgejo's and CI's behaviour | tests against the real thing, deadlines on every state they hold | only to state the assumptions explicitly |
| Infrastructure and agents | monitoring, the protocol, bounded escalation | only the bound |
| States nobody has thought of yet | randomised worlds drawn from Forgejo's data model, checked by simulation (below) | through Plausible, if the code that reads facts is in Lean |

### Finding states nobody has thought of yet

Of the 34 state-logic stalls, 14 were states the model didn't have. A proof can't find those, and neither can random testing over the model's own facts. A random fact can only reach a state the model already has, and `test_states.py` already enumerates that finite space.

Randomised testing one level down can find them, if three things hold:
- **Worlds are generated from Forgejo's data model, not the kit's.** Issues and PRs with any combination of labels, authors, open or closed, `Refs` between any items, and comments by anyone. A world like "the owner's own unlabelled issue, with a merged PR referring to it" then turns up without anyone anticipating it.
- **The check doesn't use the state function.** For example, simulate agents doing anything the protocol allows, and assert that the kit never goes quiet with an open item that nobody owes and that the human hasn't been told about. A missing state then shows up as a world that stops with open work.
- **The real code that reads facts is in the loop,** so the test also exercises the layer proofs can't reach.

The kit has tried part of this before. `proposals/explicit-state.md` records that the randomised `NobodyOrphaned` test's "generator and oracle share the dispatcher's blind spots": its worlds were shaped like the rules' expectations, and its check reused the stall check. Even so, it found the two `Blocked-by:` lines that name each other. Generating from Forgejo's data model, and checking by simulation, fixes both weaknesses.

Of the 14 missing-state stalls, such a test could plausibly have found #2, 4, 9, 10, 12, 16, 19, 32, 38, 40 and 47. That's a judgement from the taxonomy, not a measurement. It couldn't have found:
- **Gaps in what agents could express,** such as waits from one agent to another (#33, #44), which agents could only state in prose until the protocol changed.
- **Forgejo behaving differently from the fake,** which a generator over the fake can't reveal. Contract tests against the real Forgejo keep the fake honest.

The tool depends on the architecture. [Plausible](https://github.com/leanprover-community/plausible), Lean's property-based testing library, tests Lean code. So it reaches the code that reads facts only if that code is in Lean. With the dispatcher in Python, Hypothesis does the same job against the fake Forgejo.

## Architecture options

| Option | What it means | Cost | What it buys |
|---|---|---|---|
| **1. The whole dispatcher in Lean** | webhook server, Forgejo client, SQLite, timers, threads | medium to high: the libraries exist, but rewriting about 3,000 lines is the cost (see below) | proofs about the code that runs, but the code that reads facts stays unproved, and that's where the remaining risk is |
| **2. A Lean core inside Python** | the states and the scheduler's decisions compiled from Lean, called from Python over JSON or a C library; Python keeps all I/O | medium: about 600 lines to port, plus the boundary | proofs about the decisions that run; two languages in the kit |
| **3. A Lean model beside Python** | the model is the specification. For the states, Lean prints every combination of facts with its state and holders, and a test compares Python's answers, exhaustively. For the scheduler, property tests replay the model's executions against `advance()` | low | the liveness proofs, and a check that Python matches them; drift is caught by the tests, not ruled out |

The libraries option 1 needs all exist:
- **Webhooks.** `Std.Http.Server`, in Lean core (4.36.0 here), is an HTTP/1.1 server with keep-alive, chunked encoding and limits. It describes itself as low-level.
- **The forge client.** [leancurl](https://github.com/paulbutcher/leancurl), a binding to libcurl, covers HTTPS too, so it would serve GitHub as well as Forgejo. It needs libcurl and `pkg-config` where it's built.
- **The ledger.** [leansqlite](https://github.com/leanprover/leansqlite), from the Lean FRO, bundles SQLite. Its README calls its higher-level API experimental.
- **JSON.** `Lean.Json`, in core. (Superseded: lean-json, as the web stack uses; see "What is rewritten".)

So the cost of option 1 is the rewrite itself, and building native dependencies into the dispatcher's image for arm64. It isn't missing libraries. The recommendation rests on where proofs pay off, not on what Lean can do.

Option 1 has one advantage the others lack: one language for both kinds of assurance. Proofs would cover the abstract model, and Plausible the step from raw Forgejo data to facts, in the same code. With options 2 and 3, the randomised tests are in Python (Hypothesis) and the proofs in Lean, about different code.

**The investigation recommended option 3. The decision is option 1, and further: no Python anywhere in the kit's own code.** With everything in Lean:
- the proofs apply to the code that runs;
- Plausible can test the step from raw Forgejo or GitHub data to facts, in the same code;
- there's no boundary between languages.

## What the decision means

### What is rewritten

| Part | Python today | In Lean |
|---|---|---|
| Dispatcher | `dispatcher.py`, `states.py`, `activity.py` (about 3,600 lines) | webhooks on `Std.Http.Server`, the forge API through leancurl, the ledger through leansqlite, JSON through lean-json (as the web stack uses, rather than `Lean.Json`, which links the compiler's front end into the binary), and the human's web UI ("The human's web UI" below) |
| Gatekeeper | `gatekeeper.py` (513 lines) | the same libraries |
| Agents' tools | `forge`, `agent-runner`, `inkproof-hook` (about 1,150 lines) | compiled executables in the agent image. `inkproof-hook` runs on every tool call, so its start-up time matters; a compiled binary should be fast |
| Tests | `test_dispatcher.py`, `test_states.py`, `test_gatekeeper.py`, `test_forge.py`, `test_inkproof_hook.py` (about 2,360 lines) | Lean tests run by `lake test`, with a fake forge, and Plausible for properties |
| Host scripts | inline `python3 -c` in about ten scripts, which read the ledger or the status, or probe the network | the web UI for everything the human watches or controls day to day (status, agents, pause and drain, notices, activity, reviews); subcommands of the dispatcher's executable for what remains, and `curl` for the probes |
| Images | the dispatcher's and gatekeeper's images are Python's | Lean's toolchain at build time; a slim runtime image with libcurl. arm64 for the Mac |

The rewrite keeps the ledger's SQLite schema, so the Lean dispatcher opens the Python one's ledger, and switching back stays possible.

### Order of work

1. **The pure core, with its proofs.** Port the states and the obligation scheduler from the prototype, with the quiet-obligation fix. Prove the scheduler's properties (bounded escalation; woken again while the state stands), the absence of loops in the waits between units, and that every cycle through the world is bounded (property 8, "Retries an agent can repeat forever" below), with the run budget per unit as a backstop. This needs no I/O, and settles the core's types.
2. **The gatekeeper, as the first whole service.** It is the smallest service, and it exercises everything the dispatcher will need: an HTTP server for webhooks, forge API calls, JSON, and an image built for arm64. Stage 6 tests it unchanged, because it tests behaviour, not language.
3. **The agents' tools:** `inkproof-hook`, `forge` and `agent-runner`, checked by stage 3 and their ported unit tests. `forge ci --rerun` enforces the re-run limit, and a test with a CI that always fails the same way checks that the limit holds and that the human is told.
4. **The dispatcher.**
   - Port `test_dispatcher.py`'s cases first, against a fake forge in Lean, so the rewrite has its tests from the start.
   - Then the fact-reading layer and the HTTP and ledger plumbing.
   - Then the web UI, read-only pages first (the kit, agents, notices), then its buttons (stop a run, stop or start an agent, pause, drain, resume, mark a notice seen), then activity and reviews. Then the subcommands left for host scripts.
   - Stages 4, 5, 7 and 9 test it, changed only where they read the kit's state from `status.sh`, which they read from the UI's JSON instead.
5. **The randomised world test, with Plausible:** worlds drawn from the forge's data model, checked by simulation, against the real fact-reading code.
6. **Switching the Mac's kit.** Only once every stage test passes in the dev container. The Mac's kit is production, and the ledger's compatibility keeps a way back.

This fits the other two plans. The forge seam (`generic-specific-split.md`, seam 12) can be built into the Lean code from the start, rather than added to Python and ported. The GitHub adapter is then written once, in Lean, using leancurl, which covers HTTPS.

### The human's web UI

Decided 2026-11-25: the human runs the kit through a small web UI that the kit serves, rather than through command-line tools. The UI is where the human:
- starts and stops agents;
- sees the kit's state;
- is notified.

It uses the web stack of [lean-todomvc-max](https://github.com/paulbutcher/lean-todomvc-max): `Std.Http.Server`, HTMX with as little JavaScript as possible, lean-html and lean-htmx for typed markup, lean-css, lean-routing, lean-middleware for sessions and anti-forgery, and lean-json.

**What it replaces:**

| Today | In the UI |
|---|---|
| `status.sh`, `watch.sh` | **The kit page:** the kit's state line (working, stuck, failing, quiet) and what's behind it, what waits on the human with links into the forge, and CI. HTMX refreshes it every 15 seconds, as `watch.sh` does. |
| `status.sh`'s per-agent part, `stop-run.sh` | **The agents page:** each agent's state, queued work, what it owes and how often it has been woken, runs today and spend, and its recent runs, each with its log. Each agent has buttons to stop its run, and to stop or start it, meaning whether it may claim work. Each run's transcript is still opened in VS Code or the Claude app. |
| `pause.sh`, `resume.sh`, `drain.sh` | **Buttons on the kit page,** with the drain's progress shown there until it is safe to stop. |
| `notify.sh` and its launchd job | **The notices page:** an inbox of the dispatcher's notices, each one marked seen by the human. While a tab is open, the browser's own notifications show each new notice. A page served from `localhost` is a secure context, so this needs no certificate. Marking a notice seen is also what restarts a unit's run budget ("Retries an agent can repeat forever" below). |
| `activity.sh`, `reviews.sh` | **The activity and reviews pages,** with the same filters. |

**What it doesn't replace.** Decisions stay in the forge. A decision is still an issue the human answers with a comment, because agents read it there, and the forge's mentions and assignments go on notifying as before. The UI links to them; it doesn't duplicate them. Setting up and running the kit itself also stays on the command line: `bootstrap`, `setup-repo`, `new-library`, secrets, `up` and `down`, images, backups and restores, `open.sh` and `remote.sh`. The UI runs inside the kit, so it can't start the kit, and starting or stopping an agent in the UI never touches Docker. The dispatcher simply stops or resumes giving that agent work, so the UI needs no Docker socket.

**Where it runs.** It is served by the dispatcher, which already holds everything the UI shows: the ledger, the notices, the owed steps, the item index and the CI flags. Like today's status page, it reads nothing from the forge. It listens on its own port, separate from the webhook endpoint, and that port is published only on the Mac's loopback interface.

**Agents must not reach it.** An agent that could press the UI's buttons could pause the kit, resume a paused one, or mark its own notices seen. So:
- **The UI's port is on a Docker network the agents aren't on,** and the webhook port accepts only the webhook routes.
- **Every request needs a session.** The human signs in with a secret the agents never see, and the session is kept in a sealed cookie (lean-middleware).
- **Every button carries an anti-forgery token,** so another page open in the human's browser can't press one.

These are the kind of properties the stack already proves (`csrfAttrs_nonempty_iff`, `nothing_mutates_without_write`), and the UI should prove its own: no route that changes anything is reachable without a session and a token.

**Tests.** Page views are pure functions from the ledger's data to typed markup, so `lake test` tests them directly, as lean-todomvc-max tests its views. The stage tests read the kit's state today from `status.sh`'s output (stage 4 matches its wording). They need the same facts from the UI. Each page should also serve its data as JSON when asked, so that a test, or a future script, needn't parse HTML.

**Settled (2026-11-25):**
- **The ledger stays in SQLite,** through leansqlite. It keeps the ledger compatible with the Python dispatcher, and so the way back at the switch, and adds no database service to the kit. Where the web stack's libraries assume Postgres, the kit uses them without their database parts, or with leansqlite in their place.
- **The UI is reachable only at the Mac, for now.** It is published on loopback alone. Being told while away stays with optional SMTP and with the Claude app through Remote Control. Reaching the UI from elsewhere would need proper sign-in (lean-authentication), and is left for later.

### Retries an agent can repeat forever

A requirement found on 2026-11-25, after the taxonomy above: a 52nd stall, in category D, of the kind a proved bound limits.

**What happened.** On 2026-11-25, `demos` re-ran CI on tentenths/inkproof-demos#176 41 times over about 9 hours. Every attempt failed on the same browser test timeout, a fault in the test rather than the runner. Each re-run cost an agent run and about 12 minutes of the runner, and nothing reached the human. The loop:
1. CI fails on the pull request's head. The state is `CI failing`, and its author is woken.
2. The author reads the log, takes the timeout for a runner fault, and runs `forge ci <repo> <pr> --rerun`, as PROTOCOL.md allows.
3. The re-run puts the pull request back in `CI running`, so the author owes nothing, and the run ends successfully. When the re-run fails, step 1 starts again.

**Why nothing stopped it:**
- **The protocol's limit relies on the agent's memory.** "If it fails the same way again, it isn't the runner" assumes the agent knows. But each run is a fresh session. Nothing told it that the failing run was a re-run, or how many attempts it had had, though Forgejo had counted 40, and the agent left no comment recording that it re-ran.
- **The escalation counts wakes without acting.** A re-run is acting, and the state changes each time, so the count never grows.
- **The retry cap counts failed agent runs,** and every one of these runs succeeded.
- **Nothing else limits re-runs of one head commit,** or tells the human when they repeat.

**Why the checks and the prototype miss it.** Every property they check holds. Each step moves the unit on (`CI failing` to `CI running`). Finishing stays reachable, because CI might pass. And `ordinary_settles` bounds only wakes without acting. The loop goes through the world: an agent's step hands the unit to CI, and CI hands it back. A proof that assumes CI eventually passes a re-run would "prove" this loop ends, and that assumption is false for a test that is broken.

**What the Lean kit does:**
- **Attempts are a fact read from the forge,** never from the agent's memory: the number of times the head commit's run has been run (Forgejo's attempt count, GitHub's `run_attempt`). Whether Forgejo's API exposes the count needs checking; if it doesn't, the kit records each re-run in the ledger when `forge ci --rerun` asks for it.
- **A limit per head commit,** a setting that defaults to one re-run, which is what the protocol means today. A new push starts again from zero, because it is new code. The same applies to `main`'s runs, which are re-run under the release states.
- **The tool enforces it.** `forge ci` shows the attempt count, and `--rerun` refuses past the limit, saying why and what to do instead: fix the code, or say on an issue what's wrong. Once a re-run is no longer possible, every step left to the author either changes the code or doesn't count as acting, so the ordinary escalation bounds the rest.
- **Reaching the limit tells the human,** as a notice. The pull request stays with its author, but a test that fails the same way on unchanged code is something the human wants to know about, even if the agent then fixes it.
- **DESIGN.md's Triggers and `forge ci` sections, and PROTOCOL.md, describe this,** rather than the current rule.

**What the proofs must say.** A new property, alongside the seven above:

8. **Every cycle through the world is bounded.** With the world adversarial, CI failing every time and reviewers asking for changes every time, an agent can't restart the same wait on the same code more than a fixed number of times, and the bound reaches the human.

This means modelling the world as adversarial wherever an agent's step hands the unit to it, and making the attempt count part of the facts. Each such step needs checking for the same loop: a CI re-run, a re-requested review, a scheduled merge that is scheduled again, a `Waiting-on:` that is set again. Each needs either a count the kit reads, or an argument that it can't repeat.

**A run budget per unit, as a backstop** (decided 2026-11-25). Property 8 deliberately leaves alone loops that change the code each time, such as an author pushing attempted fixes indefinitely. To catch those, and loops nobody has thought of yet, each unit has a budget of agent runs:
- **What counts:** every agent run woken for the unit, by any holder, whether it acted or not. The count comes from the ledger. Today's ledger already links each run to the triggers it took (`triggers.run_id`), and a trigger's key names its unit, so the count is a query, not a new record.
- **When the budget is spent, the human gets a notice,** naming the unit, its state and how many runs it has had. That is all: the unit keeps its state and holders, and agents keep working on it, because some pull requests legitimately take many rounds.
- **The notice is given once per budget.** Once the human marks it seen in the web UI, the count starts again, so a unit that keeps going sends another notice after another full budget, never one per run.
- **The budget is a setting,** with a default to be chosen from the ledger's history. It should sit well above what an ordinary pull request or issue takes.
- **It is in the proofs as a bound:** under any behaviour of the agents and the world, a unit can't take more than a budget of runs without the human being told. Since the count comes from the ledger rather than from the states, this holds even for a loop the states don't describe.

### Settled

- **"No Python" covers the kit's own code, not third-party tools.** The agent image keeps Python for `lean-lsp-mcp`, the Lean MCP server the agents use.
- **The Python dispatcher is left as it is until the switch,** quiet-obligation starvation included. The fix arrives with the Lean scheduler (step 1).

## Next steps (from the investigation, before the decision)


1. **Fix quiet starvation in the dispatcher.**
   - The proposed rule: a quiet obligation waits for a busy agent at most a few times (`patience`), then wakes it anyway.
   - Write a unit test of the starving case first, to show the fix is needed in the real code and not only in the model.
2. **Extend the model to the waits between units.** Prove that the waits form no loops and that every chain ends at a holder who can act. The dispatcher already refuses a `Blocked-by:` loop; prove it can't arise anywhere else.
3. **Extend the scheduler model:**
   - several obligations per agent;
   - caps and pause, held by the human, with the human assumed to act eventually;
   - retries that don't count environment failures.

   Prove property 1 (woken again while the state stands) and property 6 (bounded escalation) for it.
4. **Generate the comparison tests from the model** for `states.py` and `advance()`, and run them in `scripts/test-dispatcher.sh`.
5. **For the fact-reading layer, which Lean can't reach:**
   - contract tests against the dev container's real Forgejo, for the behaviours the stalls exposed (scheduled merges, dropped labels, paging, requested reviewers, `updated_at`);
   - property tests of the parsers (`Refs`, `Blocked-by:`, `Waiting-on:`).
6. **A randomised world test, to find states nobody has thought of yet.**
   - Generate worlds from Forgejo's data model, not from the kit's facts.
   - Run the real code that reads facts and evaluates states, with simulated agents doing anything the protocol allows.
   - Assert that the kit never goes quiet with an open item that nobody owes and that the human hasn't been told about.
   - Use Plausible, now that the code that reads facts moves to Lean.
   - Count it a success if it finds a state the model lacks.

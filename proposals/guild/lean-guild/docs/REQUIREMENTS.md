# Requirements

The guild is a kit that runs a team of Claude Code agents on a Lean project through a forge, with a human in charge. Agents own repositories, take work from issues, open pull requests, review each other's work, and release; a dispatcher decides who is woken for what; a gatekeeper approves on CI; the human decides what the kit can't. This repository is the generic kit. A consuming project, such as [inkproof-guild](https://github.com/paulbutcher/inkproof-guild), is a separate repository that supplies the project layer and pins a guild release.

## The reference

- The guild reimplements [inkproof-forge](https://github.com/paulbutcher/inkproof-forge), a working kit written in Python and shell, at the commit named in [PLAN.md](PLAN.md#the-reference). Clone it beside this checkout (`../inkproof-forge`) at that commit, read it, and never change it.
- Its `DESIGN.md` is the behavioural baseline: the guild does what it describes, except where this document says otherwise. Its `RUNBOOK.md` describes how a human operates it.
- Its `proposals/` hold the reasoning behind every change listed here: `lean-core.md` (the core in Lean, its properties, the web UI, retries), `generic-specific-split.md` (layers and seams), `github-forge-research.md` (GitHub mode) and `lean-model/` (the prototype proofs).
- Its `docker/dispatcher/states.py` and `test_states.py` define the units' states and the completeness checks; its `scripts/test-stage*.sh` define acceptance.
- Where the reference and this document disagree, this document wins. Where both are silent, ask the human.

## Principles

- **Lean, and no Python.** The guild's own code, tests and tools are Lean, with shell where Lean is overkill. Third-party tools may use Python, such as `lean-lsp-mcp` in the agent image.
- **Generic.** No project's names appear in the guild's code or tests. Everything a project decides is in its configuration, and the guild is tested with test projects of its own.
- **Proved where it pays.** The scheduler's liveness and the waits between units are theorems over infinite executions, as in the reference's `lean-model/`. Finite state spaces are checked exhaustively by the kernel.
- **Correctness before compatibility,** until 1.0. Breaking changes are allowed, and each is recorded in `CHANGELOG.md`.
- **Never near production.** Nothing in this repository runs against a real project's forge or repositories. Every test runs in the dev container's own Docker engine.

## Layers

- **Kit:** how agents, the dispatcher, the gatekeeper, the forge, CI and the human work together. Lives in `Guild/` and the kit's images and scripts.
- **Lean layer:** what every Lean project needs: elan, `lean-lsp-mcp`, CI's `lake build` and tests, the library template, `API.lock` and its generator, Lean's allowlist entries and protocol sections. Lives in `layers/lean/`.
- **Lasm layer, optional, on top of Lean:** what a project targeting the browser needs: Node, Playwright, Chromium and WebKit, npm, browser CI steps, the `site` kind, and the optional macOS runner. Lives in `layers/lasm/`. A kit without it builds, runs and passes its tests.
- **Project:** in the consuming repository. See "The project interface".
- **The forge** is a separate axis: Forgejo or GitHub, behind one adapter each.

## The project interface

- A consuming project is a Lake package that requires `guild` at a release tag and builds the kit's executables with its configuration.
- Static configuration is Lean values of the guild's types, checked when the project compiles: agents and their kinds, repositories (an explicit list, not a prefix), dependencies between them, reviewers, upstream projects and the agents that track them, autonomy levels, bounds, layers, and the forge mode. A reviewer that names no agent, or a dependency on an unlisted repository, is a compile error.
- Deployment settings and secrets (ports, paths, tokens, the human's account) are read from the environment at run time, never compiled in.
- Text the project supplies (roles, protocol sections, `REQUIREMENTS.md`, `PRIOR-ART.md`) stays as files. A module that embeds one with `include_str` declares it as a Lake `input_file` with `text := true` and lists it in the library's `needs`, or edits to it rebuild nothing.
- The reference's rules that name agents become configuration (`generic-specific-split.md`, seam 4), and its repository contract becomes explicit (seam 13).

## States and the scheduler

- Port the reference's states for every unit (issues, pull requests, releases, milestones, the kit) and its obligations, with the reference's `test_states.py` checks as kernel-checked theorems.
- Prove the properties ranked in `lean-core.md`: every unfinished state has a holder who is woken, and woken again while it stands; every step changes a fact the state depends on; no marker outlives its cause; every state held by the forge or CI has a deadline; an unresolved obligation reaches the human within a bounded number of wakes; the waits between units have no loops.
- A quiet obligation waits for a busy agent at most `patience` times (`patient_settles`), fixing the reference's starvation.
- Property 8: every cycle through the world is bounded. CI re-runs are counted per head commit from the forge, not the agent's memory; `forge ci --rerun` refuses past the limit (default one); reaching it sends the human a notice. Every other step that hands a unit to the world is checked for the same loop.
- A run budget per unit: when a unit's agent runs, counted from the ledger, reach the budget, the human gets one notice; marking it seen restarts the count. It is proved as a bound that holds whatever the states say.

## The human's web UI

- The dispatcher serves a minimal web UI built on the stack of [lean-todomvc-max](https://github.com/paulbutcher/lean-todomvc-max): `Std.Http.Server`, HTMX, lean-html, lean-htmx, lean-css, lean-routing, lean-middleware and lean-json.
- Pages: the kit (its state, what waits on the human, CI, pause, drain and resume); agents (state, queued and owed work, runs and spend, stop a run, stop or start an agent); notices (an inbox, mark seen, browser notifications while open); activity; reviews. Each page also serves its data as JSON, for tests.
- It reads nothing from the forge. Decisions stay in the forge as issues, and the UI links to them. Stopping an agent stops its work being given out, and never touches Docker.
- It listens on its own port, published only on the host's loopback, on a network agents aren't on. Every request needs a session signed in with a secret agents never see, and every action an anti-forgery token. Prove that no route that changes anything is reachable without both.
- It replaces the reference's `status.sh`, `watch.sh`, `stop-run.sh`, `pause.sh`, `resume.sh`, `drain.sh`, `notify.sh`, `activity.sh` and `reviews.sh`. Setting up and running the kit (bootstrap, repositories, secrets, up and down, images, backups, opening an agent's container) stays on the command line.

## The ledger

- SQLite through leansqlite.
- The guild opens a ledger written by the reference's dispatcher, and the reference's dispatcher opens one the guild has written, so a consumer can switch to the guild and back. Changes to the schema are additive only.

## The forge

- One adapter per forge, chosen by configuration, behind everything the kit does through a forge: API calls, the agents' `forge` command, identities, protection, CI and webhooks (`generic-specific-split.md`, seam 12). Forgejo first, with no change in behaviour; GitHub second.
- Load stays at or below the reference's after its fixes: webhooks rather than polling, an item cache validated by `updated_at`, CI flags marked by events, and a reconciliation sweep. The reference measured about 1,070 to 1,750 requests an hour.
- Two GitHub modes: work on GitHub with no Forgejo, and import into Forgejo with a mirror to GitHub. On GitHub: GitHub Apps rather than machine users, one serial client per installation, conditional requests, and webhooks with a sweep (`github-forge-research.md`).
- Adoption: an existing project whose repositories are all owned by one GitHub account can be adopted, and takes on the kit's conventions through onboarding pull requests. Moving it to an organisation is a last resort.
- Outsiders may file issues only. Their pull requests are closed with a note asking for an issue, and no CI runs on them.

## Images, CI and browsers

- Images are built in stages, one per layer: the kit's base (Claude Code, the forge CLI, git, the kit's compiled tools), Lean on top, Lasm on top of that. The project's configuration chooses the top stage. CI's image is built the same way.
- Proxy allowlists are the kit's list plus each used layer's and the project's, joined when the proxy starts.
- The Lasm layer installs Playwright's WebKit beside Chromium.
- Optionally, the Lasm layer provides a `macos` CI job run by a runner inside a Tart macOS VM on the host, for Safari and a real GPU (`generic-specific-split.md`, "Real GPUs and Safari"). A kit without the runner skips the job rather than failing it.

## Acceptance

- `lake build` and `lake test` pass with no warnings, no `sorry`, and theorems that depend only on Lean's standard axioms.
- The reference's stage tests are ported, in shell, against the guild's own test projects: a plain Lean project, and a small one with Lasm. Stage 2, which imported Inkproof from GitHub, becomes the adoption tests.
- Contract tests against a real Forgejo in the dev container keep the fake forge honest: scheduled merges, dropped labels, paging, requested reviewers, `updated_at`, and run attempt counts.
- A randomised world test, with Plausible, draws worlds from the forge's data model, runs the real fact-reading code with simulated agents, and asserts that the kit never goes quiet with an open item nobody owes and the human hasn't been told about.

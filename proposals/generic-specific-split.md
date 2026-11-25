# Splitting the kit into generic and specific parts

This is an investigation; nothing in it changes the kit yet. The question is how easily the kit can be divided into what every kit shares and what belongs to one project, so that a new project can have its own kit and still receive the fixes made in this one.

## Summary

The split is very feasible, because the kit is already mostly generic.
- **Configuration.** Agents, their kinds and repositories already come from `agents.conf`. The roles already live in their own files. Names, the organisation and the repository prefix come from `.env`.
- **The code and the design.** Project-specific content is concentrated in a few places:
  - the dispatcher has 22 such lines out of 3,144;
  - the gatekeeper has its versioning rule;
  - `DESIGN.md`'s sections average about one such line each.
- **A second kit from one checkout.** `KIT_ENV` and `STATE_DIR` let a second kit run from the same checkout already. The dev container uses them, with its own port, secrets and state.

The work is in three places:
1. **Taking out what is specific to Lean, and to Lasm on top of it** (toolchains, CI, images, templates and `API.lock`), which is mixed in with the rest, so that a kit can be built for plain Lean or for Lean with Lasm.
2. **Moving rules that name this project's agents into configuration.** These are the Lasm rules and the list of milestone reviewers.
3. **Giving the kit's own tests a small project of their own.** At the moment they run against Inkproof's repositories.

Two further requirements go beyond the split (see "Forges and adoption"):
4. **The kit must run on either forge.** One mode is Forgejo with a mirror to GitHub, as now. The other is GitHub alone, with no Forgejo. So the forge becomes a seam of its own, with two adapters.
5. **The kit must be able to adopt an existing project on GitHub,** owned by a single account, in either mode. So the project layer can be derived from existing repositories, and what the kit requires of a repository becomes an explicit contract.

Estimate: one careful pass of a few days for the split inside this repository, then a smaller step to move the generic part into its own repository. The GitHub adapter is the largest single piece of work here, larger than the split itself.

## Layers

"Generic or specific" hides two middle layers. Kits will be for two kinds of Lean project: those built with Lean's own toolchain, and those that also target the browser through Lasm. So the ecosystem is two layers, one on top of the other:

| Layer | What it is | Examples |
|---|---|---|
| **Kit** | How agents, the dispatcher, the gatekeeper, the forge, CI and the human work together, whatever the project | dispatcher, runner, gatekeeper, proxies, backups, pause and drain, the human's web UI (`lean-core.md`), the protocol's workflow sections |
| **Lean** | What every Lean project needs | elan and `lean-toolchain`, `lean-lsp-mcp`, the CI image's Lean part, `checks.yml`'s `lake build` and tests, the library template, `API.lock` and its generator, Loogle and Reservoir in the allowlists, the protocol's Lean tools and reuse sources (Std, Batteries, Mathlib) |
| **Lasm** (on top of Lean) | What a project needs to target the browser | Node, Playwright, headless Chromium and Playwright's WebKit in both images; optionally, a macOS runner for real GPUs and Safari ("Real GPUs and Safari" below); npm in the allowlists; `checks.yml`'s Lasm and browser steps; the `site` kind and its Pages workflow (`templates/site/`); the protocol's "under Lasm", PLAT-3 and browser-build size; Lasm as an upstream project, with its label and its agent |
| **Project** | This project | `agents.conf`, `roles/`, the repository prefix, `REQUIREMENTS.md`, the milestone reviewers' names, `PRIOR-ART.md` |

Inkproof uses all four layers: its libraries are tested natively and under Lasm, and it has sites.

The Lasm layer is already partly optional. `checks.yml` runs Lasm and browser tests only in a repository that pins Lasm (a `test/` or `app/` `package-lock.json`), and skips them otherwise. What isn't optional yet is everything installed for it: Node, Playwright and Chromium are in every agent image and the CI image, and npm is in both allowlists. A kit for a plain Lean project should leave all of that out.

The forge, Forgejo or GitHub, is a fourth, independent axis. Both are required. It lives inside the kit layer: `tea`, the `forge` wrapper, `bootstrap` and `setup-repo`, CI, and the dispatcher's and gatekeeper's API calls. "Forges and adoption" below covers it.

**Both Lean layers need seams from the start.** Every kit is for Lean, so the Lean layer could stay inside the kit. But a plain Lean kit must work without the Lasm layer, so the Lasm layer needs to be optional. Keeping Lean as its own layer too costs little more, and keeps the kit honest about which parts are generic.

### Real GPUs and Safari

Several of Inkproof's bugs have shown up only on a real GPU or in Safari, and neither the agents nor CI could reproduce them:

- Docker Desktop's Linux VM has no GPU passthrough, so WebGL in the agents' Chromium runs on SwiftShader, a software renderer.
- Safari exists only on macOS.

Changing container runtime doesn't help. OrbStack, Colima, Podman and Apple's `container` all run Linux in a VM, so none of them can run Safari. The nearest to a real GPU is Podman's krunkit, which gives Vulkan through MoltenVK, but that is a different driver chain from Safari's. The kit stays on Docker Desktop.

What does help is a macOS VM. On Apple silicon, Virtualization.framework runs macOS guests whose graphics are paravirtualised Metal on the host's GPU. A guest has real Safari and `safaridriver`, plus Playwright's macOS WebKit and Chrome, all rendering through Metal, and it runs alongside Docker Desktop. [Tart](https://tart.run) (Cirrus Labs) is the tool: a command-line layer over Virtualization.framework whose VM images are OCI images, so a VM can be cloned from a clean image, used and thrown away. That suits running pages the agents wrote. UTM also works, but is built around a GUI and suits automation less well.

The Lasm layer provides browser coverage in two tiers:

1. **Always: Playwright's WebKit** in the agent and CI images, beside Chromium. It is nearly free, and catches differences in the WebKit engine, though on Linux it still renders in software.
2. **Optional: a macOS runner.** A CI runner inside a Tart VM on the Mac runs a `macos` job in the CI workflow, with the browser tests in Safari, WebKit and Chrome on Metal. Agents read its results through `forge ci --log`, as they read any other job's. On Forgejo the runner is a Forgejo runner; on GitHub, a self-hosted Actions runner in the same kind of VM (seam 12). The project's settings turn it on, and a kit without it skips the job rather than failing it.

Running the runner in a VM rather than directly on the Mac means that agent-written code runs in a disposable machine, not on the one that hosts production, and that Safari automation takes over the VM's window, not the human's. That gives most of the isolation of a dedicated Mac mini without buying one.

Its limits:

- At most two macOS VMs run at once on one Mac. Apple's licence sets this, and the framework enforces it.
- The VM's memory and CPU come out of what the kit has.
- Paravirtualised Metal isn't identical to bare metal, though it is far closer than SwiftShader. The human's own checks on real devices stay the final word on performance.
- Tart is free for personal and small-scale use, and paid for larger organisations. Check the current terms before relying on it.

## Inventory

How much of each area is project or ecosystem content: none, some (a few lines or one section) or most.

| Area | Kit | Lean and Lasm | Project | Notes |
|---|---|---|---|---|
| `docker/dispatcher/` | most | some | some | `API.lock` in release states; `MILESTONE_REVIEWERS`, rule 22 and rule 16 name Lasm and process |
| `docker/gatekeeper/` | most | some | none | versioning from the `API.lock` diff |
| `docker/agent/bin/` (`agent-runner`, `forge`, `inkproof-hook`, `inkproof-render`) | most | some | none | `inkproof-*` are kit tools despite their names; `forge version` reads `API.lock`; `agent-init` installs elan and `lean-lsp` |
| `docker/agent/Dockerfile` | most | some | none | `lean-lsp-mcp` (Lean); Node, Playwright and Chromium (Lasm) |
| `docker/agent/PROTOCOL.md` | most | some | some | Trust, Comments, Handling requests, Decisions, Sessions, Priority and Finishing are kit; Tools, Dependencies, Reuse, Never work around and Definition of done are ecosystem and project |
| `docker/ci/` | most | most | none | the CI image is Lean's, with Node, Playwright and Chromium for Lasm |
| `docker/proxy/` | some | some | some | allowlists mix every layer |
| `docker/tools/lib/` | most | some | some | `setup-repo` labels (`upstream:lasm`, `review:*`), code-owned `lean-toolchain`; `new-library` seeds a Lean package |
| `scripts/` | most | none | none | `inkproof-ci` image name only |
| `compose.yaml` | most | some | none | project name `inkproof-forge`, the `agent-elan` volume |
| `agents.conf`, `roles/` | none | none | all | already separate files |
| `templates/` | none | most | some | Lean library, `checks.yml`, `check-pins.sh`, site workflow |
| `tests/fixtures/` | none | most | none | Lean packages |
| Stage tests | most | some | some | run against `inkproof-core`, `inkproof-scale` and `inkproof-consumer-metrics`; stage 3 needs real Lean builds |
| `DESIGN.md` | most | some | some | Releases, milestone reviews, Dependencies we don't control, GitHub mirror and Sites |
| `RUNBOOK.md`, `README.md` | most | some | some | |

## The seams

Each seam is something the kit would take from the project instead of containing itself.

1. **Names.** The compose project name, image names, the `.inkproof/` state directories and the `inkproof-*` tool names become the kit's own name, or a `KIT_NAME` setting. `REPO_PREFIX`, `FORGE_ORG` and `GH_OWNERS` already come from `.env`.
2. **Agents and roles.** These already exist as `agents.conf` and `roles/`. They move into the project directory, and the kit reads them from `PROJECT_DIR`.
3. **The protocol.** Split `PROTOCOL.md` into the kit's sections and sections the layers and the project supply, such as Tools, Dependencies, Reuse before you build and Definition of done. Lean supplies the core of each, and Lasm adds its lines: browser tests, PLAT-3, build size, and "under Lasm" in the definition of done. `inkproof-render` already fills in `{{AGENT}}` and similar placeholders. It would also join the kit's protocol with the project's sections, in a fixed order.
4. **Rules that name agents.** These become configuration, mostly derived from `agents.conf`:
   - `MILESTONE_REVIEWERS` is every agent whose kind is `milestone-reviewer` or `supervisor`.
   - The `review:<agent>` labels come from the same list.
   - Rule 22, which wakes `lasm` for a blocking Lasm issue, becomes "an upstream project with an agent that tracks it". That could be a new column in `agents.conf` (`lasm ... upstream=lasm`) or a short project settings file. Its label `upstream:<project>` follows.
   - Rule 16's exception for process and Lasm recommendations becomes a property of a reviewer: its recommendations are the human's to decide.
5. **Versioning.** The rule is generic: semantic versions from the diff of a file that records the public API. The file's name (`API.lock`), and the script that writes it, are the Lean layer's. The kit takes the name as a setting, and the Lean layer supplies the generator. The gatekeeper, `forge version` and the release states share one function, which the tests already hold the same.
6. **Files only the human may change.** `lean-toolchain` joins `CLAUDE.md`, `.forgejo/` and `REQUIREMENTS.md` as a list in configuration.
7. **The agent image and its initialisation.** Build it in stages, one per layer:
   - the kit's base image: Claude Code, `tea`, git, and the kit's tools, compiled from Lean (`lean-core.md`, Decision);
   - Lean on top of it: `lean-lsp-mcp`, and elan, which `agent-init` uses;
   - Lasm on top of that, only where the kit uses it: Node, Playwright, Chromium and Playwright's WebKit.

   The project's settings choose the top stage. The CI image is built in the same way. `agent-init`'s toolchain step becomes a hook that each layer provides, and `agent-init` runs the hooks that are present.
8. **CI and repository templates.** The CI image, `checks.yml`, the library template and `new-library` belong to the Lean layer. `checks.yml`'s Lasm steps, its optional `macos` job, the macOS VM image and its runner's setup, the site template and the `site` kind belong to the Lasm layer. The kit keeps only the requirements: one workflow that must succeed, whose name the gatekeeper is given (`GATEKEEPER_WORKFLOW` already exists).
9. **Proxy allowlists.** The kit's list (Anthropic, the forge) plus lists from each layer the kit uses and from the project, concatenated when the proxy starts. npm comes only from the Lasm layer.
10. **Tests.** The unit tests are already self-contained: `test_dispatcher.py` writes its own `agents.conf`. The stage tests need a small test project of the kit's own, with tiny repositories in the ecosystem's language or in none. They must not run against the project's real repositories. This is the largest single piece of work, and also the most valuable. Today, testing a kit change exercises Inkproof's repositories.
11. **Documentation.** `DESIGN.md` and `RUNBOOK.md` become the kit's, with a short project document for:
    - what this project adds: its agents, its reviews, its upstream projects and its sites;
    - how its releases are versioned.
12. **The forge.** Everything the kit does through a forge goes through one adapter per forge, chosen by a setting (`FORGE=forgejo` or `github`):
    - **API calls:** the dispatcher's, the gatekeeper's and `bootstrap`'s. This covers issues, PRs, labels, reviews, merges, releases, Actions runs, and webhook headers (`X-Forgejo-*` or `X-GitHub-*`).
    - **The agents' command line:** `forge` keeps its commands and calls `tea` or `gh` underneath. The protocol's lines about tools follow.
    - **Identity:** an account per agent on Forgejo, or GitHub Apps on GitHub.
    - **Protection:** branch protection and teams on Forgejo, or rulesets on GitHub. Code owners live in `.forgejo/` or `.github/`.
    - **CI:** Forgejo Actions with the kit's own runner, or GitHub Actions. On GitHub, the runner could be GitHub's or a self-hosted one on the Mac. Either way, the Lasm layer's optional `macos` job runs on a self-hosted runner in a Tart VM. On GitHub that runner must never take jobs from outsiders' PRs, which issues-only contributions already ensure.
    - **Webhooks:** delivered directly on Forgejo, or through a tunnel or relay on GitHub (the GitHub report, section 4).
13. **What the kit requires of a repository.** Today this is implicit, because the kit created or reshaped every repository it runs. Adoption makes it an explicit contract, checked when a repository is adopted and met through onboarding PRs:
    - the CI workflow;
    - code owners for the autonomy level;
    - `API.lock` and its generator;
    - `CHANGELOG.md` headings;
    - `vX.Y.Z` tags;
    - the files only the human may change.

    Most of the items are the Lean layer's. With them comes an explicit list of repositories in place of `REPO_PREFIX`, because an adopted account's repositories won't share a prefix. The dispatcher uses the prefix to decide what counts as a project repository. Also, a coordination repository is created if the project lacks one; today it is named `<prefix>coordination`.

## Forges and adoption

The kit must support two modes, and adopt an existing project into either:

| | **Forgejo mode** (today) | **GitHub mode** |
|---|---|---|
| Where work happens | Forgejo, on the Mac | GitHub |
| GitHub's role | a push mirror, for backup and distribution | everything |
| Agent identities | a Forgejo account each | GitHub Apps |
| CI | Forgejo Actions, with the kit's runner and its own Docker engine | GitHub Actions |
| Webhooks | direct, on internal networks | through a tunnel or relay to the Mac |
| Suspension risk | none from Forgejo; GitHub sees only mirror pushes | real (the GitHub report) |

### Adopting a project

Common to both modes:
- **Derive the project layer.** An adopted project arrives with no `agents.conf`, roles or `REQUIREMENTS.md`. Adoption proposes them from the repositories: one owning agent per repository, dependencies from `lakefile`s and `lake-manifest.json` (the Lean layer's job), and draft roles from the READMEs. The human edits the proposal before anything starts.
- **Check the repository contract** (seam 13). Adopted projects take on the kit's conventions, so adoption opens onboarding PRs for what's missing, for the human to review. Most of what they change is code-owned by the human. The kit never adapts to a project's own conventions.
- **Treat existing history with care.** Existing tags may not be `vX.Y.Z`, which the versioning rule reads. Existing open issues and PRs may be by authors the kit doesn't trust.

Specific to **Forgejo mode**: `scripts/migrate.sh` and `scripts/mirrors.sh` already import and mirror. Adoption adds a cut-over:
- **The mirror would delete branches.** With the default `MIRROR_BRANCHES=main`, the first sync deletes every other branch on GitHub. That's fine for Inkproof, but destructive for a live project with open PRs or other people's branches. Adoption should mirror every branch at first, and narrow it only once GitHub's open PRs are dealt with.
- **Freeze GitHub first.** Anything done on GitHub after the import is lost to the kit, so the project's GitHub side must stop taking changes first.
- **Imported issues are history, never instructions.** They are posted as Ghost and never trusted, as now.

Specific to **GitHub mode**: there's no import, and the kit works in place. But:
- **The repositories can stay where they are.** The human prefers not to move them to an organisation. The App can be owned by a separate, empty organisation, made public, and installed on the personal account, because a public App can be installed by other accounts. Moving the repositories stays the fallback; it changes their URLs, which `GH_FORMER_OWNERS` already handles for Lake.
- **Private repositories need GitHub Pro.** On GitHub Free, a personal account's private repositories get no branch protection or rulesets.
- **Outside contributors are a goal, not just a cost.** Accepting outside issues and PRs is a major reason for GitHub mode, though not a hard requirement. The GitHub report's section 9 analyses it. In short: issues are feasible with a quarantine agent, a human gate and caps. PRs are feasible only if agents never build them, CI runs on GitHub's runners, and the human approves every merge. The decision is issues only at first.
- **One App may not be enough.** Required approvals need distinct identities: the gatekeeper's, conformance's and the coordinator's. A single App shared by every agent can't give them, so GitHub mode probably needs one App per reviewing role, at least. Whether an App's approval counts as a required review needs testing. If it doesn't, required status checks pinned to each App do the same job.

### Testing both modes

The stage tests run against the dev container's Forgejo. GitHub mode needs either a test organisation on real GitHub, which carries suspension risk of its own, or a fake GitHub API, which drifts from the real one. See the GitHub report's open questions. The seam helps here: most of the kit can be tested in Forgejo mode, leaving only the GitHub adapter to test live.

## How to package it

| Option | How it works | For | Against |
|---|---|---|---|
| **A. Template repository** | A new project copies the kit and changes it | Simplest to start | Fixes don't flow between kits, and this kit has needed many |
| **B. Kit as a versioned dependency** | A kit repository, and a thin project repository with `agents.conf`, roles, protocol sections, templates and `.env`. The project pins a kit version (a git submodule or a tag) and runs the kit's scripts with `PROJECT_DIR` set | Fixes flow by bumping the pin; each kit upgrades when its human chooses | A versioned interface to keep stable; two repositories to change for a cross-cutting fix |
| **C. One repository, several projects** | `projects/inkproof/`, `projects/<new>/`, each with its own `.env` and state, run through `KIT_ENV` as the dev container does now | Fixes reach every kit at once; no interface versioning | Upgrading for one project upgrades all; this one's production kit changes when the other's needs do |

**Recommendation: B, reached through C.**
- First, separate the layers inside this repository. Move the project layer into `projects/inkproof/`, and the two ecosystem layers into `layers/lean/` and `layers/lasm/`. Then make the kit read the project through `PROJECT_DIR`, and the layers the project names in its settings. Each step can be tested here, with the stage tests in the dev container, while the Mac's kit keeps running.
- Once the new project exists and the interface has settled, move the kit into its own repository with tagged versions. `projects/inkproof/` becomes the Inkproof kit's repository, pinned to a kit version.

B over C because the Mac's kit is production. A change made for the new project shouldn't reach the Inkproof kit until its human chooses to take it.

## Order of work

1. Names (seam 1): mechanical, and makes the rest easier to read.
2. Rules that name agents (seam 4): a dispatcher change with unit tests, and worth doing on its own, because it makes the rules say what they mean.
3. Protocol and roles (seams 2 and 3).
4. Image, CI, templates, allowlists and versioning (seams 5 to 9).
5. A test project for the stage tests (seam 10), in plain Lean, so that the kit's own tests don't need the Lasm layer. A second, small one with Lasm tests the layer, including WebKit.
6. Documentation (seam 11), then extraction into two repositories.
7. The repository contract and adoption in Forgejo mode (seam 13): the derived project layer, onboarding PRs, and the gentler cut-over. Most of the machinery exists.
8. The forge seam (seam 12). First put Forgejo behind the adapter, with no change in behaviour, tested by the existing stage tests. Then write the GitHub adapter, using the GitHub report's design: one serial client, webhooks with a reconciliation sweep, and Apps.
9. Adoption in GitHub mode.
10. The Lasm layer's macOS runner, once real-GPU and Safari coverage is worth the VM's share of the Mac. It is independent of the rest, so it can come earlier if a bug calls for it.

## Questions for the human

- **Does the new project target the browser?** If not, it is the first kit without the Lasm layer, and the test that the layer really is optional.
- **Do Tart's current licence terms fit?** It is free for personal and small-scale use; this needs checking before the macOS runner is built.
- **Does the new project need anything Inkproof doesn't use?** For example, agent kinds, review stages or levels. Anything it needs belongs in the kit layer, and is cheaper to know before the interface is drawn.

Settled:
- Outside contributions start with issues only. Outsiders' PRs are closed with a note asking for an issue (GitHub report, section 9).
- Adopted projects take on the kit's conventions.
- Accepting outside contributions is a goal of GitHub mode, but not a hard requirement.
- Moving adopted repositories to an organisation is a fallback, to avoid if possible.

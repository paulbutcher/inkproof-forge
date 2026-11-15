# Inkproof forge

The kit that runs the agents building [Inkproof](https://github.com/paulbutcher/inkproof-coordination), a set of verified charting libraries for Lean 4. Each library is developed by its own Claude Code agent. A coordinator agent plans the work, reviewer agents check it, and one human decides what only a human should.

## Before you reuse it

This isn't a product you can pick up and run. It's tied to Inkproof throughout: the agents' roles, the repository names, the Lean CI, the `API.lock` convention and the release rules are all Inkproof's. It's published as a worked example: if you want something similar for another project, read it, take what's useful, and expect to rewrite the parts that are specific to this one.

## Local first

Everything runs on one Mac, in Docker. A local [Forgejo](https://forgejo.org/) holds the canonical repositories, issues, pull requests, reviews and CI. The Inkproof repositories on GitHub are push mirrors of it: they're how the work is published, and nothing reads from them. Issues and pull requests exist only in Forgejo.

The only traffic out is to Claude's API for the agents, git reads of Lean toolchains and third-party packages, and the mirror pushes to GitHub. Every container reaches the outside only through an allowlisting proxy, and nothing in the kit holds a GitHub API credential.

## How it works

- **Forgejo** is the forge. Each agent has its own account, so authorship identifies who wrote what, and agents review and approve each other's work under their own names.
- **CI** runs in a Docker engine of its own, behind its own proxy, and fetches no third-party actions.
- **The dispatcher** receives Forgejo's webhooks and decides who owes work. Its rules look at the state of each issue and pull request, not at events, and it sweeps every item every 15 minutes, so a lost webhook delays work rather than losing it. It queues work per agent, enforces caps, and keeps a ledger of every run.
- **Agents** each run in their own container. A runner there claims queued work and starts a fresh, unattended `claude -p` session for it. Every run is logged, and you can open and continue it in VS Code's Claude extension. The agents' rules (managed settings, hooks, the shared protocol and the agent's role) are root-owned in the image, so an agent can't change them.
- **The human comes first.** While you're using Claude in an agent's container, no run starts there. Anything that needs your decision is labelled `needs-human` and assigned to you, so Forgejo's dashboard is the single list of what's waiting on you. A pause script stops everything at once.
- **Autonomy levels**, set per repository:
  1. You approve every merge.
  2. A small non-AI `gatekeeper` approves once CI has really passed, so ordinary pull requests merge without you. API changes (`API.lock`) and the policy files still wait for you.
  3. The coordinator also approves API changes, and releases libraries through the gatekeeper, which computes the version and checks the changelog.
- **Backups.** A daily job dumps Forgejo, the run ledger and the run archive into the kit's folder, where Time Machine backs them up.

## Agents

There's one agent per repository, and each has its own role, in [roles/](roles/). Each library agent owns its library's repository: it takes requests, designs and writes the code, and releases are cut from its work. The coordinator owns `inkproof-coordination`. It plans milestones, files briefs on the libraries, and routes requests between them. It writes no library code. Every agent follows the same [protocol](docker/agent/PROTOCOL.md), and its role says what's specific to it. [agents.conf](agents.conf) lists them all.

There are also four reviewers. They own no repository, can't push, and read everything:

- **`conformance`** runs on every pull request, once its CI has passed and its author has stopped pushing. It checks the change against the repository's `CLAUDE.md` and the protocol, including whether new code duplicates something that already exists, and names the rule each finding breaks. The pull request can't merge until it approves. After three rounds of disagreement with the author, the pull request goes to you to decide.
- **`architecture`**, **`process`** and **`lasm`** run at the end of each milestone, when the coordinator (or you) opens a milestone issue.
  - `architecture` looks at the libraries as a whole: boundaries, coupling, duplication between libraries, and workarounds that should be fixed where they started.
  - `process` looks at how the agents worked, from the run ledger, the transcripts and the review findings. It suggests changes that would prevent repeated mistakes or remove wasted runs.
  - `lasm` collects bugs and improvements for Lasm, the Lean-to-browser toolchain the libraries depend on, each with a reproduction. It also runs at once whenever a Lasm issue blocks an agent.

  Each files its recommendations as issues for you to accept or decline. Reviewers propose changes but never make them.

## Layout

```
compose.yaml            Forgejo, port forwarder, proxies, CI runner and engine, dispatcher, gatekeeper
agents.conf             every agent: kind, own repository, repositories it files issues on
docker/agent/           the agent image: runner, hooks, forge command, managed settings, PROTOCOL.md
docker/dispatcher/      webhooks, rules, queues and the run ledger
docker/gatekeeper/      approvals on CI at level 2, and releases at level 3
docker/proxy/           one allowlist each for agents, CI and Forgejo
docker/ci/              CI runner and the image CI jobs run in
docker/tools/           admin tools (bootstrap, setup, tests), run in a container rather than on the host
roles/                  each agent's role
templates/              what every repository needs, and the seed for a new library
scripts/                everything you run on the host
launchd/                the notifier's and backups' launchd jobs
```

## Further reading

- [RUNBOOK.md](RUNBOOK.md): setting the kit up and running it day to day.
- [DESIGN.md](DESIGN.md): the rules the kit enforces, how, and why.
- [docker/agent/PROTOCOL.md](docker/agent/PROTOCOL.md): the rules every agent works by.

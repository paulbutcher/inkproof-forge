# Requirements

This repository is Inkproof's instance of [lean-guild](https://github.com/paulbutcher/lean-guild). The guild is the generic kit; this repository is the project layer that configures it for Inkproof, and the record of how Inkproof's running kit moves onto it. Inkproof's kit runs today on [inkproof-forge](https://github.com/paulbutcher/inkproof-forge), in production on the human's Mac, and keeps running there until the switch.

## The reference

- inkproof-forge, at the commit named in [PLAN.md](PLAN.md#the-reference), is both the source of Inkproof's project layer and the behaviour Inkproof's kit must keep. Clone it beside this checkout (`../inkproof-forge`) at that commit, read it, and never change it.
- The guild's `docs/REQUIREMENTS.md` lists every way the guild differs from it. Inkproof takes those differences and no others.
- Where this document, the guild's and the reference disagree, ask the human.

## What Inkproof is

- **Agents:** a coordinator; library agents (devtools, float, core, scale, axis, viewport, scene, gpu, export, web, consumer-metrics); one site agent (demos); a pull request reviewer (conformance); milestone reviewers (architecture, lasm); and a supervisor (process). The reference's `agents.conf` lists each with its repository and dependencies.
- **Repositories:** `inkproof-coordination` and each agent's own, in the Forgejo organisation `tentenths`, mirrored to GitHub.
- **Layers:** Lean and Lasm. Libraries are tested natively and under Lasm; `inkproof-demos` is a site deployed to GitHub Pages.
- **Upstream:** Lasm is an upstream project tracked by the `lasm` agent, whose recommendations, like `process`'s, are the human's to decide.
- **Forge mode:** Forgejo, with push mirrors to GitHub.

## What this repository holds

- Inkproof's configuration, as Lean values of the guild's types, checked when it compiles: agents, repositories, dependencies, reviewers, upstream projects, autonomy levels, bounds, layers and the forge mode.
- Inkproof's text: each agent's role (the reference's `roles/`), the project's protocol sections (the parts of the reference's `docker/agent/PROTOCOL.md` the guild's layers don't supply), and the project document the guild's documentation asks for.
- Deployment: a settings template (the reference's `.env.example`, less what the guild owns), Inkproof's allowlist additions, and any compose overrides.
- `RUNBOOK.md`: Inkproof's setup and the switch, for the human to follow on the Mac.
- Nothing generic. Anything another project would also need belongs in the guild, and is asked for there (`docs/COORDINATION.md`).

## Behaviour

- Inkproof's kit behaves as the reference does, with the guild's listed changes: the web UI in place of the status, notification and control scripts; re-run limits and run budgets; the quiet-obligation fix; the ledger kept compatible.
- Inkproof turns on the Lasm layer's macOS runner only when the human decides, after the guild provides it.
- Settings that are bounds (runs per day, spend, run length, the re-run limit, the run budget) start at the reference's values, or, for new ones, at values chosen from the production ledger's history, which the human exports.

## The switch

- The guild's release replaces the reference's kit on the Mac, keeping Forgejo's data, the ledger, every volume (agents' workspaces, Claude logins, the run archive) and the secrets.
- The order: drain the reference's kit; back up; stop it; start the guild's on the same data; check; resume. Launchd jobs for notifications go; those for backups move to the guild's scripts.
- The way back: drain the guild's kit, stop it, and start the reference's on the same ledger, which the guild keeps compatible.
- The human runs every step on the Mac. Agents write the steps and rehearse them in the dev container, never on the Mac.
- After a settled switch, the human archives inkproof-forge.

## Rehearsal

- The rehearsal uses a backup of the production kit, which the human copies into the checkout outside git. Backups hold secrets' hashes and transcripts: never commit or publish any part of one.
- It restores the backup into the dev container's own Docker engine, as the reference's `scripts/restore-test.sh` does: on an internal network, with mirrors, webhooks and Actions off, and no Claude credential, so nothing can reach GitHub or start a run.
- With every agent stopped, the guild evaluates the restored copy, and its view (what waits on the human, what each agent owes, each unit's state) is compared with the reference kit's view of the same copy. Each difference is either one of the guild's listed changes or a bug to report to the guild.
- The switch and the way back are each rehearsed on the restored copy, end to end, before the human does either on the Mac.

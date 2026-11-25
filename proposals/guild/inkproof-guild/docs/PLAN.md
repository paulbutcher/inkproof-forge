# Plan

The order of work for Inkproof's instance of the guild. [REQUIREMENTS.md](REQUIREMENTS.md) says what each stage builds; [COORDINATION.md](COORDINATION.md) says how it fits with the guild's own plan.

## The reference

- [inkproof-forge](https://github.com/paulbutcher/inkproof-forge) at commit `c82e23a54bc2854f18130d88a519d9e63766836d`, the same commit the guild's plan names.
- Clone it with `git clone https://github.com/paulbutcher/inkproof-forge ../inkproof-forge && git -C ../inkproof-forge checkout c82e23a54bc2854f18130d88a519d9e63766836d`. Read it; never change it.

## How to work

- One session per stage. Start by reading REQUIREMENTS.md, COORDINATION.md and this file; work only on the first stage not marked done, and only once what it waits on is done.
- A stage is done when its acceptance passes in the dev container. Mark it done here, with the date; the human commits.
- Never patch the guild here, and never edit its copy under `.lake`. Draft an issue for lean-guild as raw markdown instead, saying what Inkproof needs in terms another project could use; the human files it.
- Never write to the production Forgejo, never run tests on the Mac's own Docker engine, and never commit any part of a backup.

## Stages

| # | Stage | Builds | Acceptance | Waits on |
|---|---|---|---|---|
| 0 | Foundations | A dev container with its own Docker engine (the reference's `.devcontainer` shows how), CI | `docker run hello-world` in the dev container | |
| 1 | The inventory | `docs/INVENTORY.md`: everything Inkproof-specific in the reference, by the seams of `proposals/generic-specific-split.md`, each with where it goes (configuration, text, settings) and anything the guild's interface must express to hold it | every line of `agents.conf`, `roles/`, `.env.example`, `proxy/domains.txt`, `templates/` and the dispatcher's Inkproof-specific rules is accounted for; an issue drafted for lean-guild pointing to it | |
| 2 | Configuration | Inkproof's configuration on the guild's `v0.1.0`, its roles and protocol sections | compiles; each agent's assembled protocol matches the reference's rendered one, except for differences the guild lists | guild stage 2 |
| 3 | Deployment | The settings template, allowlist additions, compose overrides, the runbook's setup part | the guild's kit comes up in the dev container with Inkproof's configuration against an empty Forgejo, bootstraps Inkproof's organisation, repositories and accounts, and starts every agent stopped | guild stage 10 |
| 4 | Rehearsal | Restoring a production backup, the comparison of views, the switch and the way back on the restored copy | every difference in the comparison explained or fixed in the guild; switch and way back both rehearsed end to end | the human's backup |
| 5 | The switch | The runbook's switch and way-back parts, from the rehearsal | the human switches the Mac; the kit runs a week with nothing the way back would fix | the human |
| 6 | After the switch | Taking guild releases as they come; the macOS runner when the human decides | each bump passes stage 3's acceptance and a fresh rehearsal before the Mac takes it | guild releases |

## Status

| # | Done |
|---|---|
| 0 | |
| 1 | |
| 2 | |
| 3 | |
| 4 | |
| 5 | |
| 6 | |

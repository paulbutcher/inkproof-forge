# Coordination

How work on this repository and on [lean-guild](https://github.com/paulbutcher/lean-guild) fits together. This file is the one place that orders both; each repository's `docs/PLAN.md` orders its own stages and names what they wait on.

## The repositories

- **lean-guild:** the generic kit. It knows nothing of Inkproof: no Inkproof names in its code or tests.
- **inkproof-guild (this one):** Inkproof's configuration, text and deployment, and the switch. It holds nothing another project would also need.
- **inkproof-forge:** the reference, and production on the Mac until the switch. Neither repository changes it. If the human changes it, they move both plans' reference commit together.
- **The human** commits, tags, files the issues agents draft, and runs everything on the Mac.

## The dependency

- This repository requires `guild` in its lakefile, by a release tag. Between releases, the human may point it at a commit on lean-guild's `main`; never at a local path.
- A guild release records interface changes in its `CHANGELOG.md`, breaking ones included, since the guild is before 1.0.
- Taking a new guild release is a change of its own here: bump the requirement, adapt, and pass the latest stage's acceptance again.

## Requests between the repositories

- When this repository needs something of the guild, its agent stops and drafts a lean-guild issue as raw markdown, stating the need in terms any project could use and linking to what here motivates it. The human files it.
- When the guild's work needs an answer about Inkproof, such as whether the interface can express something, its agent drafts an issue here in the same way.
- A workaround here for something that belongs in the guild is not allowed, even temporarily.

## The order of both

| Phase | lean-guild | inkproof-guild | In step |
|---|---|---|---|
| A | 0 Foundations, 1 The pure core | 0 Foundations, 1 The inventory | both run in parallel |
| B | 2 The project interface; `v0.1.0` | | guild stage 2 starts from inkproof-guild's inventory |
| C | 3 to 10, infrastructure to the randomised world test | 2 Configuration | inkproof-guild stage 2 starts at `v0.1.0`; interface gaps it finds go to the guild as issues |
| D | releases fixes as reported | 3 Deployment, 4 Rehearsal | starts at the guild's post-stage-10 release |
| E | | 5 The switch | the human, on the Mac |
| F | 11 GitHub, 12 The macOS runner | 6 After the switch | Inkproof takes each release when the human chooses |

## Sync points

- **S1, the inventory.** The guild's stage 2 doesn't settle its configuration types until inkproof-guild's `docs/INVENTORY.md` exists, so that the interface is drawn knowing everything its first consumer has.
- **S2, `v0.1.0`.** inkproof-guild's configuration starts on the first tagged interface. The interface may still change before 1.0; each change reaches Inkproof as a release.
- **S3, a complete guild.** Deployment and rehearsal start only at a release that has passed every ported stage test and the randomised world test.
- **S4, a clean rehearsal.** The human switches the Mac only after the comparison has no unexplained difference and the switch and the way back have both been rehearsed.

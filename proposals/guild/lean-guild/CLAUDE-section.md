## This project

- The guild is a generic kit for running a team of agents on a Lean project. What it must do is in [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), and the order of work in [docs/PLAN.md](docs/PLAN.md): read both before starting, and work only on the first stage not marked done.
- The reference implementation is inkproof-forge at the commit PLAN.md names, cloned to `../inkproof-forge`. Read it; never change it.
- No Python in this repository's code, tests or tools; shell where Lean is overkill. No project's names in code or tests.
- The host runs a production kit: run Docker only in this dev container's own engine, and never against a real project's forge.
- Work this repository needs from a consuming project, or answers it needs from one, goes as a drafted issue for the human to file ([inkproof-guild's docs/COORDINATION.md](https://github.com/paulbutcher/inkproof-guild/blob/main/docs/COORDINATION.md)).

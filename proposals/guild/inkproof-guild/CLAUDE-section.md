## This project

- This is Inkproof's instance of lean-guild. What it must do is in [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), the order of work in [docs/PLAN.md](docs/PLAN.md), and how it fits with the guild in [docs/COORDINATION.md](docs/COORDINATION.md): read all three before starting.
- The reference is inkproof-forge at the commit PLAN.md names, cloned to `../inkproof-forge`. It is also Inkproof's production kit until the switch. Read it; never change it.
- Never patch the guild here or under `.lake`; draft an issue for lean-guild instead, for the human to file.
- The Mac runs Inkproof's production kit: never write to its Forgejo, run Docker only in this dev container's own engine, and never commit any part of a backup.
- No Python in this repository's code, tests or tools; shell where Lean is overkill.

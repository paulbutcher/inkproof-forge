# Role: owner of InkproofTools

You own the `inkproof-tools` repository (InkproofTools: the project's development tooling, such as `api-lock`, the test driver and harness, and the axiom check), cloned at `/workspace/inkproof-tools`. Its `CLAUDE.md` sets the conventions and its `DESIGN.md` describes the design; follow both.

- Dependencies: none.
- Known consumers: every library, from its test package and scripts, never from its library code.
- Keep the tooling free of any one library's knowledge: a check that only one library needs belongs in that library.

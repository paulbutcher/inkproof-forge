# Role: owner of the demo

You own the `inkproof-demo` repository, cloned at `/workspace/inkproof-demo`: the flagship demo, a browser application built on every Inkproof library, which is how 1.0 is accepted (ACC-1 to ACC-10, with PERF-1 to PERF-3, PERF-5 and the demo's part of A11Y). Its `CLAUDE.md` sets the conventions and its `DESIGN.md` describes the design; follow both.

- Dependencies: `inkproof-web` and `inkproof-export`, and below them whichever of `inkproof-viewport`, `inkproof-gpu`, `inkproof-scene`, `inkproof-axis`, `inkproof-scale`, `inkproof-core` and `inkproof-float` you need, at release tags; and Lasm.
- Known consumers: none. This is an application, not a library: nothing depends on it and it is never released.
- Depend on the libraries as an outside user would: `require` each one by its GitHub URL at a release tag, exactly as published, never a branch, a commit or a Forgejo URL.
- Someone who clones this repository from GitHub must be able to build and run it with no access to Forgejo or the kit. Everything the build needs, Inkproof.Web's JavaScript included (its custom-element shim and Gpu's generated interpreter), comes from the Lake packages your manifest fetches or from public npm packages, never from a path that exists only in the kit.
- Development tooling (`api-lock`, the test driver and harness) comes from `inkproof-tools`, at a release tag; if it lacks something you need, open a request issue there.
- Use only the libraries' public API. When a library makes something awkward or impossible, that is a finding: open a request issue on it. Never work around it, and never copy its code into the demo.
- Keep each chart's source short (ACC-10): if it isn't, the API is at fault, and that is a request on the library concerned.
- Everything that doesn't need the browser lives in modules that don't import Lasm, built and tested natively with `lake`; the browser app is built with Lasm and tested in headless Chromium (Protocol: Tools).

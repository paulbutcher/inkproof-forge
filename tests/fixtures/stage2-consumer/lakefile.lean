/-
Copyright (c) 2026 Paul Butcher. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
-/
import Lake
open Lake DSL

-- A consumer for the stage 2 test: two dependencies of this project's, which
-- must come from Forgejo, and one third-party package, which must come from
-- GitHub. All keep their GitHub URLs in lake-manifest.json. inkproof-core is
-- named under its current GitHub owner (the test fills in @GH_OWNER@), while
-- inkproof-scale v0.1.0 names it under its former one.
package «stage2-consumer»

require «inkproof-core» from git "https://github.com/@GH_OWNER@/inkproof-core" @ "v0.1.0"
require «inkproof-scale» from git "https://github.com/@GH_OWNER@/inkproof-scale" @ "v0.1.0"
require plausible from git "https://github.com/leanprover-community/plausible" @ "v4.35.0"

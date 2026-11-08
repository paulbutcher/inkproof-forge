/-
Copyright (c) 2026 Paul Butcher. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
-/
import Lake
open Lake DSL

-- A consumer for the stage 2 test: one dependency of this project's, which
-- must come from Forgejo, and one third-party package, which must come from
-- GitHub. Both keep their GitHub URLs in lake-manifest.json.
package «stage2-consumer»

require «inkproof-core» from git "https://github.com/tentenths/inkproof-core" @ "v0.1.0"
require plausible from git "https://github.com/leanprover-community/plausible" @ "v4.35.0"

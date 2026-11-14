{{HEADER}}
import Lake
open Lake DSL

package «{{REPO}}-test» where
  leanOptions := #[
    ⟨`autoImplicit, false⟩,
    ⟨`relaxedAutoImplicit, false⟩,
    ⟨`warningAsError, true⟩
  ]

require «{{REPO}}» from ".."

@[default_target, test_driver]
lean_exe tests where
  root := `Main

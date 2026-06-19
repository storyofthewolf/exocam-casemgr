# PROPOSED_PLANS.md

Forward-looking design ideas for the ExoCAM case-management toolchain that are
**not yet decided and not yet implemented**. This file is deliberately separate
from `CLAUDE.md` (architecture of record) and `DEVELOPER_NOTES.md` (implementation
reference) so that speculative proposals are never mistaken for current behavior.

Entries here are opinions and sketches. Promote an idea to a real plan (and into
the code + the other docs) only after it has been agreed and validated.

---

## 2026-06 — Connecting case-mgr to the ExoCAM tool kits (albedos + IC files)

**Status:** idea only. Discussed, not committed.

**Motivation:** Two per-case inputs are currently produced by hand or by external
tools and pasted into the experiment matrix:

1. The sea-ice/snow broadband albedos (`nl_cice_params`: `albicei`, `albicev`,
   `albsnowi`, `albsnowv`), which depend on `exo_solar_file` and are computed by
   `ExoCAM/tools/py_progs/broadband_albedo_calculator.py`.
2. The `ncdata` initial-condition file at a given surface pressure, currently made
   by `ExoCAM/tools/idl_progs/changepress_cesm.pro` (IDL).

The question: should `build.py` auto-generate these as part of `generate`, and if
so, where do the hooks live?

### Guiding principle

`build.py generate` produces self-contained, reviewable shell scripts and **touches
nothing on disk**; `build.py make` runs them. This `generate`/`make` split is what
makes the toolkit trustworthy. The rule that keeps it intact:

> case-mgr should **orchestrate derivations, not embed heavyweight side effects.**
> Small pure derivations (a few floats) may run in-process at generate time.
> Large disk-writing operations (multi-MB IC files) should be **emitted as guarded
> steps into the generated shell script**, to run on the HPC — not executed inside
> the Python.

There is also a host-machine split: `generate` runs **locally**; albedo calc needs
the stellar `.nc`, and IC generation needs HPC scratch and source IC files. "Where
the hook lives" is really "on which machine does it run."

### Proposal 1 — Auto-derive `nl_cice_params` from `exo_solar_file`  (LIKELY YES, low risk)

Output is four small floats derived from a file already named in the spec, so this
fits the "pure derivation" category.

- **Hook location:** a `_derive_cice_albedos(spec)` step called from / just after
  `resolve_case()` in `build.py`, **before** script generation. If the case has an
  `exo_solar_file` and no explicit `nl_cice_params`, compute the 4 albedos and
  inject them into the spec; the existing `_build_nl_append_block` writes them with
  no downstream change.
- **Override semantics:** explicit `nl_cice_params` in the matrix wins; derivation
  only fills the gap.
- **Why it's good:** today the albedos and `exo_solar_file` can silently drift out
  of sync (wrong star -> wrong albedos, no error). Deriving them makes the spec
  internally consistent by construction, and removes a manual, error-prone step.
- **Frictions (all manageable):**
  - *Import coupling.* `broadband_albedo_calculator.py` is a flat top-to-bottom
    `argparse` script with hardcoded relative paths to `../spectral_albedos/`.
    Refactor it into an importable function, e.g.
    `compute_albedos(spec_path, mode) -> (vis, ir)`. (Worth doing regardless.)
    Shelling out and scraping stdout is the fragile alternative; prefer the refactor.
  - *Cross-repo dependency.* case-mgr would import from `ExoCAM/tools/`. Make the
    path configurable (e.g. `paths.exocam_tools` in `config_registry.yaml`) and skip
    gracefully when unavailable.
  - *File availability at generate time.* If `exo_solar_file` is an HPC-only path,
    local derivation can't run; need the `.nc` mirrored locally or defer to a
    cluster-side step.
- **Auditability caution:** silent derivation means a wrong `exo_solar_file` yields
  wrong albedos with no trace in the matrix. Have the derivation **echo what it
  computed** as a comment in the build script
  (`# derived cice albedos from <solar_file>: albicei=...`), same reasoning as
  keeping the rendered `exoplanet_mod.F90` inline rather than staged.

### Proposal 2 — Auto-generate the `ncdata` IC file  (RIGHT INSTINCT, not now, not in build.py)

Different animal from Proposal 1 for three reasons:

1. **Prerequisite blocker.** Depends on porting `changepress_cesm.pro` (IDL ->
   Python) first, and **validating it against the IDL output**. That port is the
   real work (pressure-grid interpolation/rescaling — a science-correctness task,
   not plumbing) and should land and be trusted on its own before anything calls it.
2. **Heavyweight, stateful side effect.** Writing a multi-MB NetCDF IC file to HPC
   scratch is exactly what `generate` is designed never to do. Embedding it would
   break the reviewable-script invariant.
3. **Wrong machine.** IC generation belongs on the HPC (scratch + source IC files
   live there); `generate` runs locally.

- **Where it should hook, if at all:** not in `build.py` Python — emit it into the
  **generated build shell script** as a guarded preamble, the same pattern as the
  inline `exoplanet_mod.F90` heredoc:

  ```sh
  # (in <case>_build.sh, before create_newcase)
  if [ ! -f <ncdata_path> ]; then
      python changepress_cesm.py --target <P>bar --out <ncdata_path> ...
  fi
  ```

  This keeps it (a) on the HPC, (b) idempotent (`if not exists`), (c) visible in the
  reviewable script, (d) consistent with how the build script already prepares
  environment-specific files. case-mgr stays "generate the recipe"; the recipe
  includes "make the IC file if missing."

### Suggested sequencing

| Step | Effort | Risk | Do it? |
|---|---|---|---|
| Refactor `broadband_albedo_calculator.py` into an importable function | small | low | Yes — worth it regardless |
| Auto-derive `nl_cice_params` from `exo_solar_file` in `resolve_case` | small | low | **Yes** |
| Add `paths.exocam_tools` to `config_registry.yaml` + graceful skip | small | low | Yes (enabler for above) |
| Port `changepress_cesm.pro` -> Python + validate vs IDL | **large** | medium (science) | Separate effort |
| Emit guarded IC-gen call into the build shell script | small | low | Yes, *after* the port |

**Bottom line:** Proposal 1 is a clean, low-risk extension in the same spirit as the
existing `nl_cice_params` plumbing — prototype it as a separate, reviewable change.
Proposal 2 is a good long-term goal but is gated on the IDL->Python port and must be
emitted into the shell script, never run as an in-process side effect of `generate`.

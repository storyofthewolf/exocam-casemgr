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

1. The sea-ice/snow broadband albedos (`cice_params`: `albicei`, `albicev`,
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

> case-mgr should **orchestrate generations, not embed heavyweight side effects.**
> Small pure generations (a few floats) may run in-process at generate time.
> Large disk-writing operations (multi-MB IC files) should be **emitted as guarded
> steps into the generated shell script**, to run on the HPC — not executed inside
> the Python.

There is also a host-machine split: `generate` runs **locally**; albedo calc needs
the stellar `.nc`, and IC generation needs HPC scratch and source IC files. "Where
the hook lives" is really "on which machine does it run."

### Proposal 1 — Auto-derive `cice_params` from `exo_solar_file`  (LIKELY YES, low risk)

Output is four small floats derived from a file already named in the spec, so this
fits the "pure generation" category.

- **Hook location:** a `_derive_cice_albedos(spec)` step called from / just after
  `resolve_case()` in `build.py`, **before** script generation. If the case has an
  `exo_solar_file` and no explicit `cice_params`, compute the 4 albedos and
  inject them into the spec; the existing `_build_nl_append_block` writes them with
  no downstream change.
- **Override semantics:** explicit `cice_params` in the matrix wins; generation
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
    local generation can't run; need the `.nc` mirrored locally or defer to a
    cluster-side step.
- **Auditability caution:** silent generation means a wrong `exo_solar_file` yields
  wrong albedos with no trace in the matrix. Have the generation **echo what it
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
| Auto-derive `cice_params` from `exo_solar_file` in `resolve_case` | small | low | **Yes** |
| Add `paths.exocam_tools` to `config_registry.yaml` + graceful skip | small | low | Yes (enabler for above) |
| Port `changepress_cesm.pro` -> Python + validate vs IDL | **large** | medium (science) | Separate effort |
| Emit guarded IC-gen call into the build shell script | small | low | Yes, *after* the port |

**Bottom line:** Proposal 1 is a clean, low-risk extension in the same spirit as the
existing `cice_params` plumbing — prototype it as a separate, reviewable change.
Proposal 2 is a good long-term goal but is gated on the IDL->Python port and must be
emitted into the shell script, never run as an in-process side effect of `generate`.

---

## 2026-09-13 — `build.py prep`: generated inputs (star → solar file → albedos; template IC → ncdata)

**Naming note (2026-09-14):** this section predates the final naming. The module
shipped as `inputs_utils.py` (not `derive.py`), the matrix key as `make_albedos`
(not `derive_albedos`), and the registry path as `paths.generated_inputs`. The
"derive" vocabulary below is kept as the historical record of the proposal;
CLAUDE.md is the record of current behavior.

**Status:** IMPLEMENTED 2026-09-13 (same day), all six sequencing steps, with these
deviations from the text below — CLAUDE.md "build.py prep — generated inputs" is now
the record of behavior:

- `make_albedos` defaults to true but **only acts when the matrix names `star`**
  (`star: sun` opts the Sun in). The consistency check proposed below failed: the
  calculator's solar values (blue ice 0.82/0.25, snow 0.98/0.59) are not the
  CICE-tuned template defaults (0.67/0.30, 0.80/0.68). A default-on generation
  would therefore have silently changed every existing solar matrix. Whether
  non-solar cases should use raw values or template × (star/sun) ratios is an
  open owner question.
- The albedos travel as a sourced shell fragment (`<solar-stem>__<surface>.albedos.sh`)
  rather than numbers in the script, since `generate` cannot see the spectrum.
- `--verify` on an un-prepped case warns (`run build.py prep`) instead of failing.
- Chain B's pressure rescale was validated byte-for-byte against the IDL-made
  `ic_*bar_L40*` files (the IDL keeps fields in sigma space and rewrites PS/P0 flat).
- Remaining: Discover setup (grid copy, `paths.*`), and the open questions at the end.

Supersedes the 2026-06 sketch above (Proposal 1 + Proposal 2), which it absorbs.

### Problem

Three per-case inputs are consequences of physics the matrix already states, but
today they are produced by hand across four repos and pasted in as filenames:

| Matrix key (today) | Produced by hand with | Really a function of |
|---|---|---|
| `exo_solar_file` | `stellar-interpolator` → `ExoRT/tools/makeStellarSpectrum_fromSED.py` | star (Teff, [M/H], log g) + `exort_pkg` resolution |
| `cice_params` (4 albedos) | `ExoCAM/tools/py_progs/broadband_albedo_calculator.py` | `exo_solar_file` + surface reflectance choice |
| `ncdata` | `ExoCAM/tools/idl_progs/changepress_cesm.pro`, `changevert_cesm.pro` (IDL); partial port in `py_progs/mod_cam.i.file.py` | template IC + `pstd` (from gas bars) + `nlev` + dry/wet |

The hazards are the usual ones: albedos silently out of sync with the star (a
live example: `experiment_matrix.example.yaml` runs a K2V case on the template's
solar albedos), a pressure/IC mismatch found only at a crash, and no record of
how any input was made.

### Principle

**The matrix declares physics; casemgr makes the input files and parameters.** Explicit
`exo_solar_file` / `cice_params` / `ncdata` in the matrix always win — generation
fills gaps, never overrides. A generation that is required but impossible is a
generate-time **error**, never a silent fallback (same rule as
`_missing_template_error`).

### Ownership (decided)

- `stellar-interpolator` owns Teff/[M/H]/log g → raw SED text. Stays standalone.
- ExoRT owns raw SED → ExoRT-binned `.nc` (`makeStellarSpectrum_fromSED.py`).
- ExoCAM owns IC/initial-file modification (`mod_cam.i.file.py`) and the albedo
  calculator. **Python only — the IDL scripts are legacy** and are not called.
- casemgr owns orchestration, naming, caching, provenance, and verification. It
  reaches the engines through `paths.*` in `config_registry.yaml`
  (`stellar_interpolator`, `exocam_root/tools/py_progs`, `exort_root/tools`); the
  engines are refactored to importable functions with no cwd or hardcoded-path
  assumptions but stay in their home repos.

### Two chains

**Chain A — stellar.**
1. `star: {teff, feh, logg}` → stellar-interpolator → raw SED `.txt`.
   `star: sun` (the default) skips this: the shipped `G2V_SUN_<res>.nc` is used,
   exactly as `SOLAR_FILE_STEMS` resolves it today.
2. SED + resolution from `exort_pkg` → ExoRT binner → `exo_solar_file` `.nc`.
3. That `.nc` → albedo calculator, run twice (ice surface, snow surface) →
   `albicei/albicev/albsnowi/albsnowv`. The calculator reads ExoRT `.nc` directly,
   so the albedos are computed from **exactly the spectrum the model sees**.

**Chain B — initial condition.**
`ic_template` (a base `cam.i` file) + `pstd` (`compute_pstd_from_spec`) + `nlev`
+ `dry` → `mod_cam.i.file.py` → `ncdata`. The `ic_files` registry table remains
the path for hand-crafted profiles (isothermal, eyeball, zmean) that are not
derivable; generation is used only when the matrix names an `ic_template`.

**Gate on Chain B:** `mod_cam.i.file.py` does level changes but its surface-pressure
rescale is commented out and `--surface_pressure` is parsed and unused. Finish it
(flat-field and scaled-field modes, as in the IDL), then validate against the
existing IDL-made `ic_*bar_L*_ic.nc` files — regenerate one from its template and
diff. That comparison is the acceptance test, and it is science work, not plumbing.

### New matrix keys

| Key | Default | Meaning |
|---|---|---|
| `star` | `sun` | `sun`, or `{teff: K, feh: dex, logg: cgs}` |
| `make_albedos` | `true` | run Chain A step 3 when no explicit `cice_params` |
| `ice_surface` | (TBD: `blueice`) | reflectance file for `albice*`: `blueice` \| `mixed`. A scientific choice — must be visible in the matrix, never a hidden constant. Snow always uses `snow100um`. |
| `ic_template` | none | base `cam.i` file for Chain B; absent → `ic_files` table lookup as today |
| `ic_dry` | `true` | zero Q/CLDLIQ/CLDICE in the derived IC |

`make_albedos: true` with `star: sun` yields solar albedos, which should
reproduce the template `user_nl_cice` defaults (0.30/0.67/0.68/0.80). Check this
once; it is a free consistency test of the calculator, and it means turning the
default on does not change existing solar matrices.

### Verbs and where things run

- **`build.py generate`** stays pure and local. It resolves each case's derived
  input *names* (deterministic, see naming) and emits a **required-inputs block**
  into the build script: one comment per input recording its generation
  (`# derived: exo_solar_file <- star T3400 g4.5 m0.0, n68`), then an existence
  check that `exit 1`s before `create_newcase`. It never opens the input files —
  it cannot; they are on the HPC.
- **`build.py prep <matrix> [NAME…|--prefix]`** (new) runs **where the data is**
  (Discover). For each case: compute the needed inputs; if a target exists,
  nothing is done; if not, run the chain. Idempotent by name. Preview by default
  (`--execute` to write), listing `present` / `missing → will derive` per input.
  Writes each product plus a sidecar manifest `<file>.prep.yaml` (inputs, tool
  paths + git SHAs, timestamp, casemgr version).
- **`build.py generate --verify`** reports the derived names and, on the HPC,
  their existence via the existing `NCFILE_FIELDS` machinery.
- **`scan.py`** reads the sidecar manifests where present so the registry carries
  `star` and the IC recipe, and `query.py export` round-trips physics, not paths.
- **Machine:** the IC chain needs HPC scratch; the stellar chain is cheap but its
  BT-Settl grid is local and gitignored. Put the grid on Discover once and run all
  of `prep` there — one machine, one set of paths.

### Naming conventions (owner decision needed)

Products live under a new `paths.generated_inputs` root, **not** inside the ExoCAM
or ExoRT checkouts, in `<generated_inputs>/solar/` and `<generated_inputs>/ic/<config_type>/`.

Current solar names are inconsistent (`G2V_SUN_n68.nc`, `BT_Settl_3400K_n28.nc`,
`bt-settl_2600_logg4.5_FeH0_n68.nc`, `blackbody_3400K_n68.nc`, `K2V_n68.nc`).
Proposal, one pattern for derived files:

```
solar:  btsettl_T<teff>_g<logg>_m<feh>_<res>.nc      e.g. btsettl_T3400_g4.5_m0.0_n68.nc
ic:     ic_<template-tag>_<P>bar_L<nlev>[_dry]_ic.nc  e.g. ic_ctrl_1.5bar_L40_dry_ic.nc
```

- `<P>` uses `bar_to_pressure_str` (6 sig figs, trailing zeros stripped) so it
  matches the `ic_files` table key convention. A `pstd` that is a sum of gas bars
  (e.g. `1.000123`) names an odd file; that is honest — it is the pressure the IC
  actually has.
- `<template-tag>` is a short label declared beside the template in the registry
  (`ic_templates: {ctrl: control_L51.cam.i.nc, …}`), because the product depends
  on the template and the name must say which one.
- Existing hand-made files keep their names; only generated products follow the
  pattern. Renaming the ExoRT `data/solar/` tree is out of scope.

### Sequencing

1. Refactor engines to importable functions: `broadband_albedo_calculator.py`
   (drop the `../spectral_albedos` cwd assumption), `mod_cam.i.file.py` (flat
   script → function), `makeStellarSpectrum_fromSED.py` (already close). Each in
   its own repo, each usable standalone as before.
2. `paths.*` entries + `generated_inputs` root + naming helpers in casemgr.
3. **Chain A end to end** + `prep` verb + required-inputs block + `--verify`
   reporting. Fixes the live K2V drift; unblocks M-dwarf suites.
4. Finish the pressure rescale in `mod_cam.i.file.py`; validate vs IDL products.
5. **Chain B** plugs into the same `prep`/manifest/naming machinery as a second
   producer.
6. Registry round-trip (scan manifests, export `star`/`ic_template`).

### Open questions

- `ice_surface` default: `blueice` or `mixed`?
- Should `make` call `prep` automatically (`make --prep`), or stay separate with
  the script's existence guard as the safety net? Leaning separate.
- Solar-constant normalization (`--snorm`) is a property of the star *and* the
  orbit; today it lives in `exoplanet_mod.F90` (`exo_scon`). Confirm the binner
  output is shape-only so `prep` never has to know the orbit.
- Does the derived-IC template need config_type-specific handling (land/mixed
  ICs carry surface fields the aqua template lacks)?

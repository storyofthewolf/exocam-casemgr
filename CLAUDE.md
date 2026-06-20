# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git workflow

- **Bug fixes, docs, small changes** → commit directly to `main`. Do NOT branch,
  do NOT ask. If the prompt obviously describes a fix, just commit.
- **Significant feature changes, new features, or refactors** → before
  committing, ASK whether to create a new branch or commit to `main`. Do not
  branch automatically and do not assume.
- When in doubt, lean toward committing to `main`.

## What this is

ExoCAM case management tools — Python scripts that automate building, inspecting, and managing [ExoCAM](https://github.com/storyofthewolf/ExoCAM) simulation cases on HPC systems. ExoCAM is a fork of CESM 1.2.1 for exoplanet climate modeling. Scripts cover the full simulation lifecycle: YAML experiment matrix → CESM shell build scripts → YAML registry → disk management.

Target runtime: NASA Discover (SLURM HPC). Build scripts are generated locally, reviewed, then run on the cluster.

## Architecture

### Data flow

```
experiment_matrix.yaml
  + config_registry.yaml
       ↓
  build.py
       ↓
  scripts/<case>_build.sh    ← self-contained shell script: create_newcase/create_clone + build
                               (rendered exoplanet_mod.F90 embedded as inline heredoc)

CASE directories on HPC
       ↓
  scan.py
       ↓
  active.yaml                ← queryable YAML registry (active cases)
  retired.yaml               ← queryable YAML registry (retired cases)
       ↓
  query.py                   ← search registry, export experiment matrices

cases/ + rundir/ + archive/ on HPC
       ↓
  datamgr.py cata             ← surgical output housekeeping: purge-bld, purge-restarts,
  │                             purge-hist, purge-logs, move-hist
  datamgr.py                  ← disk reporting, averaging, retirement lifecycle
  diff.py                    ← SourceMods diff before retiring
```

### Module roles

- **`parse_utils.py`** — pure parsing primitives; no filesystem side effects (invariant)
- **`build.py`** — validates experiment matrix, generates self-contained shell build scripts
- **`scan.py`** — walks CASE directories, extracts metadata, writes grouped YAML registry
- **`query.py`** — searches registry, exports experiment matrices
- **`datamgr.py`** — case data management: `report` (disk survey), `cata` (surgical purge/move), `avg` (permanent N-year averaging), `retire` (end-of-life archival)
- **`manage_utils.py`** — shared utility layer imported by `datamgr.py`, `runmgr.py`, and `build.py`: constants (`ARCHIVE_MODELS`, `HIST_MODELS`, `MODEL_STEM`, `AVG_HIST_DEFAULT_MODELS`), `load_paths()`, disk helpers (`dir_size_bytes`, `fmt_size`, `list_files_with_size`), `discover_cases()`, hist-year filtering, `restart_sets()`, `confirm()`, `_require_cases()`, `submit_case()` (the single `sbatch` code path, shared by `runmgr.py submit` and `build.py make --send-it`)
- **`runmgr.py`** — run control tool; `check` subcommand (CaseStatus parsing, SLURM probe, optional hist/energy info); `submit` subcommand (sbatch a built case as-is, no xmlchange — the launch step after `build.py make`; requires `<case>.run`); `continue` subcommand (set CONTINUE_RUN=TRUE, update STOP_N/RESUBMIT, sbatch); `restart` subcommand (set CONTINUE_RUN=FALSE, apply arbitrary `--set VAR=VALUE` xmlchange calls, sbatch)
- **`diff.py`** — SourceMods diff tool; used before retiring to check for custom Fortran worth preserving
- **`config_registry.yaml`** — machine-specific paths, CESM config per config_type, IC file table; must be edited per user/machine

### Key non-obvious behaviors

- `scan.py --update` **clobbers** the registry — does not merge with pre-existing content. Live rows take precedence over archive rows on name collision.
- `build.py generate` never executes scripts; `build.py make` runs them (with confirmation prompt). `make` **builds but does not submit** — submission is a separate step (`runmgr.py submit`, or `make --send-it` to fold both together). The three run verbs are distinct: `submit` (launch a built case as-is, no xmlchange), `continue` (CONTINUE_RUN=TRUE), `restart` (CONTINUE_RUN=FALSE + fixes).
- All destructive `datamgr.py` operations (including `cata`) default to **preview mode**; `--execute` required to act.
- `exoplanet_mod.F90` is embedded inline in each build script via heredoc — no staging directory.
- In clone mode, `user_nl_cam` is copied verbatim from the clone source, so namelist params use **upsert** semantics (grep/sed/echo) rather than plain append, to avoid duplicate keys.
- `exort_pkg` ending in `*` signals custom RT copied into SourceMods. In newcase mode this is a validation error; in clone mode it is allowed and triggers `_build_usr_src_fix_block` to rewrite the inherited `-usr_src` path.
- `runmgr.py check` defaults to **all discoverable cases** when given no names — unlike every destructive subcommand, which requires explicit names.

---

## runmgr.py check — internals

### CaseStatus parsing

`$caseroot/<case>/CaseStatus` is read and only the **last non-blank line** is used. Each line is parsed as `<event> <YYYY-MM-DD> <HH:MM:SS>` by splitting off the last two whitespace tokens; everything before is the event prefix.

Segment history counts (run ok/failed, first start, last success) are intentionally **not reported**. CaseStatus is inherited verbatim when a case is cloned, so cumulative counts from the full file are unreliable for clone cases.

Event prefix → status label mapping (matched by `str.startswith`):

| Event prefix | Status label |
|---|---|
| `run SUCCESSFUL` | `COMPLETE` |
| `run FAILED` | `FAILED` |
| `run started` | `RUNNING` |
| `build complete` | `BUILT` |
| `cesm_setup` | `CLEANED` (covers `cesm_setup -clean`) |
| (anything else) | `UNKNOWN` |

Output per case is a single columnar line: case name left-justified to the longest name in the current output set, status tag `[STATUS]` left-justified to 15 characters, then the timestamp. All results are collected before printing so `max_name_len` is known. Example:

```
cam_mixed_fv_modern               [COMPLETE]       2026-03-07 13:00:02
cam_mixed_fv_modern_eruption      [BUILT]          2026-05-13 02:23:19
cam_mixed_fv_modern_eruption_it2  [FAILED]         2026-05-09 16:24:59
```

If `CaseStatus` is missing (no caseroot dir), status is shown as `NO_CASEDIR`.

### SLURM probe

When the last CaseStatus event starts with `run started` or `run SUCCESSFUL`, `squeue --name <case> -h` is run as a subprocess:
- **Job found + last event was `run SUCCESSFUL`** → status shown as `RESUBMITTED`
- **No job + last event was `run started`** → status shown as `RUNNING?` (started but no longer queued — likely crashed without writing to CaseStatus)
- **`FileNotFoundError`** (squeue not in PATH) or **non-zero exit code** → probe silently omitted, original status label retained

### --energy computation

1. List `*.cam.h0.*.nc` files in `$archive/<case>/atm/hist/` excluding filenames containing `"avg"`. Sort lexicographically (= chronological for CESM date strings).
2. Take the last 12 (or fewer, with a warning printed).
3. Run `ncra <file1> ... <fileN> /tmp/runmgr_energy_<case>.nc`. If `ncra` is not found, print a warning and skip.
4. Open the temp file with `netCDF4`. Extract `TS`, `FSNT`, `FLNT`. If any variable is missing, print a warning and skip.
5. Compute area weights: `w = cos(lat * π/180)`, broadcast across the lon dimension, normalize to sum to 1.
6. Compute global means via `sum(data * w2d)`.
7. Print `Last Nmo:  TS = 287.3 K    Etop = +0.8 W/m²` (Etop = FSNT_mean − FLNT_mean, signed, 1 decimal).
8. The temp file is always deleted in a `finally` block, even on error.

---

## Config types

| `config_type` | Description |
|---|---|
| `cam_aqua_fv` | Aquaplanet, finite-volume dynamics |
| `cam_land_fv` | Land/continent, finite-volume |
| `cam_mixed_fv` | Mixed ocean/land, finite-volume |
| `cam_aqua_se_ne5` / `ne16` | Aquaplanet, spectral-element dynamics |

SE configs strip the `_ne5`/`_ne16` suffix when resolving SourceMods template directories.

Config-conditional logic (present in both `build.py` and `scan.py`):
- `cam_land_fv`, `cam_mixed_fv` → parse/sed `user_nl_clm` for `finidat`/`fsurdat`
- `cam_aqua_fv`, `cam_aqua_se_*`, `cam_mixed_fv` → parse/sed `user_docn.streams.txt.som` for SOM forcing file

`_infer_config_type()` in `scan.py` decides config_type from SourceMods subdirectory presence:
- `src.cice` + `src.clm` → `cam_mixed_fv`
- `src.cice` only → `cam_aqua_fv`
- `src.clm` only → `cam_land_fv`
- neither → `unknown`

**This decision tree is the authoritative source for config_type — it must stay consistent with `config_registry.yaml` entries.**

---

## Pressure and N2 handling

`render_exoplanet_mod` behaves differently for newcase vs clone (controlled by its `is_clone` flag):

- **Newcase (`is_clone=False`) — clean slate.** Every radiatively-active gas in `GAS_BAR_PARAMS` (CO2, CH4, C2H6, NH3, CO, H2, O2) that is *not* named in the matrix is forced to `0.0` — the template's modern-Earth defaults (e.g. `exo_o2bar = 0.2095`) must not leak in. N2 is **always** emitted as an explicit numeric fill: `exo_n2bar_explicit` if set, otherwise `compute_pstd_from_spec(spec) − sum(specified gases)`. The Fortran `1 - sum(others)` expression line is never relied upon for newcase.
- **Clone (`is_clone=True`) — preserve composition.** Only the gas params named in the matrix are substituted; all unspecified gases and N2 keep whatever the clone-source `exoplanet_mod.F90` has. `exo_n2bar` is patched only when `exo_n2bar_explicit` is set (high-pressure case); otherwise the source's expression line is left intact.

`_fortran_value` formats gas bar values at 12 significant figures (`%.12g`) so the full input precision of the N2 fill survives without float noise.

Total surface pressure (`compute_pstd_from_spec`) is the sum of individual gas bar values: `exo_n2bar_explicit + sum(others)` when explicit N2 is set, else `sum(others)` (defaulting to 1.0 for ≤1 bar). Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are IC file table keys and must exactly match substrings in IC filenames.

---

## Common modification patterns

### Adding a new registry field
1. Add the key to the appropriate group's field list in `scan._REGISTRY_GROUPS`.
2. Add collection logic in `inspect_case()` in `scan.py`.
3. If it should appear in exported matrices, add it to `_BASE_FIELD_ORDER` in `query.py`.

### Adding a new config_type
1. Add an entry to `config_registry.yaml` under `cesm_config` (`res`, `compset`, `phys`).
2. Add IC file entries under `ic_files` in `config_registry.yaml`.
3. Verify `_infer_config_type()` in `scan.py` will assign the new type correctly.
4. Verify config-conditional blocks in `build.py` and `scan.py` cover the new type.

### Adding a new EXO_PARAMS parameter
1. Add the parameter name to the `EXO_PARAMS` set in `build.py`.
2. Ensure the corresponding `parameter ::` declaration exists in the `exoplanet_mod.F90` template.
3. If it should be scanned into the registry, add it to `inspect_case()` in `scan.py` and to `_REGISTRY_GROUPS`.

### Extending `query.py export` output fields
1. Add the registry key to `_BASE_FIELD_ORDER` in `query.py`.
2. If it should appear in clone-mode sparse exports, add it to `_CLONE_BASE_FIELDS`.
3. If the registry key name differs from the matrix key name, add a rename entry to `_KEY_RENAMES`.

---

## Design invariants — do not violate

- `parse_utils.py` must remain free of filesystem side effects. It reads files via paths passed to it; it never discovers or writes files itself.
- All destructive `datamgr.py` operations (including `cata`) require `--execute`. Without it, every command only prints what it would do.
- No `--all` flag exists for destructive operations in either tool. Cases must be named explicitly.
- `build.py generate` generates scripts but never executes them. `build.py make` runs them (with confirmation prompt).
- `scan.py --update` clobbers the registry with exactly the cases scanned in the current run. It does not merge with pre-existing registry content.
- `exoplanet_mod.F90` is always skipped by `diff.py` (it is patched per-case and is not meaningful to diff).

---

## Known limitations

### Pre-existing registry rows lack run_type (scan.py)
Cases scanned before `run_type` support was added will not have `run_type`, `run_refcase`, `run_refdate`, or `brnch_retain_casename`. `query.py export` defaults `run_type` to `'startup'` for backward compatibility. Re-scan with `scan.py` to populate from live `env_run.xml`.

### Custom RT packages not supported in `create_newcase` builds (build.py)
`generate_shell_script` only supports RT packages via `-usr_src ../ExoRT/3dmodels/*`. Cases with custom RT copied into SourceMods must use clone mode (`create_clone` inherits SourceMods from the source case).

### `n68equiv.haze` registered as `n68equiv` (scan.py)
`scan.py` does not distinguish the `.haze` suffix in `-usr_src` paths. No fix planned — `n68equiv.haze` is expected to merge into `n68equiv` in a future ExoRT update.

### diff.py: non-standard ExoRT package directory paths
`build_exort_fileset` constructs the ExoRT reference as `{exort_root}/3dmodels/src.cam.{exort_pkg}/`. Experimental branches outside this path cause RT detection to silently return `{}` — affected files appear as `CASE ONLY`. Cases with non-standard RT are flagged with `*` in `query.py search` output. Future fix: add `paths.exort_pkg_dirs` map to `config_registry.yaml`.

---

## Session handoff — 2026-06-19

Three generated-build-script bugs surfaced from a real batch (`gplfr_grp3.yaml`). All three fixes affect only **newly generated** `build_scripts/*.sh` — existing scripts on the HPC must be regenerated to benefit.

**1. Namelist upsert duplicate bug (`_nl_upsert_lines`, build.py):** The clone-mode `grep "KEY" && sed -i "s|KEY = .*|...|" || echo >>` idiom appended a duplicate (e.g. cice albedos) instead of replacing, because the `&& … || …` chain falls through to the append branch on *any* non-zero `sed` exit, and the unanchored single-space pattern was brittle. Replaced with `if grep -qE "^[[:space:]]*KEY[[:space:]]*=" T; then sed -i -E ... ; else echo >> ; fi`. Append now fires only when the key is genuinely absent.

**2. ncdata absolute-path mangling (build.py):** Explicit absolute `ncdata` values were double-prefixed with the config-type IC dir, producing `.../cam_aqua_fv//gpfsm/.../ic_*.nc`. New `resolve_ic_path()` helper: bare filename → prepend IC base dir; absolute/dir-bearing path → verbatim. Used by both `generate_shell_script` and `generate_clone_script`.

**3. Newcase clean-slate gas composition (`render_exoplanet_mod`, build.py):** Added an `is_clone` flag. Newcase now forces every unspecified `GAS_BAR_PARAMS` gas to `0.0` (no more inherited modern-Earth `exo_o2bar=0.2095`) and always emits explicit `exo_n2bar` (`exo_n2bar_explicit` or `target − sum(specified)`). Clone preserves the source composition (unchanged behavior). `_fortran_value` bumped to 12 sig figs so the N2 fill precision survives. "Pressure and N2 handling" section above rewritten to document both paths.

**Git workflow policy added** (this CLAUDE.md + global `~/.claude/CLAUDE.md`): bug fixes/docs/small changes commit directly to `main` without asking; significant features/new features/refactors → ask whether to branch first.

### Good starting points for next session
- Regenerate the `gplfr_grp3` build scripts on the HPC and verify all three fixes in the rendered output.
- Existing handoff items still open: stale `build.py` module docstring; `nl_cam_params` recognized by `build.py` but not scanned by `scan.py`; whether `datamgr.py avg` should move to `runmgr.py`.

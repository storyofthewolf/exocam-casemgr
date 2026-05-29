# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
  runmgr.py cata             ← active-run housekeeping: purge-bld, purge-restarts,
  │                             purge-hist, purge-logs, move-hist
  manage.py                  ← disk reporting, averaging, retirement lifecycle
  diff.py                    ← SourceMods diff before retiring
```

### Module roles

- **`parse_utils.py`** — pure parsing primitives; no filesystem side effects (invariant)
- **`build.py`** — validates experiment matrix, generates self-contained shell build scripts
- **`scan.py`** — walks CASE directories, extracts metadata, writes grouped YAML registry
- **`query.py`** — searches registry, exports experiment matrices
- **`manage.py`** — disk reporting, hist averaging, and retirement lifecycle: `report`, `avg`, `retire`
- **`manage_utils.py`** — shared utility layer imported by both `manage.py` and `runmgr.py`: constants (`ARCHIVE_MODELS`, `HIST_MODELS`, `MODEL_STEM`, `AVG_HIST_DEFAULT_MODELS`), `load_paths()`, disk helpers (`dir_size_bytes`, `fmt_size`, `list_files_with_size`), `discover_cases()`, hist-year filtering, `restart_sets()`, `confirm()`, `_require_cases()`
- **`runmgr.py`** — run supervision tool; `check` subcommand (CaseStatus parsing, SLURM probe, optional hist/energy info); `continue` subcommand (set CONTINUE_RUN=TRUE, update STOP_N/RESUBMIT, sbatch); `restart` subcommand (set CONTINUE_RUN=FALSE, apply arbitrary `--set VAR=VALUE` xmlchange calls, sbatch — for re-running from scratch after fixing a parameter); `cata` subcommand group for active-run housekeeping: `purge-bld`, `purge-restarts`, `purge-hist`, `purge-logs`, `move-hist`
- **`diff.py`** — SourceMods diff tool; used before retiring to check for custom Fortran worth preserving
- **`config_registry.yaml`** — machine-specific paths, CESM config per config_type, IC file table; must be edited per user/machine

### Key non-obvious behaviors

- `scan.py --update` **clobbers** the registry — does not merge with pre-existing content. Live rows take precedence over archive rows on name collision.
- `build.py generate` never executes scripts; `build.py make` runs them (with confirmation prompt).
- All destructive `manage.py` and `runmgr.py cata` operations default to **preview mode**; `--execute` required to act.
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

Total surface pressure is computed from the sum of individual gas bar values. N2 is implicit for ≤1 bar atmospheres (fills to 1.0). For higher pressures, `exo_n2bar_explicit` must be set in the matrix — this patches the `exo_n2bar` Fortran line with an explicit numeric value. Without it, the N2 expression line is left for the Fortran compiler to evaluate at compile time.

Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are IC file table keys and must exactly match substrings in IC filenames.

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
- All destructive `manage.py` and `runmgr.py cata` operations require `--execute`. Without it, every command only prints what it would do.
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

## Session handoff — 2026-05-13

### Work completed (2026-05-13)

**`diff.py`:**
- Added `normalize_lines` / `read_normalized` helpers; all 6 binary identity checks now use `read_normalized` so trailing-whitespace-only diffs are treated as identical.
- `diff_counts` updated to normalize before counting.
- All 4 `subprocess.run(['diff', ...])` calls in `cmd_full` now pass `-b` (ignore trailing whitespace in full diff view).

**`query.py`:**
- Added `RETIRED_REGISTRY` constant pointing to `retired.yaml`.
- Added `--retired` top-level flag as shorthand for `--registry retired.yaml`; mutually exclusive with `--registry`.
- Footer now prints `--retired` (not the path) when that flag was used.

**Rename: `archived` → `retired` (names only, no logic changes):**
- `scan.py`: `--archive` flag → `--retired`; all `args.archive` references → `args.retired`; `'archived.yaml'` string → `'retired.yaml'`; `_REGISTRY_HEADER` regeneration hint updated; docstring and epilog updated.
- `query.py`: `ARCHIVED_REGISTRY` → `RETIRED_REGISTRY`; `--archived` flag → `--retired`; mutual-exclusion message updated; clone guard updated.
- `CLAUDE.md`, `DEVELOPER_NOTES.md`, `README.md`: all `archived.yaml` / `--archive` / `--archived` references updated to match.
- `archived.yaml` renamed to `retired.yaml` on disk.

**`manage_utils.py` (new) + `runmgr.py` (new) — cata migration:**
- Created `manage_utils.py` with shared constants, `load_paths()`, disk helpers, hist-year filtering, `restart_sets()`, `confirm()`, `_require_cases()`. `manage.py` now imports all of these from there.
- Created `runmgr.py` with `cata` subcommand group: `purge-bld`, `purge-restarts`, `purge-hist`, `purge-logs`, `move-hist` — direct ports of the same commands from `manage.py`.
- Removed all five subcommands from `manage.py` (functions, argparse registrations, COMMANDS entries, docstring). `manage.py` now covers only `report`, `avg`, `retire`.

**`runmgr.py check` (new subcommand):**
- Parses `$caseroot/<case>/CaseStatus` (last non-blank line only) to determine current status (RUNNING/COMPLETE/FAILED/BUILT/CLEANED/UNKNOWN/NO_CASEDIR). Segment history not reported — CaseStatus inherited by clones makes counts unreliable.
- SLURM probe via `squeue --name <case> -h`; degrades gracefully when squeue unavailable.
- `RESUBMITTED` status when last event is `run SUCCESSFUL` but a job is still queued.
- `RUNNING?` status when last event is `run started` but no job is queued (likely crashed).
- `--info` flag: per-model hist summary and restart set count (reuses `_hist_year`, `list_files_with_size`, `restart_sets` from `manage_utils`).
- `--energy` flag: global-mean TS and Etop=FSNT-FLNT from last 12 atm h0 files via ncra + netCDF4.
- Defaults to all discoverable cases when no names or prefix given (unlike destructive subcommands).

### Good starting points for next session
- Update stale module docstring in `build.py`.
- `nl_cam_params` recognized by `build.py` but not yet scanned by `scan.py` — add to `_REGISTRY_GROUPS` and `inspect_case()` if desired.
- Consider whether `manage.py avg` should move to `runmgr.py`.

---

## Session handoff — 2026-05-20 (continue subcommand)

### Work completed (2026-05-20)

**`runmgr.py continue` (new top-level subcommand):**
- CLI: `runmgr.py continue case1 case2 ... [--stop-n N] [--resubmit N] [--execute]`
- Reads current `STOP_N`, `RESUBMIT`, `CONTINUE_RUN` from `env_run.xml` via `_read_xml_var` (ElementTree; no xmlquery subprocess).
- Status gate via existing `_parse_casestatus` + `_squeue_probe`: hard-blocks RUNNING/RESUBMITTED; soft-warns (per-case confirmation) for any non-COMPLETE status; COMPLETE proceeds silently.
- Always issues `xmlchange CONTINUE_RUN=TRUE` and `xmlchange RESUBMIT=<N>` (default 0). Only issues `xmlchange STOP_N=<N>` when `--stop-n` is explicitly passed.
- xmlchange called via `subprocess.run(['./xmlchange', ...], cwd=case_dir)` — same pattern as build scripts.
- `sbatch <case>.run` called from `cwd=case_dir`; job ID extracted from stdout and printed.
- Preview (no `--execute`): prints the full planned action for every case and exits without touching anything.
- Registered as top-level subcommand alongside `check` and `cata`. Added to module docstring, README subcommand table, and CLAUDE.md module roles.
- `_read_xml_var(xml_path, var_name)` — new private helper; parses CESM 1.x `<entry id="..." value="..."/>` format.

**`runmgr.py continue` refinements (2026-05-20):**
- `--prefix PREFIX` added as an alternative to explicit case names; mutually exclusive with positional case names; errors out if no cases match.
- `STOP_OPTION` read from `env_run.xml` and displayed inline with `STOP_N` in the preview block: `STOP_N: 10 -> 1  (stop_option: nyears)`.
- Preview footer printed once after all cases: `(preview only — rerun with --execute to submit)` — only shown when `--execute` was not passed.

---

## Session handoff — 2026-05-29

### Work completed (2026-05-29)

**`runmgr.py restart` subcommand added:**
- CLI: `runmgr.py restart case1 case2 ... [--set VAR=VALUE ...] [--stop-n N] [--resubmit N] [--execute]`; also accepts `--prefix`
- Always applies `CONTINUE_RUN=FALSE` first; then applies `--set` pairs in order; then sbatches
- `--set` is repeatable and generic — any CESM xml variable; immediate use case is `--set RUN_STARTDATE=YYYY-MM-DD`
- `--stop-n` / `--resubmit` are convenience aliases (appended after `--set` items); RESUBMIT defaults to current value (unlike `continue` which defaults to 0)
- Status gating: RUNNING/RESUBMITTED → hard block; COMPLETE → silent; all others → soft-warn with per-case confirmation
- Preview: shows current → new for CONTINUE_RUN and each changed var, plus sbatch line; footer printed once

**`run_startdate` / `RUN_STARTDATE` added:**
- `parse_utils.py`: added `RUN_STARTDATE` → `run_startdate` to `parse_run_type_fields`; defaults to `None`
- `scan.py`: added `run_startdate` to `_REGISTRY_GROUPS['meta']` (after `brnch_retain_casename`)
- `query.py`: added `run_startdate` to `_BASE_FIELD_ORDER` and `_CLONE_BASE_FIELDS`
- `build.py`: added conditional `./xmlchange RUN_STARTDATE=...` after `RESUBMIT` in both `generate_shell_script` and `generate_clone_script`; emitted only when field is present in spec (optional — not in `REQUIRED_FIELDS`)

---

## Session handoff — 2026-05-20

### Work completed (2026-05-20)

**`REST_OPTION` added to build pipeline:**
- `config_registry.yaml`: added `rest_option: nyears` to `defaults:` block (after `stop_option`)
- `query.py`: added `--rest-option` CLI flag, `_cli_or_default`, `base` dict population, `_REQUIRED_LABELS` entry, and `_CLONE_BASE_FIELDS` entry — all parallel to `stop_option`
- `build.py`: added `./xmlchange REST_OPTION=...` after `./xmlchange STOP_OPTION=...` in both `generate_shell_script` and `generate_clone_script`; added `rest_option` to `REQUIRED_FIELDS` and `REQUIRED_FIELDS_CLONE`
- `DEVELOPER_NOTES.md`: updated `REQUIRED_FIELDS`, `REQUIRED_FIELDS_CLONE`, `_CLONE_BASE_FIELDS`, and `config_registry.yaml` structure docs

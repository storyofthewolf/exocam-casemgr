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
  manage.py                  ← disk reporting, purge, move-hist, retire
  diff.py                    ← SourceMods diff before retiring
```

### Module roles

- **`parse_utils.py`** — pure parsing primitives; no filesystem side effects (invariant)
- **`build.py`** — validates experiment matrix, generates self-contained shell build scripts
- **`scan.py`** — walks CASE directories, extracts metadata, writes grouped YAML registry
- **`query.py`** — searches registry, exports experiment matrices
- **`manage.py`** — disk reporting, purge/move/retire operations; lifecycle: `report`, `avg`, `retire`
- **`diff.py`** — SourceMods diff tool; used before retiring to check for custom Fortran worth preserving
- **`config_registry.yaml`** — machine-specific paths, CESM config per config_type, IC file table; must be edited per user/machine

### Key non-obvious behaviors

- `scan.py --update` **clobbers** the registry — does not merge with pre-existing content. Live rows take precedence over archive rows on name collision.
- `build.py generate` never executes scripts; `build.py make` runs them (with confirmation prompt).
- All destructive `manage.py` operations default to **preview mode**; `--execute` required to act.
- `exoplanet_mod.F90` is embedded inline in each build script via heredoc — no staging directory.
- In clone mode, `user_nl_cam` is copied verbatim from the clone source, so namelist params use **upsert** semantics (grep/sed/echo) rather than plain append, to avoid duplicate keys.
- `exort_pkg` ending in `*` signals custom RT copied into SourceMods. In newcase mode this is a validation error; in clone mode it is allowed and triggers `_build_usr_src_fix_block` to rewrite the inherited `-usr_src` path.

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
- All destructive `manage.py` operations require `--execute`. Without it, every command only prints what it would do.
- No `--all` flag exists for destructive operations. Cases must be named explicitly.
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

### Good starting points for next session
- Update stale module docstring in `build.py`.
- `nl_cam_params` recognized by `build.py` but not yet scanned by `scan.py` — add to `_REGISTRY_GROUPS` and `inspect_case()` if desired.
- Consider whether `manage.py avg` should move to `runmgr.py`.

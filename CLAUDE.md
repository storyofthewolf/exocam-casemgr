# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ExoCAM case management tools — Python scripts that automate building, inspecting, and managing [ExoCAM](https://github.com/storyofthewolf/ExoCAM) simulation cases on HPC systems. ExoCAM is a fork of CESM 1.2.1 for exoplanet climate modeling. The scripts here cover the full simulation lifecycle: translating a YAML experiment matrix into CESM shell build scripts, scanning existing CASE directories into a queryable YAML registry, and managing disk usage across cases, run, and archive storage.

The target runtime environment is NASA Discover (SLURM HPC). Build scripts are generated locally, reviewed, then run on the cluster.

## Running the tools

```bash
# Generate build scripts (preview only — no execution)
python exo_build.py experiment_matrix.yaml --outdir scripts/

# Generate AND execute builds
python exo_build.py experiment_matrix.yaml --outdir scripts/ --execute

# Run a single generated build script
bash scripts/my_case_build.sh

# Run all build scripts in a directory
bash run_builds.sh scripts/

# Inspect a case by bare name (resolved to caseroot from config_registry.yaml)
python exo_inspect.py my_case --registry cases.yaml

# Preview inspection without writing the registry
python exo_inspect.py my_case --dry-run

# Update (merge) instead of overwriting registry
python exo_inspect.py my_case --registry cases.yaml --update

# Disk usage report across all cases (default when called with no args)
python exo_data.py

# Purge/move commands — preview by default, --execute to act
python exo_data.py purge-bld --execute
python exo_data.py purge-restarts --keep 1 --execute
python exo_data.py move-hist --models atm --execute my_case

# Search and export from registry
python exo_query.py search --config-type cam_land_fv --nlev 51
python exo_query.py show ExoCAM_thai_ben1_L51_n68equiv
python exo_query.py export case_a case_b -o sweep.yaml --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126
python exo_query.py export my_base_case -o clone.yaml --clone my_base_case --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126
```

Dependencies: `pip install pyyaml` (required); `pip install netCDF4` (optional, for solar file nw validation)

## Architecture

### Data flow

```
experiment_matrix.yaml
  + config_registry.yaml
       ↓
  exo_build.py
       ↓
  scripts/<case>_build.sh                     ← shell script: create_newcase/create_clone + build
  scripts/staging/<case>/exoplanet_mod.F90    ← patched Fortran parameter file

CASE directories on HPC
       ↓
  exo_inspect.py
       ↓
  cases.yaml                                  ← queryable YAML registry
       ↓
  exo_query.py                                ← search registry, export experiment matrices

cases/ + rundir/ + archive/ on HPC
       ↓
  exo_data.py                                 ← disk reporting, purge, move
```

### `exo_parse.py` — pure parsing primitives, no filesystem side effects

Used by both `exo_build.py` and `exo_inspect.py`.

- `parse_exoplanet_mod(path)` — reads Fortran parameter file → flat dict. Evaluates arithmetic expressions (e.g. `0.91*6.37122e6_R8`) and symbol-substitution expressions (e.g. `1.0 - exo_co2bar - exo_ch4bar`) using `_try_eval_expr()`, which substitutes already-resolved param values then evals in a restricted namespace. Unevaluable expressions fall back to `name_expr` raw string storage.
- `parse_user_nl_cam(path)` — reads CESM namelist → dict. Captures `ncdata`, IC pressure/level from filename, and any `carma_*` / `volc_*` keys as nested dicts. Handles both single- and double-quoted values.
- `parse_user_nl_clm(path)` — reads `user_nl_clm` → dict with `finidat` and `fsurdat`. Called for `cam_land_fv` and `cam_mixed_fv` only.
- `parse_docn_som(path)` — reads `user_docn.streams.txt.som` (XML fragment, wrapped in synthetic root for ElementTree) → dict with `som_pop_frc_file`. Called for aqua and mixed configs only.
- `parse_cam_config_opts(xmlpath)` — reads `env_build.xml` for `-nlev`, `-usr_src` (exort_pkg), cloud scheme.
- `compute_pstd_bar(params)` — derives total surface pressure from gas bar values.
- `read_solar_nw(path)` — reads `nw` dimension from a NetCDF solar file using `netCDF4`. Returns `None` if the library is absent or the file is inaccessible. Used for solar file / exort_pkg consistency checking in `exo_inspect.py`.

### `exo_build.py` — validation and shell script generation

- `resolve_case(base, overrides)` — merges base + per-case dict.
- `validate_case(spec, registry)` — returns list of error strings; checks required fields, IC file availability, solar/exort consistency, synchronous rotation math. Clone cases (`clone` present) use `REQUIRED_FIELDS_CLONE` (relaxed — config fields are inherited from source case).
- `render_exoplanet_mod(template_path, spec)` — regex-patches active Fortran parameter lines for all names in `EXO_PARAMS`. When `exo_n2bar_explicit` is set, also patches the `exo_n2bar` line with the explicit numeric value (high-pressure cases); otherwise leaves the N2 expression line unchanged for the Fortran compiler to evaluate.
- `generate_shell_script(...)` — writes the `create_newcase` + `cesm_setup` + build script. Config-specific shell commands emitted:
  - All configs: `sed` to update `ncdata` in `user_nl_cam`; `echo >>` for `carma_params`/`volc_params`.
  - All configs: `sed` to patch `#SBATCH --account` and `-J` in `${CASE}.run` after `cesm_setup` (if `account`/`job_name` present in spec).
  - Land/mixed: `sed` for `finidat`/`fsurdat` in `user_nl_clm`.
  - Aqua/mixed: `sed` for `pop_frc*` path in `user_docn.streams.txt.som`.
- `generate_clone_script(...)` — same as above but uses `create_clone -clone $CLONE_OF -case $CASE` for Step 1, skips the SourceMods/namelist copy step (Step 2 of newcase), and makes IC file lookup and `CAM_CONFIG_OPTS` conditional on `config_type`/`exort_pkg`/`nlev` being present.
- `_build_nl_append_block(spec)` — `echo >>` lines for carma/volc namelist params.
- `_build_clm_update_block(spec, paths)` — `sed` lines for CLM land files.
- `_build_docn_update_block(spec)` — `sed` lines for SOM ocean forcing file.
- `_build_run_script_block(spec)` — `sed` lines to patch SBATCH directives into `${CASE}.run`.
- `EXO_PARAMS` — set of parameter names that map directly to `exoplanet_mod.F90` and can be patched from the experiment matrix.
- `REQUIRED_FIELDS` — required for newcase mode: `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`.
- `REQUIRED_FIELDS_CLONE` — required for clone mode: `clone`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`.

### `exo_inspect.py` — CASE directory scanner → YAML registry

Walks CASE directories (identified by `SourceMods/src.share/exoplanet_mod.F90`), extracts scientific metadata, writes a grouped YAML registry. Bare case names are resolved relative to `caseroot` from `config_registry.yaml`.

- `inspect_case(casedir)` — collects all metadata into a flat row dict. `config_type` is inferred from `SourceMods/` subdirectory structure before the config-conditional parse calls.
- `_infer_config_type(casedir)` — checks presence of `src.cice` and `src.clm` to determine aqua/land/mixed.
- `check_consistency(meta)` — generates warnings for pressure mismatches, level mismatches, and solar file/exort_pkg mismatches. Uses `read_solar_nw()` when the file is accessible; falls back to filename stem check otherwise.
- `_rows_to_ordered(rows)` — converts flat row dicts to the grouped YAML structure.
- `write_registry(rows, path)` — writes grouped YAML via `_rows_to_ordered`.
- `load_registry(path)` — reads grouped YAML and flattens groups back to plain dicts for internal use.
- `_REGISTRY_GROUPS` — defines group names and field ordering for YAML output.
- `SOLAR_NW_MAP` — expected `nw` dimension per `exort_pkg`: `{n68equiv: 68, n84equiv: 84, n28archean: 28, n42h2o: 42}`.

### `exo_query.py` — registry search and experiment matrix export

- `load_registry(path)` — loads `cases.yaml` into flat dicts (one per case) for search/export.
- `load_registry_raw(path)` — loads `cases.yaml` preserving grouped structure; used by `show` to reproduce the exact `cases.yaml` format.
- `cmd_search` — tabular listing filtered by `--name` (substring), `--config-type`, `--exort-pkg`, `--nlev` (exact).
- `cmd_show` — dumps one case's full grouped YAML, identical in format to `cases.yaml`.
- `cmd_export` — generates a ready-to-use `experiment_matrix.yaml` from one or more registry cases. For multiple cases, shared fields are factored into `base` automatically. `mach` and `resubmit` are populated from `config_registry.yaml` unless overridden via CLI flags. Required fields left blank are written as empty strings with a `# FIXME` header.
- `_row_to_base(row, bare=False)` — converts a flat registry row to a matrix base dict. `bare=True` strips atmosphere, geophysical, model_options, and special fields; used for clone exports where the clone source supplies those values.
- `_BARE_STRIP_KEYS` — set of fields omitted from `base` in bare mode.
- Clone export behavior: when `--clone` is supplied, bare mode is the default (minimal base, case stubs ready for per-case deltas). `--full` overrides to include all scientific parameters. Without `--clone`, full output is always produced.
- Key renames from registry to matrix: `clm_finidat` → `finidat`, `clm_fsurdat` → `fsurdat`, `ncdata` → `ncdata_override`.
- Registry-only fields stripped from matrix output: `case_name`, `casedir`, `inspect_date`, `ncdata_pressure_str`, `ncdata_levels`, `exo_n2bar`, `exo_n2bar_expr`, `exo_sday_expr`, `exo_pstd_computed_bar`, `warnings`.

### `exo_data.py` — disk management tool

Discovers cases by scanning `caseroot`, `rundir`, and `archive` directories on disk — no registry required. All destructive subcommands are **non-destructive by default**; `--execute` is required to make changes, and each case prompts `yes/N` before acting.

- `discover_cases(paths)` — union of folder names across all three storage roots.
- `case_sizes(case, paths)` — returns per-area byte counts: `casedir`, `bld`, `run`, `hist`, `logs`, `rest`.
- `restart_sets(case, paths)` — returns sorted list of `(date_str, path)` for dated subdirs in `archive/<case>/rest/`.
- `cmd_report` — prints aligned disk usage table: CASEDIR, BLD, RUN, HIST, LOGS, REST, TOTAL.
- `cmd_purge_bld` — deletes `rundir/<case>/bld/`. `--logs-only` removes only `.o`/`.mod` files.
- `cmd_purge_restarts` — trims old restart sets keeping the N most recent (`--keep N`, default 1).
- `cmd_purge_hist` — deletes `archive/<case>/<model>/hist/` contents. `--models` restricts components.
- `cmd_move_hist` — moves hist files to `long_term/<case>/<model>/hist/`, leaving source dir empty.
- `cmd_move_case` — moves entire case tree to long-term storage. `--no-casedir/--no-rundir/--no-archive` skip areas.
- `ARCHIVE_MODELS` — `['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']`.

### `config_registry.yaml` — machine-specific, must be edited per user

Holds:
- `machine` — CESM machine name (e.g. `discover`); read by `exo_query.py export` to populate `mach` automatically
- `resubmit` — default RESUBMIT value (e.g. `1`); read by `exo_query.py export` when `--resubmit` not supplied
- `paths` — `cesm_scripts`, `caseroot`, `rundir`, `archive`, `long_term`, `exocam_root`, `exort_root`
- `cesm_config` — `compset`, `res`, `phys` per `config_type`, used in `create_newcase`
- `ic_files` — IC file lookup table keyed by `config_type → pressure_str → nlev`
- `solar_file_stems` — filename stem expected per `exort_pkg` (fallback for when NetCDF read is unavailable)

### `run_builds.sh` — batch runner

Loops over all `*_build.sh` files in a directory, runs each with `bash`, reports pass/fail per case, and prints a summary. A failed build does not abort remaining cases.

## YAML registry structure

`_REGISTRY_GROUPS` defines six groups; `write_registry` emits one block per group, omitting empty groups.

```yaml
cases:
- meta:          # case identity, CESM config, IC file info, CLM files, SOM file
  atmosphere:    # gas bars, pstd, scon, solar file
  geophysical:   # ndays, porb, sday, gravity, radius, eccen, obliq
  model_options: # do_exo_* flags, exo_convect_plim, exo_rad_step, rt flags
  special:       # carma_params, volc_params (nested dicts; omitted if absent)
  diagnostics:   # warnings list (omitted if no warnings)
```

To add a new inspected field: (1) add its key to the appropriate group in `_REGISTRY_GROUPS`; (2) add it to the collection loop in `inspect_case` (or add a new parse call if it comes from a new source file).

## Experiment matrix format

Copy `experiment_matrix.yaml.example`, set `config_registry` path, fill `base` defaults, add per-case overrides under `cases`. Each case inherits all base values; any key in a case dict overrides the base.

Key matrix-level keys:
- `config_registry` — required path to `config_registry.yaml`
- `paths` — optional overrides of machine paths from the registry
- `base` — shared defaults for all cases
- `cases` — list of case dicts, each with a required `name` key

Special case keys:
- `clone` — triggers clone mode (`create_clone`) instead of `create_newcase`. Typically set in `base` so all cases share the same clone source. The `exoplanet_mod.F90` template is taken from the clone source's SourceMods; only parameters explicitly listed in the matrix are patched.
- `ncdata_override` — bypasses automatic IC file lookup
- `exo_n2bar_explicit` — required for non-1-bar atmospheres; sets N2 directly and patches `exo_n2bar` in Fortran
- `account` — `#SBATCH --account` written to `${CASE}.run` (typically in `base`)
- `job_name` — `#SBATCH -J` written to `${CASE}.run` (typically per-case)
- `carma_params`, `volc_params` — nested dicts appended to `user_nl_cam` via `echo >>`

## Pressure representation

Total surface pressure is computed from the sum of individual gas bar values. N2 is implicit for ≤1 bar atmospheres (fills to 1.0). For higher pressures, `exo_n2bar_explicit` must be set. When set, `render_exoplanet_mod` patches the `exo_n2bar` Fortran line with the explicit numeric value. For standard cases, the N2 expression line is left unchanged and evaluated by the Fortran compiler at compile time.

Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are used as keys in the IC file table and must exactly match substrings in IC filenames.

## Fortran patching

`render_exoplanet_mod` matches active parameter lines via `_RE_PARAM_LINE` (real/integer/logical with `parameter ::`). Skips commented lines. Values formatted with `_r8` kind suffix for Fortran reals; logicals become `.true.`/`.false.`.

`parse_exoplanet_mod` uses `_try_eval_expr`:
- Strips Fortran kind suffixes (`_r8`, `_R8`)
- Substitutes already-resolved numeric params by name, longest-name-first to avoid partial matches
- Only calls `eval()` if result matches `_RE_SAFE_EXPR` (pure arithmetic: digits, operators, parens)
- Runs `eval` with `__builtins__: {}` to restrict the namespace
- Falls back to `name_expr` raw string storage on any failure

## Config types and config-specific behavior

| `config_type` | Description |
|---|---|
| `cam_aqua_fv` | Aquaplanet, finite-volume dynamics |
| `cam_land_fv` | Land/continent, finite-volume |
| `cam_mixed_fv` | Mixed ocean/land, finite-volume |
| `cam_aqua_se_ne5` / `ne16` | Aquaplanet, spectral-element dynamics |

SE configs (`_ne5`, `_ne16`) strip the suffix when looking up the SourceMods template directory.

Config-type-specific behavior:
- `cam_land_fv`, `cam_mixed_fv`: parse `user_nl_clm` for `finidat`/`fsurdat`; generate sed updates for those paths.
- `cam_aqua_fv`, `cam_aqua_se_*`, `cam_mixed_fv`: parse `user_docn.streams.txt.som` for `som_pop_frc_file`; generate sed updates for SOM forcing file.

## carma_params and volc_params

Both are nested dicts in the experiment matrix spec and in the YAML registry. `_build_nl_append_block` → `_nl_append_lines` converts them to `echo "key = 'value'" >> user_nl_cam` shell lines. Value quoting rules:
- Already single- or double-quoted: emitted as-is (inner `"` escaped for surrounding `echo "..."`).
- Python floats: formatted with `%g` to preserve scientific notation.
- All other bare values: wrapped in single quotes (Fortran namelist string convention).

## Known limitations

### Branch runs not implemented (exo_build.py)

`RUN_TYPE=branch` — starting a case from a specific restart file rather than initial conditions — has not been implemented. Branch runs require setting `RUN_TYPE`, `RUN_REFCASE`, and `RUN_REFDATE` via xmlchange, and staging the restart files. Currently, restarting from a specific point must be handled manually after the build script runs.

### Custom RT packages not supported in `create_newcase` builds (exo_build.py)

`generate_shell_script` only supports radiative transfer packages referenced via `-usr_src ../ExoRT/3dmodels/*`. Cases with custom-modified RT source copied into SourceMods cannot be built via `create_newcase`. For custom RT, clone from an existing case using `clone` in the experiment matrix, which uses `create_clone` and inherits SourceMods from the source case.

### `n68equiv.haze` registered as `n68equiv` (exo_inspect.py)

Some cases were built using `ExoRT/3dmodels/src.cam.n68equiv.haze`, a special variant of n68equiv that includes CARMA haze optics. `exo_inspect.py` currently registers these as plain `n68equiv` — the `.haze` suffix in the `-usr_src` path is not distinguished. No special handling has been implemented because `n68equiv.haze` is expected to be merged into `n68equiv` in a future ExoRT update, at which point the distinction disappears.

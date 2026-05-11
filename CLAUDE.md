# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ExoCAM case management tools — Python scripts that automate building, inspecting, and managing [ExoCAM](https://github.com/storyofthewolf/ExoCAM) simulation cases on HPC systems. ExoCAM is a fork of CESM 1.2.1 for exoplanet climate modeling. The scripts cover the full simulation lifecycle: translating a YAML experiment matrix into CESM shell build scripts, scanning existing CASE directories into a queryable YAML registry, and managing disk usage across cases, run, and archive storage.

The target runtime environment is NASA Discover (SLURM HPC). Build scripts are generated locally, reviewed, then run on the cluster.

## Running the tools

```bash
# --- SETUP: building new cases ---

# Generate build scripts (preview only — no execution)
python build.py experiment_matrix.yaml --outdir scripts/

# Generate AND execute builds
python build.py experiment_matrix.yaml --outdir scripts/ --execute

# Run a single generated build script
bash scripts/my_case_build.sh

# Run all build scripts in a directory
bash run_builds.sh scripts/

# --- INSPECTION: scanning cases into a registry ---

# Scan all cases under caseroot, print only (no file written)
python scan.py

# Scan all cases under caseroot and write active.yaml
python scan.py --update

# Inspect a single case by bare name (resolved to caseroot from config_registry.yaml)
python scan.py my_case

# Inspect and merge into active.yaml
python scan.py my_case --registry active.yaml --update

# Scan long_term archive entries only, print only
python scan.py --archive

# Scan long_term archive entries and write archived.yaml
python scan.py --archive --update

# Merge live inspection + archived entries into active.yaml
python scan.py my_case --archive --update

# --- QUERYING: search and export from the registry ---

# Search — all cases, by exact name, by prefix, or by metadata filters
python query.py search
python query.py search ExoCAM_thai_ben1_L51_n68equiv
python query.py search --prefix ExoCAM_thai
python query.py search --config-type cam_land_fv --nlev 51
python query.py search --exort-pkg n68equiv

# Print all parameters for one or more cases (exact names required)
python query.py show ExoCAM_thai_ben1_L51_n68equiv
python query.py show case_a case_b

# Export one or more cases to a new experiment matrix
# (required run fields missing from config_registry.yaml defaults
#  are written as empty strings with a FIXME warning header)
python query.py export case_a case_b -o sweep.yaml \
    --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126

# Export with a clone source — produces a sparse matrix (scientific params stripped,
# inherited from clone source). Per-case name stubs written as ''  # FIXME: set new case name
python query.py export my_base_case -o clone.yaml \
    --clone --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126

# --- DISK MANAGEMENT ---

# Disk usage report across all cases (default when called with no args)
# Scans disk and saves results to usage.yaml automatically
python manage.py
python manage.py report                   # same; optional explicit subcommand
python manage.py report my_case           # scan single case, print only (no yaml write)

# Print last saved usage.yaml without touching disk
# (incompatible with explicit case names)
python manage.py report --cached

# Purge/move commands — preview by default, --execute to act
# All destructive subcommands require explicit case name(s) — no --all flag.
python manage.py purge-bld my_case --execute
python manage.py purge-bld my_case --logs-only --execute   # remove .o/.mod only
python manage.py purge-restarts my_case --keep 1 --execute
python manage.py purge-hist my_case --models atm --execute
python manage.py purge-logs my_case --execute
python manage.py move-hist my_case --models atm --execute

# Retire a case — must state intent explicitly with one of these flags:
#   --purge            write case.yaml only to long-term, then delete everything
#   --keep-config      copy SourceMods/, user_*, env_* to long-term, then delete everything
#   --keep-years N     move N most recent hist years to long-term, then delete everything
#   --keep-restarts    move most recent restart to long-term, then delete everything
# --keep-config, --keep-years, --keep-restarts are freely combinable; --purge is mutually exclusive with all
# avg files (filenames containing "avg") are always moved to long-term unconditionally
python manage.py retire my_case --purge --execute
python manage.py retire my_case --keep-config --execute
python manage.py retire my_case --keep-config --keep-years 5 --keep-restarts --execute

# --- TIME AVERAGING ---

# Inspect history file coverage per model (non-avg files only; notes avg file presence)
python manage.py avg my_case --info
python manage.py avg my_case --info --models atm lnd

# Compute time average over last N years (preview by default, --execute to run ncra)
python manage.py avg my_case --last 10
python manage.py avg my_case --last 10 --models atm lnd --execute

# --- SOURCEMODS DIFF: check for custom Fortran before retiring ---

python diff.py my_case                        # summary: MODIFIED / CASE ONLY per file (IDENTICAL suppressed)
python diff.py my_case --verbose              # show all files including IDENTICAL matches
python diff.py my_case --full physpkg.F90     # full diff for one file (or contents if CASE ONLY)
python diff.py case1 --case2 case2            # case-vs-case summary (four categories)
python diff.py case1 --case2 case2 --full physpkg.F90  # full diff between two cases
```

Dependencies: `pip install pyyaml` (required); `pip install netCDF4` (optional, for solar file nw validation)

---

## Architecture

### Data flow

```
experiment_matrix.yaml
  + config_registry.yaml
       ↓
  build.py
       ↓
  scripts/<case>_build.sh                     ← self-contained shell script: create_newcase/create_clone + build
                                               (rendered exoplanet_mod.F90 embedded as inline heredoc)

CASE directories on HPC
       ↓
  scan.py
       ↓
  active.yaml                                 ← queryable YAML registry (active cases)
  archived.yaml                               ← queryable YAML registry (retired cases)
       ↓
  query.py                                    ← search registry, export experiment matrices

cases/ + rundir/ + archive/ on HPC
       ↓
  manage.py                                   ← disk reporting, purge, move-hist, retire
```

### `parse_utils.py` — pure parsing primitives, no filesystem side effects

Used by both `build.py` and `scan.py`. Must never be given filesystem side effects.

- `parse_exoplanet_mod(path)` — reads Fortran parameter file → flat dict. Handles `real(r8)`, `integer`, `logical`, and `character` parameter declarations. Evaluates arithmetic expressions (e.g. `0.91*6.37122e6_R8`) and symbol-substitution expressions (e.g. `1.0 - exo_co2bar - exo_ch4bar`) using `_try_eval_expr()`, which substitutes already-resolved param values then evals in a restricted namespace. Unevaluable expressions fall back to `name_expr` raw string storage.
- `parse_user_nl_cam(path)` — reads CESM namelist → dict. Captures `ncdata`, IC pressure/level from filename, and any `carma_*` / `volc_*` keys as nested dicts. Handles both single- and double-quoted values. carma/volc values are coerced via `_coerce_nl_value`: Fortran logicals (`.true.`/`.false.`) → Fortran-style strings (not Python bool, for namelist round-trip correctness); numeric strings → int or float; others remain str.
- `parse_user_nl_clm(path)` — reads `user_nl_clm` → dict with `finidat` and `fsurdat`. Called for `cam_land_fv` and `cam_mixed_fv` only.
- `parse_docn_som(path)` — reads `user_docn.streams.txt.som` (XML fragment, wrapped in synthetic root for ElementTree) → dict with `som_pop_frc_file`. Called for aqua and mixed configs only.
- `parse_cam_config_opts(xmlpath)` — reads `env_build.xml` (falls back to `env_run.xml` if absent) for `-nlev`, `-usr_src` (exort_pkg), cloud scheme.
- `compute_pstd_bar(params)` — derives total surface pressure from gas bar values. Returns `(pstd_bar, n2bar_computed)` tuple. N2 is implicit for ≤1 bar atmospheres (fills to 1.0); for higher pressures, expects an explicit float `exo_n2bar` in params.
- `pressure_str_to_bar(s)` — converts pressure strings like `'1bar'` → `1.0`, `'0.1bar'` → `0.1`. Used by `check_consistency` to compare computed pstd against IC file pressure.
- `parse_run_type_fields(xmlpath)` — reads `env_run.xml` for `RUN_TYPE`, `RUN_REFCASE`, `RUN_REFDATE`, and `BRNCH_RETAIN_CASENAME`. Returns dict with lowercase keys; `run_type` defaults to `'startup'`, others to `None`. `brnch_retain_casename` stored as lowercase string (`'true'`/`'false'`). Falls back to line scan if ElementTree parse fails.
- `read_solar_nw(path)` — reads `nw` dimension from a NetCDF solar file using `netCDF4`. Returns `None` if the library is absent or the file is inaccessible. Used for solar file / exort_pkg consistency checking in `scan.py`.

### `build.py` — validation and shell script generation

- `resolve_case(base, overrides)` — merges base + per-case dict.
- `validate_case(spec, registry)` — returns list of error strings; checks required fields, IC file availability, solar/exort consistency, synchronous rotation math. Clone cases (`clone` present) use `REQUIRED_FIELDS_CLONE` (relaxed — config fields are inherited from source case). Branch/hybrid cases must supply `run_refcase` and `run_refdate`. `exort_pkg` ending in `'*'` is an error in newcase mode (custom RT cannot be replicated by `create_newcase`) but is allowed in clone mode (RT is inherited from the clone source).
- `render_exoplanet_mod(template_path, spec)` — regex-patches active Fortran parameter lines for all names in `EXO_PARAMS`. When `exo_n2bar_explicit` is set, also patches the `exo_n2bar` line with the explicit numeric value (high-pressure cases); otherwise leaves the N2 expression line unchanged for the Fortran compiler to evaluate.
- `_heredoc_exoplanet_mod(exoplanet_mod_content)` — returns shell lines that write `exoplanet_mod.F90` inline via a quoted heredoc (`<< 'EXOPLANET_MOD_EOF'`). If `exoplanet_mod_content` is `None` (template not found), emits comment lines directing manual installation instead. Quoted delimiter suppresses bash variable expansion inside the Fortran content.
- `generate_shell_script(...)` — writes the `create_newcase` + `cesm_setup` + build script. Each script is fully self-contained: the rendered `exoplanet_mod.F90` is embedded inline via heredoc (no staging directory). Script declares `RUNDIR`, `ARCHIVE`, and `LONG_TERM` shell variables from registry paths. Config-specific shell commands emitted:
  - All configs: `sed` to update `ncdata` in `user_nl_cam`; `echo >>` for `carma_params`/`volc_params`.
  - All configs: `sed` to patch `#SBATCH --account` and `-J` in `${CASE}.run` after `cesm_setup` (if `account`/`job_name` present in spec).
  - Land/mixed: `sed` for `finidat`/`fsurdat` in `user_nl_clm`.
  - Aqua/mixed: `sed` for `pop_frc*` path in `user_docn.streams.txt.som`.
  - Branch/hybrid: see below.
- `generate_clone_script(...)` — same as above but uses `create_clone -clone $CLONE_OF -case $CASE` for Step 1, skips the SourceMods/namelist copy step (Step 2 of newcase), and makes IC file lookup conditional on `ncdata` being explicitly set in the spec. `exoplanet_mod.F90` is also embedded inline via heredoc.
- `_build_branch_pre_setup(spec)` — if `run_type` is `branch` or `hybrid`, emits `./xmlchange RUN_TYPE=startup` before `cesm_setup`. CESM requires `RUN_TYPE=startup` during `cesm_setup` when the rundir does not yet exist; the actual run type is applied afterward.
- `_build_branch_post_setup(spec, paths)` — if `run_type` is `branch` or `hybrid`, emits: copy restart files from `$RUN_REFDIR` to rundir, then `xmlchange` calls to set `RUN_TYPE`, `CONTINUE_RUN=FALSE`, `BRNCH_RETAIN_CASENAME`, `RUN_REFCASE`, and `RUN_REFDATE`. `RUN_REFDIR` is constructed as `${ARCHIVE}/${RUN_REFCASE}/rest/${RUN_REFDATE}-00000` (with `-00000` appended because CESM restart directories are named `YYYY-MM-DD-SSSSS` but `RUN_REFDATE` contains only `YYYY-MM-DD`).
- `_branch_var_block(spec)` — emits shell variable declarations for `RUN_REFCASE`, `RUN_REFDATE`, and `RUN_REFDIR` at the top of branch/hybrid scripts. `RUN_REFDIR` always appends `-00000` to `RUN_REFDATE` since CESM restart seconds are always zero.
- `_build_nl_append_block(spec)` — `echo >>` lines for carma/volc namelist params.
- `_format_nl_value(val)` — formats a Python value as a CESM namelist RHS. Type dispatch: `bool` → `.true.`/`.false.`; `int` → bare integer; `float` → `%g` with decimal ensured; `str` Fortran logical → pass through; `str` numeric → coerce; `str` other → single-quoted. Used by `_nl_append_lines`.
- `_build_clm_update_block(spec, paths)` — `sed` lines for CLM land files.
- `_build_docn_update_block(spec)` — `sed` lines for SOM ocean forcing file.
- `_build_run_script_block(spec)` — `sed` lines to patch SBATCH directives into `${CASE}.run`.
- `EXO_PARAMS` — set of parameter names that map directly to `exoplanet_mod.F90` and can be patched from the experiment matrix. Includes gas bars, geophysical parameters, logical flags, and RT tuning parameters (`Tmax`, `swFluxLimit`, `lwFluxLimit`, `exo_albdif`, `exo_albdir`, `exo_mvelp`, `exo_ve`).
- `REQUIRED_FIELDS` — required for newcase mode: `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`.
- `REQUIRED_FIELDS_CLONE` — required for clone mode: `clone`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`.

### `scan.py` — CASE directory scanner → YAML registry

Walks CASE directories (identified by `SourceMods/src.share/exoplanet_mod.F90`), extracts scientific metadata, writes a grouped YAML registry. Bare case names are resolved relative to `caseroot` from `config_registry.yaml`.

- `inspect_case(casedir)` — collects all metadata into a flat row dict. Reads `exoplanet_mod.F90`, `user_nl_cam`, optionally `user_nl_clm` and `user_docn.streams.txt.som`, `env_build.xml` (falling back to `env_run.xml` for CAM_CONFIG_OPTS), and `env_run.xml` (for run type fields). `config_type` is inferred from `SourceMods/` subdirectory structure before the config-conditional parse calls.
- `_infer_config_type(casedir)` — decision tree based on SourceMods subdirectory presence:
  - `src.cice` + `src.clm` present → `cam_mixed_fv`
  - `src.cice` only → `cam_aqua_fv`
  - `src.clm` only → `cam_land_fv`
  - neither → `unknown`
  - (Note: `src.docn` is also checked but not currently used in the decision logic)
- `check_consistency(meta)` — generates warnings for: (1) pressure mismatch >5% between computed pstd and ncdata pressure string; (2) level count mismatch between ncdata filename and `-nlev`; (3) solar file / exort_pkg mismatch, preferring direct NetCDF `nw` dimension read; falls back to stem substring check for standard solar filenames, silently skips custom stellar spectra (BT-Settl etc.) that lack the standard stem.
- `find_case_dirs(path)` — returns list of case directories under a path. A directory qualifies if it contains `SourceMods/src.share/exoplanet_mod.F90`. If the path itself qualifies, returns `[path]`; otherwise scans one level of children.
- `_rows_to_ordered(rows)` — converts flat row dicts to the grouped YAML structure defined by `_REGISTRY_GROUPS`.
- `write_registry(rows, path)` — writes grouped YAML via `_rows_to_ordered`, prepending a `# Auto-generated cache` comment header.
- `load_registry(path)` — reads grouped YAML and flattens groups back to plain dicts for internal use.
- `scan_archive_entries(long_term_path)` — walks `long_term/` for subdirectories containing `case.yaml`; reads each as a pre-captured registry entry without touching any Fortran or namelist files. Returns flat row dicts. Handles both full registry-format entries (`{'cases': [...]}`) and minimal stubs (`{'case_name': ..., 'retired_date': ...}`). Sets `config_saved` (bool) on every row by checking whether `SourceMods/` exists in the case's long-term directory.
- `--archive` flag — when passed, calls `scan_archive_entries` using `long_term` from `config_registry.yaml`. May be used alone (no live case paths) or combined with live paths and `--update`; live inspection always takes precedence over archived entries on name collision; archived entries take precedence over existing registry entries.
- `_REGISTRY_GROUPS` — list of `(group_name, [field_names])` tuples defining group names and field ordering for YAML output.
- `SOLAR_NW_MAP` — expected `nw` dimension per `exort_pkg`: `{n68equiv: 68, n84equiv: 84, n28archean: 28, n42h2o: 42}`.
- `SOLAR_STEM_MAP` — expected solar filename stem per `exort_pkg`: `{n68equiv: 'n68', n84equiv: 'n84', n28archean: 'n28', n42h2o: 'n42'}`.

### `query.py` — registry search and experiment matrix export

- `load_registry(path)` — loads `active.yaml` (or `archived.yaml`) into flat dicts (one per case) for search/export.
- `load_registry_raw(path)` — loads the registry preserving grouped structure; used by `show` to reproduce the exact registry format.
- `cmd_search` — tabular listing filtered by optional positional `cases` (exact names), `--prefix` (case-insensitive startswith), `--config-type` (exact), `--exort-pkg` (exact), `--nlev` (exact integer). `cases` and `--prefix` are mutually exclusive. Columns: CASE, CONFIG_TYPE, EXORT_PKG, NLEV, INSPECT_DATE. A CONFIG column is appended (showing `yes` or `-`) when at least one result row contains `config_saved` — present when searching `archived.yaml`, absent when searching `active.yaml`.
- `cmd_show` — dumps full grouped YAML for one or more cases by exact name (one or more required). If any name is not found in the registry, prints `ERROR: case '<name>' not found in registry.` for each missing name and exits. Multiple results are separated by `---`.
- `cmd_export` — generates a ready-to-use `experiment_matrix.yaml` from one or more registry cases. For multiple cases, shared fields are factored into `base` automatically. `mach` and run defaults are populated from `config_registry.yaml` unless overridden via CLI flags. Required fields left blank are written as empty strings with a prominent `# FIXME` warning header prepended to the file.
- `_row_to_base(row)` — converts a flat registry row to a matrix base dict.
- `_CLONE_BASE_FIELDS` — allowlist of fields included in clone-mode base: `clone`, `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`, `account`, `run_type`, `run_refcase`, `run_refdate`, `brnch_retain_casename`. All other fields are omitted (inherited from clone source).
- Clone export behavior: `--clone` (boolean `store_true`) produces a sparse base filtered to `_CLONE_BASE_FIELDS`. Per-case name stubs are written as `''  # FIXME: set new case name`. Without `--clone`, full output is always produced.
- `exort_pkg '*'` warning: if any matched row has `exort_pkg` ending in `'*'` (custom RT copied into SourceMods), a warning is printed to stderr after the matrix output so it is visible at the end. Warning is suppressed in `--clone` mode since RT is inherited from the clone source.
- Key renames from registry to matrix: `clm_finidat` → `finidat`, `clm_fsurdat` → `fsurdat`.
- Registry-only fields stripped from matrix output: `case_name`, `casedir`, `inspect_date`, `ncdata_pressure_str`, `ncdata_levels`, `exo_n2bar`, `exo_n2bar_expr`, `exo_sday_expr`, `exo_pstd_computed_bar`, `warnings`, `config_saved`.
- The exported matrix always includes a `meta` block (`description`, `author`, `created`, `source_registry`) that `query.py export` auto-populates; `description` and `author` are written as empty strings for the user to fill in.

### `manage.py` — disk management tool

Discovers cases by scanning `caseroot`, `rundir`, and `archive` directories on disk — no registry required. All destructive subcommands are **non-destructive by default**; `--execute` is required to make changes, and each case confirms the action before acting.

- `discover_cases(paths)` — union of folder names across caseroot, rundir, and archive.
- `case_sizes(case, paths)` — returns per-area byte counts: `casedir`, `bld`, `run`, `hist`, `logs`, `rest`, `archive_total`.
- `restart_sets(case, paths)` — returns sorted list of `(date_str, path)` for dated subdirs in `archive/<case>/rest/`.
- `list_files_with_size(directory)` — returns `(filenames, total_bytes)` for files directly in a directory (subdirectories ignored); used by hist/logs/move operations.
- `_hist_keep_years_filter(archive_path, models, keep_n)` — partitions hist files into keep/delete lists based on the most-recent N model years; shared by `purge-hist` and `retire`.
- `save_usage_yaml(path, cases_data, generated_ts)` — clobber-writes `usage.yaml` with the full snapshot; does not merge with any existing content.
- `load_usage_yaml(path)` — loads `usage.yaml`; exits with an error if the file is missing.
- `cmd_report` — prints aligned disk usage table: CASEDIR, BLD, RUN, HIST, LOGS, REST, TOTAL. Bare invocation scans all cases, prints the table, and clobbers `usage.yaml` with the complete snapshot. Named-case invocation (`report my_case`) and `--prefix` filtered invocations are diagnostic only — they print but do not write to `usage.yaml`. `--cached` loads `usage.yaml` and prints without scanning disk; incompatible with explicit case names. `--prefix STR` filters cases by case-insensitive prefix match.
- `cmd_purge_bld` — deletes `rundir/<case>/bld/`. `--logs-only` removes only `.o`/`.mod` object files, preserving the rest of the bld directory.
- `cmd_purge_restarts` — trims old restart sets keeping the N most recent (`--keep N`, default 1).
- `cmd_purge_hist` — deletes `archive/<case>/<model>/hist/` contents. `--models` restricts components. Requires `--keep-years N` or `--models` to prevent accidental total deletion.
- `cmd_purge_logs` — deletes log files from both `archive/<case>/<model>/logs/` and `$CASE/logs/`. `--no-archive-logs`/`--no-case-logs` skip one side. `--models` restricts archive-side components.
- `cmd_move_hist` — moves hist files to `long_term/<case>/<model>/hist/`, leaving source dir empty. Uses `shutil.move` — no intermediate copy, peak disk usage stays flat.
- `cmd_retire_case` — retires a case from cesm_scratch (`retire` subcommand). Requires at least one intent flag: `--purge` (write `case.yaml` to long-term only, then delete everything), `--keep-config` (copy SourceMods/, user_*, env_* to long-term, then delete everything), `--keep-years N` (move N most recent hist years to long-term, then delete), or `--keep-restarts` (move most recent restart to long-term, then delete). `--keep-config`, `--keep-years`, and `--keep-restarts` are freely combinable; `--purge` is mutually exclusive with all three. Avg files (filenames containing `"avg"`) in any `archive/<case>/<model>/hist/` are always moved to long-term unconditionally. `case.yaml` is always written from `--registry` (default: `active.yaml`); falls back to a minimal stub if the case is not found in the registry. `--prefix STR` matches cases by case-insensitive prefix and shows a single batch confirmation instead of per-case prompts.
- `cmd_avg_hist` — `avg` subcommand. Inspects or computes time-averaged history files. `--info` prints file count, year span, and total size per model (non-avg files only; avg-file presence noted), followed by a `rest:` row showing restart folder count and total size from `restart_sets()`. `--last N` selects the N most recent model years via `_hist_keep_years_filter`, excludes avg files from inputs, and runs `ncra` to produce `<case>.<stem>.h0.avg_last{N}yr.nc` in the same hist directory. Default models: `atm`, `lnd`, `ice`. `--prefix STR` selects cases by prefix. `--execute` required to actually run ncra.
- `_require_cases(all_cases, args)` — validates that explicit case names were provided; exits with an error if none given. No `--all` flag — bulk operations must list cases explicitly.
- `ARCHIVE_MODELS` — `['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']`.
- `HIST_MODELS` — `ARCHIVE_MODELS` minus `'rest'`; the components with `hist/` and `logs/` subdirs.

### `diff.py` — SourceMods and namelist diff tool

Compares a case's `SourceMods/` and namelist files against either the ExoCAM reference source or another case. ExoCAM reference paths: `{exocam_root}/cesm1.2.1/configs/{config_type}/SourceMods/` for Fortran files; `{exocam_root}/cesm1.2.1/configs/{config_type}/namelist_files/` for namelists. `config_type` is looked up from `active.yaml`. RT files are detected by matching against the ExoRT package directory and reported as separate categories. Used before retiring to determine whether custom Fortran or namelists are worth preserving.

- `load_case_meta(case, cases_yaml_path)` — reads `active.yaml`, matches on `meta.case_name`, returns `{'config_type': ..., 'exort_pkg': ...}` with the `*` suffix stripped from `exort_pkg`. Exits with a clear error if `active.yaml` is missing (directs user to run `scan.py`).
- `build_exort_fileset(exort_root, exort_pkg)` — returns `{filename: filepath}` for all files in `exort_root/3dmodels/src.cam.{exort_pkg}/`. Returns empty dict if directory does not exist.
- `_load_exort_fileset(paths, exort_pkg)` — wraps `build_exort_fileset` with three warning paths: `exort_root` not configured, `exort_pkg` missing from active.yaml, or package directory not on disk. Returns `{}` (RT detection disabled) in all three cases.
- `walk_sourcemods(sourcemods_root)` — walks each component directory recursively; returns `{component: {filename: abs_path}}`. Skips editor backup files (`~`). Shallowest occurrence wins on filename collision across subdirs.
- `walk_namelists(casedir)` — returns `{filename: abs_path}` for whichever files in `NAMELIST_FILES` exist directly in the case directory.
- `_print_namelist_section(nl1, nl2, label1, label2, verbose)` — prints the namelist diff block for case-vs-case mode; returns `(n_mod, n_c1, n_c2, n_eq)` counts folded into the overall summary.
- `find_exocam_counterpart(filename, component, exocam_sm_root)` — checks for filename at the top level of the ExoCAM reference component dir; returns path or `None`. Only used in case-vs-ExoCAM mode.
- `diff_counts(path_a, path_b)` — returns `(added, removed)` line counts of a vs b using `collections.Counter`. Pure Python, no subprocess.
- `cmd_summary(args, paths)` — branches on `args.case2`. Case-vs-ExoCAM: five categories for SourceMods (`IDENTICAL`, `MODIFIED`, `RT IDENTICAL`, `RT MODIFIED`, `CASE ONLY`); ExoCAM match takes priority over RT match. Namelists use three categories (`IDENTICAL`, `MODIFIED`, `CASE ONLY`) diffed against `namelist_files/` reference. Case-vs-case: four categories (`IDENTICAL`, `MODIFIED`, `CASE1 ONLY`, `CASE2 ONLY`) for both SourceMods and namelists; no `active.yaml` or ExoRT lookup. Always prints all component sections then a `namelists` section. `exoplanet_mod.F90` always skipped. Ends with a one-line verdict and `(registry: ...)` footer. By default, `IDENTICAL` and `RT IDENTICAL` lines are suppressed; pass `--verbose` to show them.
- `cmd_full(args, paths)` — branches on `args.case2`. Resolves `--full` target against SourceMods first, then namelists. In case-vs-ExoCAM mode, namelist targets diff against `namelist_files/` reference or print contents if `CASE ONLY`; SourceMods targets resolve classification (ExoCAM → RT → CASE ONLY) and diff against the appropriate reference. In case-vs-case mode, diffs the two files or prints the one-sided file.
- `COMPONENTS` — `['src.cam', 'src.share', 'src.drv', 'src.clm', 'src.cice']`; printed in this order.
- `SKIP_FILES` — `{'exoplanet_mod.F90'}`; always skipped.
- `NAMELIST_FILES` — `['user_nl_cam', 'user_nl_clm', 'user_nl_cice', 'user_docn.streams.txt.som', 'user_nl_cpl', 'user_nl_docn', 'user_nl_rtm']`; checked in the case directory root.

### `config_registry.yaml` — machine-specific, must be edited per user

Holds:
- `machine` — CESM machine name (e.g. `discover`); read by `query.py export` to populate `mach` automatically.
- `defaults` — default run parameters applied when a matrix field is absent: `resubmit`, `stop_option`, `stop_n`, `rest_n`, `ntasks`, `account`. Read by both `build.py` (fills base before case resolution) and `query.py export` (fills exported matrix fields). CLI flags on `query.py export` override these; per-case or per-matrix values override them in `build.py`.
- `paths` — `cesm_scripts`, `caseroot`, `rundir`, `archive`, `long_term`, `exocam_root`, `exort_root`.
- `cesm_config` — `compset`, `res`, `phys` per `config_type`; used in `create_newcase`.
- `ic_files` — IC file lookup table keyed by `config_type → pressure_str → nlev`. Filename only — the full path is prepended as `{exocam_root}/cesm1.2.1/initial_files/<config_type>/`.
- `solar_file_stems` — filename stem expected per `exort_pkg`; fallback for when NetCDF read is unavailable.

### `run_builds.sh` — batch runner

Loops over all `*_build.sh` files in a directory, runs each with `bash`, reports pass/fail per case, and prints a summary. A failed build does not abort remaining cases.

---

## YAML registry structure

`_REGISTRY_GROUPS` is a list of `(group_name, [fields])` tuples in `scan.py`; `write_registry` emits one block per group, omitting empty fields.

```yaml
cases:
- meta:          # case identity, CESM config, IC file info, CLM files, SOM file, run_type fields
  atmosphere:    # gas bars, pstd, scon, solar file
  geophysical:   # ndays, porb, sday, gravity, radius, eccen, obliq
  model_options: # do_exo_* flags, exo_convect_plim, exo_rad_step, rt flags
  special:       # carma_params, volc_params (nested dicts; omitted if absent)
  diagnostics:   # warnings list (omitted if no warnings)
```

To add a new inspected field:
1. Add its key to the appropriate group's field list in `scan._REGISTRY_GROUPS`.
2. Add collection logic to `inspect_case()` in `scan.py` (or add a new parse call if from a new source file).
3. If the field should appear in exported matrices, also add it to `_BASE_FIELD_ORDER` in `query.py`.

---

## Experiment matrix format

Copy `experiment_matrix.yaml.example`, set `config_registry` path, fill `base` defaults, add per-case overrides under `cases`. Each case inherits all base values; any key in a case dict overrides the base.

Key matrix-level keys:
- `config_registry` — required path to `config_registry.yaml`
- `meta` — optional block with `description`, `author`, `created`, `source_registry`; auto-populated by `query.py export`
- `paths` — optional overrides of machine paths from the registry
- `base` — shared defaults for all cases
- `cases` — list of case dicts, each with a required `name` key

Special case keys:
- `clone` — triggers clone mode (`create_clone`) instead of `create_newcase`. Typically set in `base` so all cases share the same clone source. The `exoplanet_mod.F90` template is taken from the clone source's SourceMods; only parameters explicitly listed in the matrix are patched.
- `ncdata` — bypasses automatic IC file lookup
- `exo_n2bar_explicit` — required for non-1-bar atmospheres; sets N2 directly and patches `exo_n2bar` in Fortran
- `account` — `#SBATCH --account` written to `${CASE}.run` (typically in `base`)
- `job_name` — `#SBATCH -J` written to `${CASE}.run` (typically per-case)
- `carma_params`, `volc_params` — nested dicts appended to `user_nl_cam` via `echo >>`
- `run_type` — CESM run type: `startup` (default), `branch`, or `hybrid`. Branch/hybrid cases require `run_refcase` and `run_refdate`. The generated script handles the cesm_setup workaround automatically (sets `startup` before setup, switches back to `branch`/`hybrid` after copying restart files).
- `run_refcase` — name of the reference case to branch/hybridize from. Required when `run_type` is `branch` or `hybrid`.
- `run_refdate` — reference date string (e.g. `0021-01-01`) matching the restart set to use. Required when `run_type` is `branch` or `hybrid`.
- `brnch_retain_casename` — `'true'` or `'false'`; passed to `BRNCH_RETAIN_CASENAME` xmlchange in branch/hybrid scripts. Defaults to `'false'`.

---

## Pressure representation

Total surface pressure is computed from the sum of individual gas bar values. N2 is implicit for ≤1 bar atmospheres (fills to 1.0). For higher pressures, `exo_n2bar_explicit` must be set. When set, `render_exoplanet_mod` patches the `exo_n2bar` Fortran line with the explicit numeric value. For standard cases, the N2 expression line is left unchanged and evaluated by the Fortran compiler at compile time.

Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are used as keys in the IC file table and must exactly match substrings in IC filenames.

---

## Fortran patching

`render_exoplanet_mod` matches active parameter lines via `_RE_PARAM_LINE` (real/integer/logical with `parameter ::`). Skips commented lines. Values formatted with `_r8` kind suffix for Fortran reals; logicals become `.true.`/`.false.`.

`parse_exoplanet_mod` uses `_try_eval_expr`:
- Strips Fortran kind suffixes (`_r8`, `_R8`)
- Substitutes already-resolved numeric params by name, longest-name-first to avoid partial matches
- Only calls `eval()` if result matches `_RE_SAFE_EXPR` (pure arithmetic: digits, operators, parens)
- Runs `eval` with `__builtins__: {}` to restrict the namespace
- Falls back to `name_expr` raw string storage on any failure

---

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

---

## carma_params and volc_params

Both are nested dicts in the experiment matrix spec and in the YAML registry. `_build_nl_append_block` → `_nl_append_lines` → `_format_nl_value` converts them to `echo "key = val" >> user_nl_cam` shell lines. Type dispatch rules (bool checked before int, since bool is a subclass of int in Python):
- `bool` → `.true.` / `.false.` (unquoted Fortran logical)
- `int` → bare integer string (unquoted)
- `float` → `%g` notation with decimal point ensured (unquoted)
- `str` matching `.true.`/`.false.` → passed through unquoted
- `str` that parses as a number → coerced to float and formatted unquoted
- `str` other (file paths etc.) → single-quoted

---

## Common modification patterns

### Adding a new registry field
1. Add the key to the appropriate group's field list in `scan._REGISTRY_GROUPS`.
2. Add collection logic in `inspect_case()` in `scan.py` (or a new parse call for a new source file).
3. If the field should appear in exported matrices, add it to `_BASE_FIELD_ORDER` in `query.py`.

### Adding a new config_type
1. Add an entry to `config_registry.yaml` under `cesm_config` with `res`, `compset`, `phys`.
2. Add IC file entries under `ic_files` in `config_registry.yaml`.
3. Verify `_infer_config_type()` in `scan.py` will correctly assign the new type from SourceMods directory structure.
4. Verify the config-conditional blocks in `build.py` (`_build_clm_update_block`, `_build_docn_update_block`) and `scan.py` (`inspect_case` clm/docn blocks) cover the new type correctly.

### Adding a new EXO_PARAMS parameter
1. Add the parameter name to the `EXO_PARAMS` set in `build.py`.
2. Ensure the corresponding `parameter ::` declaration exists in the `exoplanet_mod.F90` template.
3. If it should be scanned into the registry, add it to `inspect_case()` in `scan.py` and to the appropriate `_REGISTRY_GROUPS` entry.

### Extending `query.py export` output fields
1. Add the registry key to `_BASE_FIELD_ORDER` in `query.py` (controls output key order).
2. If it should be included in clone-mode sparse exports, add it to `_CLONE_BASE_FIELDS`.
3. If the registry uses a different key name than the matrix, add a rename entry to `_KEY_RENAMES`.

---

## Design invariants — do not violate

- `parse_utils.py` must remain free of filesystem side effects. It reads files via paths passed to it; it never discovers or writes files itself.
- All destructive `manage.py` operations require `--execute`. Without it, every command only prints what it would do.
- No `--all` flag exists for destructive operations. Cases must be named explicitly.
- `build.py` generates scripts but never executes them unless `--execute` is passed.
- `scan.py --update` clobbers the registry with exactly the cases scanned in the current run (live rows + archive rows, live takes precedence on name collision). It does not merge with any pre-existing registry content.
- `exoplanet_mod.F90` is always skipped by `diff.py` (it is patched per-case and is not meaningful to diff).

---

## Known limitations

### Pre-existing registry rows lack run_type (scan.py)

Cases that were scanned into `active.yaml` or `archived.yaml` before `run_type` support was added will not have `run_type`, `run_refcase`, `run_refdate`, or `brnch_retain_casename` fields. When `query.py export` encounters such rows, it defaults `run_type` to `'startup'` for backward compatibility. Re-scan the case with `scan.py` to populate the run type fields from the live `env_run.xml`.

### Custom RT packages not supported in `create_newcase` builds (build.py)

`generate_shell_script` only supports radiative transfer packages referenced via `-usr_src ../ExoRT/3dmodels/*`. Cases with custom-modified RT source copied into SourceMods cannot be built via `create_newcase`. For custom RT, clone from an existing case using `clone` in the experiment matrix, which uses `create_clone` and inherits SourceMods from the source case.

### `n68equiv.haze` registered as `n68equiv` (scan.py)

Some cases were built using `ExoRT/3dmodels/src.cam.n68equiv.haze`, a special variant of n68equiv that includes CARMA haze optics. `scan.py` currently registers these as plain `n68equiv` — the `.haze` suffix in the `-usr_src` path is not distinguished. No special handling has been implemented because `n68equiv.haze` is expected to be merged into `n68equiv` in a future ExoRT update.

### diff.py: non-standard ExoRT package directory paths

`build_exort_fileset` constructs the ExoRT reference directory as:
  `{exort_root}/3dmodels/src.cam.{exort_pkg}/`

Experimental or non-standard ExoRT branches may live outside this path
(e.g. `source/experimental/src.n68equiv_exp/shr/`). When this occurs,
`build_exort_fileset` returns an empty dict, RT file detection is silently
disabled, and affected files appear as `CASE ONLY` in diff.py output.

Cases with embedded non-standard RT are already flagged with `*` in
`query.py search` output (e.g. `n68equiv_exp*`). If RT file detection
fails unexpectedly, verify that `exort_pkg` (stripped of `*`) maps to a
valid directory under `{exort_root}/3dmodels/`.

Future fix: add `paths.exort_pkg_dirs` map to `config_registry.yaml` to
support non-standard package directory paths without code changes.

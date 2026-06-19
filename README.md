# exocam-casemgr

Case management tools for [ExoCAM](https://github.com/storyofthewolf/ExoCAM) — an exoplanet climate model based on CESM 1.2.1. These scripts cover the full lifecycle of ExoCAM simulations: translating a YAML experiment matrix into ready-to-run CESM build scripts, scanning existing CASE directories into a queryable YAML registry, and managing disk space across the cases, run, and archive storage areas.

## Requirements

```bash
pip install pyyaml
```

Optional (for solar file `nw` validation in `scan.py`):
```bash
pip install netCDF4
```

Python 3.8+.

## Files

| File | Purpose |
|---|---|
| `build.py` | Build script generator — validation, Fortran patching, shell script writer |
| `scan.py` | CASE directory scanner → YAML registry |
| `parse_utils.py` | Parsing primitives shared by build and inspect (no side effects) |
| `datamgr.py` | Data management — disk usage reporting and case retirement |
| `manage_utils.py` | Shared utility layer for `datamgr.py` and `runmgr.py` — constants, disk helpers, case selection |
| `runmgr.py` | Run and case lifecycle management — check SLURM status, cata subcommands for purge and move operations |
| `query.py` | Registry search and experiment matrix export |
| `diff.py` | SourceMods diff tool — compare case Fortran against ExoCAM reference source |
| `config_registry.yaml` | Machine paths, CESM compset/res per config type, IC file table |
| `blueprints/experiment_matrix.example.yaml` | Annotated template for writing experiment matrices |

---

## Workflow

### 1. Configure `config_registry.yaml`

Edit `config_registry.yaml` for your machine — set HPC paths and verify the IC file table matches what you have on disk.

```yaml
paths:
  cesm_scripts: /path/to/cesm1_2_1/scripts
  caseroot:     /path/to/scratch/cases
  rundir:       /path/to/scratch/rundir
  archive:      /path/to/scratch/archive
  long_term:    /path/to/long_term_storage
  exocam_root:  /path/to/ExoCAM
  exort_root:   /path/to/ExoRT
```

### 2. Write an experiment matrix

Copy the example and edit:

```bash
cp blueprints/experiment_matrix.example.yaml blueprints/my_runs.yaml
```

The matrix has a `base` section (shared defaults) and a `cases` list. Each case inherits all base values and can override any of them. See `blueprints/experiment_matrix.example.yaml` for the full annotated parameter set, organized into these groups:

| Group | Keys |
|---|---|
| CESM config | `config_type`, `exort_pkg`, `cloud_scheme`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks` |
| HPC batch | `account` (SLURM charge account), `job_name` (short queue label, per-case) |
| Run type | `run_type` (`startup`/`branch`/`hybrid`), `run_refcase`, `run_refdate`, `brnch_retain_casename` |
| Atmospheric composition | `exo_co2bar`, `exo_ch4bar`, `exo_h2bar`, `exo_o2bar`, `exo_c2h6bar`, `exo_nh3bar`, `exo_cobar`, `exo_n2bar_explicit` |
| Stellar forcing | `exo_scon`, `exo_solar_file` |
| Geophysical | `exo_surface_gravity`, `exo_planet_radius`, `exo_ndays`, `exo_porb`, `exo_sday`, `exo_eccen`, `exo_obliq` |
| Model options | `do_exo_atmconst`, `do_exo_rt`, `do_exo_synchronous`, `do_exo_gw`, `do_exo_simplevolc` |
| Radiation options | `exo_convect_plim`, `exo_rad_step`, `do_exo_rt_clearsky`, `do_exo_rt_spectral`, `do_exo_rt_carma` |
| Ocean/SOM forcing | `som_pop_frc_file` (aqua/mixed configs only) |
| CLM land files | `finidat`, `fsurdat` (land/mixed configs only) |
| CARMA aerosols | `carma_params` dict (if `do_exo_rt_carma: true`) |
| Volcanic forcing | `volc_params` dict (if `do_exo_simplevolc: true`) |

### 3. Generate build scripts

```bash
python build.py generate my_runs.yaml
```

Scripts are written to `build_scripts/` by default. Use `--scripts-dir` to change the output directory:

```bash
python build.py --scripts-dir scripts/ generate my_runs.yaml
```

This validates every case and writes one self-contained script per case:
- `build_scripts/<case>_build.sh` — complete CESM `create_newcase` + `cesm_setup` + build script with the rendered `exoplanet_mod.F90` embedded inline as a heredoc (no staging directory needed)

The build script handles all config-specific file path updates:
- All configs: `user_nl_cam` (ncdata via `sed`; carma/volc/nl_cam params written via append or upsert)
- All configs: `${CASE}.run` SBATCH directives (`--account`, `-J`) patched after `cesm_setup`
- Land/mixed: `user_nl_clm` (finidat, fsurdat)
- Aqua/mixed: `user_docn.streams.txt.som` (pop_frc file path and name)

**Review the generated scripts before running.** To list available blueprint matrices:

```bash
python build.py generate --list
```

To run all generated scripts (prompts for confirmation; logs written to `build_scripts/logs/`):

```bash
python build.py make                                  # build only — inspect, then submit later
python build.py make --prefix ExoCAM_thai             # filter by case name prefix
python build.py make --send-it                        # build then submit each passed case via sbatch
python build.py make --prefix ExoCAM_thai --send-it   # filter + build + submit
```

`make` **builds but does not submit** — review the cases, then launch them with
`runmgr.py submit` (the standalone launch step):

```bash
python runmgr.py submit my_case                       # preview
python runmgr.py submit --prefix ExoCAM_thai          # preview a whole set
python runmgr.py submit my_case --execute             # sbatch the built case as-is
```

`make --send-it` is the power-user shortcut that folds build + submit into one
step. Either way, `submit` makes no `xmlchange` calls — it runs exactly what you
built. Use `runmgr.py continue` / `restart` instead to manage a case that has
already run.

To run a single build script directly:

```bash
bash build_scripts/my_case_build.sh
```

#### Clone mode

When branching from a completed case, put `clone` in `base` so all cases share the same source:

```yaml
base:
  clone: my_completed_base_case
  stop_option: nyears
  stop_n: 20
  rest_n: 5
  resubmit: 4
  ntasks: 126

cases:
  - name: my_case_2x_co2
    exo_co2bar: 0.0008
  - name: my_case_4x_co2
    exo_co2bar: 0.0016
```

The generated script uses `create_clone`, inheriting SourceMods, namelists, and all CESM configuration from the source case. The `exoplanet_mod.F90` template is taken from the clone source's SourceMods rather than the repo default, so any custom parameter baselines are preserved. Only the parameters explicitly listed in the matrix are patched. `config_type`, `exort_pkg`, and `nlev` are optional — supply them to enable IC file lookup and `CAM_CONFIG_OPTS` update; omit them to inherit everything from the source.

#### Generating a clone matrix with query.py

```bash
# Sparse export — only run config in base, stubs in cases (scientific params inherited from clone source)
python query.py export my_base_case -o sweep.yaml \
    --clone --stop-option nyears --stop-n 20 --rest-n 5 \
    --resubmit 4 --ntasks 126 --account s2427
```

Then add per-case entries with only the parameters that differ from the clone source.

### 4. Search the registry and export experiment matrices

```bash
# List all cases
python query.py search

# Filter by exact name, prefix, or metadata
python query.py search ExoCAM_thai_ben1_L51_n68equiv
python query.py search --prefix ExoCAM_thai
python query.py search --config-type cam_land_fv --nlev 51
python query.py search --exort-pkg n68equiv

# Show all parameters for one or more cases (exact names required)
python query.py show ExoCAM_thai_ben1_L51_n68equiv
python query.py show case_a case_b

# Export a full matrix from one or more registry cases
python query.py export case_a case_b -o sweep.yaml \
    --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126 --account s2427

# Export a sparse clone matrix (minimal base, stubs per case)
python query.py export my_base_case -o clone_sweep.yaml \
    --clone --stop-option nyears --stop-n 20 --rest-n 5 \
    --resubmit 4 --ntasks 126 --account s2427
```

`mach` and `resubmit` are read automatically from `config_registry.yaml` if not supplied on the command line. Any required fields left blank are written as empty strings and flagged with a `# FIXME` header at the top of the output file.

For multi-case exports, shared parameters are automatically factored into `base`; only differing values appear per-case.

### 6. Inspect existing cases

```bash
# Bare case name — resolved relative to caseroot in config_registry.yaml (print only)
python scan.py my_case

# Scan all cases in caseroot and write active.yaml
python scan.py --update

# Multiple cases at once, write to active.yaml
python scan.py case1 case2 case3 --update

# Scan all cases in caseroot (pass the full path)
python scan.py /path/to/cases/

# Add new cases to an existing registry (merges with current active.yaml)
python scan.py my_new_case --registry active.yaml --update

# Scan long_term archive entries and write retired.yaml
python scan.py --retired --update
```

A CASE directory is recognized by the presence of `SourceMods/src.share/exoplanet_mod.F90`. The registry captures metadata from multiple sources per case:

| Source | Fields captured |
|---|---|
| `exoplanet_mod.F90` | All gas bars, radiation/run flags, orbital/geophysical params |
| `user_nl_cam` | `ncdata` (+ pressure/level parsed from filename), `carma_params`, `volc_params` |
| `user_nl_clm` | `finidat`, `fsurdat` (land/mixed only) |
| `user_docn.streams.txt.som` | `som_pop_frc_file` (aqua/mixed only) |
| `env_build.xml` | `nlev`, `exort_pkg`, `cloud_scheme` |
| `env_run.xml` | `run_type`, `run_refcase`, `run_refdate`, `brnch_retain_casename` |

The output YAML is organized into named groups:

```yaml
cases:
- meta:
    case_name: my_case
    config_type: cam_aqua_fv
    nlev: 40
    ncdata: /path/to/ic_1bar_L40.nc
  atmosphere:
    exo_co2bar: 0.0004
    exo_n2bar: 0.7901
    exo_pstd_computed_bar: 1.0
    ...
  geophysical:
    exo_ndays: 1.0
    exo_surface_gravity: 9.81
    ...
  model_options:
    do_exo_atmconst: 'true'
    do_exo_rt: 'true'
    exo_rad_step: 3
    ...
  diagnostics:
    warnings:
    - level mismatch: ncdata has L30 but CAM_CONFIG_OPTS has -nlev 40
```

Consistency warnings are generated for pressure mismatches, level mismatches, and solar file / exort package mismatches. When `netCDF4` is installed and the solar file is accessible, the `nw` spectral dimension is read directly from the file for a more reliable check; otherwise the tool falls back to a filename stem check.

### 7. Manage disk space and case lifecycle

#### Report disk usage (datamgr.py)

```bash
# Show disk usage across cases/, rundir/, and archive/ (default when called with no args)
python datamgr.py
python datamgr.py report               # explicit
python datamgr.py report case1 case2   # specific cases only
```

#### Purge and move files (datamgr.py cata)

All destructive subcommands are **non-destructive by default**. `--execute` is required to make any changes, and each case prompts for confirmation before acting. There is no `--all` flag — case names must always be listed explicitly.

```bash
# Preview what each command would do (safe default — nothing is changed)
python datamgr.py cata purge-bld my_case
python datamgr.py cata purge-restarts my_case --keep 1
python datamgr.py cata purge-hist my_case --models atm lnd
python datamgr.py cata purge-logs my_case
python datamgr.py cata move-hist my_case --models atm

# Add --execute to actually perform the action (prompts yes/no per case)
python datamgr.py cata purge-bld my_case --execute
python datamgr.py cata purge-restarts my_case --keep 1 --execute
python datamgr.py cata move-hist my_case --models atm --execute
```

#### Average history files (datamgr.py)

```bash
python datamgr.py avg my_case --info                   # inspect available files
python datamgr.py avg my_case --last 10 --execute     # average last 10 timesteps
```

#### Subcommand reference

| Subcommand | What it does |
|---|---|
| `datamgr.py report` | Disk usage table: CASEDIR, BLD, RUN, HIST, LOGS, REST, TOTAL per case. Bare invocation scans all cases and clobbers `usage.yaml`. Named-case or `--prefix` invocations print only. `--cached` prints last saved snapshot. |
| `datamgr.py avg` | Inspect or compute time-averaged history files using ncra (NCO). |
| `runmgr.py submit` | `sbatch` a built case as-is — no `xmlchange`. The launch step after `build.py make`. Requires `<case>.run` (skips with a message if not built). Hard-blocks RUNNING/RESUBMITTED; silent for BUILT/COMPLETE; soft-warns otherwise. Preview-only without `--execute`. |
| `runmgr.py continue` | Set `CONTINUE_RUN=TRUE`, optionally update `STOP_N` and `RESUBMIT` (default 0), then `sbatch` the run script. Hard-blocks on RUNNING/RESUBMITTED; soft-warns for non-COMPLETE. Preview-only without `--execute`. |
| `runmgr.py restart` | Set `CONTINUE_RUN=FALSE`, apply `--set VAR=VALUE` `xmlchange` calls, then `sbatch`. Use to fix and rerun from scratch after a completed or failed run. Same gating as `continue`. |
| `datamgr.py cata purge-bld` | Delete `rundir/<case>/bld/` (build objects and logs). Safe after a successful build. `--logs-only` removes only `.o`/`.mod` files and keeps logs. |
| `datamgr.py cata purge-restarts` | Trim old restart sets in `archive/<case>/rest/`, keeping the N most recent (default: 1). |
| `datamgr.py cata purge-hist` | Delete history NetCDF files from `archive/<case>/<model>/hist/`. Requires `--keep-years N` or `--models` as a safety guard. `--keep-years N` retains the N most recent model years (cutoff shared across all targeted components). |
| `datamgr.py cata purge-logs` | Delete log files from `archive/<case>/<model>/logs/` and `caseroot/<case>/logs/`. Both locations safe to purge after a run. `--no-archive-logs` / `--no-case-logs` skip one side. |
| `datamgr.py cata move-hist` | Move history files to long-term storage, preserving directory structure. Source hist/ is left empty. |
| `datamgr.py retire` | Retire a completed case (three tiers — see below). |

#### Retiring a case with `retire`

`retire` is the end-of-life command for a case. Three tiers of increasing preservation:

| Tier | Invocation | What is written to long-term |
|---|---|---|
| Tombstone | bare (no flags) | `case.yaml` only |
| Preserve artifacts | `--keep-*` flags | `case.yaml` (implicit) + selected files |
| Complete erasure | `--purge` | **Nothing** — no record written |

`--keep-config`, `--keep-years N`, and `--keep-restarts` may be freely combined. `--purge` is mutually exclusive with all three. Avg files (filenames containing `"avg"`) are always moved to long-term unconditionally (except under `--purge`). All `retire --execute` invocations prompt for confirmation.

```bash
# Preview (no --execute — always safe to run first)
python datamgr.py retire my_case --keep-config --keep-years 5 --keep-restarts

# Tier 1: tombstone only (case.yaml written, everything deleted)
python datamgr.py retire my_case --execute

# Tier 2: save config files only
python datamgr.py retire my_case --keep-config --execute

# Tier 2: save config files, 1 year of history, and most recent restart
python datamgr.py retire my_case --keep-config --keep-years 1 --keep-restarts --execute

# Tier 3: complete erasure (prominent warning shown before confirmation)
python datamgr.py retire my_case --purge --execute
```

Run any subcommand with `--help` for full options.

---

## Config types

| `config_type` | Dynamics | Ocean/Land |
|---|---|---|
| `cam_aqua_fv` | Finite-volume | Aquaplanet |
| `cam_land_fv` | Finite-volume | Land surface |
| `cam_mixed_fv` | Finite-volume | Mixed ocean+land |
| `cam_aqua_se_ne5` | Spectral-element (ne5) | Aquaplanet |
| `cam_aqua_se_ne16` | Spectral-element (ne16) | Aquaplanet |

## High-pressure atmospheres

For total surface pressure > 1 bar, set `exo_n2bar_explicit` in the case spec. N2 is otherwise computed implicitly as `1 - sum(other gases)`. The `exo_n2bar` Fortran parameter line is patched with the explicit value when `exo_n2bar_explicit` is set; for standard 1-bar cases the expression line is left unchanged and evaluated by the Fortran compiler. Use `nlev` and IC files consistent with your total pressure — the tool validates these against `config_registry.yaml`.

## Fortran expression evaluation

`parse_utils.py` evaluates arithmetic expressions in `exoplanet_mod.F90` parameter lines rather than treating them as opaque strings. Parameters defined as multiplicative factors of Earth values (e.g. `0.91*6.37122e6_R8`) are evaluated to their numeric result. Parameters defined in terms of previously defined parameters (e.g. `1.0 - exo_co2bar - exo_ch4bar`) are resolved by substituting the already-parsed values. This means `scan.py` correctly recovers numeric values for gravity, radius, and N2 bar even from older cases that use expression-style definitions.

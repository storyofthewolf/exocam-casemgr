# exocam-casemgr

Case management tools for [ExoCAM](https://github.com/storyofthewolf/ExoCAM) — an exoplanet climate model based on CESM 1.2.1. These scripts cover the full lifecycle of ExoCAM simulations: translating a YAML experiment matrix into ready-to-run CESM build scripts, scanning existing CASE directories into a queryable YAML registry, and managing disk space across the cases, run, and archive storage areas.

## Requirements

```bash
pip install pyyaml
```

Optional (for solar file `nw` validation in `inspect.py`):
```bash
pip install netCDF4
```

Python 3.8+.

## Files

| File | Purpose |
|---|---|
| `build.py` | Build script generator — validation, Fortran patching, shell script writer |
| `inspect.py` | CASE directory scanner → YAML registry |
| `parse_utils.py` | Parsing primitives shared by build and inspect (no side effects) |
| `manage.py` | Data management — disk usage reporting, purging, and moving data |
| `query.py` | Registry search and experiment matrix export |
| `run_builds.sh` | Batch runner for all `*_build.sh` scripts in a directory |
| `config_registry.yaml` | Machine paths, CESM compset/res per config type, IC file table |
| `experiment_matrix.yaml.example` | Annotated template for writing experiment matrices |

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
cp experiment_matrix.yaml.example my_runs.yaml
```

The matrix has a `base` section (shared defaults) and a `cases` list. Each case inherits all base values and can override any of them. See `experiment_matrix.yaml.example` for the full annotated parameter set, organized into these groups:

| Group | Keys |
|---|---|
| CESM config | `config_type`, `exort_pkg`, `cloud_scheme`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks` |
| HPC batch | `account` (SLURM charge account), `job_name` (short queue label, per-case) |
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
python build.py my_runs.yaml --outdir scripts/
```

This validates every case and writes one script per case:
- `scripts/<case>_build.sh` — complete CESM `create_newcase` + `cesm_setup` + build script
- `scripts/staging/<case>/exoplanet_mod.F90` — parameter file patched with your case values

The build script handles all config-specific file path updates:
- All configs: `user_nl_cam` (ncdata via `sed`; carma/volc params appended via `echo >>`)
- All configs: `${CASE}.run` SBATCH directives (`--account`, `-J`) patched after `cesm_setup`
- Land/mixed: `user_nl_clm` (finidat, fsurdat)
- Aqua/mixed: `user_docn.streams.txt.som` (pop_frc file path and name)

**Review the generated scripts before running.** To also execute the builds immediately:

```bash
python build.py my_runs.yaml --outdir scripts/ --execute
```

Build output is tee'd to `scripts/<case>.build.log`. Job submission (`.run`) is always manual.

To run a single build script:

```bash
bash scripts/my_case_build.sh
```

To run all build scripts in a directory in sequence:

```bash
bash run_builds.sh scripts/
```

`run_builds.sh` reports pass/fail for each case and prints a summary at the end. A failed build does not abort the remaining cases.

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
# Bare export (default when --clone is used) — only run config in base, stubs in cases
python query.py export my_base_case -o sweep.yaml \
    --clone my_base_case --stop-option nyears --stop-n 20 --rest-n 5 \
    --resubmit 4 --ntasks 126 --account s2427

# Full export — all scientific parameters in base for reference
python query.py export my_base_case -o sweep.yaml \
    --clone my_base_case --full --stop-option nyears --stop-n 20 --rest-n 5 \
    --resubmit 4 --ntasks 126
```

Then add per-case entries with only the parameters that differ from the clone source.

### 4. Search the registry and export experiment matrices

```bash
# List all cases
python query.py search

# Filter by metadata
python query.py search --name thai
python query.py search --config-type cam_land_fv --nlev 51
python query.py search --exort-pkg n68equiv

# Show all parameters for one case
python query.py show ExoCAM_thai_ben1_L51_n68equiv

# Export a full matrix from one or more registry cases
python query.py export case_a case_b -o sweep.yaml \
    --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126 --account s2427

# Export a bare clone matrix (minimal base, stubs per case)
python query.py export my_base_case -o clone_sweep.yaml \
    --clone my_base_case --stop-option nyears --stop-n 20 --rest-n 5 \
    --resubmit 4 --ntasks 126 --account s2427
```

`mach` and `resubmit` are read automatically from `config_registry.yaml` if not supplied on the command line. Any required fields left blank are written as empty strings and flagged with a `# FIXME` header at the top of the output file.

For multi-case exports, shared parameters are automatically factored into `base`; only differing values appear per-case.

### 6. Inspect existing cases

```bash
# Bare case name — resolved relative to caseroot in config_registry.yaml
python inspect.py my_case

# Multiple cases at once
python inspect.py case1 case2 case3 --registry cases.yaml

# Scan all cases in caseroot (pass the full path or use . from caseroot)
python inspect.py /path/to/cases/

# Add new cases to an existing registry without overwriting old rows
python inspect.py my_new_case --registry cases.yaml --update

# Preview inspection results without writing the registry
python inspect.py my_case --dry-run
```

A CASE directory is recognized by the presence of `SourceMods/src.share/exoplanet_mod.F90`. The registry captures metadata from multiple sources per case:

| Source | Fields captured |
|---|---|
| `exoplanet_mod.F90` | All gas bars, radiation/run flags, orbital/geophysical params |
| `user_nl_cam` | `ncdata` (+ pressure/level parsed from filename), `carma_params`, `volc_params` |
| `user_nl_clm` | `finidat`, `fsurdat` (land/mixed only) |
| `user_docn.streams.txt.som` | `som_pop_frc_file` (aqua/mixed only) |
| `env_build.xml` | `nlev`, `exort_pkg`, `cloud_scheme` |

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

### 7. Manage disk space

```bash
# Show disk usage across cases/, rundir/, and archive/ (default when called with no args)
python manage.py
python manage.py report               # explicit
python manage.py report case1 case2   # specific cases only

# Preview what each command would do (safe default — nothing is changed)
python manage.py purge-bld my_case
python manage.py purge-restarts my_case --keep 1
python manage.py purge-hist my_case --models atm lnd
python manage.py purge-logs my_case
python manage.py move-hist my_case --models atm

# Add --execute to actually perform the action (prompts yes/no per case)
python manage.py purge-bld my_case --execute
python manage.py purge-restarts my_case --keep 1 --execute
python manage.py move-hist my_case --models atm --execute
```

All destructive subcommands are **non-destructive by default**. `--execute` is required to make any changes, and each case prompts for confirmation before acting. There is no `--all` flag — case names must always be listed explicitly.

| Subcommand | What it does |
|---|---|
| `report` | Disk usage table: CASEDIR, BLD, RUN, HIST, LOGS, REST, TOTAL per case. Read-only; bare invocation reports all cases. |
| `purge-bld` | Delete `rundir/<case>/bld/` (build objects and logs). Safe after a successful build. `--logs-only` removes only `.o`/`.mod` files and keeps logs. |
| `purge-restarts` | Trim old restart sets in `archive/<case>/rest/`, keeping the N most recent (default: 1). |
| `purge-hist` | Delete history NetCDF files from `archive/<case>/<model>/hist/`. Requires `--keep-years N` or `--models` as a safety guard. `--keep-years N` retains the N most recent model years (cutoff shared across all targeted components). |
| `purge-logs` | Delete log files from `archive/<case>/<model>/logs/` and `caseroot/<case>/logs/`. Both locations safe to purge after a run. `--no-archive-logs` / `--no-case-logs` skip one side. |
| `move-hist` | Move history files to long-term storage, preserving directory structure. Source hist/ is left empty. |
| `retire-case` | Retire a completed case. Requires an explicit intent flag (see below). |

#### Retiring a case with `retire-case`

`retire-case` is the end-of-life command for a case. It requires at least one intent flag so the operation is always deliberate:

| Flag | What it does |
|---|---|
| `--keep-case` | Move the entire case tree (caseroot + rundir + archive) to long-term storage intact. No deletions. |
| `--keep-years N` | Move hist files from the N most recent model years to long-term, then delete everything from cesm_scratch. |
| `--keep-restarts` | Move the single most recent restart set to long-term, then delete everything from cesm_scratch. |
| `--purge-only` | Delete everything. No preservation. Mutually exclusive with the other flags. |

`--keep-years` and `--keep-restarts` may be combined with each other and with `--keep-case`. `--purge-only` is mutually exclusive with all three.

```bash
# Preview (no --execute — always safe to run first)
python manage.py retire-case my_case --keep-years 5 --keep-restarts

# Move full case tree to long-term, no deletions
python manage.py retire-case my_case --keep-case --execute

# Keep last 5 years of history + most recent restart, delete the rest
python manage.py retire-case my_case --keep-years 5 --keep-restarts --execute

# Delete everything (case has no long-term value)
python manage.py retire-case my_case --purge-only --execute

# With registry pre-flight check
python manage.py retire-case my_case --purge-only --registry cases.yaml --execute
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

`parse_utils.py` evaluates arithmetic expressions in `exoplanet_mod.F90` parameter lines rather than treating them as opaque strings. Parameters defined as multiplicative factors of Earth values (e.g. `0.91*6.37122e6_R8`) are evaluated to their numeric result. Parameters defined in terms of previously defined parameters (e.g. `1.0 - exo_co2bar - exo_ch4bar`) are resolved by substituting the already-parsed values. This means `inspect.py` correctly recovers numeric values for gravity, radius, and N2 bar even from older cases that use expression-style definitions.

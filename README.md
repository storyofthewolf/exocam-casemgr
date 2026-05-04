# exocam-casemgr

Case management tools for [ExoCAM](https://github.com/storyofthewolf/ExoCAM) — an exoplanet climate model based on CESM 1.2.1. These scripts cover the full lifecycle of ExoCAM simulations: translating a YAML experiment matrix into ready-to-run CESM build scripts, scanning existing CASE directories into a queryable YAML registry, and managing disk space across the cases, run, and archive storage areas.

## Requirements

```bash
pip install pyyaml
```

Optional (for solar file `nw` validation in `exo_inspect.py`):
```bash
pip install netCDF4
```

Python 3.8+.

## Files

| File | Purpose |
|---|---|
| `exo_build.py` | Build script generator — validation, Fortran patching, shell script writer |
| `exo_inspect.py` | CASE directory scanner → YAML registry |
| `exo_parse.py` | Parsing primitives shared by build and inspect (no side effects) |
| `exo_data.py` | Data management — disk usage reporting, purging, and moving data |
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
| CESM config | `config_type`, `exort_pkg`, `cloud_scheme`, `nlev`, `mach`, `stop_*`, `rest_n`, `ntasks` |
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
python exo_build.py my_runs.yaml --outdir scripts/
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
python exo_build.py my_runs.yaml --outdir scripts/ --execute
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

When only a few parameters differ from an existing case, use `clone_of` instead of `create_newcase`:

```yaml
cases:
  - name: co2_modern_2x_scon
    clone_of:  co2_modern     # source case name
    exo_scon:  2720.0
```

The generated script uses `create_clone`, skipping the SourceMods/namelist copy step since those are inherited from the source. `config_type`, `exort_pkg`, and `nlev` are optional for clone cases — if supplied, the IC file lookup and `CAM_CONFIG_OPTS` update are included; otherwise they are inherited from the source.

### 4. Inspect existing cases

```bash
# Bare case name — resolved relative to caseroot in config_registry.yaml
python exo_inspect.py my_case

# Multiple cases at once
python exo_inspect.py case1 case2 case3 --registry cases.yaml

# Scan all cases in caseroot (pass the full path or use . from caseroot)
python exo_inspect.py /path/to/cases/

# Add new cases to an existing registry without overwriting old rows
python exo_inspect.py my_new_case --registry cases.yaml --update

# Preview inspection results without writing the registry
python exo_inspect.py my_case --dry-run
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

### 5. Manage disk space

```bash
# Show disk usage across cases/, rundir/, and archive/ (default when called with no args)
python exo_data.py
python exo_data.py report               # explicit
python exo_data.py report case1 case2   # specific cases only

# Preview what each command would do (safe default — nothing is changed)
python exo_data.py purge-bld
python exo_data.py purge-restarts --keep 1
python exo_data.py purge-hist --models atm lnd
python exo_data.py move-hist --models atm
python exo_data.py move-case my_old_case

# Add --execute to actually perform the action (prompts yes/N per case)
python exo_data.py purge-bld --execute
python exo_data.py purge-restarts --keep 1 --execute
python exo_data.py move-hist --models atm --execute my_case
python exo_data.py move-case --execute my_old_case
```

All destructive subcommands are **non-destructive by default**. `--execute` is required to make any changes, and each case prompts for confirmation before acting.

| Subcommand | What it does |
|---|---|
| `report` | Disk usage table: CASEDIR, BLD, RUN, HIST, LOGS, REST, TOTAL per case |
| `purge-bld` | Delete `rundir/<case>/bld/` (build objects and logs). Safe after a successful build. `--logs-only` removes only `.o`/`.mod` files and keeps logs. |
| `purge-restarts` | Trim old restart sets in `archive/<case>/rest/`, keeping the N most recent (default: 1). |
| `purge-hist` | Delete history NetCDF files from `archive/<case>/<model>/hist/`. Use `--models` to target specific components. |
| `move-hist` | Move history files to long-term storage, preserving directory structure. Source hist/ is left empty. |
| `move-case` | Move an entire case tree (cases + rundir + archive) to long-term storage. Use `--no-casedir`, `--no-rundir`, or `--no-archive` to skip areas. |

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

`exo_parse.py` evaluates arithmetic expressions in `exoplanet_mod.F90` parameter lines rather than treating them as opaque strings. Parameters defined as multiplicative factors of Earth values (e.g. `0.91*6.37122e6_R8`) are evaluated to their numeric result. Parameters defined in terms of previously defined parameters (e.g. `1.0 - exo_co2bar - exo_ch4bar`) are resolved by substituting the already-parsed values. This means `exo_inspect.py` correctly recovers numeric values for gravity, radius, and N2 bar even from older cases that use expression-style definitions.

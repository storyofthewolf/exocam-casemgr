# exocam-casemgr

Case management tools for [ExoCAM](https://github.com/storyofthewolf/ExoCAM) — an exoplanet climate model based on CESM 1.2.1. These scripts translate a YAML experiment matrix into ready-to-run CESM build scripts and a staged `exoplanet_mod.F90`, and can scan existing CASE directories into a queryable YAML registry.

## Requirements

```bash
pip install pyyaml
```

Python 3.8+.

## Workflow

### 1. Configure `config_registry.yaml`

Edit `config_registry.yaml` for your machine — set HPC paths and verify the IC file table matches what you have on disk.

```yaml
paths:
  cesm_scripts: /path/to/cesm1_2_1/scripts
  caseroot:     /path/to/scratch/cases
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
| CESM config | `config_type`, `exort_pkg`, `cloud_scheme`, `nlev`, `mach`, `stop_*`, `rest_n`, `ntasks_atm` |
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

This validates every case and writes:
- `scripts/<case>_build.sh` — complete CESM `create_newcase` + `cesm_setup` + build script
- `scripts/staging/<case>/exoplanet_mod.F90` — parameter file patched with your case values

The build script also handles config-specific file path updates:
- All configs: `user_nl_cam` (ncdata, carma/volc params appended via `echo >>`)
- Land/mixed: `user_nl_clm` (finidat, fsurdat)
- Aqua/mixed: `user_docn.streams.txt.som` (pop_frc file path and name)

**Review the generated scripts before running.** The default is always dry-run.

To also execute the builds:

```bash
python exo_build.py my_runs.yaml --outdir scripts/ --execute
```

Build output is tee'd to `scripts/<case>.build.log`. Job submission (`.run`) is always manual.

### 4. Inspect existing cases

```bash
# Scan a parent directory containing CASE dirs, write YAML registry
python exo_inspect.py /path/to/cases/ --registry cases.yaml

# Add new cases to an existing registry without overwriting old rows
python exo_inspect.py /path/to/new_cases/ --registry cases.yaml --update
```

A CASE directory is recognized by the presence of `SourceMods/src.share/exoplanet_mod.F90`. The registry captures metadata from multiple sources per case:

| Source | Fields captured |
|---|---|
| `exoplanet_mod.F90` | All gas bars, radiation/run flags, orbital/geophysical params |
| `user_nl_cam` | `ncdata` (+ pressure/level parsed from filename), `carma_params`, `volc_params` |
| `user_nl_clm` | `finidat`, `fsurdat` (land/mixed only) |
| `user_docn.streams.txt.som` | `som_pop_frc_file` (aqua/mixed only) |
| `env_build.xml` | `nlev`, `exort_pkg`, `cloud_scheme` |

The output YAML is organized into named groups for readability:

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

Consistency warnings are generated for pressure mismatches between `exoplanet_mod.F90` and the IC filename, level mismatches, and solar file / exort package mismatches.

## Config types

| `config_type` | Dynamics | Ocean/Land |
|---|---|---|
| `cam_aqua_fv` | Finite-volume | Aquaplanet |
| `cam_land_fv` | Finite-volume | Land surface |
| `cam_mixed_fv` | Finite-volume | Mixed ocean+land |
| `cam_aqua_se_ne5` | Spectral-element (ne5) | Aquaplanet |
| `cam_aqua_se_ne16` | Spectral-element (ne16) | Aquaplanet |

## High-pressure atmospheres

For total surface pressure > 1 bar, set `exo_n2bar_explicit` in the case spec. N2 is otherwise computed implicitly as `1 - sum(other gases)`. Use `nlev` and IC files consistent with your total pressure — the tool validates these against `config_registry.yaml`.

## Fortran expression evaluation

`exo_parse.py` evaluates arithmetic expressions in `exoplanet_mod.F90` parameter lines rather than treating them as opaque strings. Parameters defined as multiplicative factors of Earth values (e.g. `0.91*6.37122e6_R8`) are evaluated to their numeric result. Parameters defined in terms of previously defined parameters (e.g. `1.0 - exo_co2bar - exo_ch4bar`) are resolved by substituting the already-parsed values. This means `exo_inspect.py` correctly recovers numeric values for gravity, radius, and N2 bar even from older cases that use expression-style definitions.

## Files

| File | Purpose |
|---|---|
| `exo_build.py` | Build script generator — validation, Fortran patching, shell script writer |
| `exo_parse.py` | Parsing primitives (no side effects) — shared by build and inspect |
| `exo_inspect.py` | CASE directory scanner → YAML registry |
| `config_registry.yaml` | Machine paths, CESM compset/res per config type, IC file table |
| `experiment_matrix.yaml.example` | Annotated template for writing experiment matrices |

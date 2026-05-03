# exocam-casemgr

Case management tools for [ExoCAM](https://github.com/storyofthewolf/ExoCAM) — an exoplanet climate model based on CESM 1.2.1. These scripts translate a YAML experiment matrix into ready-to-run CESM build scripts and a staged `exoplanet_mod.F90`, and can scan existing CASE directories into a queryable CSV registry.

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

The matrix has a `base` section (shared defaults) and a `cases` list. Each case inherits all base values and can override any of them:

```yaml
config_registry: /path/to/config_registry.yaml

base:
  config_type:  cam_aqua_fv
  exort_pkg:    n68equiv
  nlev:         40
  mach:         discover
  exo_co2bar:   0.0004
  exo_o2bar:    0.2095
  stop_option:  nyears
  stop_n:       20
  rest_n:       5
  ntasks_atm:   256
  ...

cases:
  - name: co2_modern
    exo_co2bar: 0.0004

  - name: co2_10x
    exo_co2bar: 0.004
```

See `experiment_matrix.yaml.example` for the full set of supported parameters including gas pressures, orbital/rotation parameters, stellar constants, and cloud scheme.

### 3. Generate build scripts

```bash
python exo_build.py my_runs.yaml --outdir scripts/
```

This validates every case and writes:
- `scripts/<case>_build.sh` — complete CESM `create_newcase` + `cesm_setup` + build script
- `scripts/staging/<case>/exoplanet_mod.F90` — parameter file patched with your case values

**Review the generated scripts before running.** The default is always dry-run.

To also execute the builds:

```bash
python exo_build.py my_runs.yaml --outdir scripts/ --execute
```

Build output is tee'd to `scripts/<case>.build.log`. Job submission (`.run`) is always manual.

### 4. Inspect existing cases

```bash
# Scan a parent directory containing CASE dirs, write CSV
python exo_inspect.py /path/to/cases/ --registry cases.csv

# Add new cases to an existing registry without overwriting old rows
python exo_inspect.py /path/to/new_cases/ --registry cases.csv --update
```

A CASE directory is recognized by the presence of `SourceMods/src.share/exoplanet_mod.F90`. The CSV captures gas pressures, orbital parameters, IC file, ExoRT package, number of levels, and consistency warnings (pressure/level mismatches, solar file mismatches).

## Config types

| `config_type` | Dynamics | Ocean/Land |
|---|---|---|
| `cam_aqua_fv` | Finite-volume | Aquaplanet |
| `cam_land_fv` | Finite-volume | Land surface |
| `cam_mixed_fv` | Finite-volume | Mixed ocean+land |
| `cam_aqua_se_ne5` | Spectral-element (ne5) | Aquaplanet |
| `cam_aqua_se_ne16` | Spectral-element (ne16) | Aquaplanet |

## High-pressure atmospheres

For total surface pressure > 1 bar, set `exo_n2bar_explicit` in the case spec. The N2 partial pressure is otherwise computed implicitly as `1 - sum(other gases)`. Use `nlev` and IC files consistent with your total pressure — the tool validates these against `config_registry.yaml`.

## Files

| File | Purpose |
|---|---|
| `exo_build.py` | Build script generator — validation, Fortran patching, shell script writer |
| `exo_parse.py` | Parsing primitives (no side effects) — shared by build and inspect |
| `exo_inspect.py` | CASE directory scanner → CSV registry |
| `config_registry.yaml` | Machine paths, CESM compset/res per config type, IC file table |
| `experiment_matrix.yaml.example` | Annotated template for writing experiment matrices |

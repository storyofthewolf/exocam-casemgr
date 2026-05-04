# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ExoCAM case management tools — Python scripts that automate building and inspecting [ExoCAM](https://github.com/storyofthewolf/ExoCAM) simulation cases on HPC systems. ExoCAM is a fork of CESM 1.2.1 for exoplanet climate modeling; the scripts here translate a YAML experiment matrix into CESM shell build scripts and a staged `exoplanet_mod.F90`.

The target runtime environment is NASA Discover (SLURM HPC). Scripts are generated locally, reviewed, then run on the cluster.

## Running the tools

```bash
# Generate build scripts (dry-run — no execution)
python exo_build.py experiment_matrix.yaml --outdir scripts/

# Generate AND execute builds
python exo_build.py experiment_matrix.yaml --outdir scripts/ --execute

# Inspect existing CASE directories, write YAML registry
python exo_inspect.py /path/to/cases/ --registry cases.yaml

# Update (merge) instead of overwriting registry
python exo_inspect.py /path/to/cases/ --registry cases.yaml --update
```

Dependency: `pip install pyyaml`

## Architecture

### Data flow

```
experiment_matrix.yaml
  + config_registry.yaml
       ↓
  exo_build.py
       ↓
  scripts/<case>_build.sh          ← shell script for CESM create_newcase / build
  scripts/staging/<case>/exoplanet_mod.F90  ← patched Fortran parameter file
```

**`exo_parse.py`** — pure parsing primitives, no filesystem side effects. Used by both other scripts.
- `parse_exoplanet_mod()` — reads Fortran parameter file → flat dict
- `parse_user_nl_cam()` — reads CESM namelist file → dict (includes IC pressure/level extracted from filename)
- `parse_cam_config_opts()` — reads `env_build.xml` for `-nlev`, `-usr_src` (exort_pkg), cloud scheme
- `compute_pstd_bar()` — derives total surface pressure from gas bar values

**`exo_build.py`** — orchestrates validation and code generation.
- `resolve_case(base, overrides)` — merges base + per-case dict
- `validate_case(spec, registry)` — returns list of error strings; checks required fields, IC file availability, solar/exort consistency, synchronous rotation math
- `render_exoplanet_mod(template_path, spec)` — regex-patches active Fortran parameter lines in-place; leaves commented lines and expression-derived constants untouched
- `generate_shell_script(...)` — writes the CESM `create_newcase` + `cesm_setup` + build shell script

**`exo_inspect.py`** — walks existing CASE directories (identified by presence of `SourceMods/src.share/exoplanet_mod.F90`), extracts scientific metadata, and writes a queryable CSV.

**`config_registry.yaml`** — machine-specific file that must be edited per user/machine. Holds HPC paths, CESM compset/resolution per config type, and the IC file lookup table keyed by `config_type → pressure_str → nlev`.

### Pressure representation

Total surface pressure is computed from the sum of individual gas bar values (`exo_co2bar`, `exo_ch4bar`, etc.). N2 is implicit: for ≤1 bar atmospheres it fills to 1.0. For higher pressures, `exo_n2bar_explicit` must be set in the case spec. The Fortran source keeps N2 as a derived expression — the scripts never rewrite that line.

Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are used as keys in the IC file table and must exactly match substrings in IC filenames.

### Fortran patching

`render_exoplanet_mod` matches active parameter lines via `_RE_PARAM_LINE` (real/integer/logical with `parameter ::`). It skips commented lines and passes expression-RHS lines through unchanged. Values are formatted with `_r8` kind suffix for Fortran reals; logicals become `.true.`/`.false.`.

### Config types

| `config_type` | Description |
|---|---|
| `cam_aqua_fv` | Aquaplanet, finite-volume dynamics |
| `cam_land_fv` | Land/continent, finite-volume |
| `cam_mixed_fv` | Mixed ocean/land, finite-volume |
| `cam_aqua_se_ne5` / `ne16` | Aquaplanet, spectral-element dynamics |

SE configs (`_ne5`, `_ne16`) strip the suffix when looking up the SourceMods template directory.

## Experiment matrix format

Copy `experiment_matrix.yaml.example`, set `config_registry` path, fill `base` defaults, add per-case overrides under `cases`. Each case inherits all base values; any key in a case dict overrides the base. Use `ncdata_override` to bypass the automatic IC file lookup.

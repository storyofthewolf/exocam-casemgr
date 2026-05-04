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
- `parse_exoplanet_mod()` — reads Fortran parameter file → flat dict. Evaluates arithmetic expressions (e.g. `0.91*6.37122e6_R8`) and symbol-substitution expressions (e.g. `1.0 - exo_co2bar - exo_ch4bar`) using `_try_eval_expr()`, which substitutes already-resolved param values then evals in a restricted namespace. Unevaluable expressions (unknown symbols) fall back to `name_expr` raw string storage.
- `parse_user_nl_cam()` — reads CESM namelist file → dict. Captures `ncdata`, IC pressure/level from filename, and any `carma_*` / `volc_*` keys as nested dicts. Handles both single- and double-quoted values.
- `parse_user_nl_clm()` — reads `user_nl_clm` → dict with `finidat` and `fsurdat`. Called for `cam_land_fv` and `cam_mixed_fv` only.
- `parse_docn_som()` — reads `user_docn.streams.txt.som` (XML fragment) → dict with `som_pop_frc_file` (full path). Called for aqua and mixed configs only.
- `parse_cam_config_opts()` — reads `env_build.xml` for `-nlev`, `-usr_src` (exort_pkg), cloud scheme.
- `compute_pstd_bar()` — derives total surface pressure from gas bar values.

**`exo_build.py`** — orchestrates validation and code generation.
- `resolve_case(base, overrides)` — merges base + per-case dict.
- `validate_case(spec, registry)` — returns list of error strings; checks required fields, IC file availability, solar/exort consistency, synchronous rotation math.
- `render_exoplanet_mod(template_path, spec)` — regex-patches active Fortran parameter lines in-place; leaves commented lines and expression-derived constants untouched.
- `generate_shell_script(...)` — writes the CESM `create_newcase` + `cesm_setup` + build shell script. Config-specific file updates are emitted as shell commands:
  - All configs: `sed` to update `ncdata` in `user_nl_cam`; `echo >>` to append `carma_params` and/or `volc_params`.
  - Land/mixed: `sed` to update `finidat` and `fsurdat` in `user_nl_clm`.
  - Aqua/mixed: `sed` to update the `pop_frc*` path and filename in `user_docn.streams.txt.som`.
- `EXO_PARAMS` — set of all parameter names that map directly to `exoplanet_mod.F90` lines and can be patched from the experiment matrix.

**`exo_inspect.py`** — walks existing CASE directories (identified by presence of `SourceMods/src.share/exoplanet_mod.F90`), extracts scientific metadata, and writes a queryable YAML registry.

**`config_registry.yaml`** — machine-specific file that must be edited per user/machine. Holds HPC paths, CESM compset/resolution per config type, and the IC file lookup table keyed by `config_type → pressure_str → nlev`.

### YAML registry structure

`exo_inspect.py` writes a grouped YAML (not CSV). Groups are defined in `_REGISTRY_GROUPS` and `write_registry` emits one YAML block per group. `load_registry` flattens the groups back to plain dicts for internal use (merge, summary table). Groups with no populated fields are omitted.

```yaml
cases:
- meta:         # case identity, CESM config, IC file info, CLM files, SOM file
  atmosphere:   # gas bars, pstd, scon, solar file
  geophysical:  # ndays, porb, sday, gravity, radius, eccen, obliq
  model_options: # do_exo_* flags, exo_convect_plim, exo_rad_step, rt flags
  special:      # carma_params, volc_params (nested dicts; omitted if absent)
  diagnostics:  # warnings list (omitted if no warnings)
```

To add a new inspected field: (1) add its key to the appropriate group in `_REGISTRY_GROUPS`; (2) add it to the collection loop in `inspect_case` (or add a new parse call if it comes from a new source file).

### Pressure representation

Total surface pressure is computed from the sum of individual gas bar values (`exo_co2bar`, `exo_ch4bar`, etc.). N2 is implicit: for ≤1 bar atmospheres it fills to 1.0. For higher pressures, `exo_n2bar_explicit` must be set in the case spec. The Fortran source keeps N2 as a derived expression — the scripts never rewrite that line.

Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are used as keys in the IC file table and must exactly match substrings in IC filenames.

### Fortran patching

`render_exoplanet_mod` matches active parameter lines via `_RE_PARAM_LINE` (real/integer/logical with `parameter ::`). It skips commented lines and passes expression-RHS lines through unchanged. Values are formatted with `_r8` kind suffix for Fortran reals; logicals become `.true.`/`.false.`.

`parse_exoplanet_mod` uses `_try_eval_expr` to evaluate parameter RHS values:
- Strips Fortran kind suffixes (`_r8`, `_R8`)
- Substitutes already-resolved numeric params by name (longest-first to avoid partial matches)
- Only calls `eval()` if the result matches `_RE_SAFE_EXPR` (pure arithmetic: digits, operators, parens)
- Runs `eval` with `__builtins__: {}` to restrict the namespace
- Falls back to `name_expr` raw string storage on any failure

### Config types

| `config_type` | Description |
|---|---|
| `cam_aqua_fv` | Aquaplanet, finite-volume dynamics |
| `cam_land_fv` | Land/continent, finite-volume |
| `cam_mixed_fv` | Mixed ocean/land, finite-volume |
| `cam_aqua_se_ne5` / `ne16` | Aquaplanet, spectral-element dynamics |

SE configs (`_ne5`, `_ne16`) strip the suffix when looking up the SourceMods template directory.

Config-type-specific behavior:
- `cam_land_fv`, `cam_mixed_fv`: read `user_nl_clm` for `finidat`/`fsurdat`; generate sed updates for those paths.
- `cam_aqua_fv`, `cam_aqua_se_*`, `cam_mixed_fv`: read `user_docn.streams.txt.som` for `som_pop_frc_file`; generate sed updates for the SOM forcing file.

### carma_params and volc_params

Both are nested dicts in the experiment matrix spec and in the YAML registry. In `exo_build.py`, `_build_nl_append_block` converts them to `echo "key = 'value'" >> user_nl_cam` shell lines. Value quoting rules:
- Already single- or double-quoted values: emitted as-is (inner `"` escaped for the surrounding `echo "..."`).
- Python floats: formatted with `%g` to preserve scientific notation.
- All other bare values: wrapped in single quotes (Fortran namelist string convention).

## Experiment matrix format

Copy `experiment_matrix.yaml.example`, set `config_registry` path, fill `base` defaults, add per-case overrides under `cases`. Each case inherits all base values; any key in a case dict overrides the base. Use `ncdata_override` to bypass the automatic IC file lookup. The `carma_params` and `volc_params` keys take nested dicts and can appear in `base` or per-case.

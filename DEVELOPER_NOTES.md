# DEVELOPER_NOTES.md

Human-readable reference for the ExoCAM case management toolchain. This document is a companion to `CLAUDE.md` (architectural overview) and the source files themselves. It is not loaded into Claude Code's working memory — consult it directly when you need implementation details.

---

## Quick CLI reference

```bash
# Build scripts
python build.py generate experiment_matrix.yaml        # generate shell scripts into build_scripts/
python build.py --scripts-dir scripts/ generate matrix.yaml
python build.py generate --list                        # list blueprint matrices
python build.py make                                   # run all *_build.sh (with confirmation)
python build.py make --prefix ExoCAM_thai              # run only matching scripts

# Scan cases into registry
python scan.py                                         # scan caseroot, print only
python scan.py --update                                # scan and write active.yaml (clobbers)
python scan.py my_case                                 # inspect single case, print only
python scan.py my_case --registry active.yaml --update # inspect and merge into registry
python scan.py --retired                               # scan long_term/ entries only
python scan.py --retired --update                      # scan long_term/ and write retired.yaml

# Query registry
python query.py search                                 # all cases
python query.py search --prefix ExoCAM_thai
python query.py search --config-type cam_land_fv --nlev 51
python query.py search --exort-pkg n68equiv
python query.py show my_case                           # full YAML dump
python query.py export case_a case_b -o sweep.yaml \
    --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126
python query.py export my_base_case -o clone.yaml \
    --clone --stop-option nyears --stop-n 20 --rest-n 5 --resubmit 4 --ntasks 126

# Disk management (all destructive ops: preview by default, --execute to act)
python manage.py report                                # scan all cases, write usage.yaml
python manage.py report my_case                        # single case, print only, no yaml write
python manage.py report --cached                       # read usage.yaml, no disk scan
python manage.py purge-bld my_case --execute
python manage.py purge-bld my_case --logs-only --execute
python manage.py purge-restarts my_case --keep 1 --execute
python manage.py purge-hist my_case --models atm --execute
python manage.py purge-logs my_case --execute
python manage.py move-hist my_case --models atm --execute
python manage.py avg my_case --info
python manage.py avg my_case --last 10 --execute
python manage.py retire my_case --execute                                          # tombstone only
python manage.py retire my_case --keep-config --keep-years 5 --keep-restarts --execute
python manage.py retire my_case --purge --execute                                  # complete erasure

# SourceMods diff (before retiring)
python diff.py my_case                                 # summary: MODIFIED / CASE ONLY per file
python diff.py my_case --verbose                       # include IDENTICAL files
python diff.py my_case --full physpkg.F90              # full diff for one file
python diff.py case1 --case2 case2                     # case-vs-case comparison
python diff.py case1 --case2 case2 --full physpkg.F90
```

Dependencies: `pip install pyyaml` (required); `pip install netCDF4` (optional, for solar file nw validation)

---

## Experiment matrix format

Start from `experiment_matrix.yaml.example`. Each case inherits all `base` values; any key in a case dict overrides the base.

**Top-level keys:**

| Key | Description |
|---|---|
| `config_registry` | Required path to `config_registry.yaml` |
| `meta` | Optional: `description`, `author`, `created`, `source_registry`; auto-populated by `query.py export` |
| `paths` | Optional overrides of machine paths from the registry |
| `base` | Shared defaults for all cases |
| `cases` | List of case dicts, each with a required `name` key |

**Special case/base keys:**

| Key | Description |
|---|---|
| `clone` | Triggers clone mode (`create_clone`). Typically in `base` so all cases share the same source. `exoplanet_mod.F90` template taken from clone source; only matrix-listed params are patched. |
| `ncdata` | Bypasses automatic IC file lookup |
| `exo_n2bar_explicit` | Required for non-1-bar atmospheres; patches `exo_n2bar` Fortran line with explicit value |
| `account` | `#SBATCH --account` written to `${CASE}.run` (typically in `base`) |
| `job_name` | `#SBATCH -J` written to `${CASE}.run` (typically per-case) |
| `carma_params` | Nested dict → `user_nl_cam` (append in newcase, upsert in clone) |
| `volc_params` | Same as `carma_params` |
| `nl_cam_params` | Catch-all for any other `user_nl_cam` keys (e.g. `nhtfrq`, `mfilt`, tuning knobs) |
| `run_type` | `startup` (default), `branch`, or `hybrid` |
| `run_refcase` | Reference case name for branch/hybrid |
| `run_refdate` | Reference date string, e.g. `0021-01-01` |
| `brnch_retain_casename` | `'true'` or `'false'`; passed to `BRNCH_RETAIN_CASENAME` xmlchange |

---

## YAML registry structure

Written by `scan.py`, read by `query.py` and `manage.py`. Groups are defined by `_REGISTRY_GROUPS` in `scan.py`.

```yaml
cases:
- meta:          # case_name, casedir, config_type, exort_pkg, nlev, inspect_date,
                 # ncdata, ncdata_pressure_str, ncdata_levels, clm_finidat, clm_fsurdat,
                 # som_pop_frc_file, run_type, run_refcase, run_refdate, brnch_retain_casename
  atmosphere:    # gas bars (exo_co2bar, exo_ch4bar, ...), exo_pstd_computed_bar,
                 # exo_scon, solar_file, exort_pkg
  geophysical:   # exo_ndays, exo_porb, exo_sday, exo_gravity, exo_radius,
                 # exo_eccen, exo_obliq, exo_mvelp, exo_ve
  model_options: # do_exo_* flags, exo_convect_plim, exo_rad_step, RT tuning flags
  special:       # carma_params, volc_params (nested dicts; omitted if absent)
  diagnostics:   # warnings list (omitted if no warnings)
```

Fields stripped when exporting to matrix (registry-internal only):
`case_name`, `casedir`, `inspect_date`, `ncdata_pressure_str`, `ncdata_levels`,
`exo_n2bar`, `exo_n2bar_expr`, `exo_sday_expr`, `exo_pstd_computed_bar`, `warnings`, `config_saved`.

Key renames on export: `clm_finidat` → `finidat`, `clm_fsurdat` → `fsurdat`.

---

## config_registry.yaml structure

Machine-specific file; must be edited per user/machine. Not committed to shared repos.

```yaml
machine: discover
defaults:
  resubmit: 4
  stop_option: nyears
  stop_n: 10
  rest_n: 1
  ntasks: 126
  account: YOUR_ACCOUNT
paths:
  cesm_scripts: /path/to/cesm1.2.1/scripts
  caseroot: /path/to/cases
  rundir: /path/to/run
  archive: /path/to/archive
  long_term: /path/to/long_term
  exocam_root: /path/to/ExoCAM
  exort_root: /path/to/ExoRT
cesm_config:
  cam_aqua_fv:
    compset: F_AMIP_CN
    res: f45_g37
    phys: cam5
  cam_land_fv:
    ...
ic_files:
  cam_aqua_fv:
    1bar:
      51: aqua_1bar_L51_ncdata.nc
      ...
solar_file_stems:
  n68equiv: n68
  n84equiv: n84
  n28archean: n28
  n42h2o: n42
```

---

## parse_utils.py — parsing internals

All functions take explicit file paths and return dicts. No filesystem side effects — this is a hard invariant.

| Function | Input | Output | Notes |
|---|---|---|---|
| `parse_exoplanet_mod(path)` | `exoplanet_mod.F90` | flat dict of param → value | Evaluates arithmetic/symbol-substitution expressions via `_try_eval_expr`; falls back to `name_expr` raw string on failure |
| `parse_user_nl_cam(path)` | `user_nl_cam` | dict with `ncdata`, pressure/level, `carma_*`/`volc_*` nested dicts | `_coerce_nl_value` preserves Fortran logicals as strings for round-trip correctness |
| `parse_user_nl_clm(path)` | `user_nl_clm` | dict with `finidat`, `fsurdat` | Called for `cam_land_fv` and `cam_mixed_fv` only |
| `parse_docn_som(path)` | `user_docn.streams.txt.som` | dict with `som_pop_frc_file` | XML fragment; wrapped in synthetic root for ElementTree |
| `parse_cam_config_opts(xmlpath)` | `env_build.xml` (falls back to `env_run.xml`) | dict with `nlev`, `exort_pkg`, cloud scheme | |
| `parse_run_type_fields(xmlpath)` | `env_run.xml` | dict with `run_type`, `run_refcase`, `run_refdate`, `brnch_retain_casename` | Falls back to line scan if ElementTree fails |
| `compute_pstd_bar(params)` | params dict | `(pstd_bar, n2bar_computed)` | N2 implicit for ≤1 bar; explicit `exo_n2bar` required above 1 bar |
| `pressure_str_to_bar(s)` | `'1bar'`, `'0.1bar'` etc. | float | Used by `check_consistency` |
| `read_solar_nw(path)` | NetCDF solar file | int or None | Returns None if netCDF4 unavailable or file inaccessible |

**`_try_eval_expr` safety rules:**
- Strips Fortran kind suffixes (`_r8`, `_R8`)
- Substitutes already-resolved numeric params by name, longest-name-first (avoids partial matches)
- Calls `eval()` only if result matches `_RE_SAFE_EXPR` (pure arithmetic: digits, operators, parens)
- Runs `eval` with `__builtins__: {}` to restrict namespace
- Falls back to `name_expr` raw string on any failure

---

## build.py — key constants and validation logic

**`EXO_PARAMS`** — parameters patchable from the experiment matrix into `exoplanet_mod.F90`:
gas bars (`exo_co2bar`, `exo_ch4bar`, `exo_n2bar`, `exo_o2bar`, `exo_h2bar`, `exo_arbar`),
geophysical (`exo_gravity`, `exo_radius`, `exo_porb`, `exo_ndays`, `exo_sday`, `exo_scon`, `exo_eccen`, `exo_obliq`),
logical flags (`do_exo_*`),
RT tuning (`Tmax`, `swFluxLimit`, `lwFluxLimit`, `exo_albdif`, `exo_albdir`, `exo_mvelp`, `exo_ve`).

**`REQUIRED_FIELDS`** (newcase): `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`

**`REQUIRED_FIELDS_CLONE`**: `clone`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`

**Namelist value formatting** (`_format_nl_value`): `bool` → `.true.`/`.false.`; `int` → bare integer; `float` → `%g` with decimal ensured; `str` Fortran logical → pass through; `str` numeric → coerced; `str` other → single-quoted. Note: `bool` is checked before `int` because Python's `bool` is a subclass of `int`.

**Newcase vs clone namelist behavior:**
- Newcase: plain `echo >> user_nl_cam` (template is fresh, no existing keys)
- Clone: `grep -q / sed -i / echo >>` upsert (clone copies `user_nl_cam` verbatim from source, so appending duplicates keys)

**Branch/hybrid CESM workaround:** CESM requires `RUN_TYPE=startup` during `cesm_setup` when the rundir doesn't yet exist. Build scripts set `startup` before setup, then switch back to `branch`/`hybrid` after copying restart files. `RUN_REFDIR` always appends `-00000` to `RUN_REFDATE` (CESM restart dirs are named `YYYY-MM-DD-SSSSS`; seconds are always zero for ExoCAM cases).

**`_build_usr_src_fix_block` (clone + custom RT):** When `exort_pkg` ends with `*`, `create_clone` inherits `-usr_src` from the source case verbatim. This block reads the inherited `CAM_CONFIG_OPTS` via `xmlquery | sed` (CESM 1.2.1 lacks `xmlquery --value`), extracts the old path, then calls `xmlchange` with the new path inlined as a double-quoted sed replacement so shell variables expand before `xmlchange` sees the value. `xmlchange` rejects unexpanded shell variable references.

---

## scan.py — key constants

**`SOLAR_NW_MAP`** — expected `nw` dimension per `exort_pkg`:
`{n68equiv: 68, n84equiv: 84, n28archean: 28, n42h2o: 42}`

**`SOLAR_STEM_MAP`** — expected solar filename stem per `exort_pkg`:
`{n68equiv: 'n68', n84equiv: 'n84', n28archean: 'n28', n42h2o: 'n42'}`

**`check_consistency` warnings generated for:**
1. Pressure mismatch >5% between computed pstd and ncdata pressure string
2. Level count mismatch between ncdata filename and `-nlev`
3. Solar file / exort_pkg mismatch (prefers direct NetCDF `nw` read; falls back to stem check; silently skips custom stellar spectra like BT-Settl)

**`scan_archive_entries`** handles two `case.yaml` formats:
- Full registry-format: `{'cases': [...]}`
- Minimal stub: `{'case_name': ..., 'retired_date': ...}`

Sets `config_saved` (bool) on every row by checking whether `SourceMods/` exists in the long-term directory.

---

## query.py — export internals

**`_CLONE_BASE_FIELDS`** — fields included in clone-mode sparse export:
`clone`, `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_n`, `resubmit`, `ntasks`, `account`, `run_type`, `run_refcase`, `run_refdate`, `brnch_retain_casename`.

`exort_pkg *` warning: printed to stderr after matrix output (visible at end). Suppressed in `--clone` mode since RT is inherited from the clone source.

`cmd_search` appends a CONFIG column (showing `yes` or `-`) only when at least one result row contains `config_saved` — this is present in `retired.yaml` searches, absent in `active.yaml` searches.

---

## manage.py — key constants and retire tiers

**`ARCHIVE_MODELS`**: `['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']`

**`HIST_MODELS`**: `ARCHIVE_MODELS` minus `'rest'` (components with `hist/` and `logs/` subdirs)

**`retire` three tiers:**

| Invocation | What happens |
|---|---|
| `retire my_case --execute` | Writes `case.yaml` tombstone to long-term, then deletes everything from scratch |
| `retire my_case --keep-config --keep-years N --keep-restarts --execute` | `case.yaml` written implicitly; SourceMods/user_*/env_* copied; N most recent hist years moved; most recent restart moved. Flags freely combinable. |
| `retire my_case --purge --execute` | Complete erasure. Nothing written to long-term. Prominent `*** WARNING ***` shown in preview and at confirmation prompt. Mutually exclusive with all `--keep-*` flags. |

Avg files (filenames containing `"avg"`) are always moved to long-term unconditionally, except under `--purge`.

`case.yaml` source priority: live scan → `--registry` (default: `active.yaml`) → minimal stub.

**`report` write behavior:**
- Bare `report` (all cases): scans disk, clobbers `usage.yaml`
- `report my_case` or `report --prefix STR`: prints only, never writes `usage.yaml`
- `report --cached`: reads `usage.yaml`, no disk scan; incompatible with explicit case names

---

## diff.py — classification logic

**Case-vs-ExoCAM categories (SourceMods):**
1. `IDENTICAL` — matches ExoCAM reference
2. `MODIFIED` — differs from ExoCAM reference (ExoCAM match takes priority over RT match)
3. `RT IDENTICAL` — matches ExoRT package file
4. `RT MODIFIED` — differs from ExoRT package file
5. `CASE ONLY` — not in ExoCAM reference or ExoRT package

**Case-vs-ExoCAM categories (namelists):**
`IDENTICAL`, `MODIFIED`, `CASE ONLY` — diffed against `namelist_files/` reference.

**Case-vs-case categories:** `IDENTICAL`, `MODIFIED`, `CASE1 ONLY`, `CASE2 ONLY` (no active.yaml or ExoRT lookup).

**`COMPONENTS`** (printed in this order): `['src.cam', 'src.share', 'src.drv', 'src.clm', 'src.cice']`

**`SKIP_FILES`**: `{'exoplanet_mod.F90'}` — always skipped (patched per-case, not meaningful to diff)

**`NAMELIST_FILES`**: `['user_nl_cam', 'user_nl_clm', 'user_nl_cice', 'user_docn.streams.txt.som', 'user_nl_cpl', 'user_nl_docn', 'user_nl_rtm']`

ExoCAM reference paths:
- SourceMods: `{exocam_root}/cesm1.2.1/configs/{config_type}/SourceMods/`
- Namelists: `{exocam_root}/cesm1.2.1/configs/{config_type}/namelist_files/`
- ExoRT package: `{exort_root}/3dmodels/src.cam.{exort_pkg}/` (`*` suffix stripped before path construction)

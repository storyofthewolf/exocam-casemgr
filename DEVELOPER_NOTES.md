# DEVELOPER_NOTES.md

Human-readable reference for the ExoCAM case management toolchain. This document is a companion to `CLAUDE.md` (architectural overview) and the source files themselves. It is not loaded into Claude Code's working memory — consult it directly when you need implementation details.

---

## Concepts — the mental model

Read this first if you're returning to the toolchain after a break. The pieces only
make sense once you hold three ideas in mind: **two formats, one bridge.**

### The two formats hold different things

| | **Experiment matrix** (`exp_matrices/*.yaml`) | **Registry** (`active.yaml` / `retired.yaml`) |
|---|---|---|
| Direction | **input** — what you intend to build | **output** — what was actually built |
| Author | **you**, by hand | **`scan.py`**, by walking live CASE dirs |
| Shape | `base:` + `cases:` (factored) | flat, **explicit per case** |
| Contents | only the knobs you set | every field, incl. derived/diagnostic |
| Optimized for | writing | querying |

The key asymmetry: the matrix's `base:`/`cases:` split encodes **authorial intent** —
"these values are shared *on purpose*; these per-case lines are the *intentional*
deviations." That factoring is information only you have. By the time `scan.py` sees a
directory of finished cases, the build has already "compiled away" the `base:`/`cases:`
structure into fully-resolved configurations. **You cannot recover intent from facts**,
so the registry doesn't try — it stores each case completely, on its own terms.

This is also why the registry holds fields that are *not* valid matrix inputs:
`ncdata_pressure_str` (parsed from a filename), `exo_pstd_computed_bar` (computed),
`warnings` / `inspect_date` (observations). These are *results*, not *settings*.

### `export` is the (deliberately lossy) bridge back

`query.py export` goes registry → matrix, re-imposing a `base:` factoring that was never
recoverable. So it punts on the hard version: it dumps fields into `base:` (or, in
`--clone` mode, prunes to a known field set) rather than guessing what "should" be shared
vs. per-case. It also **strips** registry-only fields (`_SKIP_KEYS`), **renames** registry
keys to matrix keys (`clm_finidat` → `finidat`), and lets you inject fresh run settings
(`--stop-n`, `--account`). You then re-impose the real factoring by hand — because you're
the only one who knows it.

**Rule of thumb: store rich, emit lean.** Scan everything observable into the registry;
export only the subset that is a legitimate, re-runnable input.

### Two reads of a case, two different shapes

- **`query.py show <case>`** — dumps the registry entry **verbatim**, in its **grouped**
  structure (`meta:`, `atmosphere:`, …). A faithful window *into* storage. Use it to
  inspect "everything we know about this case."
- **`query.py export <case>`** — **synthesizes a build-ready matrix** (`base:` + `cases:`),
  flat and filtered, to feed back into `build.py`. Use it to start a new sweep from an
  existing case.

Different shapes because they serve opposite directions of data flow. (This is why adding
a new namelist field, like `cice_params`, means touching **both** `scan.py` — to store
it — *and* `query.py`'s `_BASE_FIELD_ORDER` — to forward it through the bridge.)

### Why some namelist values are top-level and some are subgroup dicts

`user_nl_cam`, `user_nl_clm`, `user_nl_cice` are sibling source files, but their individual
variables sort into two buckets:

- **Promoted to top level** (`ncdata`, `finidat`, `fsurdat`): a fixed set of high-value
  scalars the tooling *reasons about* — `ncdata` gets its pressure/level string parsed out;
  `finidat`/`fsurdat` are config-gated. They earn first-class keys because code does things
  with them.
- **Kept in subgroup dicts** (`carma_params`, `volc_params`, `cice_params`): open-ended
  bags of namelist values the tooling forwards **verbatim** without interpreting. Bundling
  keeps them extensible (prefix-based groups accept new keys with no code change) and keeps
  the top level uncluttered.

### The one safety invariant to keep in your head

`build.py generate` produces self-contained, reviewable shell scripts and **touches nothing
on disk**; `build.py make` runs them. Every destructive `datamgr.py` op defaults to preview
and needs `--execute`. The toolchain is trustworthy precisely because *generating a recipe*
and *running it* are separate steps.

---

## Quick CLI reference

```bash
# Build scripts
python build.py generate experiment_matrix.yaml        # generate shell scripts into build_scripts/
python build.py --scripts-dir scripts/ generate matrix.yaml
python build.py generate --list                        # list experiment matrices
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

# Disk management — reporting and retirement (destructive ops: preview by default, --execute to act)
python datamgr.py report                                # scan all cases, write usage.yaml
python datamgr.py report my_case                        # single case, print only, no yaml write
python datamgr.py report --cached                       # read usage.yaml, no disk scan
python datamgr.py avg my_case --info
python datamgr.py avg my_case --last 10 --execute
python datamgr.py retire my_case --execute                                          # tombstone only
python datamgr.py retire my_case --keep-config --keep-years 5 --keep-restarts --execute
python datamgr.py retire my_case --purge --execute                                  # complete erasure

# Run lifecycle management — check status, continue/restart, purge/move files (all destructive ops preview by default)
python runmgr.py check                                 # probe SLURM, show case status
python runmgr.py check --info                         # include CESM event log tail
python runmgr.py check --energy                       # include energy balance (TS/FSNT/FLNT)
python runmgr.py continue case1 --set STOP_N=10 --set RESUBMIT=5  # CONTINUE_RUN=TRUE + xmlchange
python runmgr.py restart case1 --set RUN_STARTDATE=0001-01-01 --execute  # CONTINUE_RUN=FALSE + xmlchange
python datamgr.py clean purge-bld my_case --execute
python datamgr.py clean purge-bld my_case --logs-only --execute
python datamgr.py clean purge-restarts my_case --keep 1 --execute
python datamgr.py clean purge-hist my_case --models atm --execute
python datamgr.py clean purge-hist --prefix exovolc --models all --execute  # bulk: all matching cases, all hist components
python datamgr.py clean purge-logs my_case --execute
python datamgr.py clean move-hist my_case --models atm --execute

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

Start from `exp_matrices/experiment_matrix.example.yaml`. Each case inherits all `base` values; any key in a case dict overrides the base.

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
| `ncdata` | Bypasses automatic IC file lookup. May be a bare filename (placed under the config-type IC dir) or an absolute/dir-bearing path (used verbatim by `resolve_ic_path` — never re-prefixed). |
| `exo_n2bar_explicit` | Patches `exo_n2bar` with an explicit value. Required above 1 bar for clone builds; for newcase builds N2 is always explicit (this value if present, else `target − sum(specified gases)`). |
| `account` | `#SBATCH --account` written to `${CASE}.run` (typically in `base`) |
| `job_name` | `#SBATCH -J` written to `${CASE}.run` (typically per-case) |
| `carma_params` | Nested dict → `user_nl_cam` (append in newcase, upsert in clone) |
| `volc_params` | Same as `carma_params` |
| `nl_cam_params` | Catch-all for any other `user_nl_cam` keys (e.g. `nhtfrq`, `mfilt`, tuning knobs) |
| `run_type` | `startup` (default), `branch`, or `hybrid` |
| `run_refcase` | Reference case name for branch/hybrid |
| `run_refdate` | Reference date string, e.g. `0021-01-01` |
| `run_startdate` | Start date for the run, e.g. `0001-01-01` (startup/hybrid only; optional) |
| `brnch_retain_casename` | `'true'` or `'false'`; passed to `BRNCH_RETAIN_CASENAME` xmlchange |

---

## YAML registry structure

Written by `scan.py`, read by `query.py` and `datamgr.py`. Groups are defined by `_REGISTRY_GROUPS` in `scan.py`.

```yaml
cases:
- meta:          # case_name, casedir, config_type, exort_pkg, nlev, inspect_date,
                 # ncdata, ncdata_pressure_str, ncdata_levels, clm_finidat, clm_fsurdat,
                 # som_pop_frc_file, run_type, run_refcase, run_refdate, brnch_retain_casename,
                 # run_startdate
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
  rest_option: nyears
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
| `parse_run_type_fields(xmlpath)` | `env_run.xml` | dict with `run_type`, `run_refcase`, `run_refdate`, `brnch_retain_casename`, `run_startdate` | Falls back to line scan if ElementTree fails |
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

**`GAS_BAR_PARAMS`** — the radiatively-active gas bars (`exo_co2bar`, `exo_ch4bar`, `exo_c2h6bar`, `exo_nh3bar`, `exo_cobar`, `exo_h2bar`, `exo_o2bar`; excludes N2). Used by `render_exoplanet_mod` for newcase clean-slate zeroing and the N2 fill sum.

**`render_exoplanet_mod(template_path, spec, is_clone)`** — renders gas/parameter values into the F90 template. Newcase (`is_clone=False`): every `GAS_BAR_PARAMS` gas absent from the spec is forced to `0.0`, and `exo_n2bar` is always written as an explicit number (`exo_n2bar_explicit` or `compute_pstd_from_spec − sum(specified)`). Clone (`is_clone=True`): only matrix-named params are patched; unspecified gases and N2 keep the source template's values.

**`resolve_ic_path(ic_file, config_type, paths)`** — bare filename → prepend `{exocam_root}/cesm1.2.1/initial_files/{config_type}/`; path containing `/` (absolute or dir-bearing) → verbatim. Called by both build-script generators to set `ncdata`.

**`_fortran_value`** — formats numeric RHS at 12 sig figs (`%.12g`, or `%.10e` for very small/large magnitudes) so the `exo_n2bar` fill precision survives; appends `_r8`.

**`REQUIRED_FIELDS`** (newcase): `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_option`, `rest_n`, `resubmit`, `ntasks`

**`REQUIRED_FIELDS_CLONE`**: `clone`, `stop_option`, `stop_n`, `rest_option`, `rest_n`, `resubmit`, `ntasks`

**Namelist value formatting** (`_format_nl_value`): `bool` → `.true.`/`.false.`; `int` → bare integer; `float` → `%g` with decimal ensured; `str` Fortran logical → pass through; `str` numeric → coerced; `str` other → single-quoted. Note: `bool` is checked before `int` because Python's `bool` is a subclass of `int`.

**Newcase vs clone namelist behavior:**
- Newcase: plain `echo >> user_nl_cam` (template is fresh, no existing keys)
- Clone (`_nl_upsert_lines`): replace-or-append upsert (clone copies `user_nl_cam`/`user_nl_cice` verbatim from source, so a plain append would duplicate keys). Emits `if grep -qE "^[[:space:]]*KEY[[:space:]]*=" T; then sed -i -E "s|^[[:space:]]*KEY[[:space:]]*=.*|KEY = VAL|" T; else echo "KEY = VAL" >> T; fi`. The anchored pattern tolerates leading whitespace and any spacing around `=`, ignores trailing inline comments, and avoids matching a different key that contains this one as a substring. The explicit `if/then/else` ensures the append branch fires only when the key is genuinely absent — never as a fallback for a `sed` that exited non-zero (the old `grep && sed || echo` chain appended a duplicate on any `sed` failure).

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
`clone`, `config_type`, `exort_pkg`, `nlev`, `mach`, `stop_option`, `stop_n`, `rest_option`, `rest_n`, `resubmit`, `ntasks`, `account`, `run_type`, `run_refcase`, `run_refdate`, `brnch_retain_casename`, `run_startdate`.

`exort_pkg *` warning: printed to stderr after matrix output (visible at end). Suppressed in `--clone` mode since RT is inherited from the clone source.

`cmd_search` appends a CONFIG column (showing `yes` or `-`) only when at least one result row contains `config_saved` — this is present in `retired.yaml` searches, absent in `active.yaml` searches.

---

## datamgr.py — key constants and retire tiers

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

**`clean` subcommand group (surgical output housekeeping):**
- `clean purge-bld` — delete build objects from `rundir/<case>/bld/`
- `clean purge-restarts` — trim old restart sets in `archive/<case>/rest/`
- `clean purge-hist` — delete history files from `archive/<case>/<model>/hist/`
- `clean purge-logs` — delete logs from `archive/<case>/<model>/logs/` and `caseroot/<case>/logs/`
- `clean move-hist` — move history files to long-term storage

All `clean` subcommands: preview mode by default, `--execute` required to act. Finer-grained than `retire` (which acts on a whole case); all take explicit case names **or** a `--prefix` bulk filter (mutually exclusive; enforced by `_require_cases`). Under `--execute` they print every case's preview, then ask a **single batch `[yes/no]`** covering the whole set (not one prompt per case) — driven by `_run_batch()` over `batch_confirm()`. In preview mode a single `preview_hint()` reminder prints after the last `[preview]` block.

`--models` (on `purge-hist`, `purge-logs`, `move-hist`) accepts component names or the literal `all`, which expands to the verb's default component set (`HIST_MODELS` — `rest/` excluded). `purge-hist` additionally requires `--keep-years N` or `--models` (a guard against nuking all history by omission); `--models all` satisfies it.

---

## manage_utils.py — shared utilities

Extracted from `manage.py` to support both `datamgr.py` and `runmgr.py`.

**Constants:**
- `ARCHIVE_MODELS` — all archive subdirectories: `['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']`
- `HIST_MODELS` — components with history/logs subdirs: `ARCHIVE_MODELS` minus `'rest'`
- `MODEL_STEM` — CESM model name per archive component (e.g. `'atm'` → `'cam'`)
- `AVG_HIST_DEFAULT_MODELS` — default models when `--models` is unspecified in avg/retire: `['atm', 'lnd', 'ice']`

**Path and config helpers:**
- `load_paths(args)` — merge `config_registry.yaml` paths with CLI overrides
- `discover_cases(paths)` — list case names present in caseroot/rundir/archive

**Disk helpers:**
- `dir_size_bytes(path)` — recursive disk usage
- `fmt_size(nbytes)` — human-readable size string
- `list_files_with_size(directory)` — files directly in directory with total size

**History filtering (shared by purge-hist and retire):**
- `_hist_year(filename)` — extract year string from hist filename (e.g. `'0050'` from `case.cam.h0.0050-01.nc`)
- `_hist_keep_years_filter(archive_path, models, keep_n)` — partition files by year, return keep/delete lists

**Restart management:**
- `restart_sets(case, paths)` — list restart sets as `(date_str, path)` tuples, sorted

**Safety helpers:**
- `confirm(prompt, execute)` — per-case gate: show preview or prompt for yes/no. Still exported; no longer used by `datamgr.py` clean verbs (they use `batch_confirm` instead).
- `batch_confirm(action, n)` — single `[yes/no]` gate over a whole case set (`"<action> N case(s)? [yes/no]"`); EOF/interrupt → no. The clean-verb counterpart to runmgr's `_batch_confirm`.
- `preview_hint(execute)` — print one `(preview only — rerun with --execute …)` reminder; no-op under `--execute`.
- `_require_cases(all_cases, args)` — resolve cases from explicit `args.cases` **or** an `args.prefix` bulk filter (mutually exclusive; errors if neither given — no `--all` flag for destructive ops). Shared by every `datamgr.py` destructive verb including all `clean` verbs and `retire`.

---

## runmgr.py — run lifecycle management

Subcommands: `check`, `xml`, `submit`, `continue`, `restart`. (Surgical output purge/move lives in `datamgr.py clean` — see above.)

**`check` subcommand:**
- Probes SLURM for running jobs (`squeue --name <case> -h`)
- Parses `CaseStatus` file (last non-blank line only), extracts event and timestamp
- Optional `--info`: appends last 5 lines of `${CASE}.log` event file
- Optional `--energy`: computes global-mean energy balance from 12 most recent cam.h0 files using ncra + netCDF4
- Columnar output: case name, status tag, timestamp

**Energy balance computation (`_energy_balance`):**
- Collects last 12 non-avg `cam.h0` files from `archive/<case>/atm/hist/`
- Averages with `ncra` into temp file
- Extracts `TS` (surface temperature), `FSNT` (top-of-atmosphere solar), `FLNT` (top-of-atmosphere LW)
- Computes area weights: `w = cos(lat)` normalized to sum to 1.0
- Returns `(ts_mean, fsnt_mean, flnt_mean, n_used)` or `(None, None, None, 0)` on failure
- Cleans up temp file in finally block even on early return

**`continue` subcommand:**
- Sets `CONTINUE_RUN=TRUE` and sbatches the run script
- Use `--set VAR=VALUE` (repeatable) to apply arbitrary `xmlchange` calls before submitting
- Example: `--set STOP_N=10 --set RESUBMIT=5`
- Status gating: RUNNING/RESUBMITTED → hard block; COMPLETE → silent; others → soft-warn with per-case confirmation
- Preview mode by default; `--execute` required to submit

**`restart` subcommand:**
- Sets `CONTINUE_RUN=FALSE` and sbatches the run script (for rerunning from scratch)
- Use `--set VAR=VALUE` (repeatable) to apply arbitrary `xmlchange` calls before submitting
- Example: `--set RUN_STARTDATE=0001-01-01 --set RESUBMIT=9`
- Status gating: RUNNING/RESUBMITTED → hard block; COMPLETE → silent; others → soft-warn with per-case confirmation
- Preview mode by default; `--execute` required to submit

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

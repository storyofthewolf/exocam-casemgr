# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git workflow

- **Bug fixes, docs, small changes** → commit directly to `main`. Do NOT branch,
  do NOT ask. If the prompt obviously describes a fix, just commit.
- **Significant feature changes, new features, or refactors** → before
  committing, ASK whether to create a new branch or commit to `main`. Do not
  branch automatically and do not assume.
- When in doubt, lean toward committing to `main`.

## What this is

ExoCAM case management tools — Python scripts that automate building, inspecting, and managing [ExoCAM](https://github.com/storyofthewolf/ExoCAM) simulation cases on HPC systems. ExoCAM is a fork of CESM 1.2.1 for exoplanet climate modeling. Scripts cover the full simulation lifecycle: YAML experiment matrix → CESM shell build scripts → YAML registry → disk management.

Target runtime: NASA Discover (SLURM HPC). Build scripts are generated locally, reviewed, then run on the cluster.

## Architecture

### Data flow

```
experiment_matrix.yaml
  + config_registry.yaml
       ↓
  build.py
       ↓
  scripts/<case>_build.sh    ← self-contained shell script: create_newcase/create_clone + build
                               (rendered exoplanet_mod.F90 embedded as inline heredoc)

CASE directories on HPC
       ↓
  build.py patch             ← in-place exoplanet_mod.F90 edit + <case>.build
                               (the only way to change a compiled-in parameter
                                without recreating the case)
       ↓
  scan.py
       ↓
  active.yaml                ← queryable YAML registry (active cases)
  retired.yaml               ← queryable YAML registry (retired cases)
       ↓
  query.py                   ← search registry, export experiment matrices

cases/ + rundir/ + archive/ on HPC
       ↓
  datamgr.py clean            ← surgical output housekeeping: purge-bld, purge-restarts,
  │                             purge-hist, purge-logs, move-hist
  datamgr.py                  ← disk reporting, averaging, retirement lifecycle
  diff.py                    ← SourceMods diff before retiring
```

### Module roles

- **`parse_utils.py`** — pure parsing primitives; no filesystem side effects (invariant)
- **`build.py`** — validates experiment matrix, generates self-contained shell build scripts; `generate --verify` checks matrix coherency (value types + netCDF file existence) without generating; `patch` edits `exoplanet_mod.F90` in place in existing cases and rebuilds
- **`scan.py`** — walks CASE directories, extracts metadata, writes grouped YAML registry
- **`query.py`** — searches registry, exports experiment matrices
- **`datamgr.py`** — case data management: `report` (disk survey), `clean` (surgical purge/move), `avg` (permanent N-year averaging), `retire` (end-of-life archival)
- **`manage_utils.py`** — shared utility layer imported by `datamgr.py`, `runmgr.py`, and `build.py`: constants (`ARCHIVE_MODELS`, `HIST_MODELS`, `MODEL_STEM`, `AVG_HIST_DEFAULT_MODELS`), `load_paths()`, disk helpers (`dir_size_bytes`, `fmt_size`, `list_files_with_size`), `discover_cases()`, hist-year filtering, `restart_sets()`, `confirm()`, `batch_confirm()` (the single `[yes/no]` gate over a whole case set, shared by datamgr's clean/retire verbs and runmgr's run-control verbs), `preview_hint()` (one trailing `--execute` reminder), `_require_cases()` (explicit-names-or-`--prefix` selection with mutual-exclusion + no-`--all` guard, shared by every `datamgr.py` destructive verb), `submit_case()` (the single `sbatch` code path, shared by `runmgr.py submit` and `build.py make --send-it`)
- **`runmgr.py`** — run control tool; `check` subcommand (CaseStatus parsing, SLURM probe, optional hist/energy info); `xml` subcommand (ad-hoc `--query VAR` / `--change VAR=VALUE` over a case set — no CONTINUE_RUN forcing, no sbatch; the only way to inspect/edit XML without launching a run); `submit` subcommand (sbatch a built case as-is, no xmlchange — the launch step after `build.py make`; requires `<case>.run`); `continue` subcommand (set CONTINUE_RUN=TRUE, update STOP_N/RESUBMIT, sbatch); `restart` subcommand (set CONTINUE_RUN=FALSE, apply arbitrary `--set VAR=VALUE` xmlchange calls, sbatch). Shared helpers `_resolve_cases()` (explicit-names-or-`--prefix`, no `--all`), `_parse_set_pairs()`, `_apply_xmlchange()` (the single `./xmlchange` code path), and `_probe_status()` (CaseStatus + SLURM probe) back all the run-control subcommands.
- **`diff.py`** — SourceMods diff tool; used before retiring to check for custom Fortran worth preserving
- **`config_registry.yaml`** — machine-specific paths, CESM config per config_type, IC file table; must be edited per user/machine

### Key non-obvious behaviors

- `scan.py --update` **clobbers** the registry — does not merge with pre-existing content. Live rows take precedence over archive rows on name collision.
- `build.py generate` never executes scripts; `build.py make` runs them (with confirmation prompt). `make` **builds but does not submit** — submission is a separate step (`runmgr.py submit`, or `make --send-it` to fold both together). The run verbs are distinct: `submit` (launch a built case as-is, no xmlchange), `continue` (CONTINUE_RUN=TRUE), `restart` (CONTINUE_RUN=FALSE + fixes). `xml` is the odd one out — it changes/queries XML but **never** launches a job, so it has no `submit`/`continue`/`restart` semantics; on `--change --execute` it uses the same single batch `[yes/no]` gate as the other verbs (RUNNING/RESUBMITTED cases are flagged in the preview, not hard-blocked). See "Run-control gating" below.
- `build.py make` accepts explicit `NAME` positionals (bare case name or full `*_build.sh` filename) to run a named subset, in addition to `--prefix`. If neither `NAME` args nor `--prefix` nor `--all` is given, it just **lists** the scripts in `scripts-dir` and exits — it does not run anything. `--all` is required to intentionally run every script in `scripts-dir` at once, mirroring the "no implicit --all" convention used by destructive `datamgr.py`/`runmgr.py` subcommands.
- `datamgr.py` status-gates like the run-control verbs: `retire` **hard-blocks** RUNNING/RESUBMITTED cases (probe via `runmgr._probe_status`, lazily imported — the same pattern as `build.py patch`); `clean purge-restarts`/`purge-hist`/`move-hist` **flag** active jobs in the preview without blocking. `retire` also hard-blocks when a preserve target already exists in long-term (a prior interrupted retire): `shutil.move` onto an existing path would silently overwrite a file or nest a directory (`rest/<date>/<date>`). `move-hist` never overwrites — files already present in long-term are skipped and left in the archive. `avg --last` overwrites an existing avg file (`ncra -O`), disclosed in the preview, and gates `--execute` behind the same single batch `[yes/no]` as the clean verbs. `purge-restarts --keep 0` and `purge-hist --keep-years 0` are explicit delete-alls with an honest "keeping NONE" preview; negative values are rejected.
- All destructive `datamgr.py` operations (including `clean`) default to **preview mode**; `--execute` required to act. Under `--execute` the `clean` verbs ask a **single batch `[yes/no]`** covering the whole set (`Delete … for N case(s)? [yes/no]`), not one prompt per case — matching `retire` and the runmgr run-control verbs. The two-pass flow (print all previews → one confirm → act) is driven by `_run_batch()` (datamgr.py) over `batch_confirm()` (manage_utils.py). In preview mode a single `preview_hint()` reminder prints after the last `[preview]` block. Answering no → `Aborted.`, nothing touched.
- `exoplanet_mod.F90` is embedded inline in each build script via heredoc — no staging directory.
- In clone mode, `user_nl_cam` is copied verbatim from the clone source, so namelist params use **upsert** semantics (grep/sed/echo) rather than plain append, to avoid duplicate keys.
- `exort_pkg` ending in `*` signals custom RT copied into SourceMods. In newcase mode this is a validation error; in clone mode it is allowed and triggers `_build_usr_src_fix_block` to rewrite the inherited `-usr_src` path.
- `runmgr.py check` defaults to **all discoverable cases** when given no names — unlike every destructive subcommand, which requires explicit names.

---

## runmgr.py check — internals

### CaseStatus parsing

`$caseroot/<case>/CaseStatus` is read and only the **last non-blank line** is used. Each line is parsed as `<event> <YYYY-MM-DD> <HH:MM:SS>` by splitting off the last two whitespace tokens; everything before is the event prefix.

Segment history counts (run ok/failed, first start, last success) are intentionally **not reported**. CaseStatus is inherited verbatim when a case is cloned, so cumulative counts from the full file are unreliable for clone cases.

Event prefix → status label mapping (matched by `str.startswith`):

| Event prefix | Status label |
|---|---|
| `run SUCCESSFUL` | `COMPLETE` |
| `run FAILED` | `FAILED` |
| `run started` | `RUNNING` |
| `build complete` | `BUILT` |
| `cesm_setup` | `CLEANED` (covers `cesm_setup -clean`) |
| (anything else) | `UNKNOWN` |

Output per case is a single columnar line: case name left-justified to the longest name in the current output set, status tag `[STATUS]` left-justified to 15 characters, then the timestamp. All results are collected before printing so `max_name_len` is known. Example:

```
cam_mixed_fv_modern               [COMPLETE]       2026-03-07 13:00:02
cam_mixed_fv_modern_eruption      [BUILT]          2026-05-13 02:23:19
cam_mixed_fv_modern_eruption_it2  [FAILED]         2026-05-09 16:24:59
```

If `CaseStatus` is missing (no caseroot dir), status is shown as `NO_CASEDIR`.

### SLURM probe

When the last CaseStatus event starts with `run started` or `run SUCCESSFUL`, `squeue --name <case> -h` is run as a subprocess:
- **Job found + last event was `run SUCCESSFUL`** → status shown as `RESUBMITTED`
- **No job + last event was `run started`** → `cases/<case>/run.out` is checked (`_run_out_walltimeout`):
  - timeout found → status shown as `WALLCLOCK`
  - otherwise → status shown as `RUNNING?` (started but no longer queued — likely crashed without writing to CaseStatus)
- **`FileNotFoundError`** (squeue not in PATH) or **non-zero exit code** → probe silently omitted, original status label retained

**The probe matches on the SLURM job name (`-J`), so per-case correctness depends on `-J` equalling the case name.** `build.py` (`_build_run_script_block`) therefore defaults `#SBATCH -J` to the full case name (`${CASE}`) for every build — an explicit matrix `job_name` overrides it. Without this, CESM truncates `-J` to a short, non-unique label (e.g. all of `exocam_ML_grp3_pt*` collapse to `exocam_M`); `squeue --name <full_case>` then matches nothing and running cases show as `RUNNING?`. **Build scripts generated before this default must be regenerated** — the fix only affects newly rendered `.run` patch blocks, not `.run` files already on the HPC.

### WALLCLOCK detection (run.out)

A SLURM wall-clock kill never updates `CaseStatus` (it would otherwise read `RUNNING?`), but it leaves a `CANCELLED ... DUE TO TIME LIMIT` line in `cases/<case>/run.out`. `_run_out_walltimeout(run_out_path)` resolves the `RUNNING?` ambiguity:

1. `run.out` is **appended to on every run attempt**, so only the segment after the **last** `CSM EXECUTION BEGINS HERE` line is examined — a timeout in an earlier segment followed by a fresh success must not register.
2. Returns `True` if any line in that last segment contains **both** `CANCELLED` and `DUE TO TIME LIMIT`. Missing/unreadable file → `False`.

`WALLCLOCK` is a probe-derived label (like `RESUBMITTED`/`RUNNING?`), not a `CaseStatus` event mapping. It is non-`COMPLETE`, so the run-control verbs (`continue`/`restart`/`submit`) **flag** it in the per-case preview (`<- not COMPLETE`/`<- not BUILT/COMPLETE`), the same as `FAILED` — appropriate for a timed-out case the user wants to relaunch. Wired into all four probe sites: `check`, `continue`, `restart`, `submit`.

## Run-control gating (continue / restart / submit / xml --change)

All four run-control verbs use the **same double-gate ergonomics as `build.py make`**, so `--execute` behaves consistently across the package:

1. **Gate 1 — `--execute`.** Without it, the verb prints the per-case preview and exits (`(preview only — rerun with --execute …)`).
2. **Gate 2 — a single batch `[yes/no]`.** With `--execute`, after the preview the verb asks **one** confirmation (`Continue … and submit N case(s)? [yes/no]`, `Submit N case(s)?`, `Apply XML changes to N case(s)?`) covering the whole set, then acts. Answering no → `Aborted.`, nothing submitted. This replaced the old per-case soft-block prompts.

Status handling within the preview:
- **RUNNING / RESUBMITTED** → **hard block**: dropped from the set, never submitted (a job is already active).
- **COMPLETE** (and **BUILT** for `submit`) → the normal, unflagged case.
- **anything else** (FAILED, WALLCLOCK, RUNNING?, …) → **flagged** in the preview line (`<- not COMPLETE`), but not separately prompted — the single batch confirm covers it.

`xml --query` (no `--change`) is read-only and never gates. `batch_confirm(action, n)` in `manage_utils.py` is the shared gate helper (runmgr's former private `_batch_confirm` copy was removed); `submit_case()` (in `manage_utils.py`) is the single `sbatch` path used by all three submitting verbs — `continue`/`restart` no longer carry their own inline `subprocess.run(['sbatch', …])` block. `_apply_xmlchange` raises `RuntimeError` (caught per-case) when `./xmlchange` is missing, so a bad case dir reports an error instead of crashing the batch.

### --energy computation

1. List `*.cam.h0.*.nc` files in `$archive/<case>/atm/hist/` excluding filenames containing `"avg"`. Sort lexicographically (= chronological for CESM date strings).
2. Take the last 12 (or fewer, with a warning printed).
3. Run `ncra <file1> ... <fileN> /tmp/runmgr_energy_<case>.nc`. If `ncra` is not found, print a warning and skip.
4. Open the temp file with `netCDF4`. Extract `TS`, `FSNT`, `FLNT`. If any variable is missing, print a warning and skip.
5. Compute area weights: `w = cos(lat * π/180)`, broadcast across the lon dimension, normalize to sum to 1.
6. Compute global means via `sum(data * w2d)`.
7. Print `Last Nmo:  TS = 287.3 K    Etop = +0.8 W/m²` (Etop = FSNT_mean − FLNT_mean, signed, 1 decimal).
8. The temp file is always deleted in a `finally` block, even on error.

---

## build.py patch — in-place SourceMods edit + rebuild

```
build.py patch --prefix noO3_grp3 --set exo_convect_plim=5.0 --execute
build.py patch case_a case_b --set do_exo_rt_clearsky=true --execute
```

Rewrites the matching `parameter ::` line in `<case>/SourceMods/src.share/exoplanet_mod.F90` and runs `./<case>.build`. **No `clean_build`** — for a file already present under `SourceMods`, the CESM dependency scan picks up the change. (Other scenarios do require a clean rebuild; `patch` does not cover them.)

- `--set VAR=VALUE`, repeatable, over any `EXO_PARAMS` member. Validated against `PARAM_TYPES` — the same tags `generate --verify` enforces — before anything is touched.
- Case selection via `_require_cases()`: explicit names or `--prefix`, mutually exclusive, no `--all`. Same convention as every destructive verb.
- Preview by default; `--execute` adds a single batch `[yes/no]` over the whole set.
- Reuses `_RE_PARAM_LINE` + `_fortran_value`, so declaration spacing and trailing `!!` comments survive. Commented-out `parameter` lines are never touched.
- **Gas bars warn.** `exo_n2bar` was computed at generate time as `target − sum(gases)` and is *not* recomputed here, so patching a gas shifts total surface pressure by the delta. Harmless at trace (ppm) magnitudes — the model self-adjusts — but a real composition change should go through `generate`.
- `RUNNING`/`RESUBMITTED` are **flagged with a count before the confirm, not blocked**. Recompiling a queued case so its next resubmit segment picks up the new binary is a deliberate, supported use. (Contrast the run-control verbs, which hard-block these.)
- A failed `.build` is reported per-case without aborting the batch. The source edit has already landed, so rerunning `<case>.build` after fixing the cause suffices — no re-patch needed.
- Experiment matrices are **not** updated; a reminder prints. Close the drift by hand or a future `generate` silently reverts the change.

**Known wart:** `render_exoplanet_mod` has the same whitespace bug `patch_exoplanet_mod` fixes — `_RE_PARAM_LINE`'s value group `([^!\n]+)` greedily eats the spaces before a trailing `!!` comment. Invisible in `generate` because it renders into a throwaway heredoc from a pristine template. Fixing it would change generated-script bytes for every case.

---

## build.py generate --verify

`build.py generate <matrix> --verify` checks matrix coherency and **generates no scripts** (exits 1 if any case fails). It catches transposition mistakes — wrong value types, missing input files — before they reach the rendered build scripts. Beyond those hard checks it raises a small number of **scientific-consistency warnings** (see below), which never fail a case or affect the exit code — `--verify` asks, it does not presume to know the science.

Three checks per resolved case spec (`verify_case` in `build.py`), returning `(errors, warnings, notes)`:

1. **Type tags** — every matrix value with a `PARAM_TYPES` entry is checked against `bool` / `int` / `real` / `str` (`_check_type`). `bool` accepts python bool or the strings `true`/`false`; `int` accepts ints or integral-valued numerics (rejects `26.5`); `real` accepts any numeric; `str` rejects numeric/bool. `PARAM_TYPES` is the authoritative table — add new params there. (A python bool is explicitly rejected for int/real/str since `bool` is an `int` subclass.)
2. **NetCDF file existence** — each field in `NCFILE_FIELDS` (`ncdata`, `exo_solar_file`, `som_pop_frc_file`, `finidat`, `fsurdat`) is resolved to a path using **the same logic as its build block** (`ncdata` → `resolve_ic_path`; `finidat`/`fsurdat` → `cam_land_fv` IC dir; solar/pop_frc → verbatim), then existence-checked locally.

Existence checking assumes `--verify` runs on the HPC, where every input file should live. A var-free path whose file **or its parent directory** is absent is a hard **failure** (`file not found` / `directory not found`) — a missing dir is the common symptom of a mistyped/transposed path. The only SKIPPED (`·` note) case is a path that still contains an **unexpanded `$VAR`** (the env var isn't set, so it genuinely can't be resolved). Config-restricted fields present under the wrong `config_type` (e.g. `finidat` on an aqua config) are noted as ignored, not checked.

3. **Scientific consistency** (`_verify_o2_ozone`, `_verify_ozone_convect_plim`) — O2 vs ozone, and ozone vs `exo_convect_plim`. **Warnings only.** See "Composition inheritance → `--verify` consistency warnings" above for the rules.

Verify mode runs the type/nc checks **before** `validate_case`, because `validate_case` coerces values to float (via `compute_pstd_from_spec`) and would raise on a mistyped numeric; `--verify` reports a clean `type:` message instead. `validate_case` only runs if types pass. Output: `OK:` / `FAIL:` per case, `-` lines for errors, `!` lines for warnings, `·` lines for skip notes, then a summary count. A case with warnings but no errors still reports `OK` and exits 0.

---

## Config types

| `config_type` | Description |
|---|---|
| `cam_aqua_fv` | Aquaplanet, finite-volume dynamics |
| `cam_land_fv` | Land/continent, finite-volume |
| `cam_mixed_fv` | Mixed ocean/land, finite-volume |
| `cam_aqua_se_ne5` / `ne16` | Aquaplanet, spectral-element dynamics |

SE configs strip the `_ne5`/`_ne16` suffix when resolving SourceMods template directories.

Config-conditional logic (present in both `build.py` and `scan.py`):
- `cam_land_fv`, `cam_mixed_fv` → parse/sed `user_nl_clm` for `finidat`/`fsurdat`
- `cam_aqua_fv`, `cam_aqua_se_*`, `cam_mixed_fv` → parse/sed `user_docn.streams.txt.som` for SOM forcing file

`_infer_config_type()` in `scan.py` decides config_type from SourceMods subdirectory presence:
- `src.cice` + `src.clm` → `cam_mixed_fv`
- `src.cice` only → `cam_aqua_fv`
- `src.clm` only → `cam_land_fv`
- neither → `unknown`

**This decision tree is the authoritative source for config_type — it must stay consistent with `config_registry.yaml` entries.**

---

## Composition inheritance — the matrix is the sole arbiter

**For a newcase, the experiment matrix is the only source of atmospheric composition. Nothing is inherited from the ExoCAM config templates.** Silence in the matrix uniformly means *no O2, no O3*.

This is enforced in two places, by two different mechanisms, because composition arrives through two different files:

| Parameter | Lives in | Silence in matrix → |
|---|---|---|
| `GAS_BAR_PARAMS` (`exo_o2bar`, `exo_co2bar`, …) | `exoplanet_mod.F90` | forced to `0.0` (`render_exoplanet_mod`) |
| `prescribed_ozone_file` / `_datapath` | `user_nl_cam` | forced to the zeroVMR file (`generate_shell_script`) |

The ozone default is `{exocam_root}/cesm1.2.1/initial_files/cam_aqua_fv/ozone_1.9x2.5_L26_zeroVMR.nc` — a single shared file used by *every* config_type. `ZERO_OZONE_IC_DIR` / `ZERO_OZONE_FILE` in `build.py` are the only constants to change if the ExoCAM `initial_files` tree is reorganized. The datapath is derived from `paths.exocam_root`, never hardcoded.

The two ozone keys are defaulted **as a unit**: a matrix naming either one owns the whole ozone setting and must supply its own datapath. `prescribed_ozone_cycle_yr` / `_name` / `_type` ship with the namelist and are never touched.

**Why this matters.** `cam_mixed_fv`'s shipped `namelist_files/user_nl_cam` carries modern-Earth ozone. Before this rule, a matrix mentioning neither `exo_o2bar` nor ozone produced a case with **no O2 and full ozone** — incoherent, since ozone is photochemically produced from O2, and silently so. The config templates deliberately retain their per-config defaults (aqua/land neutral, mixed Earth-like); those serve users driving ExoCAM by hand, and casemgr simply ignores them.

**Clone is exempt.** `generate_clone_script` never applies these defaults. A clone preserves its source case's composition — that is the point of cloning. See "Pressure and N2 handling" below for the corresponding `is_clone` split in `render_exoplanet_mod`.

**Consequence:** matrices written before this rule that relied on inheriting Earth-like ozone will produce no-ozone cases when regenerated. Audit before regenerating.

### `--verify` consistency warnings

`generate --verify` raises **warnings** (never failures; exit code unaffected) for combinations that are scientifically contradictory. `_effective_ozone_file()` models the newcase default, so a matrix silent on ozone is checkable rather than unknown:

- `exo_o2bar == 0.0` with a non-zeroVMR ozone file → ozone without its precursor.
- `exo_o2bar > 0.0` with a zeroVMR file (**including by default**) → O2 without the ozone it would produce.
- ozone present and `exo_convect_plim < 4.e3` Pa → convection reaching the stratosphere is a numerical stability hazard. This is a **floor, not an equality**: values above `4.e3` merely clamp convection lower and are safe. Without ozone the parameter is freely tunable and nothing is warned.

Detection keys on the **absence of the `zeroVMR` substring**, never on the stock ozone filename — that tag drifts between input datasets; the zeroVMR convention is stable.

---

## Pressure and N2 handling

`render_exoplanet_mod` behaves differently for newcase vs clone (controlled by its `is_clone` flag):

- **Newcase (`is_clone=False`) — clean slate.** Every radiatively-active gas in `GAS_BAR_PARAMS` (CO2, CH4, C2H6, NH3, CO, H2, O2) that is *not* named in the matrix is forced to `0.0` — the template's modern-Earth defaults (e.g. `exo_o2bar = 0.2095`) must not leak in. N2 is **always** emitted as an explicit numeric fill: `exo_n2bar_explicit` if set, otherwise `compute_pstd_from_spec(spec) − sum(specified gases)`. The Fortran `1 - sum(others)` expression line is never relied upon for newcase.
- **Clone (`is_clone=True`) — preserve composition.** Only the gas params named in the matrix are substituted; all unspecified gases and N2 keep whatever the clone-source `exoplanet_mod.F90` has. `exo_n2bar` is patched only when `exo_n2bar_explicit` is set (high-pressure case); otherwise the source's expression line is left intact.

`_fortran_value` formats gas bar values at 12 significant figures (`%.12g`) so the full input precision of the N2 fill survives without float noise.

Total surface pressure (`compute_pstd_from_spec`) is the sum of individual gas bar values: `exo_n2bar_explicit + sum(others)` when explicit N2 is set, else `sum(others)` (defaulting to 1.0 for ≤1 bar). Pressure strings (e.g. `"1bar"`, `"0.1bar"`) are IC file table keys and must exactly match substrings in IC filenames.

---

## Common modification patterns

### Adding a new registry field
1. Add the key to the appropriate group's field list in `scan._REGISTRY_GROUPS`.
2. Add collection logic in `inspect_case()` in `scan.py`.
3. If it should appear in exported matrices, add it to `_BASE_FIELD_ORDER` in `query.py`.

### Adding a new config_type
1. Add an entry to `config_registry.yaml` under `cesm_config` (`res`, `compset`, `phys`).
2. Add IC file entries under `ic_files` in `config_registry.yaml`.
3. Verify `_infer_config_type()` in `scan.py` will assign the new type correctly.
4. Verify config-conditional blocks in `build.py` and `scan.py` cover the new type.

### Adding a new EXO_PARAMS parameter
1. Add the parameter name to the `EXO_PARAMS` set in `build.py`.
2. Ensure the corresponding `parameter ::` declaration exists in the `exoplanet_mod.F90` template.
3. If it should be scanned into the registry, add it to `inspect_case()` in `scan.py` and to `_REGISTRY_GROUPS`.
4. For `generate --verify` type checking, add it to `PARAM_TYPES` in `build.py` with its `bool`/`int`/`real`/`str` tag (params absent from `PARAM_TYPES` are not type-checked).
5. If it is a radiatively-active gas partial pressure, add it to `GAS_BAR_PARAMS` — otherwise newcase will not zero it (breaking "matrix is sole arbiter") and it will not be subtracted from the N2 fill (silently shifting total surface pressure). Adding to `GAS_BAR_PARAMS` also makes `build.py patch` warn when it is patched in place.

### Adding a new netCDF file field to `--verify`
1. Add `(field, resolver, restrict_config_types)` to `NCFILE_FIELDS` in `build.py`.
2. The resolver must mirror how the field's build block turns the value into a path (reuse `resolve_ic_path` / `_resolve_clm_field` / `_resolve_verbatim_field` or add one). Existence checking is otherwise automatic, including local/HPC skip handling.

### Extending `query.py export` output fields
1. Add the registry key to `_BASE_FIELD_ORDER` in `query.py`.
2. If it should appear in clone-mode sparse exports, add it to `_CLONE_BASE_FIELDS`.
3. If the registry key name differs from the matrix key name, add a rename entry to `_KEY_RENAMES`.

---

## Design invariants — do not violate

- `parse_utils.py` must remain free of filesystem side effects. It reads files via paths passed to it; it never discovers or writes files itself.
- All destructive `datamgr.py` operations (including `clean`) require `--execute`. Without it, every command only prints what it would do.
- No `--all` flag exists for destructive operations in either tool. Cases must be selected explicitly — either by name or via a `--prefix` bulk filter (mutually exclusive). `_require_cases()` (manage_utils.py) enforces this for every `datamgr.py` destructive verb, including all `clean` verbs and `retire`.
- `build.py generate` generates scripts but never executes them. `build.py make` runs them (with confirmation prompt).
- `build.py patch` is the **only** way to change an `exoplanet_mod.F90` parameter in an already-built case. `generate` cannot: it renders the F90 into a fresh build script whose first act is `create_newcase`/`create_clone`, which would recreate the case and destroy the run. These are Fortran `parameter` constants compiled into the binary — no `xmlchange` or `user_nl` path can reach them.
- For a newcase, the experiment matrix is the sole arbiter of atmospheric composition. Nothing is inherited from the config templates. See "Composition inheritance" above.
- `scan.py --update` clobbers the registry with exactly the cases scanned in the current run. It does not merge with pre-existing registry content.
- `exoplanet_mod.F90` is always skipped by `diff.py` (it is patched per-case and is not meaningful to diff).

---

## Known limitations

### Pre-existing registry rows lack run_type (scan.py)
Cases scanned before `run_type` support was added will not have `run_type`, `run_refcase`, `run_refdate`, or `brnch_retain_casename`. `query.py export` defaults `run_type` to `'startup'` for backward compatibility. Re-scan with `scan.py` to populate from live `env_run.xml`.

### Custom RT packages not supported in `create_newcase` builds (build.py)
`generate_shell_script` only supports RT packages via `-usr_src ../ExoRT/3dmodels/*`. Cases with custom RT copied into SourceMods must use clone mode (`create_clone` inherits SourceMods from the source case).

### `n68equiv.haze` registered as `n68equiv` (scan.py)
`scan.py` does not distinguish the `.haze` suffix in `-usr_src` paths. No fix planned — `n68equiv.haze` is expected to merge into `n68equiv` in a future ExoRT update.

### diff.py: non-standard ExoRT package directory paths
`build_exort_fileset` constructs the ExoRT reference as `{exort_root}/3dmodels/src.cam.{exort_pkg}/`. Experimental branches outside this path cause RT detection to silently return `{}` — affected files appear as `CASE ONLY`. Cases with non-standard RT are flagged with `*` in `query.py search` output. Future fix: add `paths.exort_pkg_dirs` map to `config_registry.yaml`.

---

## Session handoff — 2026-07-09

**`build.py patch`, newcase namelist upsert, and composition inheritance closed.** Three commits on `main`.

1. **`build.py patch` (`6a8c8cd`).** New subcommand: edits `exoplanet_mod.F90` in place in existing cases and reruns `<case>.build`. Motivated by a real incident — a batch of no-O3 cases was built with `exo_convect_plim = 4.e3` (the with-ozone value) because the matrix wasn't switched back. These are compiled-in Fortran `parameter` constants; no `xmlchange`/`user_nl` path reaches them, and `generate` would recreate the case. See the "build.py patch" section above.

2. **Newcase upserts `nl_cam_params` (`eafd3ef`, bug fix).** `_build_nl_append_block` asserted "the newcase namelist never contains these entries, so plain append is correct." True for CARMA/volc keys; **false** for `prescribed_ozone_*`, which `cam_mixed_fv`'s shipped `namelist_files/user_nl_cam` carries. A matrix setting `prescribed_ozone_file` produced *two* lines for the key, and the winner depended on the namelist reader's duplicate handling. Newcase now uses `_build_nl_upsert_block`, as clone already did. `_build_nl_append_block`/`_nl_append_lines` removed (no other callers).

3. **Composition inheritance closed (`b3d1da6`).** The matrix was already the arbiter for gases (newcase force-zeroes unspecified `GAS_BAR_PARAMS`) but *not* for ozone, which was inherited from the shipped namelist. A `cam_mixed_fv` matrix mentioning neither produced **no O2 and full ozone** — incoherent, and silent. Newcase now injects the zeroVMR no-ozone default unless the matrix names an ozone key. Silence uniformly means "no O2, no O3." `--verify` gained warnings for O2/ozone contradictions and the ozone/`exo_convect_plim` floor. Config templates keep their per-config defaults for users driving ExoCAM by hand; casemgr ignores them.

All three changes affect **newly generated scripts only** — existing `*_build.sh` on the HPC must be regenerated.

### Good starting points for next session
- **Audit existing matrices before regenerating.** Any that relied on inheriting Earth-like ozone will now produce no-ozone cases. This is the one migration hazard introduced by `b3d1da6`.
- **Regenerate the affected no-O3 build scripts** and confirm `prescribed_ozone_file` + `exo_convect_plim` agree in the rendered output.
- `render_exoplanet_mod` still eats the whitespace before trailing `!!` comments (`_RE_PARAM_LINE` group 4 is `([^!\n]+)`). `patch_exoplanet_mod` fixes it locally; the `generate` path does not. Fixing it changes generated-script bytes for every case — do it deliberately, alone.
- Existing handoff items still open: stale `build.py` module docstring; `nl_cam_params` recognized by `build.py` but not scanned by `scan.py` (now more visible — ozone settings live there); whether `datamgr.py avg` should move to `runmgr.py`; `confirm()` in `manage_utils.py` is dead code.

---

## Session handoff — 2026-07-08

**`datamgr.py clean` bulk selection + batch ergonomics (datamgr.py, manage_utils.py).** The `clean` verbs' help promised `--prefix`, but it was never wired in — a real `clean purge-hist --prefix …` errored `unrecognized arguments: --prefix`. Four related changes, all UX/ergonomics (no change to what gets deleted):

1. **`--prefix` on all clean verbs.** Added to `_add_destructive_args`; `_require_cases()` (manage_utils.py) now honors it with the same explicit-names-vs-`--prefix` mutual exclusion + no-`--all` guard `retire` already used. `retire`'s bespoke selection block now delegates to `_require_cases` (its duplicate `--prefix` arg removed); it still branches on `prefix_filter` for its batch-vs-per-case confirm.
2. **`--models all`.** `_add_models_arg` accepts the literal `all`; new `_resolve_models(args, default)` expands `all` (and the omitted case) to the verb's own default set — `HIST_MODELS` for purge-hist/purge-logs/move-hist (`rest/` excluded), `AVG_HIST_DEFAULT_MODELS` for avg. Satisfies purge-hist's `--keep-years`/`--models` guard without typing the full component list.
3. **Single batch confirm.** All five clean verbs (`purge-bld`, `purge-restarts`, `purge-hist`, `purge-logs`, `move-hist`) switched from per-case `confirm()` to the two-pass pattern retire/runmgr use: build deferred per-case closures during the preview pass, then under `--execute` ask **one** `batch_confirm()` (`Delete … for N case(s)? [yes/no]`) covering the whole set. New helpers: `_run_batch()` (datamgr.py) and `batch_confirm()` (manage_utils.py). `confirm()` is no longer used in datamgr.py (kept in manage_utils.py).
4. **Trailing `--execute` hint.** `preview_hint()` (manage_utils.py) prints one `(preview only — rerun with --execute …)` line after the last `[preview]` block; no-op under `--execute`.

All five commits are on `main` and pushed (`fb7d45c`..`b4a5d20`). Verified end-to-end against a temp-archive fixture: preview lists all cases with no prompts; `--execute`+`no` aborts leaving files intact; `--execute`+`yes` deletes the whole batch after one prompt.

### Good starting points for next session
- Existing handoff items still open: stale `build.py` module docstring; `nl_cam_params` recognized by `build.py` but not scanned by `scan.py`; whether `datamgr.py avg` should move to `runmgr.py`.
- `avg`'s `--models all` expands to `AVG_HIST_DEFAULT_MODELS` (atm/lnd/ice), i.e. "all that avg targets by default" — not the full 9-component set. Revisit if a literal-all-components meaning is wanted there.
- `confirm()` in manage_utils.py is now dead code (no caller in the package). Left in place; could be removed in a cleanup pass.

---

## Session handoff — 2026-07-02

**`build.py make` named-subset + explicit `--all` (build.py):** `make` previously only ran all scripts in `scripts-dir` or an `--prefix`-filtered subset, and a bare `make` with no filter would (after a confirmation prompt) build/submit *everything*. Two changes:

1. Added `names` positional (`nargs='*'`): `build.py make foo bar_build.sh baz --send-it` runs exactly those cases, resolved against `scripts-dir` (bare names get `_build.sh` appended). Unknown names abort before anything runs. `--prefix` is ignored if `names` are given.
2. Added `--all`: a bare `make` call (no `names`, no `--prefix`, no `--all`) now just **lists** the scripts in `scripts-dir` and exits — useful for browsing — instead of silently offering to build/submit everything. `--all` is required to intentionally run the full directory, matching the "no implicit --all" convention already used by destructive `datamgr.py`/`runmgr.py` subcommands (see "Design invariants" above, which is about those tools specifically — `make` is not destructive in the same sense but adopts the same UX guard since `--send-it` can submit many jobs at once).

### Good starting points for next session
- Existing handoff items still open: stale `build.py` module docstring; `nl_cam_params` recognized by `build.py` but not scanned by `scan.py`; whether `datamgr.py avg` should move to `runmgr.py`.

## Session handoff — 2026-06-19

Three generated-build-script bugs surfaced from a real batch (`gplfr_grp3.yaml`). All three fixes affect only **newly generated** `build_scripts/*.sh` — existing scripts on the HPC must be regenerated to benefit.

**1. Namelist upsert duplicate bug (`_nl_upsert_lines`, build.py):** The clone-mode `grep "KEY" && sed -i "s|KEY = .*|...|" || echo >>` idiom appended a duplicate (e.g. cice albedos) instead of replacing, because the `&& … || …` chain falls through to the append branch on *any* non-zero `sed` exit, and the unanchored single-space pattern was brittle. Replaced with `if grep -qE "^[[:space:]]*KEY[[:space:]]*=" T; then sed -i -E ... ; else echo >> ; fi`. Append now fires only when the key is genuinely absent.

**2. ncdata absolute-path mangling (build.py):** Explicit absolute `ncdata` values were double-prefixed with the config-type IC dir, producing `.../cam_aqua_fv//gpfsm/.../ic_*.nc`. New `resolve_ic_path()` helper: bare filename → prepend IC base dir; absolute/dir-bearing path → verbatim. Used by both `generate_shell_script` and `generate_clone_script`.

**3. Newcase clean-slate gas composition (`render_exoplanet_mod`, build.py):** Added an `is_clone` flag. Newcase now forces every unspecified `GAS_BAR_PARAMS` gas to `0.0` (no more inherited modern-Earth `exo_o2bar=0.2095`) and always emits explicit `exo_n2bar` (`exo_n2bar_explicit` or `target − sum(specified)`). Clone preserves the source composition (unchanged behavior). `_fortran_value` bumped to 12 sig figs so the N2 fill precision survives. "Pressure and N2 handling" section above rewritten to document both paths.

**Git workflow policy added** (this CLAUDE.md + global `~/.claude/CLAUDE.md`): bug fixes/docs/small changes commit directly to `main` without asking; significant features/new features/refactors → ask whether to branch first.

### Good starting points for next session
- Regenerate the `gplfr_grp3` build scripts on the HPC and verify all three fixes in the rendered output.
- Existing handoff items still open: stale `build.py` module docstring; `nl_cam_params` recognized by `build.py` but not scanned by `scan.py`; whether `datamgr.py avg` should move to `runmgr.py`.

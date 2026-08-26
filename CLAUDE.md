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
- **`build.py`** — validates experiment matrix, generates self-contained shell build scripts; `generate --verify` checks matrix coherency (value types + netCDF file existence) without generating; `patch` edits `exoplanet_mod.F90` in place in existing cases and rebuilds; `rebuild` recompiles existing cases after a hand-edit anywhere under `SourceMods` (compiles and stops — no submission, no XML edits). **Compilation lives here and only here** — no `runmgr.py` verb runs `<case>.build`.
- **`scan.py`** — walks CASE directories, extracts metadata, writes grouped YAML registry
- **`query.py`** — searches registry, exports experiment matrices
- **`datamgr.py`** — case data management: `report` (disk survey), `clean` (surgical purge/move), `avg` (permanent N-year averaging), `retire` (end-of-life archival)
- **`manage_utils.py`** — shared utility layer imported by `datamgr.py`, `runmgr.py`, and `build.py`: constants (`ARCHIVE_MODELS`, `HIST_MODELS`, `MODEL_STEM`, `AVG_HIST_DEFAULT_MODELS`), `load_paths()`, disk helpers (`dir_size_bytes`, `fmt_size`, `list_files_with_size`), `discover_cases()`, hist-year filtering, `restart_sets()`, `confirm()`, `batch_confirm()` (the single `[yes/no]` gate over a whole case set, shared by datamgr's clean/retire verbs and runmgr's run-control verbs; **only the literal `yes` proceeds** — a bare `y` does not, since this gate fronts irreversible operations like `retire --purge`), `ACTIVE_STATUSES` (the one definition of "a job is active" — `RUNNING`/`RESUBMITTED` — consumed by runmgr's hard blocks and datamgr's block/flag sites), `hist_info_line()` (the single `--info` hist-dir formatter behind `datamgr.py avg --info` and `runmgr.py check --info`), `preview_hint()` (one trailing `--execute` reminder), `_require_cases()` (explicit-names-or-`--prefix` selection with mutual-exclusion + no-`--all` guard, shared by every `datamgr.py` destructive verb), `submit_case()` (the single `sbatch` code path, shared by `runmgr.py submit` and `build.py make --send-it`)
- **`runmgr.py`** — run control tool; `check` subcommand (CaseStatus parsing, SLURM probe, optional hist/energy info; `--energy --keep` retains the atm average as the run-time counterpart to `datamgr avg`); `xml` subcommand (ad-hoc `--query VAR` / `--change VAR=VALUE` over a case set — no CONTINUE_RUN forcing, no sbatch; the only way to inspect/edit XML without launching a run); `submit` subcommand (sbatch a built case as-is, no xmlchange — the launch step after `build.py make`; requires `<case>.run`); `continue` subcommand (set CONTINUE_RUN=TRUE, update STOP_N/RESUBMIT, sbatch); `restart` subcommand (set CONTINUE_RUN=FALSE, apply arbitrary `--set VAR=VALUE` xmlchange calls, sbatch); Shared helpers `_resolve_cases()` (explicit-names-or-`--prefix`, no `--all`), `_parse_set_pairs()`, `_apply_xmlchange()` (the single `./xmlchange` code path), `_probe_status()` (CaseStatus + SLURM probe), `_rest_stop_warning()` (advisory REST_N > STOP_N check over pending edits merged onto live XML), and `_rpointer_reset_plan()`/`_reset_rpointers()`/`_find_refdir()` (branch/hybrid `run/rpointer.*` reset from the `RUN_REFCASE`/`RUN_REFDATE` reference set, used by `restart`) back the run-control subcommands. Nothing in runmgr compiles, and nothing in build.py touches run state: `build.py patch` and `build.py rebuild` lazily import `_probe_status()` from here for their status columns, and that is the only crossing.
- **`diff.py`** — SourceMods diff tool; used before retiring to check for custom Fortran worth preserving
- **`config_registry.yaml`** — machine-specific paths, CESM config per config_type, IC file table; must be edited per user/machine

### Key non-obvious behaviors

- `scan.py --update` **clobbers** the registry — does not merge with pre-existing content. Live rows take precedence over archive rows on name collision.
- `build.py generate` never executes scripts; `build.py make` runs them (with confirmation prompt). `make` **builds but does not submit** — submission is a separate step (`runmgr.py submit`, or `make --send-it` to fold both together). The run verbs are distinct: `submit` (launch a built case as-is, no xmlchange), `continue` (CONTINUE_RUN=TRUE), `restart` (CONTINUE_RUN=FALSE + fixes). `xml` is the odd one out — it changes/queries XML but **never** launches a job, so it has no `submit`/`continue`/`restart` semantics; on `--change --execute` it uses the same single batch `[yes/no]` gate as the other verbs (RUNNING/RESUBMITTED cases are flagged in the preview, not hard-blocked). See "Run-control gating" below.
- `build.py make` accepts explicit `NAME` positionals (bare case name or full `*_build.sh` filename) to run a named subset, in addition to `--prefix`. If neither `NAME` args nor `--prefix` nor `--all` is given, it just **lists** the scripts in `scripts-dir` and exits — it does not run anything. `--all` is required to intentionally run every script in `scripts-dir` at once, mirroring the "no implicit --all" convention used by destructive `datamgr.py`/`runmgr.py` subcommands.
- `datamgr.py` status-gates like the run-control verbs: `retire` **hard-blocks** RUNNING/RESUBMITTED cases (probe via `runmgr._probe_status`, lazily imported — the same pattern as `build.py patch`); `clean purge-restarts`/`purge-hist`/`move-hist` **flag** active jobs in the preview without blocking. `retire` has a **duplicate guard** (owner decision, 2026-07-13; replaced the earlier unconditional hard block): if `long_term/<case>` already exists — normally the product of a workflow mis-step, since a case should never live in both active and retired space — each duplicated case gets its own `[yes/no]` prompt under `--execute`. **yes** = the entire existing record (case.yaml, config, preserved data) is deleted in pass 2, after the batch confirm, so the new retirement fully replaces it (never mixed onto it — `shutil.move` onto an existing path would silently overwrite a file or nest a directory, `rest/<date>/<date>`); **no** = the case is skipped for offline resolution. In preview mode the duplicate is flagged without prompting. Without replace consent, a record that appears between preview and execution still blocks the case (TOCTOU re-check). `move-hist` never overwrites — files already present in long-term are skipped and left in the archive. `avg --last` overwrites an existing avg file (`ncra -O`), disclosed in the preview, and gates `--execute` behind the same single batch `[yes/no]` as the clean verbs. `purge-restarts --keep 0` and `purge-hist --keep-years 0` are explicit delete-alls with an honest "keeping NONE" preview; negative values are rejected.
- All destructive `datamgr.py` operations (including `clean`) default to **preview mode**; `--execute` required to act. Under `--execute` the `clean` verbs ask a **single batch `[yes/no]`** covering the whole set (`Delete … for N case(s)? [yes/no]`), not one prompt per case — matching `retire` and the runmgr run-control verbs. The two-pass flow (print all previews → one confirm → act) is driven by `_run_batch()` (datamgr.py) over `batch_confirm()` (manage_utils.py). In preview mode a single `preview_hint()` reminder prints after the last `[preview]` block. Answering no → `Aborted.`, nothing touched.
- Per-case `nl_cam_params`/`carma_params`/`volc_params`/`cice_params` blocks **merge one level deep** with the base block (`resolve_case`, `NL_GROUP_KEYS`): the case overrides only the inner keys it names and inherits the rest — base-specified keys are never silently dropped. An explicit `key: null` in a per-case block deletes the inherited key. A **bare group stub** (`nl_cam_params:` with nothing under it, YAML null — e.g. every inner key commented out) inherits the base group unchanged; deletion is per inner key only, never wholesale. (Previously a per-case block replaced the whole base dict, and a bare stub nulled it, either of which could silently shed base ozone keys and flip that one case to the zero-ozone newcase default.)
- `exoplanet_mod.F90` is embedded inline in each build script via heredoc — no staging directory.
- In clone mode, `user_nl_cam` is copied verbatim from the clone source, so namelist params use **upsert** semantics — since 2026-07-13 implemented as **delete-then-append** (`sed -i "/^KEY\s*=/d"` + `echo >>`): every existing line for the key is deleted, then exactly one is appended. This also collapses duplicates inherited from a clone source built by pre-2026-06 scripts, whose upsert appended instead of replacing (e.g. duplicated cice albedos). **CESM's namelist duplicate policy is last-value-wins**, so those legacy duplicates never corrupted runs, and the scan parsers (`parse_user_nl_cam`/`parse_user_nl_cice`) mirror last-wins so the registry reports the value a run actually used; duplicated keys are flagged in the row's `warnings` field.
- **Every namelist file-path key goes through a verified upsert** (`_nl_upsert_verified_lines`, build.py — since 2026-08-26). `ncdata` (user_nl_cam) and `finidat`/`fsurdat` (user_nl_clm) were previously written with an unanchored, single-quote-only in-place sed (`sed -i "s|ncdata = '.*'|...|"`). That pattern matched the substring inside commented-out `!ncdata = '...'` lines while missing a live line written with **double** quotes, so a clone faithfully rewrote six dead comments and ran on the clone template's hardcoded IC. It destroyed all 14 `exovolc_ben2_*` runs (aborted at timestep 2 in `MAPZ_MODULE`) and silently mis-initialized a hab1 case. The replacement is delete-then-append with an anchored key pattern (`^[[:space:]]*KEY[[:space:]]*=`, so `!KEY` never matches and quote style is irrelevant), **followed by a verify step in the generated script**: exactly one live line for the key must exist and it must carry the intended value, or the build `exit 1`s before `cesm_setup`. A silent no-op is no longer possible — that was the whole failure mode, and it went undetected for weeks because it only surfaced when a case happened to crash.
- **A clone that sets no `ncdata` warns at generate time — it does not fail** (owner decision, 2026-08-26). Nothing is emitted for such a case, so nothing can fail: it inherits whatever IC the clone source's `user_nl_cam` hardcodes. That inheritance is **deliberately kept working**. The `hungatonga`/`pinatubo`/`tambora` suites (63 built cases, all verified on the correct `cam_mixed_fv_modern` IC) rely on it, and the eruption clone templates deliberately keep a live `ncdata` line rather than commenting it out. The alternative was considered and rejected: commenting `ncdata` out of the templates would make an omitted key fail loudly at CESM startup, but it would break the mixed-fv workflow and force every matrix to restate its IC. **The named per-case warning is what makes inheritance safe** — the hazard was never a template holding a value, it was casemgr *claiming* to override that value and silently not doing it, which is now impossible (see the verified-upsert entry above). `exovolc_hab1_control_actest` ran hab1 on hab2's atmosphere exactly this way, and would now be named in the generate output.
- **Namelist quoting is not casemgr's business.** Reading was always quote-agnostic and comment-skipping (`_RE_NL_STR` in parse_utils.py: `["\']([^"\']+)["\']`, plus a `lstrip().startswith('!')` skip). Writing now matches on the *key*, not the value, so `ncdata = '…'`, `ncdata = "…"`, `ncdata ='…'` and extra interior whitespace are all handled identically — `cam_mixed_fv_modern_eruption` really does use the no-space `ncdata ='…'` form, which the old value-matching pattern could not touch at all. Whatever casemgr writes comes out single-quoted (`_format_nl_scalar`), so touched files converge on one style on their own. The only convention that matters is CESM's: `!` must start the line to comment a key out.
- **A key with a dedicated verified block must not also be set via `nl_cam_params`.** `_build_nl_upsert_block` runs *after* the dedicated block and would silently win, defeating the verify guard. `validate_case` rejects `ncdata`/`finidat`/`fsurdat` inside `nl_cam_params` with a message pointing at the top-level key.
- `exo_solar_file` (Fortran, not a namelist) keeps replace-in-place sed but is now comment-guarded (`/^[[:space:]]*!/! s|…|`) — it cannot be anchored at start-of-line because the live line is a `character(len=256), parameter ::` declaration.
- `exort_pkg` ending in `*` signals custom RT copied into SourceMods. In newcase mode this is a validation error; in clone mode it is allowed and triggers `_build_usr_src_fix_block` to rewrite the inherited `-usr_src` path.
- `runmgr.py check` defaults to **all discoverable cases** when given no names — unlike every destructive subcommand, which requires explicit names.
- `user_nl_cam` scanning is a **curated whitelist by design** (owner decision, 2026-07-12). `nl_cam_params` is open-ended on the build side — any CAM namelist key upserts into `user_nl_cam` — but `parse_user_nl_cam` extracts only named keys (`ncdata`, `bnd_topo`, `gw_drag_file`, `prescribed_ozone_file`/`_datapath`) plus the `carma_*`/`volc_*` prefix groups. The scanned namelist mixes shipped-template defaults with matrix-set keys, so scanning everything would drag boilerplate into the registry and pin it in exported matrices. Keys are added **à la carte** when they become scientifically worth round-tripping — see "Adding a scanned user_nl_cam key" below. On export the scanned keys are re-nested under `nl_cam_params` (`_NL_CAM_SCANNED_KEYS` in query.py); clone exports drop them (composition is inherited from the clone source).

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

One `squeue --me -h -o %j` **snapshot per process** (`_active_jobs()`, memoized — these are one-command-per-process CLI tools) replaces the old per-case `squeue --name <case>` spawn; each case is membership-tested against the snapshot. When the last CaseStatus event starts with `run started` or `run SUCCESSFUL`, the probe consults the snapshot:
- **Job found + last event was `run SUCCESSFUL`** → status shown as `RESUBMITTED`
- **No job + last event was `run started`** → `cases/<case>/run.out` is checked (`_run_out_walltimeout`):
  - timeout found → status shown as `WALLCLOCK`
  - otherwise → status shown as `RUNNING?` (started but no longer queued — likely crashed without writing to CaseStatus)
- **`FileNotFoundError`** (squeue not in PATH) or **non-zero exit code** → snapshot is `None`, probe silently omitted, original status label retained. A raw `RUNNING` label can therefore be **stale** (a crashed run also leaves `run started` as the last event); `datamgr.py retire` still hard-blocks it — fail safe — but prints a NOTE explaining the label is unverified and to re-run where squeue works.

**The probe matches on the SLURM job name (`-J`), so per-case correctness depends on `-J` equalling the case name.** `build.py` (`_build_run_script_block`) therefore defaults `#SBATCH -J` to the full case name (`${CASE}`) for every build — an explicit matrix `job_name` overrides it. Without this, CESM truncates `-J` to a short, non-unique label (e.g. all of `exocam_ML_grp3_pt*` collapse to `exocam_M`); `squeue --name <full_case>` then matches nothing and running cases show as `RUNNING?`. **Build scripts generated before this default must be regenerated** — the fix only affects newly rendered `.run` patch blocks, not `.run` files already on the HPC.

### WALLCLOCK detection (run.out)

A SLURM wall-clock kill never updates `CaseStatus` (it would otherwise read `RUNNING?`), but it leaves a `CANCELLED ... DUE TO TIME LIMIT` line in `cases/<case>/run.out`. `_run_out_walltimeout(run_out_path)` resolves the `RUNNING?` ambiguity:

1. `run.out` is **appended to on every run attempt**, so only the segment after the **last** `CSM EXECUTION BEGINS HERE` line is examined — a timeout in an earlier segment followed by a fresh success must not register.
2. Returns `True` if any line in that last segment contains **both** `CANCELLED` and `DUE TO TIME LIMIT`. Missing/unreadable file → `False`.

`WALLCLOCK` is a probe-derived label (like `RESUBMITTED`/`RUNNING?`), not a `CaseStatus` event mapping. The run-control verbs (`continue`/`restart`/`submit`) show it in the per-case preview and proceed — a timed-out case is exactly what the user is relaunching. Wired into all four probe sites: `check`, `continue`, `restart`, `submit`.

## Run-control gating (continue / restart / submit / xml --change)

All four run-control verbs use the **same double-gate ergonomics as `build.py make`**, so `--execute` behaves consistently across the package:

1. **Gate 1 — `--execute`.** Without it, the verb prints the per-case preview and exits (`(preview only — rerun with --execute …)`).
2. **Gate 2 — a single batch `[yes/no]`.** With `--execute`, after the preview the verb asks **one** confirmation (`Continue … and submit N case(s)? [yes/no]`, `Submit N case(s)?`, `Apply XML changes to N case(s)?`) covering the whole set, then acts. Answering no → `Aborted.`, nothing submitted. This replaced the old per-case soft-block prompts.

Status handling within the preview:
- **RUNNING / RESUBMITTED** → **hard block**: dropped from the set, never submitted (a job is already active), with an explicit `— skipping (job already active)` line.
- **anything else** → proceeds. The preview prints the case and its status label (`<case>  [BUILT]`) and nothing more.

**The label is reported, not judged** (owner decision, 2026-07-27). An earlier version appended `<- not COMPLETE` (`<- not BUILT/COMPLETE` for `submit`) to any status that wasn't the expected one. It was removed: the text restated the label printed immediately beside it, gated nothing (it fed only its own `print`), and misfired on the states these verbs exist to handle — `FAILED` and `WALLCLOCK` are the normal reasons to relaunch, and **`build.py rebuild` appends `build complete` to `CaseStatus`, so every case in a rebuilt ensemble reads `BUILT` and drew the flag** on the intended rebuild → restart workflow. The single batch `[yes/no]` is the decision point.

`xml --query` (no `--change`) is read-only and never gates. `batch_confirm(action, n)` in `manage_utils.py` is the shared gate helper (runmgr's former private `_batch_confirm` copy was removed); `submit_case()` (in `manage_utils.py`) is the single `sbatch` path used by all three submitting verbs — `continue`/`restart` no longer carry their own inline `subprocess.run(['sbatch', …])` block. `_apply_xmlchange` raises `RuntimeError` (caught per-case) when `./xmlchange` is missing, so a bad case dir reports an error instead of crashing the batch.

### restart — rpointer.* reset for branch/hybrid cases

`restart` sets `CONTINUE_RUN=FALSE` to relaunch from `RUN_REFCASE`/`RUN_REFDATE`, but CESM's component models read `run/rpointer.*` for the actual restart filenames — independent of those XML vars. A prior run segment (e.g. `continue`) advances `rpointer.*` past the reference point; the reference `.r.`/`.i.` files are still sitting untouched in `run/`, but the pointer no longer names them, and the restart fails at startup. This surfaced in real operation (2026-07-27).

For every case where live XML reads `RUN_TYPE=branch` or `hybrid`, `restart` locates `archive/<RUN_REFCASE>/rest/<RUN_REFDATE>-00000/` (falling back to `long_term/` if the reference case has since been retired — the same two locations, in the same order, that `build.py`'s generated `RUN_REFDIR` block resolves at build time), and copies every `rpointer.*` file there into `<case>/run/`, overwriting whatever is present. This is disclosed in the preview (`rpointer.*: reset from <refdir>/`) and happens for the whole batch, gated by the same single `[yes/no]` as the rest of the verb — no separate flag or confirm.

- All five `rpointer.{atm,drv,ice,lnd,ocn}` files are reset together, never just `rpointer.atm` — the driver and every component read their own pointer at startup, so resetting a subset would leave the fileset internally inconsistent.
- `startup`-type cases never carry `RUN_REFCASE`/`RUN_REFDATE` and are unaffected — no rpointer line appears in their preview.
- If the reference `rest/` set can't be found (missing `RUN_REFCASE`/`RUN_REFDATE`, or no matching directory in either `archive` or `long_term`), the preview prints a `WARNING` and `rpointer.*` is left untouched — the restart will likely still fail at startup, but neither verb guesses.
- **The reset deliberately stops at locating the reference set — it does not verify the `.r.`/`.i.` files the copied pointers name** (owner decision, 2026-07-27). The pointers are resolved from the case's own `RUN_REFCASE`/`RUN_REFDATE` and copied directly out of the same `archive/` restart set that holds those files, so a mismatch would mean the archive set itself is corrupt. Checking further is overkill; don't add it.
- `_rpointer_reset_plan()` / `_reset_rpointers()` / `_find_refdir()` (runmgr.py) are the implementation, private to `restart`; verified against real `hybrid`/`branch` cases on Discover (preview-only dry run) before landing. `build.py rebuild` deliberately does **not** reset rpointers — see its section below.

## build.py rebuild — recompile an ensemble after a hand-edit

```
build.py rebuild --prefix noO3_grp3 --execute
build.py rebuild case_a case_b --execute
```

Added 2026-07-27 for the case that prompted it: an across-the-ensemble bug fix to hardcoded Fortran (e.g. hand-edited `exoplanet_mod.F90`, or another file under `SourceMods`) where every case in a matrix needs the identical rebuild, with nothing else changing. Owner decision, 2026-07-27: it lives in `build.py`, not `runmgr.py` — **no runmgr verb runs `<case>.build`**, and that test (does it compile?) is what divides the two modules.

**The division of labor with `patch` is scope of the source edit, not whether a compile happens.** `patch` *automates* the edit for one file — `exoplanet_mod.F90`, whose `parameter` constants change often enough to be worth a `--set` interface — then rebuilds. It is deliberately limited to that file: extending it to arbitrary `SourceMods` source edits would be far more arduous than just editing the file. `rebuild` is the general escape hatch for exactly that — you hand-edit any file anywhere under `SourceMods`, and `rebuild` recompiles the ensemble. It makes no source edit of its own, and takes no `--set` of any kind: if a parameter or XML value also needs to change, that's `patch` or `runmgr.py restart`'s job, run separately.

**`rebuild` compiles and stops — no `--send-it`** (owner decision, 2026-07-27; an earlier iteration had one). It never submits, never runs `xmlchange`, and never touches `run/rpointer.*`, because **how a rebuilt case should resume is a decision the verb cannot make**: continuing an in-progress run from where it left off (`runmgr.py continue`, `CONTINUE_RUN=TRUE`) is as common as relaunching from the reference point (`runmgr.py restart`, `CONTINUE_RUN=FALSE` + rpointer reset), and forcing either would be wrong half the time. Wanting to rebuild *without* resetting `CONTINUE_RUN` is a real, routine situation. A `NOTE` after the batch points at `runmgr.py submit` / `continue` / `restart`.

`--send-it` stays exclusive to `make`, where it belongs: a brand-new case has no run state to preserve, so building and submitting can safely fold into one step. A rebuilt case has run state, so they cannot.

Relaunching is therefore always a second, explicit command. `build.py rebuild --help` carries the three recipes as workflow guidance, and the post-batch NOTE points at it:

| After a rebuild, to… | run |
|---|---|
| resume where the run left off (common) | `runmgr.py continue` — sets `CONTINUE_RUN=TRUE`, leaves `rpointer.*` alone |
| relaunch from the reference point (branch/hybrid) | `runmgr.py restart` — sets `CONTINUE_RUN=FALSE` + resets `rpointer.*` |
| launch a case that has never run | `runmgr.py submit` — sbatch as-is |

**`submit` submits whatever the XML already says** — it makes no `xmlchange` calls at all. On a case whose `CONTINUE_RUN` is already `TRUE` it happens to continue correctly, but on one set to `FALSE` it relaunches from the beginning, overwriting a run the user meant to continue. `continue` is the correct verb whenever continuation is the intent, since it sets the var explicitly and previews the value (`CONTINUE_RUN: TRUE -> TRUE`) before the confirm.

- **RUNNING/RESUBMITTED are flagged, never blocked** — the same policy as `patch`, and safe precisely because `rebuild` does not resubmit. Recompiling a queued case so its next segment picks up the new binary is a supported use.
- Cases with no `<case>.build` are skipped before anything is touched.
- A failed compile is reported per-case; the rest of the batch continues (same pattern as `patch`).
- The only cross-module import is `runmgr._probe_status` for the status column — the same lazy import `patch` uses. `rebuild` touches no run-control state, so it needs nothing else from runmgr.

### --energy computation

1. List `*.cam.h0.*.nc` files in `$archive/<case>/atm/hist/` excluding filenames containing `"avg"`. Sort lexicographically (= chronological for CESM date strings).
2. Take the last 12, or with `-n`/`--energy-years N` the last `12*N` (or fewer, with a warning printed). `-n` requires `--energy` and must be ≥ 1; the report line states the month count actually used (`Last 120mo: …`).
3. Run `ncra -O <file1> ... <fileN> <out>`. Without `--keep`, `<out>` is a per-invocation `mkstemp` temp file (`runmgr_energy_<case>_*.nc`). If `ncra` is not found, print a warning and skip.
4. Open the output with `netCDF4`. Extract `TS`, `FSNT`, `FLNT`. If any variable is missing, print a warning and skip.
5. Compute area weights: `w = cos(lat * π/180)`, broadcast across the lon dimension, normalize to sum to 1.
6. Compute global means via `sum(data * w2d)`.
7. Print `Last Nmo:  TS = 287.3 K    Etop = +0.8 W/m²` (Etop = FSNT_mean − FLNT_mean, signed, 1 decimal).
8. The temp file is deleted in a `finally` block, even on error — **unless `--keep` was given** (see below), in which case the average is the deliverable and is left in place.

### --keep — retain the atm average

`check <case> --energy --keep` stops discarding the averaged file `--energy` already produces and writes it into `$archive/<case>/atm/hist/` instead of a temp file. It is the **run-time counterpart to `datamgr avg`**: same atm output, same naming, produced during routine energy monitoring rather than at retirement. The two are *not* redundant — `datamgr avg` is a retirement/archival pass that averages **all** components (atm/lnd/ice) as part of end-of-life prep; `check --energy --keep` averages **atm only** (the component whose h0 carries TS/FSNT/FLNT) as a byproduct of the monitoring you already run, and that atm avg is the scientifically important artifact. Neither is deprecated.

- Requires `--energy` (`--keep` alone → `ERROR: --keep requires --energy.`, exit 1).
- Filename mirrors `datamgr avg`'s convention: with `-n N` → `<case>.cam.h0.avg_last{N}yr.nc`; bare 12-month (`--keep` without `-n`) → `<case>.cam.h0.avg_last12mo.nc`. With `-n N` the inputs (last `12*N` months, avg files excluded) are identical to `datamgr avg --last N --models atm`, so the kept file is interchangeable with that product.
- `ncra -O` overwrites an existing avg file at the target; the kept path is printed on a `Saved avg:` line under the energy report. `_energy_balance(keep_path=…)` is the single code path — `keep_path=None` restores the temp-and-delete behavior.

---

## RUN_REFDIR — resolving a branch/hybrid reference restart set

`_branch_var_block` (build.py) emits `RUN_REFCASE`/`RUN_REFDATE` plus a shell block that resolves `RUN_REFDIR` **in the generated script, at build time on the HPC** — not at generate time in Python:

```bash
REST_SUBDIR=${RUN_REFCASE}/rest/${RUN_REFDATE}-00000
if   [ -d "${ARCHIVE}/${REST_SUBDIR}" ];   then RUN_REFDIR=${ARCHIVE}/${REST_SUBDIR}
elif [ -d "${LONG_TERM}/${REST_SUBDIR}" ]; then RUN_REFDIR=${LONG_TERM}/${REST_SUBDIR}
else  echo "ERROR: ..." ; exit 1 ; fi
```

A reference case is either still in the active archive or has been retired to long-term storage by `datamgr.py retire`. Before 2026-08-08 the line was hardcoded to `${ARCHIVE}/...`, so a hybrid/branch build off a retired refcase could not reach its restart set and failed at the `cp ${RUN_REFDIR}/*` step.

- **The probe must run on the HPC, not at generate time.** Build scripts are generated locally (the documented workflow), where neither `archive` nor `long_term` is mounted. A Python-side `os.path.exists()` test would therefore report "not found" for *every* case and silently emit the wrong root — the same bug, minus the error message. This is why there is no `refcase_root` YAML key: detection happens where the filesystem is, and no suite has to declare anything. (A `refcase_root: long_term` placeholder briefly existed in two volcano suites as a proposal; it was removed when this landed.)
- **Active is tried first.** A case present in both roots is mid-retirement, and the active copy is the current one.
- **Neither root → hard `exit 1`** naming both searched paths, before `create_newcase`/`create_clone` runs. Under the script's `set -e` this aborts the build immediately rather than letting an unset `RUN_REFDIR` reach `cp`.
- Same two locations, same order, as `runmgr.py restart`'s rpointer reset (`_find_refdir`) — the two must not disagree about where a reference set lives.
- Emitted for `run_type: branch`/`hybrid` only; `startup` cases get no block. Applies to newcase and clone alike (both call `_branch_var_block`).

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

Beyond these three, `validate_case` itself (so **both** plain `generate` and `--verify`) hard-fails a case where the restart interval outruns the segment length — see "REST_N ≤ STOP_N guard" below.

Verify mode runs the type/nc checks **before** `validate_case`, because `validate_case` coerces values to float (via `compute_pstd_from_spec`) and would raise on a mistyped numeric; `--verify` reports a clean `type:` message instead. `validate_case` only runs if types pass. `validate_case` itself also runs the same type checks first (shared `_type_errors()`) and returns early on failure, so **plain `generate` is exactly as strict as `--verify` and `patch --set`** — a mistyped `PARAM_TYPES` value fails the case with a clean `type:` error instead of reaching `_fortran_value`, whose int branch would silently truncate a non-integral (`26.5` → `26`) or crash on a non-numeric. Output: `OK:` / `FAIL:` per case, `-` lines for errors, `!` lines for warnings, `·` lines for skip notes, then a summary count. A case with warnings but no errors still reports `OK` and exits 0.

---

## REST_N ≤ STOP_N guard

`_verify_rest_stop` (build.py) is a **hard error** in `validate_case`, so it blocks both plain `generate` and `--verify`, for newcase and clone alike (the `xmlchange` block that writes these is identical for both).

**The failure it prevents** (observed in real operation, 2026-07-21): when the restart interval is longer than the run, the segment ends before a restart write is ever reached. CESM still emits an end-of-run restart, but the fileset is **incomplete**, and the crash does not appear until the *next* submission — a `CONTINUE_RUN=TRUE` that cannot find the full set it needs to resume. Catching it at generate time is the only place the user sees it before burning the allocation twice.

- Units are normalized to days through `_OPTION_DAYS` before comparing, so a legitimate cross-unit pairing (`stop_option: nyears/stop_n: 5` with `rest_option: nmonths/rest_n: 12`) is **not** false-failed. The originally reported trigger (`REST_OPTION == STOP_OPTION and REST_N > STOP_N`) is the same-unit special case, where the comparison is exact.
- `rest_n == stop_n` is **allowed** — one restart lands exactly at the end of the segment.
- `nsteps`/`nstep` (timestep-dependent, unknowable from the matrix) and `date`/`ifdays0` (absolute markers, not intervals) are **skipped**, not guessed at.
- Runs after `_type_errors` has passed (`validate_case` returns early on type failure), so a mistyped `rest_n` reports as a clean `type:` error rather than reaching the comparison.
- The `_OPTION_DAYS` values are approximate (`nmonth` = 30.4375 days) and only need to order correctly across units; every real matrix uses matching units, where precision is irrelevant.

**At run time the same condition is a warning, not a block.** `runmgr.py continue --set` / `restart --set` / `xml --change` print a `! WARNING:` line in the per-case preview when the pending edit would leave `REST_N` outrunning `STOP_N`; the single batch `[yes/no]` then decides. Rationale: by then the case exists and the user may be deliberately staging an odd pair, so runmgr reports rather than refuses — the opposite of generate time, where nothing is lost by failing early.

`_rest_stop_warning()` (runmgr.py) is the shared helper, and it lazily imports `_OPTION_DAYS` from `build.py` (the `datamgr → runmgr._probe_status` pattern) so the build-time and run-time guards can never disagree about what a REST/STOP pair means. Run-time specifics that differ from the build-time check:

- The four values are **live per-case XML**, not a matrix, so pending `--set`/`--change` pairs are merged over what the env xmls currently hold (later `--set` wins, matching `_apply_xmlchange` order). Consequence: **`--set STOP_N=2` alone can warn** — shrinking the run below an untouched `REST_N` causes the identical failure.
- **Silent unless a REST/STOP var is actually being changed.** A case already violating the condition draws no comment when you edit something unrelated — the guard reports on your edit, not on pre-existing state.
- Silent when either side can't be read from the env xmls (unbuilt or odd case dir), and when either `_OPTION` is `nsteps`/`date`/`ifdays0`.
- `xml --query` is read-only and never warns.

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
- `src.cice` only → aqua; the grid discriminates SE from FV (identical SourceMods trees): ATM_GRID/GRID read from `env_case.xml`/`env_build.xml`/`env_run.xml` via `parse_atm_grid` — `ne5np4` → `cam_aqua_se_ne5`, `ne16np4` → `cam_aqua_se_ne16`, anything else (or no grid found) → `cam_aqua_fv`
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

The two ozone keys are set **as a unit**, and `prescribed_ozone_file` is the **owner key**: a matrix naming the file owns the whole ozone setting and must supply its own datapath. A datapath **alone** cannot hold ozone on — if `prescribed_ozone_file` is absent (including when a per-case `prescribed_ozone_file: null` deleted it while the base datapath survived the deep-merge), generate warns and forces the zeroVMR default for **both** keys, and `--verify` raises a matching warning. This keeps `_effective_ozone_file` (which keys on the file only) truthful. `prescribed_ozone_cycle_yr` / `_name` / `_type` ship with the namelist and are never touched.

**Why this matters.** `cam_mixed_fv`'s shipped `namelist_files/user_nl_cam` carries modern-Earth ozone. Before this rule, a matrix mentioning neither `exo_o2bar` nor ozone produced a case with **no O2 and full ozone** — incoherent, since ozone is photochemically produced from O2, and silently so. The config templates deliberately retain their per-config defaults (aqua/land neutral, mixed Earth-like); those serve users driving ExoCAM by hand, and casemgr simply ignores them.

**Clone is exempt.** `generate_clone_script` never applies these defaults. A clone preserves its source case's composition — that is the point of cloning. See "Pressure and N2 handling" below for the corresponding `is_clone` split in `render_exoplanet_mod`.

**Consequence:** matrices written before this rule that relied on inheriting Earth-like ozone will produce no-ozone cases when regenerated. Audit before regenerating.

### `--verify` consistency warnings

`generate --verify` raises **warnings** (never failures; exit code unaffected) for combinations that are scientifically contradictory. `_effective_ozone_file()` models the newcase default, so a matrix silent on ozone is checkable rather than unknown:

- `exo_o2bar == 0.0` with a non-zeroVMR ozone file → ozone without its precursor.
- `exo_o2bar > 0.0` with a zeroVMR file (**including by default**) → O2 without the ozone it would produce.
- ozone present and `exo_convect_plim < 4.e3` Pa → convection reaching the stratosphere is a numerical stability hazard. This is a **floor, not an equality**: values above `4.e3` merely clamp convection lower and are safe. Without ozone the parameter is freely tunable and nothing is warned.

Detection keys on the **absence of the `zeroVMR` substring**, never on the stock ozone filename — that tag drifts between input datasets; the zeroVMR convention is stable.

**Clone specs are exempt from the silent-matrix reasoning.** A clone matrix silent on ozone inherits it from the clone source — unknown at verify time, not zeroVMR — so `_effective_ozone_file` returns `None` there and both checks skip. They still run when a clone matrix explicitly names `prescribed_ozone_file`.

---

## Pressure and N2 handling

`render_exoplanet_mod` behaves differently for newcase vs clone (controlled by its `is_clone` flag):

- **Newcase (`is_clone=False`) — clean slate.** Every radiatively-active gas in `GAS_BAR_PARAMS` (CO2, CH4, C2H6, NH3, CO, H2, O2) that is *not* named in the matrix is forced to `0.0` — the template's modern-Earth defaults (e.g. `exo_o2bar = 0.2095`) must not leak in. N2 is **always** emitted as an explicit numeric fill: `exo_n2bar_explicit` if set, otherwise `compute_pstd_from_spec(spec) − sum(specified gases)`. The Fortran `1 - sum(others)` expression line is never relied upon for newcase.
- **Clone (`is_clone=True`) — preserve composition.** Only the gas params named in the matrix are substituted; all unspecified gases and N2 keep whatever the clone-source `exoplanet_mod.F90` has. `exo_n2bar` is patched only when `exo_n2bar_explicit` is set (high-pressure case); otherwise the source's expression line is left intact.

`_fortran_value` formats gas bar values at 12 significant figures (`%.12g`) so the full input precision of the N2 fill survives without float noise. Params tagged `int` in `PARAM_TYPES` (currently `exo_rad_step`) render as **bare integers** — never a decimal or `_r8` suffix, which would contradict the declared Fortran type — regardless of whether the value arrived as a YAML int or a `patch --set` string.

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

### Adding a scanned user_nl_cam key (curated whitelist)
1. Add the key to the `keys` whitelist in `parse_user_nl_cam` (parse_utils.py). Quoted string values are caught as-is; a bare numeric/logical key would need the `_RE_NL_VAL` path (currently only the `carma_*`/`volc_*` prefixes use it).
2. Add it to the appropriate group in `scan._REGISTRY_GROUPS` and collect it in `inspect_case()` (`row[key] = nl.get(key)`).
3. Add it to `_NL_CAM_SCANNED_KEYS` in query.py — `_row_to_base` then nests it under `nl_cam_params` in exported matrices automatically (`nl_cam_params` is already in `_BASE_FIELD_ORDER`).
4. Re-scan on the HPC (`scan.py --update`) to populate the new field.

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
- **Compilation lives in `build.py`.** No `runmgr.py` verb runs `<case>.build` — `generate`/`make` (fresh builds), `patch` (exoplanet_mod.F90 edit + rebuild), and `rebuild` (recompile after a hand-edit) are the only entry points to a compiler. "Does it compile?" is the dividing line between the two modules, not "does it submit?" — `make --send-it` sbatches from `build.py` via the shared `submit_case()`.
- **`build.py` never changes the run state of an existing case.** `patch` and `rebuild` recompile and stop: no `xmlchange`, no `rpointer.*` reset, no `sbatch`. How a rebuilt case resumes (`continue` vs `restart`) is the user's decision, made with `runmgr.py`. `make --send-it` is the one exception and is confined to *fresh* cases, which have no run state to preserve.
- `build.py patch` is the **only** way to change an `exoplanet_mod.F90` parameter in an already-built case. `generate` cannot: it renders the F90 into a fresh build script whose first act is `create_newcase`/`create_clone`, which would recreate the case and destroy the run. These are Fortran `parameter` constants compiled into the binary — no `xmlchange` or `user_nl` path can reach them.
- For a newcase, the experiment matrix is the sole arbiter of atmospheric composition. Nothing is inherited from the config templates. See "Composition inheritance" above.
- `rest_n` must never exceed `stop_n` (unit-normalized). Enforced as a hard error in `validate_case`, so no generated build script can write a REST/STOP pair that produces an incomplete restart fileset. See "REST_N ≤ STOP_N guard" above.
- `scan.py --update` clobbers the registry with exactly the cases scanned in the current run. It does not merge with pre-existing registry content.
- **A namelist write that is supposed to happen must not be able to no-op silently.** Every file-path key written into a namelist by a generated build script goes through `_nl_upsert_verified_lines`, which verifies its own result and `exit 1`s if the key is missing, duplicated, or carries the wrong value. Adding a new such key means using that helper, not a fresh `sed`. See "Every namelist file-path key goes through a verified upsert" above.
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

## Session handoff — 2026-08-26

**`ncdata` was never written to the live namelist line.** Diagnosed from the HPC side (see `volcanos/description_paper/notes/casemgr_ncdata_handoff.md`), reproduced and fixed here.

**The bug.** Both call sites (newcase + clone) wrote `ncdata` with

```
sed -i "s|ncdata = '.*'|ncdata = '<path>'|" user_nl_cam
```

Two independent defects: the pattern is **not anchored**, so `!ncdata = '...'` contains the substring and every commented line got rewritten; and it is **single-quote-only**, so a live line written with double quotes could not match and survived the clone verbatim. The build faithfully wrote the correct path into six dead comments and left the template's hardcoded IC live. `exovolc_ben1_control/run/atm_in` and `exovolc_ben2_control/run/atm_in` were byte-identical; all 14 ben2 cases aborted at timestep 2 in `MAPZ_MODULE`. The matrix was not at fault — it set `ncdata` correctly.

**Reproduced locally before fixing:** generated `exovolc_ben2_control` from `ben2_suite1.yaml`, ran the emitted sed against a replica namelist, confirmed comments rewritten and live line untouched. A `TestOldPatternIsActuallyBroken` case in the new test file locks that fixture in, so the regression tests can't drift into passing against a fixture that no longer exercises the bug.

**What changed**

1. **`_nl_upsert_verified_lines(key, value, target, label)`** (build.py) — delete-then-append with an anchored key pattern, then a verify step in the generated script: exactly one live line, carrying the intended value, or `exit 1`. Used for `ncdata` (both build paths) and `finidat`/`fsurdat` (`_build_clm_update_block`, which had the identical defect in the mirror-image form: double-quote-only and unanchored).
2. **Clone-without-`ncdata` warns at generate time.** The one remaining silent path — nothing is emitted, so nothing can fail. This is what `exovolc_hab1_control_actest` hit (the note's unresolved finding #5): it was the *first*, hand-built minimal clone matrix that set no `ncdata`, not a third code path or the `ic_file` gating misfiring. The `hungatonga`/`pinatubo`/`tambora` suites (54 cases) surface this warning too — they inherit from `cam_mixed_fv_modern_eruption` by design, but it is now visible rather than assumed.
3. **`validate_case` rejects `ncdata`/`finidat`/`fsurdat` inside `nl_cam_params`** — the group upsert runs after the dedicated block and would silently beat it.
4. **`exo_solar_file` sed comment-guarded** (`/^[[:space:]]*!/!`). Same latent defect class; it is a Fortran `character(...) :: ` declaration so it cannot be start-of-line anchored. Verified it still rewrites the live line and preserves the trailing `!!` comment.
5. **`tests/test_ncdata_upsert.py`** — 13 tests, the repo's first. Covers double- and single-quoted live lines, comment preservation, duplicate collapse, absent key, prefix-key non-matching, the loud-failure path, the clm mirror, the `nl_cam_params` collision, the solar-file comment guard, and the old-pattern anchor. Run with `python3 tests/test_ncdata_upsert.py`. It shells out to real `sed`/`grep` and normalizes GNU vs BSD in-place flags, so it validates the emitted shell rather than a Python approximation.
6. **Corrected the misleading comment in 5 matrices** (`ben1_suite1`, `ben2_suite1`, `hab1_ac_test`, `hab1_suite1`, `hab2_suite1`, under `volcanos/description_paper/experiment_yamls/`). It blamed the abort on "leaving `ncdata` unset"; the matrix *did* set it. All 12 matrices still parse.

**Verified:** 118 scripts regenerated across 8 matrices, 0 errors, all pass `bash -n`. The emitted upsert block was executed against replica namelists — comments preserved, live double-quoted line replaced, exactly one live `ncdata` — and the sabotaged-write path exits 1 with an `ERROR:` line.

**Not done — needs the HPC (task 4 of the note, and the audit):**
- ~~Fix the two clone templates.~~ **Resolved 2026-08-26: the templates keep a live `ncdata` (Option A + warning), by owner decision.** The handoff note recommended commenting them out; that was considered and rejected — see the entry above. Note the note's line numbers and paths are stale: the templates were hand-edited the same day (the commented `!ncdata` candidate lines are gone, `ncdata` is now at line 9/9/10, and `cam_land_fv_eruption` was repointed from ben1 to **ben2**). Read the live files, not the note.
- ~~Audit the generated cases.~~ **Done 2026-08-26.** Results: **14 `exovolc_ben2_*` cases confirmed carrying ben1's IC** (the 4 hand-built `_diag5/7`/`_test1/2` are correct); **`exovolc_hab1_control_actest` confirmed carrying hab2's IC**, `actest2` correct; **all 63 built mixed-fv cases (hunga 29, pinatubo 18, tambora 16) are on the correct `cam_mixed_fv_modern` IC** — the inheritance there was benign, and that branch of the audit is closed. The hab1/hab2 suites are not built yet.
- **Regenerate + rebuild the 14 ben2 cases**, then the note's verification: `ncdata` must name ben2, and ben1/ben2 `atm_in` must now **differ**. Short 1-year `exovolc_ben2_control` before committing the full 14.
- ben1's 14 completed runs have the correct IC by luck and do not need rerunning on this account.

---

## Session handoff — 2026-07-27 (evening)

**`rebuild` relocated to `build.py`; two bugs found by running it on a real ensemble.** Seven commits on `main` (`7390c04`..`464740d`), all pushed.

1. **`7390c04` — runmgr help ergonomics.** The top-level `SUBCOMMANDS` listing was hand-maintained prose while every subparser used `help=argparse.SUPPRESS`, so nothing rendered and the list drifted (`rebuild` had an extra space, breaking the column). Each subparser got a real `help=` string; argparse now renders and aligns the listing like `build.py` does. `SAFETY` stays (argparse can't express it).
2. **`58108f7` + `4ab9f72` — `rebuild` moved to `build.py`, then narrowed.** Owner's test: **no runmgr verb runs `<case>.build`** — compiling, not submitting, is what divides the modules (`make --send-it` already sbatches from `build.py`). It briefly gained `--send-it`; that was **removed** after the owner noted that rebuilding *without* resetting `CONTINUE_RUN` is routine, and forcing `CONTINUE_RUN=FALSE` + rpointer reset made it impossible. `rebuild` now compiles and stops.
3. **`597fbc0` — workflow recipes in `rebuild --help`.** The two-step relaunch was only in CLAUDE.md; now three copyable recipes (continue / restart / submit) live in the help text, including the caveat that `submit` runs whatever the XML says.
4. **`ff6b56e` — `<- not COMPLETE` preview flag removed** from `continue`/`restart`/`submit`. It restated the label beside it, gated nothing (fed only its own `print`), and fired on all 20 cases of a correct rebuild → restart pass, since `rebuild` appends `build complete` to `CaseStatus` and `_probe_status` reads only the last line.
5. **`9bb0f30` — real bug: rpointer reset wrote to `<caseroot>/<case>/run`, which has never existed.** `RUNDIR` is its own filesystem root. Every other rundir consumer already used `paths.rundir`; this one site didn't, so **the feature could never have worked outside the fixture** — which encoded the same wrong assumption and therefore validated the bug. New `_run_dir()` is the single resolver; the plan now checks the destination too, so a missing run dir warns in the preview instead of failing under `--execute`.
6. **`464740d` — startup cases state `rpointer.*: not needed`** instead of printing nothing. No behavior change: the skip was already correct (`CONTINUE_RUN=FALSE` + `RUN_TYPE=startup` initializes from `ncdata` at `RUN_STARTDATE` and never reads the pointers). Silence was indistinguishable from a reset skipped by mistake.

**Verified on Discover:** 20-case `exovolc_hunga_` ensemble rebuilt (all 20 binaries relinked, ~29 s/case), then restarted after the rpointer fix. Case census: 62 hybrid, 29 startup, 8 branch.

### Good starting points for next session
- Owner flagged that these ergonomics may be revisited as use continues — nothing is settled by fiat.
- The `exovolc_hunga_` ensemble was restarted successfully after the rpointer fix; that thread is closed.
- Existing items still open: stale `build.py` module docstring; whether `datamgr.py avg` should move to `runmgr.py`; `render_exoplanet_mod` trailing-comment whitespace wart (changes generated bytes everywhere — do it alone).

---

## Session handoff — 2026-07-11

**Full review-fix cycle closed.** Two review docs in repo root (`fable-review-correctness.md`, `fable-review-design.md`, currently untracked) drove five commits on `main`, all fixture-verified locally:

1. **`a08a6bc` — helper convergence.** runmgr's private `_batch_confirm`/`_resolve_cases` replaced by manage_utils `batch_confirm`/`_require_cases` (explicit names now validated against disk); `preview_hint` adopted everywhere; retire converged to a single batch confirm (per-case prompts removed, owner decision); dead `confirm()` removed; `scan --update` help now states clobber semantics.
2. **`e7d625d` — datamgr destructive-path safety.** retire hard-blocks RUNNING/RESUBMITTED and preserve-target collisions in long-term (shutil.move would silently overwrite files / nest directories — `rest/<date>/<date>`); clean verbs soft-flag active jobs; `--keep 0`/`--keep-years 0` are honest explicit delete-alls (the old `sets[-0:]` preview claimed to keep everything while deleting all); move-hist skips (never overwrites) long-term collisions; avg gained the double gate + `ncra -O` with overwrite disclosure.
3. **`fd8ff3f` — build.py validation.** Clone matrices no longer false-fail the IC table lookup (removed for clones; pressure from matrix-only gases is wrong by construction); clone-silent ozone exempt from `--verify` warnings (`_effective_ozone_file` → None); int-tagged params (`exo_rad_step`) render as bare integers via PARAM_TYPES dispatch (owner-confirmed; `patch --set` wrote `4.0_r8` into an integer parameter); `_sed_escape_replacement` hardening.
4. **`35e66f9` — registry round-trip.** 8 EXO_PARAMS (`exo_mvelp`, `exo_ve`, `exo_albdif`, `exo_albdir`, `do_carma_exort`, `Tmax`, `swFluxLimit`, `lwFluxLimit`) now flow scan → registry → query export; SE aqua cases no longer misregister as `cam_aqua_fv` (`parse_atm_grid` reads ATM_GRID/GRID from env xmls). **Requires `scan.py --update` re-scan on Discover**; audit matrices previously exported from SE cases (they carry FV config_type).
5. **`0666662` — per-case nl group deep-merge** (owner decision): `nl_cam_params`/`carma_params`/`volc_params`/`cice_params` case blocks inherit the base block and override only named inner keys; explicit `key: null` deletes an inherited key. Matrices that relied on wholesale replacement must switch to nulls.
6. **Phase 5 minors (this commit):** runmgr xml/continue/restart previews read all env_*.xml files (build-time vars no longer preview as `?`); continue/restart skip unbuilt cases *before* any xmlchange; `--energy` temp file is mkstemp-unique (+ `ncra -O`) instead of a fixed name in shared /tmp; query.py `DEFAULT_REGISTRY` is script-dir-absolute like `RETIRED_REGISTRY`; `--info` (runmgr check + datamgr avg) reports avg bytes separately from the non-avg count/size, and avg-only dirs contribute to TOTAL.

### Good starting points for next session
- Run `scan.py --update` on Discover (populates the 8 new registry fields, corrects SE rows), then spot-check `query.py export` output.
- Skim `exp_matrices/` for per-case nl group blocks that relied on wholesale replacement (must switch to explicit `key: null`).
- Still open from before: stale `build.py` module docstring; `nl_cam_params` recognized by `build.py` but not scanned by `scan.py`; whether `datamgr.py avg` should move to `runmgr.py`; `render_exoplanet_mod` trailing-comment whitespace wart (fix deliberately, alone — changes generated bytes everywhere).

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

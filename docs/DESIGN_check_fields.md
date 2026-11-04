# DESIGN — `runmgr.py check --fields`

Status: design agreed with the owner 2026-11-03 (hub session be2e02e3); implementing on branch `check-fields`.

## Purpose and boundary

`check --fields` reports **what a case looks like now**: over one window, the global mean of each named field, plus sanity verdicts (present, finite, populated). Its job is semantic spot checks and sanity checks, for run-time monitoring and for procedures such as the clear-sky window.

Three tools, three questions. Nothing crosses between them:

| tool | question | gives |
|---|---|---|
| `runmgr.py check --fields` | what is it now? | one window → one number per field per case, verdicts |
| `exocam-trend` (`trend.py`, `--rundir` for a live run) | how is it changing? | series, drift, slopes, convergence |
| `exocam-analysis` | how is it structured? | maps, profiles, zonal means, plots, saved files |

`--fields` never prints a series, slope, map or profile, and never writes a file. A month-by-month table is a series: it belongs to trend (`--rundir` while the job runs).

**Reporting rule (closes the loophole):** every number that appears in a LOG entry, a monitor report or a journal comes from one of these three tools, and the entry names which one. No hand-rolled reductions. This goes into the `exocam-monitor` agent and the `exocam-analysis` / `exocam-rules` skills when this ships.

## Replaces `--energy` and `--keep`

- `--energy` → `--fields @energy`, and `-n/--energy-years N` → `--last N`. The inputs are the same, `select_last_monthly_hist`, so `--last 10` reads the same months as `datamgr avg --last 10`. `--energy` is removed. Argparse catches the old flag and prints `--energy was replaced by --fields @energy` (exit 2), so a stale skill copy gets a pointer.
- `--keep` is removed. It was the one place `check` wrote a file; `datamgr avg --last N` writes the same product. `check` becomes read-only with no exception.
- No `ncra` and no temp file. Each monthly file is read with netCDF4 for the requested variables only; the global mean of each month is computed and then averaged with equal weight per month, which for these linear means equals `ncra` followed by a spatial mean.

## Area weighting: one rule, shared with trend

Global means use trend's rule (`trend_core.build_area_weights`): cell-edge Δsin(lat) × Δlon, with edges at latitude midpoints and the poles at ±90°, normalised to 1. The current `--energy` uses cos(lat) at the grid points, which gives the polar rows of the FV grid zero weight. This change retires that rule.

The repos stay independent: casemgr carries its own copy of the function, and a test in each repo checks the same fixture against the same expected weights. Two implementations of one written rule, both tested, are preferred to a cross-repo import.

## Field sets — `field_sets.yaml`

The sets live in a YAML file at the repo root, next to `config_registry.yaml`, not in Python. The owner and agents edit it, and adding a set is a data change, not a code change.

```yaml
# Each set names fields and gives each a role.
#   report:  print the global mean (also checked present + finite)
#   verify:  verdict only — present, finite, not all zero (or all zero under --expect zero)
#   derived: name -> expression over reported fields (+ - only); same definitions as exocam-trend
#   show:    which reported/derived names appear in the output, in order (default: all)
#   include: other sets merged in first
energy:
  report:  [TS, FSNT, FLNT, FSNS, FLNS, LHFLX, SHFLX]
  derived: {energy_top: FSNT - FLNT, energy_bot: FSNS - FLNS - LHFLX - SHFLX}
  show:    [TS, FSNT, FLNT, energy_top, energy_bot]
monitor:
  include: [energy]
  report:  [TMQ, ICEFRAC, TGCLDLWP]
  show:    [TS, TMQ, ICEFRAC, TGCLDLWP, energy_top, energy_bot]
clearsky:
  verify:  [FUSC, FDSC, FULC, FDLC, FSNTC, FLNTC, FSNSC, FLNSC]
```

On the command line a set is named `@name`. Sets and bare names combine: `--fields @energy,TMQ,@clearsky`. A bare name is `report`. Derived expressions are restricted to `+`/`−` of field names: they are parsed, never `eval`'d. A malformed file or an unknown set is a usage error that names the file and the key.

## Interface — agent first

The callers are agents. The output is the interface; the text view is a rendering of it.

```
runmgr.py check <cases|--prefix P> --fields SPEC [--last N] [--dir archive|run] [--expect zero] [--json]
runmgr.py check --list-sets [--json]          # sets, their fields and roles, from field_sets.yaml
```

- `--last N`: model years (12·N monthly files), default 1. Fewer files than requested → PARTIAL with a warning, never silently fewer.
- `--dir run`: read the live run directory (`_run_dir`) instead of the archive, for checks while a job runs (clearsky §5 first month).
- `--expect zero`: applies to `verify` fields only; passes when they are all zero (the flag-off check).
- `--json`: one object per case on stdout, nothing else on stdout. Human text otherwise.

**JSON record** (stable keys; additive changes only):

```json
{"case": "exocam_atlasfu_S5", "verdict": "pass",
 "window": {"dir": "archive", "first": "0157-01", "last": "0157-12", "files": 12, "requested": 12},
 "values": {"TS": {"mean": 417.82, "units": "K"}, "energy_top": {"mean": 26.86, "units": "W/m2"}},
 "checks": {"FUSC": {"ok": false, "test": "nonzero", "failed_files": ["0157-03"], "reason": "all-zero"}},
 "provenance": {"tool": "runmgr.py check --fields", "commit": "<git short sha>", "weights": "dsinlat-v1",
                "fields_spec": "@monitor", "field_sets_md5": "<md5>"}}
```

- `verdict`: `pass` | `fail` | `skipped`. `skipped` (no hist files, missing netCDF4/numpy) is never a measurement. It names the cause and the fix (e.g. `export PATH="$HPC_PYTHON_BIN:$PATH"`). This removes the "a skipped `--energy` is not a measurement of zero" caveat.
- `provenance` is what a LOG entry cites under the reporting rule.
- **Exit codes** follow the casemgr convention (`EXIT_*`), with one deliberate extension for this read-only verb: `0` all pass; `1` any case `fail` (a field missing or non-finite, or a verify test failed); `3` nothing failed but some case was `skipped` or short of files; `2` usage. An agent branches on the code and reads the JSON for the detail.
- **Text view**, one line per case plus one indented line per failing check:

```
exocam_atlasfu_S5  0157-01..0157-12 (12 archive)  TS 417.8 K  TMQ 32914  ICEFRAC 0.000  TGCLDLWP 309  energy_top +26.9  energy_bot +1.6  PASS
exocam_atlasfu_D6  0108-01..0117-12 (120 archive)  @clearsky  FAIL
    FDSC  all-zero in 3 files (0112-04, 0112-05, 0115-01)
```

- No prompts; read tier under the hpc-connect guard (casemgr is a script root), so an agent runs it unasked.

## Not in scope

Range checks (`TS:100..1000`): left out until a need arises. Per-level or regional values, series, files: trend and analysis.

## Callers to update when it ships

casemgr `CLAUDE.md` (`--energy`, `--keep` sections), `docs/DEVELOPER_NOTES.md`, `tests/test_hist_selection.py`; exocam-rules `reference/monitoring.md`, `reference/clearsky.md` §5, the `exocam-monitor` agent (plus the reporting rule); hub `CLAUDE.md` workflow table, `program/JAIME.md`, `program/USE_CASES.md`, the `exocam-experiment` skill. Global skills via skill-sync; the casemgr change via ship-tool-change (Discover checkout to the same commit, owner approval).

## Tests

Weights against the trend fixture; set resolution (include, show, bare names, unknown set); derived-expression parser (accepts `+`/`−` of names, rejects anything else); verdicts on synthetic files (all-zero, NaN, fill, missing variable, `--expect zero`); window selection identical to `datamgr avg`; JSON keys and exit codes; the `--energy` stub message.

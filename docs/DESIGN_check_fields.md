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

## Scope of this change: `--fields` only

`--fields` is added beside the existing options. **No other interface changes in this branch** (owner, 2026-11-03): `--energy`, `-n/--energy-years` and `--keep` stay exactly as they are, and no caller is updated. `--fields` is built agent-first. Applying the same treatment to other verbs is later work.

Reading: no `ncra` and no temp file. Each monthly file is read with netCDF4 for the requested variables only. The global mean of each month is computed, then the months are averaged with equal weight, which for these linear means equals `ncra` followed by a spatial mean. Window selection reuses `select_last_monthly_hist`, so `--last 10` reads the same months as `--energy -n 10` and `datamgr avg --last 10`.

### Planned, not done: retire `--energy` and `--keep`

Recorded here so the decision is not re-derived; to be done in a separate change once `--fields` has been used in practice:

- `--energy` → `--fields @energy`, and `-n` → `--last`. A stub then prints `--energy was replaced by --fields @energy` (exit 2).
- `--keep` removed. `datamgr avg --last N` writes the same product, and `check` becomes read-only with no exception.
- `--energy`'s weighting differs from `--fields` (see below). Retiring it removes the second rule from casemgr.

## Area weighting: the model's own `gw`

Global means use the Gaussian/area weights CAM writes into every h0 file, `gw(lat)`, broadcast over longitude and normalised to 1. The weights come from the model output itself, so casemgr depends on no other repo or rule for them. On the FV grid `gw` is the cell-edge area weight, with edges at latitude midpoints and the poles at ±90°. Checked on `exocam_atlasfu_D1.cam.h0.0120-12.nc`: `gw[0]` = 0.000609 = sin(−88°) − sin(−90°).

- File without `gw`: casemgr computes the same cell-edge weights from `lat` in its own code, and `provenance.weights` says which was used (`gw` | `lat-edges`).
- The existing `--energy` keeps its cos(lat)-at-grid-points rule for now. That rule gives the FV polar rows zero weight, so `--energy` and `--fields @energy` can differ slightly. This is a known difference, resolved when `--energy` retires.
- No cross-repo import, shared fixture or shared test. That `gw` also matches what exocam-trend computes is a fact about the grid, not a dependency.

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
 "provenance": {"tool": "runmgr.py check --fields", "commit": "<git short sha>", "weights": "gw", "fields_spec": "@monitor", "field_sets_md5": "<md5>"}}
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

## Docs

This branch: casemgr `CLAUDE.md` gets a `--fields` section, and `docs/DEVELOPER_NOTES.md` its entry. Shipping goes via ship-tool-change (Discover checkout to the same commit, owner approval). Then exocam-rules `reference/clearsky.md` §5 and `reference/monitoring.md` can name `--fields`, and the reporting rule can go into the `exocam-monitor` agent.

When `--energy` retires (later): casemgr `CLAUDE.md` `--energy`/`--keep` sections, `tests/test_hist_selection.py`; exocam-rules `reference/monitoring.md`; hub `CLAUDE.md` workflow table, `program/JAIME.md`, `program/USE_CASES.md`, the `exocam-experiment` skill.

## Tests

Weights from `gw` and the `lat`-edge fallback, which agree on a synthetic FV grid; set resolution (include, show, bare names, unknown set); derived-expression parser (accepts `+`/`−` of names, rejects anything else); verdicts on synthetic files (all-zero, NaN, fill, missing variable, `--expect zero`); window selection identical to `datamgr avg`; JSON keys and exit codes; `--energy` output unchanged.

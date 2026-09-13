# ExoCAM case-management review

Date: 2026-09-13  
Reviewer: Codex Astra  
Status: recommendations only; no implementation changes

## Scope and assessment

The project goal is to minimize the human work required to move from a scientific
idea through CESM/ExoCAM configuration, building, running, analysis, and archiving.
This review prioritizes efficiency, ease of use, and scientific reliability.
Security hardening is outside its scope.

The biggest opportunity is to make **an experiment, rather than an individual
case or command, the unit of work**. Most lifecycle operations already exist.
The remaining manual work is connecting them, remembering what succeeded,
recovering from interruptions, and deciding when the scientific objective has
been reached.

The existing separation between building, run control, inspection, and archiving
is worth retaining. A workflow layer can coordinate these modules while
preserving the distinctions between `continue`, `restart`, and `submit`.

The review inspected the local management code, project documentation, and
relevant local ExoCAM/CESM/ExoRT sources. All 13 existing regression tests passed
locally with `python3 -B tests/test_ncdata_upsert.py`. The HPC was not contacted.
The findings below are based on local source inspection, not a production audit
or a comprehensive HPC integration test. No implementation files were changed.

## Immediate findings, in priority order

### 1. Missing Fortran templates can omit requested configuration changes

`_generate_one_matrix()` warns when `exoplanet_mod.F90` is unavailable and
continues with `exoplanet_mod_content = None`. `_heredoc_exoplanet_mod()` then
emits only comments requesting manual installation. The generated script still
proceeds toward compilation.

This matters particularly when generating locally from a clone source that
exists only on the HPC: requested Fortran changes can be omitted while the
script compiles inherited source. A successful build would not establish that
the requested experiment was configured.

Recommendation: make unavailable templates either a clearly deferred HPC
preparation step or a generation error. Before compilation, verify the prepared
configuration against the resolved specification.

Sources: [build.py:1787](build.py#L1787),
[build.py:1806](build.py#L1806), [build.py:1243](build.py#L1243).

### 2. Scheduler checks depend on the last CESM event

`_probe_status()` only checks the queue after `run started` or `run SUCCESSFUL`.
A newly submitted, pending case whose last event is `build complete` therefore
still appears as `BUILT`. Repeating submission can submit it again. `cmd_check()`
also contains its own copy of this conditional status logic.

Recommendation: query scheduler state independently of the last CESM event,
persist submitted job IDs, and distinguish pending, running, segment complete,
and failed. Keep one shared implementation of status resolution.

Sources: [runmgr.py:323](runmgr.py#L323),
[runmgr.py:817](runmgr.py#L817), [runmgr.py:1128](runmgr.py#L1128),
[manage_utils.py:312](manage_utils.py#L312).

### 3. Several commands print failures but return process-level success

Ordinary generation reports skipped cases without a failing exit status.
Submission reports per-case errors without propagating failure to the command's
exit status. Similar gaps exist in `patch`, `rebuild`, and the submission portion
of `make --send-it`.

A calling workflow cannot reliably distinguish success from partial or complete
failure using these command results.

Recommendation: introduce consistent per-case results and aggregate exit
statuses. Distinguish successful, failed, intentionally skipped, and aborted
operations, while allowing a batch to continue past an individual case failure.

Sources: [build.py:1677](build.py#L1677),
[runmgr.py:748](runmgr.py#L748), [build.py:1984](build.py#L1984),
[build.py:2039](build.py#L2039), [build.py:2133](build.py#L2133).

### 4. Averaging commands use different time selections and stream filtering

`runmgr check --energy -n N` selects the last `12*N` non-average CAM h0 files.
`datamgr avg --last N` selects the last N year labels using
`_hist_keep_years_filter()` and does not restrict its averaging inputs to h0.

For example, with monthly output through March and sufficient preceding history,
`avg --last 1` can select three months while `check --energy -n 1` selects twelve.
Additional history streams can enter the archival average. Consequently, the
same `avg_lastNyr` filename does not guarantee the same period or inputs, despite
the documented interchangeability.

Recommendation: share a history-selection interface that specifies component,
stream, calendar, actual time coverage, missing intervals, and averaging weights.
Record the selected period and inputs with the product.

Sources: [runmgr.py:904](runmgr.py#L904),
[datamgr.py:1322](datamgr.py#L1322),
[manage_utils.py:182](manage_utils.py#L182).

### 5. SE newcase template-copy paths do not match the local ExoCAM layout

The generated newcase script uses `CONFIG_TYPE` directly in its SourceMods and
namelist copy paths. Casemgr configuration names include `cam_aqua_se_ne5` and
`cam_aqua_se_ne16`, whereas the inspected ExoCAM tree uses
`cam_aqua_se/SourceMods` and `cam_aqua_se/namelist_files/ne5np4` or `ne16np4`.
The Python-side Fortran-template lookup strips the resolution suffix, but the
generated copy commands do not apply the corresponding mapping.

Recommendation: explicitly map each supported configuration to its SourceMods
and namelist directories, and exercise the generated preparation steps for every
advertised configuration.

Sources: [build.py:1425](build.py#L1425),
[build.py:1789](build.py#L1789),
[config_registry.yaml:40](config_registry.yaml#L40); local model directory
`/Users/wolfe/models/ExoCAM/cesm1.2.1/configs/cam_aqua_se/`.

## Recommended development sequence

### 1. Make configuration resolution explicit and complete

Produce a resolved experiment record before execution. For each case, show
matrix overrides, inherited clone settings, input files, source modifications,
and required build actions.

Separate local validation from HPC validation:

- Locally: check structure, supported keys, types, and internally consistent
  settings.
- On the HPC: resolve clone contents and check actual input files.
- Before compilation: compare the prepared configuration with the resolved
  specification.

Extend validation beyond the current parameter lists:

- Detect unknown top-level keys and requested Fortran parameters absent from
  the selected template.
- Distinguish inherited clone settings from supported overrides. The clone
  generator currently does not apply ordinary `nlev` or `cloud_scheme` changes
  to `CAM_CONFIG_OPTS`.
- Validate namelist types and allowed values using CESM's definitions, with
  explicit additions for ExoCAM extensions.
- Make unresolved configuration visible as unfinished preparation, rather than
  representing it as a completed executable recipe.

This eliminates much of the uncertainty between what the YAML requests and what
the case actually receives.

Relevant sources: [build.py:624](build.py#L624),
[build.py:761](build.py#L761), [build.py:1486](build.py#L1486), and
`/Users/wolfe/models/cesm1_2_1/models/atm/cam/bld/namelist_files/namelist_definition.xml`.

### 2. Give every operation a consistent interface for automation

Add structured per-case results, meaningful exit statuses, persistent job IDs,
and an explicit noninteractive execution option. Retain readable terminal output
as another presentation of those results.

Persist the job ID already returned by `submit_case()`. Query scheduler state
independently of CESM's last event. Treat segment success and experiment success
as separate facts.

Allow the user to authorize an entire experiment with its run limits and
retention choices once, so routine transitions can proceed without repeated
prompts. This is a proposed evolution of the current interactive interface,
not a claim that existing confirmation requirements have already changed.

Stream build output into an attempt-specific log while the build runs. Currently,
`make` captures output in memory and writes its log afterward; `rebuild_case()`
returns only a final-line error summary on failure. Live logs and preserved
attempts would substantially improve troubleshooting.

Relevant sources: [manage_utils.py:255](manage_utils.py#L255),
[manage_utils.py:312](manage_utils.py#L312),
[build.py:1841](build.py#L1841), [build.py:890](build.py#L890).

### 3. Add a persistent experiment workflow that resumes unfinished work

The workflow should record:

- Case membership and dependencies, such as a control run supplying an eruption
  restart.
- Successful preparation, build, run, analysis, and archival steps.
- The outputs and conditions that constitute completion.
- Which failed steps may be retried automatically.

Rerunning the workflow should inspect current state and continue from the
unfinished step. It should tolerate terminal disconnection or a stopped
controller.

Start with a small controller running on the HPC and storing its progress there.
The local checkout can prepare and inspect experiment specifications. A single
CLI entry point would improve discoverability, but persistence and recovery are
the substantive improvements.

Automatic recovery should initially cover operational interruptions such as
wall-clock limits or failed submissions. Changes to timestep, physics, or
initial conditions should create a recorded experiment revision because they
change the scientific configuration.

Keep the existing module responsibilities: the workflow coordinates build and
run operations; it does not move compilation into `runmgr.py` or make rebuilds
implicitly choose continuation versus restart.

### 4. Connect scientific completion to existing analysis tools

`COMPLETE` currently means a successful CESM segment. An experiment may instead
require a particular model date, a stable climate, or complete coverage of an
eruption response.

Let the experiment declare its completion rule:

- Equilibrium work: a minimum duration plus configurable trend and
  energy-balance criteria evaluated across multiple windows.
- Transient work: the requested interval and output completeness.

Use small adapters to `exocam-trend`, `exocam-analysis`, and `exocam-spectral`.
Pass explicit paths, time windows, gravity, molecular weight, and spectral
configuration. The ExoCAM analysis skill informed this recommendation to reuse
the existing scientific tools rather than duplicate their calculations here.

Unify history selection before using diagnostics to drive automatic decisions.
For lightweight monitoring, compute and cache scalar diagnostics from the
required variables; the current energy check averages entire files each time.
Keep full averaged datasets available when they are requested products.

Relevant sources: [runmgr.py:760](runmgr.py#L760),
[runmgr.py:904](runmgr.py#L904), and the local companion repositories
`/Users/wolfe/Desktop/projects/exocam-trend`, `exocam-analysis`, and
`exocam-spectral`.

### 5. Preserve enough provenance to reproduce and reuse a result

The scanned registry describes current configuration, but does not establish
which source revision produced a particular executable or output segment. After
a failed rebuild, SourceMods may contain a new parameter while the executable
still represents the previous build.

Record the resolved matrix, clone identity, actual SourceMods content,
ExoCAM/ExoRT revisions and local modifications, compiler environment, input
identities, successful build, job attempts, and analysis products. Keep this
record separate from the registry regenerated by `scan --update`.

This supports the existing master-case regression design in
`/Users/wolfe/models/ExoCAM/REGRESSION_HARNESS_DESIGN.md`: versioned, validated
`cam_aqua_fv`, `cam_land_fv`, and `cam_mixed_fv` masters can supply production
clones and regression baselines.

Begin with cheap configuration checks, build checks, and short runs. Reserve
equilibrium comparisons for changes that require them. Retain validated masters
as useful artifacts, and report which fields differ and by how much when a
comparison fails.

Relevant sources: [scan.py:114](scan.py#L114),
[build.py:2039](build.py#L2039), and the regression design document above.

### 6. Automate input preparation and archive policies

The albedo integration proposed in [PROPOSED_PLANS.md](PROPOSED_PLANS.md) remains
useful. Make the existing calculator callable without command-line parsing or
working-directory assumptions, retain explicit overrides, and record the
spectrum and reflectance inputs used.

IC preparation needs a more precise assessment than the current proposal.
A Python tool already exists at
`/Users/wolfe/models/ExoCAM/tools/py_progs/mod_cam.i.file.py`, but it declares
`--surface_pressure` at line 59 without using the parsed option. Its pressure
rescaling section is commented out. Complete and validate that functionality
before connecting it to automatic preparation; the presence of the Python tool
does not establish a working pressure-conversion implementation.

For archiving, introduce named retention recipes containing required averages,
selected history, restart sets, configuration, and provenance. Apply the chosen
recipe once the required products are complete. Record dependencies so useful
reference restarts and clone masters remain available.

Relevant sources: [PROPOSED_PLANS.md](PROPOSED_PLANS.md),
[datamgr.py:860](datamgr.py#L860),
`/Users/wolfe/models/ExoCAM/tools/py_progs/broadband_albedo_calculator.py`, and
`/Users/wolfe/models/ExoCAM/tools/py_progs/mod_cam.i.file.py`.

## Smaller usability and maintenance improvements

- Repair the README's bare-`make` examples: bare `make` lists scripts, while the
  examples currently imply it builds them. See [README.md:105](README.md#L105).
- Replace the obsolete `exo_build.py` command and staged-Fortran description in
  [experiment_matrix.example.yaml](exp_matrices/experiment_matrix.example.yaml).
- Centralize duplicated parameter and configuration metadata so exposing a new
  model option does not require maintaining several independent lists.
- Add matrix sweeps with deterministic case naming to reduce repetitive YAML
  editing and make experiment membership explicit.
- Expand inexpensive regression coverage around configuration resolution,
  scheduler states, aggregate command outcomes, and history selection. The
  existing 13 tests cover the namelist-upsert incident well, but do not exercise
  the complete experiment lifecycle.

## Recommended next milestone

Run one small clone-based experiment that:

1. Prepares the requested configuration correctly.
2. Builds and runs two segments.
3. Survives interruption of its controller and resumes unfinished work.
4. Produces a defined diagnostic product.
5. Applies a selected archive recipe without intermediate prompts.

Exercise the workflow with simulated command results locally, then validate
that complete path on the HPC. Measure progress by the number of human
interventions per finished scientific experiment.

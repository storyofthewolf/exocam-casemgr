#!/usr/bin/env python3
"""
runmgr.py — ExoCAM run control tool

Manages run mechanics for in-progress or recently completed cases: status
inspection, file browsing, and CESM xml + SLURM submission operations.
Data management (disk reporting, purging, averaging, retirement) lives in
datamgr.py.

Paths are read from config_registry.yaml (paths.caseroot, paths.rundir,
paths.archive, paths.long_term). Override any path with --caseroot,
--rundir, --archive, or --long-term.

Cases are discovered by scanning those directories on disk — no separate
registry file is required.

SUBCOMMANDS
-----------
  check                 Show run status for cases (CaseStatus + SLURM probe);
                        defaults to all discoverable cases when given no names.
                        --dir lists individual files in a storage area for one case.
  xml                   Query/change CESM XML variables ad hoc (no CONTINUE_RUN,
                        no sbatch); --query VAR to inspect, --change VAR=VALUE to set
  continue              Set CONTINUE_RUN=TRUE and sbatch the run script;
                        optionally apply --set VAR=VALUE xmlchange calls first
  restart               Set CONTINUE_RUN=FALSE, apply xmlchange calls, and sbatch;
                        use to fix and rerun from scratch after a completed or failed run
  submit                sbatch a built case as-is (no xmlchange); the launch step
                        after `build.py make`. Skips cases with no <case>.run.

SAFETY
------
  xml, continue, restart, and submit require explicit case names or --prefix.
  There is no --all flag. check is read-only and needs no --execute; so is
  `xml --query` (without --change).

  Double-gate ergonomics (matching build.py make): these verbs first print a
  per-case preview, then --execute prints ONE batch [yes/no] confirmation
  before acting on the whole set. RUNNING/RESUBMITTED cases are hard-blocked
  (dropped from the set); surprising statuses (non-COMPLETE, non-BUILT, ...)
  are flagged in the preview but not separately prompted — the single batch
  confirm covers them.

  REST_N/STOP_N: continue/restart --set and xml --change print a WARNING (not
  a block) when the pending edit would leave the restart interval longer than
  the run — the segment then ends before a restart is written, leaving an
  incomplete fileset that crashes the next CONTINUE_RUN=TRUE. build.py enforces
  this as a hard error at generate time; here it is advisory, and the single
  batch [yes/no] decides.

Run any subcommand with --help for full options, e.g.:
  python runmgr.py check --help
  python runmgr.py xml --help
  python runmgr.py continue --help
  python runmgr.py restart --help
  python runmgr.py submit --help
"""

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manage_utils import (
    ARCHIVE_MODELS, AVG_HIST_DEFAULT_MODELS, ACTIVE_STATUSES, MODEL_STEM,
    DEFAULT_CONFIG, load_paths,
    dir_size_bytes, fmt_size, list_files_with_size, discover_cases,
    _hist_year, hist_info_line, restart_sets, submit_case,
    _require_cases, batch_confirm, preview_hint,
)

# ---------------------------------------------------------------------------
# Subcommand: continue
# ---------------------------------------------------------------------------

def _read_xml_var(xml_path, var_name):
    """Return the value of an XML entry id=var_name from a CESM env_*.xml file.

    Parses the file with ElementTree and looks for:
      <entry id="VAR_NAME" value="..."/>  (CESM 1.x format)
    Returns the value string, or None if not found or on any parse error.
    """
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for elem in root.iter('entry'):
            if elem.get('id') == var_name:
                return elem.get('value')
    except Exception:
        pass
    return None


# env files searched by _read_case_xml_var. env_run.xml first: it holds the
# run-control vars (CONTINUE_RUN, STOP_N, RESUBMIT, ...) these verbs
# overwhelmingly read.
_ENV_XML_FILES = ('env_run.xml', 'env_build.xml', 'env_case.xml', 'env_mach_pes.xml')


def _read_case_xml_var(case_dir, var_name):
    """Return VAR's value from whichever env_*.xml in case_dir defines it.

    ./xmlchange finds a variable in any env file, so the previews must look
    in the same places — otherwise a build-time var (e.g. CAM_CONFIG_OPTS)
    previews as '?' even though the change would apply.
    """
    for name in _ENV_XML_FILES:
        val = _read_xml_var(os.path.join(case_dir, name), var_name)
        if val is not None:
            return val
    return None


def _resolve_cases(args, paths, verb):
    """Resolve the case list from explicit names or --prefix.

    Thin wrapper over manage_utils._require_cases — the same selection helper
    every datamgr.py destructive verb uses (explicit-names-or---prefix, mutual
    exclusion, no --all flag, explicit names validated against disk) — plus a
    hard exit when nothing matches, since every run-control verb needs at
    least one case to act on.
    """
    cases = _require_cases(discover_cases(paths), args)
    if not cases:
        sys.exit(f"ERROR: no cases to act on for {verb}.")
    return cases


def _parse_set_pairs(items, flag='--set'):
    """Parse a list of 'VAR=VALUE' strings into ordered (VAR, VALUE) tuples.

    Exits on malformed entries (missing '=' or empty variable name).
    """
    pairs = []
    for item in (items or []):
        if '=' not in item:
            sys.exit(f"ERROR: {flag} requires VAR=VALUE format, got: {item!r}")
        var, _, val = item.partition('=')
        var = var.strip()
        if not var:
            sys.exit(f"ERROR: empty variable name in {flag} {item!r}")
        pairs.append((var, val.strip()))
    return pairs


def _apply_xmlchange(case_dir, var, val):
    """Run ./xmlchange VAR=VALUE in case_dir. Raises RuntimeError on failure.

    The single xmlchange code path shared by continue, restart, and xml.
    """
    try:
        result = subprocess.run(
            ['./xmlchange', f'{var}={val}'],
            cwd=case_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except FileNotFoundError:
        raise RuntimeError("./xmlchange not found in case dir")
    if result.returncode != 0:
        raise RuntimeError(
            f"xmlchange {var}={val} failed: {result.stderr.strip()}")


_REST_STOP_VARS = ('REST_N', 'STOP_N', 'REST_OPTION', 'STOP_OPTION')


def _rest_stop_warning(case_dir, pending):
    """Warn when pending XML edits would leave REST_N outrunning STOP_N.

    The run-time counterpart to build.py's _verify_rest_stop guard, but a
    WARNING rather than a hard block: at this point the case already exists and
    the user may be deliberately staging an odd pair, so this reports and lets
    the single batch [yes/no] decide.

    Unlike the build-time check, the four values are per-case live XML state
    rather than a matrix, so a --set touching only REST_N must be judged against
    the case's existing STOP_N. `pending` (an ordered list of (VAR, VALUE) about
    to be applied) is merged over what the env xmls currently hold; last write
    wins, matching the order _apply_xmlchange runs them in.

    Returns a warning string, or None when the pair is fine or unjudgeable.
    Silent unless a REST/STOP var is actually being changed — this must never
    editorialize about pre-existing state the user isn't touching.
    """
    if not any(var.upper() in _REST_STOP_VARS for var, _ in pending):
        return None

    # Lazy import (the datamgr -> runmgr._probe_status pattern): the unit table
    # is shared with build.py's _verify_rest_stop so the build-time and run-time
    # guards can never disagree about what a REST/STOP pair means.
    from build import _OPTION_DAYS

    eff = {}
    for var in _REST_STOP_VARS:
        cur = _read_case_xml_var(case_dir, var)
        if cur is not None:
            eff[var] = cur
    for var, val in pending:                      # pending overrides current,
        if var.upper() in _REST_STOP_VARS:        # later --set wins over earlier
            eff[var.upper()] = val

    if not all(var in eff for var in _REST_STOP_VARS):
        return None  # can't read one side (unbuilt/odd case dir) — say nothing

    stop_unit = str(eff['STOP_OPTION']).strip().lower()
    rest_unit = str(eff['REST_OPTION']).strip().lower()
    if stop_unit not in _OPTION_DAYS or rest_unit not in _OPTION_DAYS:
        return None  # nsteps/date/ifdays0 — not a fixed interval

    try:
        stop_n = int(str(eff['STOP_N']).strip())
        rest_n = int(str(eff['REST_N']).strip())
    except (TypeError, ValueError):
        return None

    stop_days = stop_n * _OPTION_DAYS[stop_unit]
    rest_days = rest_n * _OPTION_DAYS[rest_unit]
    if rest_days <= stop_days:
        return None

    if stop_unit == rest_unit:
        detail = f"REST_N={rest_n} > STOP_N={stop_n} (both {stop_unit})"
    else:
        detail = (f"REST_N={rest_n} {rest_unit} (~{rest_days:g}d) > "
                  f"STOP_N={stop_n} {stop_unit} (~{stop_days:g}d)")
    return (f"WARNING: {detail} — the segment ends before a restart is "
            f"written, so the restart fileset will be incomplete and the next "
            f"CONTINUE_RUN=TRUE will crash.")


def _probe_status(case_dir, case):
    """Return the CaseStatus + SLURM-probe-derived status label for a case.

    Mirrors the gate logic used across continue/restart/submit: reads the last
    CaseStatus event, and for run started / run SUCCESSFUL refines via squeue +
    run.out (RESUBMITTED / WALLCLOCK / RUNNING?). Returns 'NO_CASEDIR' if there
    is no CaseStatus file.
    """
    cs = _parse_casestatus(os.path.join(case_dir, 'CaseStatus'))
    if cs is None:
        return 'NO_CASEDIR'
    status_label = cs['status']
    if cs['last_event'] and (
        cs['last_event'].startswith('run started') or
        cs['last_event'].startswith('run SUCCESSFUL')
    ):
        job_queued = _squeue_probe(case)
        if job_queued is True and cs['last_event'].startswith('run SUCCESSFUL'):
            status_label = 'RESUBMITTED'
        elif job_queued is False and cs['last_event'].startswith('run started'):
            run_out_path = os.path.join(case_dir, 'run.out')
            status_label = 'WALLCLOCK' if _run_out_walltimeout(run_out_path) else 'RUNNING?'
    return status_label


def cmd_xml(args, paths):
    """
    Query and/or change CESM XML variables ad hoc — no CONTINUE_RUN, no sbatch.

    This is a thin wrapper over CESM's native xmlquery/xmlchange, scoped to a
    set of cases. Unlike continue/restart, it does not force CONTINUE_RUN and
    never submits a job: use it to inspect XML across a group (e.g. with
    --prefix) and optionally rewrite it, without committing to a run.

      --query VAR     (repeatable) print VAR's current value per case
      --change VAR=V  (repeatable) set VAR=V via xmlchange

    At least one of --query / --change is required; they may be combined.
    --query is always read-only. --change defaults to preview; pass --execute
    to apply. On --change --execute, the per-case preview (which flags any
    RUNNING/RESUBMITTED cases whose XML edits only take effect next segment) is
    followed by a single batch [yes/no] before any change is applied — the same
    double-gate ergonomics as the other run-control verbs. Query mode never gates.

    Changing REST_N/STOP_N (or their _OPTION units) into a pair where the
    restart interval outruns the run prints a WARNING in the preview — not a
    block. The resulting restart fileset would be incomplete.

    Requires explicit case names or --prefix — no --all flag.
    """
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    cases       = _resolve_cases(args, paths, 'xml')
    query_vars  = list(args.query or [])
    change_vars = _parse_set_pairs(args.change, flag='--change')

    if not query_vars and not change_vars:
        sys.exit("ERROR: xml requires at least one --query VAR or --change VAR=VALUE.")

    # Phase 1 — preview every case; collect the ones eligible for --change.
    actionable = []  # case_dirs that have a real dir and pending changes
    for case in cases:
        case_dir = os.path.join(caseroot, case)
        if not os.path.isdir(case_dir):
            print(f"  {case}: ERROR: caseroot directory not found: {case_dir}")
            continue

        # Read current values for everything we're about to show or change.
        cur_q = {var: (_read_case_xml_var(case_dir, var) or '?') for var in query_vars}
        cur_c = {var: (_read_case_xml_var(case_dir, var) or '?') for var, _ in change_vars}

        # Flag active jobs in the preview — edits only apply on the next segment.
        status_note = ''
        if change_vars:
            status_label = _probe_status(case_dir, case)
            if status_label in ACTIVE_STATUSES:
                status_note = f"  [{status_label} — edits apply next segment]"

        print(f"  {case}{status_note}")
        for var in query_vars:
            print(f"    {var} = {cur_q[var]}")
        for var, new_val in change_vars:
            print(f"    {var}: {cur_c[var]} -> {new_val}")
        rs_warn = _rest_stop_warning(case_dir, change_vars)
        if rs_warn:
            print(f"    ! {rs_warn}")

        if change_vars:
            actionable.append((case, case_dir))

    if not change_vars:
        return
    if not args.execute:
        preview_hint(args.execute)
        return
    if not actionable:
        print("\nNo cases to change.")
        return

    # Phase 2 — single batch gate, then apply.
    if not batch_confirm("Apply XML changes to", len(actionable)):
        print("Aborted.")
        return

    for case, case_dir in actionable:
        try:
            for var, val in change_vars:
                _apply_xmlchange(case_dir, var, val)
        except RuntimeError as e:
            print(f"  {case}: ERROR: {e}")
            continue
        print(f"  {case}: applied {len(change_vars)} change(s)")


def cmd_continue(args, paths):
    """
    Set CONTINUE_RUN=TRUE and submit the run script via sbatch.

    Use --set VAR=VALUE (repeatable) to apply any xmlchange calls before
    submitting — e.g. --set STOP_N=10 --set RESUBMIT=9.

    A --set that leaves REST_N outrunning STOP_N (judged against the case's
    live XML, so --set STOP_N alone can trigger it) prints a WARNING in the
    preview — not a block. The restart fileset would be incomplete.

    Status gating (checked via CaseStatus + SLURM probe):
      RUNNING / RESUBMITTED  — hard block: skipped, never submitted
      COMPLETE               — the normal case
      anything else          — flagged in the preview (not blocked)

    Cases with no <case>.run are skipped before any xmlchange is applied —
    a case sbatch cannot launch is never modified.

    The per-case preview is followed by a single batch [yes/no] before any job
    is submitted (the same double-gate as build.py make). Without --execute,
    prints the preview and exits. Requires explicit case names or --prefix —
    no --all flag.
    """
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    cases    = _resolve_cases(args, paths, 'continue')
    set_vars = _parse_set_pairs(args.set)

    # Phase 1 — preview; hard-block active jobs; collect actionable cases.
    actionable = []  # (case, case_dir)
    for case in cases:
        case_dir = os.path.join(caseroot, case)
        if not os.path.isdir(case_dir):
            print(f"  {case}: ERROR: caseroot directory not found: {case_dir}")
            continue

        # Pre-check the run script before anything else: submit checks it,
        # and continuing without it would apply the xmlchange calls only to
        # have sbatch fail afterwards, leaving CONTINUE_RUN already flipped.
        run_script = os.path.join(case_dir, f'{case}.run')
        if not os.path.isfile(run_script):
            print(f"  {case}: SKIP — not built ({case}.run not found). "
                  f"Run build.py make first.")
            continue

        cur_continue = _read_case_xml_var(case_dir, 'CONTINUE_RUN') or '?'
        cur_vals = {var: (_read_case_xml_var(case_dir, var) or '?') for var, _ in set_vars}

        status_label = _probe_status(case_dir, case)
        if status_label in ACTIVE_STATUSES:
            print(f"  {case}: [{status_label}] — skipping (job already active)")
            continue

        flag = '' if status_label == 'COMPLETE' else '  <- not COMPLETE'
        print(f"  {case}  [{status_label}]{flag}")
        print(f"    CONTINUE_RUN: {cur_continue} -> TRUE")
        for var, new_val in set_vars:
            print(f"    {var}: {cur_vals[var]} -> {new_val}")
        rs_warn = _rest_stop_warning(case_dir, set_vars)
        if rs_warn:
            print(f"    ! {rs_warn}")
        print(f"    sbatch: {run_script}")
        actionable.append((case, case_dir))

    if not args.execute:
        preview_hint(args.execute)
        return
    if not actionable:
        print("\nNo cases to submit.")
        return

    # Phase 2 — single batch gate, then apply xmlchange + sbatch.
    if not batch_confirm("Continue (CONTINUE_RUN=TRUE) and submit", len(actionable)):
        print("Aborted.")
        return

    for case, case_dir in actionable:
        try:
            _apply_xmlchange(case_dir, 'CONTINUE_RUN', 'TRUE')
            for var, val in set_vars:
                _apply_xmlchange(case_dir, var, val)
        except RuntimeError as e:
            print(f"  {case}: ERROR: {e}")
            continue

        ok, detail = submit_case(case_dir, case)
        if ok:
            print(f"  {case}: submitted job {detail}")
        else:
            print(f"  {case}: ERROR: {detail}")


# ---------------------------------------------------------------------------
# Subcommand: restart
# ---------------------------------------------------------------------------

def cmd_restart(args, paths):
    """
    Set CONTINUE_RUN=FALSE, apply arbitrary xmlchange calls, and sbatch the run script.

    Use this to fix and resubmit a case from the beginning — e.g. after
    identifying a wrong parameter value in a completed or failed run.

    XML variable changes are specified with --set VAR=VALUE (repeatable).
    CONTINUE_RUN=FALSE is always applied first; --set changes follow in order.

    A --set that leaves REST_N outrunning STOP_N (judged against the case's
    live XML, so --set STOP_N alone can trigger it) prints a WARNING in the
    preview — not a block. The restart fileset would be incomplete.

    Status gating (checked via CaseStatus + SLURM probe):
      RUNNING / RESUBMITTED  — hard block: skipped, never submitted
      COMPLETE               — the normal case
      anything else          — flagged in the preview (not blocked)

    Cases with no <case>.run are skipped before any xmlchange is applied —
    a case sbatch cannot launch is never modified.

    The per-case preview is followed by a single batch [yes/no] before any job
    is submitted (the same double-gate as build.py make). Without --execute,
    prints the preview and exits. Requires explicit case names or --prefix —
    no --all flag.
    """
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    cases    = _resolve_cases(args, paths, 'restart')
    set_vars = _parse_set_pairs(args.set)

    # Phase 1 — preview; hard-block active jobs; collect actionable cases.
    actionable = []  # (case, case_dir)
    for case in cases:
        case_dir = os.path.join(caseroot, case)
        if not os.path.isdir(case_dir):
            print(f"  {case}: ERROR: caseroot directory not found: {case_dir}")
            continue

        # Pre-check the run script before anything else (same rationale as
        # continue: never apply xmlchange to a case sbatch cannot launch).
        run_script = os.path.join(case_dir, f'{case}.run')
        if not os.path.isfile(run_script):
            print(f"  {case}: SKIP — not built ({case}.run not found). "
                  f"Run build.py make first.")
            continue

        # Read current values for CONTINUE_RUN and each var being changed
        cur_continue = _read_case_xml_var(case_dir, 'CONTINUE_RUN') or '?'
        cur_vals = {var: (_read_case_xml_var(case_dir, var) or '?') for var, _ in set_vars}

        status_label = _probe_status(case_dir, case)
        if status_label in ACTIVE_STATUSES:
            print(f"  {case}: [{status_label}] — skipping (job already active)")
            continue

        flag = '' if status_label == 'COMPLETE' else '  <- not COMPLETE'
        print(f"  {case}  [{status_label}]{flag}")
        print(f"    CONTINUE_RUN: {cur_continue} -> FALSE")
        for var, new_val in set_vars:
            cur = cur_vals.get(var, '?')
            print(f"    {var}: {cur} -> {new_val}")
        rs_warn = _rest_stop_warning(case_dir, set_vars)
        if rs_warn:
            print(f"    ! {rs_warn}")
        print(f"    sbatch: {run_script}")
        actionable.append((case, case_dir))

    if not args.execute:
        preview_hint(args.execute)
        return
    if not actionable:
        print("\nNo cases to submit.")
        return

    # Phase 2 — single batch gate, then apply xmlchange + sbatch.
    if not batch_confirm("Restart (CONTINUE_RUN=FALSE) and submit", len(actionable)):
        print("Aborted.")
        return

    for case, case_dir in actionable:
        try:
            _apply_xmlchange(case_dir, 'CONTINUE_RUN', 'FALSE')
            for var, val in set_vars:
                _apply_xmlchange(case_dir, var, val)
        except RuntimeError as e:
            print(f"  {case}: ERROR: {e}")
            continue

        ok, detail = submit_case(case_dir, case)
        if ok:
            print(f"  {case}: submitted job {detail}")
        else:
            print(f"  {case}: ERROR: {detail}")


# ---------------------------------------------------------------------------
# Subcommand: submit
# ---------------------------------------------------------------------------

def cmd_submit(args, paths):
    """
    sbatch a built case's run script as-is — no XML changes.

    Use this to launch cases after `build.py make` (which builds but does not
    submit) once you have inspected the build. Unlike continue/restart, submit
    makes no xmlchange calls: it runs exactly what you built. It is not this
    tool's job to build — a case with no <case>.run is skipped with a message.

    Status gating (checked via CaseStatus + SLURM probe):
      RUNNING / RESUBMITTED  — hard block: skipped (a job is already active)
      BUILT / COMPLETE       — the normal case (BUILT is the normal post-make
                               state; COMPLETE covers re-launch and clones that
                               inherit the source's CaseStatus)
      anything else          — flagged in the preview (not blocked)

    The per-case preview is followed by a single batch [yes/no] before any job
    is submitted (the same double-gate as build.py make). Without --execute,
    prints the preview and exits. Requires explicit case names or --prefix —
    no --all flag.
    """
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    cases = _resolve_cases(args, paths, 'submit')

    # Phase 1 — preview; hard-block active jobs and unbuilt cases; collect the rest.
    actionable = []  # (case, case_dir)
    for case in cases:
        case_dir = os.path.join(caseroot, case)
        if not os.path.isdir(case_dir):
            print(f"  {case}: ERROR: caseroot directory not found: {case_dir}")
            continue

        run_script = os.path.join(case_dir, f'{case}.run')
        if not os.path.isfile(run_script):
            print(f"  {case}: SKIP — not built ({case}.run not found). "
                  f"Run build.py make first.")
            continue

        status_label = _probe_status(case_dir, case)
        if status_label in ACTIVE_STATUSES:
            print(f"  {case}: [{status_label}] — skipping (job already active)")
            continue

        flag = '' if status_label in ('BUILT', 'COMPLETE') else '  <- not BUILT/COMPLETE'
        print(f"  {case}  [{status_label}]{flag}")
        print(f"    sbatch: {run_script}")
        actionable.append((case, case_dir))

    if not args.execute:
        preview_hint(args.execute)
        return
    if not actionable:
        print("\nNo cases to submit.")
        return

    # Phase 2 — single batch gate, then sbatch.
    if not batch_confirm("Submit", len(actionable)):
        print("Aborted.")
        return

    for case, case_dir in actionable:
        ok, detail = submit_case(case_dir, case)
        if ok:
            print(f"  {case}: submitted job {detail}")
        else:
            print(f"  {case}: ERROR: {detail}")


# ---------------------------------------------------------------------------
# Subcommand: check
# ---------------------------------------------------------------------------

# CaseStatus event prefix → status label
_STATUS_MAP = {
    'run SUCCESSFUL':  'COMPLETE',
    'run FAILED':      'FAILED',
    'run started':     'RUNNING',
    'build complete':  'BUILT',
    'cesm_setup':      'CLEANED',   # "cesm_setup -clean"
}


def _parse_casestatus(casestatus_path):
    """Parse CaseStatus file. Only the last non-blank line is used.

    Segment history counts are intentionally not reported: CaseStatus is
    inherited verbatim by cloned cases, making cumulative counts unreliable.

    Returns dict with keys:
      status     : str label (RUNNING/COMPLETE/FAILED/BUILT/CLEANED/UNKNOWN)
      last_event : raw event prefix of the last non-blank line
      last_ts    : timestamp string of the last non-blank line
    Returns None if the file does not exist.
    """
    if not os.path.isfile(casestatus_path):
        return None

    with open(casestatus_path) as f:
        raw_lines = f.readlines()

    lines = [l.rstrip('\n') for l in raw_lines if l.strip()]
    if not lines:
        return {'status': 'UNKNOWN', 'last_event': None, 'last_ts': None}

    # Only the last non-blank line matters.
    line = lines[-1]
    parts = line.rsplit(None, 2)
    if len(parts) < 3:
        event = line.strip()
        ts = ''
    else:
        event = parts[0].strip()
        ts = f"{parts[1]} {parts[2]}"

    status = 'UNKNOWN'
    for prefix, label in _STATUS_MAP.items():
        if event.startswith(prefix):
            status = label
            break

    return {'status': status, 'last_event': event, 'last_ts': ts}


# Per-process squeue snapshot. Sentinel = not yet probed; None = squeue
# unavailable; otherwise a frozenset of the user's active job names.
_ACTIVE_JOBS_UNPROBED = object()
_active_jobs_cache = _ACTIVE_JOBS_UNPROBED


def _active_jobs():
    """Return the set of the invoking user's queued/running SLURM job names,
    or None if squeue is unavailable (missing binary or non-zero exit —
    graceful degradation).

    One `squeue --me` snapshot per process, memoized: these tools are one
    command per process, and the previous per-case `squeue --name <case>`
    probe cost one controller round-trip per case in every bulk preview.
    Job names equal case names (build.py defaults #SBATCH -J to the full
    case name), and they are the user's own jobs, so --me scoping is exact.
    """
    global _active_jobs_cache
    if _active_jobs_cache is _ACTIVE_JOBS_UNPROBED:
        try:
            result = subprocess.run(
                ['squeue', '--me', '-h', '-o', '%j'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except FileNotFoundError:
            _active_jobs_cache = None
        else:
            if result.returncode != 0:
                _active_jobs_cache = None
            else:
                _active_jobs_cache = frozenset(
                    line.strip() for line in result.stdout.splitlines()
                    if line.strip())
    return _active_jobs_cache


def _squeue_probe(case):
    """Return True if a SLURM job named *case* is currently queued/running.

    Returns None if squeue is unavailable (see _active_jobs). Backed by the
    per-process snapshot rather than a per-case squeue spawn.
    """
    active = _active_jobs()
    if active is None:
        return None
    return case in active


# Markers in cases/<case>/run.out. The file is appended to on every run attempt,
# so only the segment after the LAST "CSM EXECUTION BEGINS HERE" is relevant.
_RUN_OUT_BEGIN_MARKER = 'CSM EXECUTION BEGINS HERE'
_RUN_OUT_TIMEOUT_MARKERS = ('CANCELLED', 'DUE TO TIME LIMIT')


def _run_out_walltimeout(run_out_path):
    """Return True if the most recent run.out segment ended in a SLURM wall-clock
    timeout (slurmstepd "CANCELLED ... DUE TO TIME LIMIT").

    Only the segment after the last "CSM EXECUTION BEGINS HERE" is examined,
    because run.out is appended to on each run attempt. Returns False if the file
    is missing/unreadable or shows no timeout in the last segment.
    """
    try:
        with open(run_out_path) as f:
            lines = f.readlines()
    except OSError:
        return False

    last_begin = -1
    for i, line in enumerate(lines):
        if _RUN_OUT_BEGIN_MARKER in line:
            last_begin = i
    if last_begin < 0:
        return False

    segment = lines[last_begin:]
    return any(
        all(marker in line for marker in _RUN_OUT_TIMEOUT_MARKERS)
        for line in segment
    )


_RE_HIST_DATE = re.compile(r'\.cam\.h0\.(\d{4}-\d{2})')


def _hist_date(filename):
    """Extract model date stem (YYYY-MM) from a cam.h0 hist filename, or None."""
    m = _RE_HIST_DATE.search(filename)
    return m.group(1) if m else None


def _energy_balance(case, archive, n_months=12, keep_path=None):
    """Compute global-mean energy balance from the last N atm h0 files.

    Returns (ts_mean, fsnt_mean, flnt_mean, n_used, date_first, date_last,
    saved_path) or None on any failure. date_first/date_last are the
    model-date stems (YYYY-MM) of the first and last selected files (None if
    unparseable). Prints a warning and returns None if ncra or netCDF4 is
    unavailable.

    The averaged file is a byproduct of the energy computation. By default it
    is written to a unique temp file and deleted in the finally block. When
    keep_path is given, ncra writes the average there instead and the file is
    left in place; saved_path in the return is the retained path (else None).
    With keep_path pointing at archive/<case>/atm/hist/, the kept file uses
    datamgr avg's naming convention and is interchangeable with what
    `datamgr avg --last N --models atm` produces from the same inputs.
    """
    try:
        import numpy as np
    except ImportError:
        print(f"  {case}: WARNING: numpy not available — skipping --energy", file=sys.stderr)
        return None
    try:
        import netCDF4 as nc4
    except ImportError:
        print(f"  {case}: WARNING: netCDF4 not available — skipping --energy", file=sys.stderr)
        return None

    hist_dir = os.path.join(archive, case, 'atm', 'hist')
    if not os.path.isdir(hist_dir):
        print(f"  {case}: WARNING: atm/hist/ not found — skipping --energy")
        return None

    # Collect *.cam.h0.*.nc files that are not avg files, sort lexicographically
    try:
        all_files = sorted(
            f for f in os.listdir(hist_dir)
            if f.endswith('.nc') and '.cam.h0.' in f and 'avg' not in f
        )
    except OSError:
        print(f"  {case}: WARNING: cannot read atm/hist/ — skipping --energy")
        return None

    if not all_files:
        print(f"  {case}: WARNING: no cam.h0 files found — skipping --energy")
        return None

    selected = all_files[-n_months:]
    n_used = len(selected)
    if n_used < n_months:
        print(f"  {case}: WARNING: only {n_used} month(s) available (requested {n_months})")

    date_first = _hist_date(selected[0])
    date_last = _hist_date(selected[-1])

    input_paths = [os.path.join(hist_dir, f) for f in selected]
    if keep_path is not None:
        # Keep mode: ncra writes the average directly to keep_path (in the
        # hist dir) and it is not deleted afterward. -O overwrites an existing
        # avg file without ncra's interactive prompt (same as datamgr avg).
        tmp_path = keep_path
        remove_after = False
    else:
        # Unique per-invocation temp name: a fixed name in shared /tmp collides
        # with other users' leftovers (ncra then fails on a file it cannot
        # overwrite). mkstemp pre-creates the file, so ncra needs -O to write
        # into it without its interactive overwrite prompt.
        fd, tmp_path = tempfile.mkstemp(prefix=f'runmgr_energy_{case}_', suffix='.nc')
        os.close(fd)
        remove_after = True

    try:
        try:
            result = subprocess.run(
                ['ncra', '-O'] + input_paths + [tmp_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except FileNotFoundError:
            print(f"  {case}: WARNING: ncra not found in PATH — skipping --energy")
            return None

        if result.returncode != 0:
            print(f"  {case}: WARNING: ncra failed — skipping --energy")
            if result.stderr.strip():
                print(f"    {result.stderr.strip()}", file=sys.stderr)
            return None

        try:
            ds = nc4.Dataset(tmp_path)
        except Exception as e:
            print(f"  {case}: WARNING: cannot open ncra output ({e}) — skipping --energy")
            return None

        try:
            for var in ('TS', 'FSNT', 'FLNT'):
                if var not in ds.variables:
                    print(f"  {case}: WARNING: variable {var} missing — skipping --energy")
                    ds.close()
                    return None

            # Identify lat/lon dimension names
            lat_name = next((v for v in ('lat', 'latitude') if v in ds.variables), None)
            lon_name = next((v for v in ('lon', 'longitude') if v in ds.variables), None)
            if lat_name is None or lon_name is None:
                print(f"  {case}: WARNING: lat/lon not found — skipping --energy")
                ds.close()
                return None

            lat = ds.variables[lat_name][:]
            # cos-latitude weights, shape (nlat,), broadcast to (nlat, nlon)
            w1d = np.cos(lat * math.pi / 180.0)
            w1d = np.where(w1d < 0, 0.0, w1d)
            nlon = ds.variables[lon_name].shape[0]
            w2d = np.broadcast_to(w1d[:, np.newaxis], (len(lat), nlon)).copy()
            w2d /= w2d.sum()  # normalize to 1

            def _gmean(varname):
                data = ds.variables[varname][:]
                # data may be (time, lat, lon) or (lat, lon); squeeze time dim
                if data.ndim == 3:
                    data = data[0]
                return float(np.sum(data * w2d))

            ts_mean   = _gmean('TS')
            fsnt_mean = _gmean('FSNT')
            flnt_mean = _gmean('FLNT')
            ds.close()
            saved_path = keep_path if not remove_after else None
            return (ts_mean, fsnt_mean, flnt_mean, n_used,
                    date_first, date_last, saved_path)

        except Exception as e:
            print(f"  {case}: WARNING: error reading variables ({e}) — skipping --energy")
            try:
                ds.close()
            except Exception:
                pass
            return None

    finally:
        if remove_after:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _rundir_info(case, rundir):
    """Return info lines summarizing files in rundir/<case>/run/ (no individual filenames)."""
    import re
    run_dir = os.path.join(rundir, case, 'run')
    if not os.path.isdir(run_dir):
        return ["  run/:       (not found)"], 0
    file_pairs = []  # list of (filename, size_bytes)
    try:
        with os.scandir(run_dir) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        file_pairs.append((entry.name, entry.stat(follow_symlinks=False).st_size))
                except OSError:
                    pass
    except OSError:
        return ["  run/:       (error reading directory)"], 0

    hist_files = [(f, sz) for f, sz in file_pairs
                  if re.match(rf'^{re.escape(case)}\.cam\.h0\.\d{{4}}-\d{{2}}.*\.nc$', f)]
    rest_files = [(f, sz) for f, sz in file_pairs
                  if re.match(rf'^{re.escape(case)}\.cam\.r\.\d{{4}}-\d{{2}}.*\.nc$', f)]

    hist_count = len(hist_files)
    hist_size  = sum(sz for _, sz in hist_files)
    rest_count = len(rest_files)
    rest_size  = sum(sz for _, sz in rest_files)

    if hist_count == 0:
        hist_line = f"  run/hist:     0 cam.h0"
    else:
        years = sorted(y for y in (_hist_year(f) for f, _ in hist_files) if y)
        year_span = f"years {years[0]}–{years[-1]}" if years else "years unknown"
        hist_line = f"  run/hist:  {hist_count:>4} cam.h0,  {year_span}  ({fmt_size(hist_size)})"

    rptr_date = None
    rptr_path = os.path.join(run_dir, f'{case}.rpointer.atm')
    if not os.path.isfile(rptr_path):
        for f, _ in file_pairs:
            if f.endswith('.rpointer.atm'):
                rptr_path = os.path.join(run_dir, f)
                break
        else:
            rptr_path = None
    if rptr_path and os.path.isfile(rptr_path):
        try:
            with open(rptr_path) as fh:
                first_line = fh.readline().strip()
            m = re.search(r'\d{4}-\d{2}-\d{2}', first_line)
            if m:
                rptr_date = m.group(0)
        except OSError:
            pass

    date_suffix = f"  [restart @ {rptr_date}]" if rptr_date else ""
    rest_line = (f"  run/rest:  {rest_count:>4} cam.r found  "
                 f"({fmt_size(rest_size)}){date_suffix}")

    total_run_size = dir_size_bytes(run_dir)
    lines = [hist_line, rest_line, f"  run/total:         {fmt_size(total_run_size)}"]
    return lines, total_run_size


# Maps the short label used on the CLI to a callable returning the absolute
# directory path given (case, paths).
_DIR_RESOLVERS = {
    'atm/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'atm', 'hist'),
    'lnd/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'lnd', 'hist'),
    'ice/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'ice', 'hist'),
    'ocn/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'ocn', 'hist'),
    'rest':     lambda case, p: os.path.join(p.get('archive', ''), case, 'rest'),
    'run':      lambda case, p: os.path.join(p.get('rundir',  ''), case, 'run'),
}


def cmd_check(args, paths):
    """
    Show run status for cases based on CaseStatus file and SLURM queue probe.

    Defaults to all discoverable cases when no case names or --prefix are given.
    Read-only — no --execute flag required or accepted.

    Default output per case (single line):
      <case>  [STATUS]  (<timestamp of last CaseStatus line>)
      Status labels: RUNNING, COMPLETE, FAILED, BUILT, CLEANED, UNKNOWN,
      NO_CASEDIR, RESUBMITTED, RUNNING?, WALLCLOCK
      Segment history counts are not reported — CaseStatus is inherited by
      cloned cases, making cumulative counts unreliable.

    SLURM probe: when the last CaseStatus event is 'run started' or
    'run SUCCESSFUL', squeue --name <case> -h is run. A queued job with a
    SUCCESSFUL last event is shown as RESUBMITTED. If squeue is unavailable or
    errors, the probe is silently omitted.

    WALLCLOCK: when a 'run started' case is no longer queued, run.out is checked.
    A SLURM wall-clock kill ('CANCELLED ... DUE TO TIME LIMIT' in the last
    run.out segment) is shown as WALLCLOCK rather than the generic RUNNING?.

    --info: additionally print per-model hist file count, year span, and size
            (atm, lnd, ice) and restart set count, led by a TOTAL line summing
            bytes across every reported location (archive hist + rest + run).

    --energy: compute global-mean energy balance from the last 12 atm h0 files
              via ncra + netCDF4. Reports TS and Etop = FSNT - FLNT, plus the
              model-date span (YYYY-MM) of the averaged files. Requires ncra in
              PATH and netCDF4 + numpy Python packages.

    -n / --energy-years N: with --energy, average the last N model years
              (12*N monthly h0 files) instead of the default last 12 months —
              e.g. `check <case> --energy -n 10` averages the last 120 months.
              The report line states the month count actually used (fewer
              files than requested prints a warning, same as the default).

    --keep: with --energy, retain the atm average (normally a discarded
              byproduct) in archive/<case>/atm/hist/ as
              <case>.cam.h0.avg_last{N}yr.nc (with -n N) or ...avg_last12mo.nc
              (bare 12-month). This is the run-time counterpart to
              `datamgr avg`: same atm output and naming, produced during
              routine energy monitoring rather than at retirement. An existing
              avg file at the target is overwritten (ncra -O). Requires --energy.

    --dir DIR: drill down into a specific storage area for exactly one case.
               Lists individual files with sizes (sorted by name) and a total.
               For 'rest', lists top-level subdirectory entries (restart sets).
               Prints the absolute path as a header. Requires exactly one
               explicit case name; incompatible with --prefix.
               DIR choices: atm/hist, lnd/hist, ice/hist, ocn/hist, rest, run.
    """
    caseroot = paths.get('caseroot', '')
    archive  = paths.get('archive',  '')

    requested = getattr(args, 'cases', None) or []
    prefix_filter = getattr(args, 'prefix', None)

    if requested and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")

    target_dir = getattr(args, 'dir', None)
    if target_dir is not None:
        if prefix_filter:
            sys.exit("ERROR: --dir cannot be combined with --prefix.")
        if len(requested) != 1:
            sys.exit("ERROR: --dir requires exactly one explicit case name.")

    all_cases = discover_cases(paths)

    if requested:
        missing = [c for c in requested if c not in all_cases]
        if missing:
            print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
        cases = [c for c in requested if c in all_cases]
    elif prefix_filter:
        cases = [c for c in all_cases if c.lower().startswith(prefix_filter.lower())]
        if not cases:
            print(f"No cases matching prefix '{prefix_filter}'.")
            return
    else:
        cases = all_cases

    if not cases:
        print("No cases found.")
        return

    do_info   = getattr(args, 'info',   False)
    do_energy = getattr(args, 'energy', False)

    energy_years = getattr(args, 'energy_years', None)
    if energy_years is not None:
        if not do_energy:
            sys.exit("ERROR: -n/--energy-years requires --energy.")
        if energy_years < 1:
            sys.exit("ERROR: -n/--energy-years must be >= 1.")
    energy_months = 12 * energy_years if energy_years is not None else 12

    keep_avg = getattr(args, 'keep', False)
    if keep_avg and not do_energy:
        sys.exit("ERROR: --keep requires --energy.")
    # Kept-file naming mirrors datamgr avg: avg_last{N}yr with -n N, else the
    # bare 12-month avg_last12mo. atm is the only component --energy averages.
    keep_suffix = f"avg_last{energy_years}yr" if energy_years is not None else "avg_last12mo"

    # Collect all results before printing so max_name_len is known for alignment.
    # Each entry: (case, status_label, status_ts, info_lines, energy_line)
    results = []
    for case in cases:
        casestatus_path = os.path.join(caseroot, case, 'CaseStatus') if caseroot else ''
        cs = _parse_casestatus(casestatus_path) if casestatus_path else None

        if cs is None:
            status_label = 'NO_CASEDIR'
            status_ts = ''
        else:
            status_label = cs['status']
            status_ts = cs['last_ts'] or ''

            # SLURM probe when last event is run started or run SUCCESSFUL
            if cs['last_event'] and (
                cs['last_event'].startswith('run started') or
                cs['last_event'].startswith('run SUCCESSFUL')
            ):
                job_queued = _squeue_probe(case)
                if job_queued is True and cs['last_event'].startswith('run SUCCESSFUL'):
                    status_label = 'RESUBMITTED'
                elif job_queued is False and cs['last_event'].startswith('run started'):
                    # Started but no longer queued — likely crashed without updating CaseStatus.
                    # SLURM wall-clock kills don't update CaseStatus either, but they DO leave a
                    # "CANCELLED ... DUE TO TIME LIMIT" line in the last run.out segment.
                    run_out_path = os.path.join(caseroot, case, 'run.out') if caseroot else ''
                    if run_out_path and _run_out_walltimeout(run_out_path):
                        status_label = 'WALLCLOCK'
                    else:
                        status_label = 'RUNNING?'

        info_lines = []
        if do_info and archive:
            grand_total = 0  # bytes summed across every reported location
            for model in AVG_HIST_DEFAULT_MODELS:
                hist_dir = os.path.join(archive, case, model, 'hist')
                files, total = list_files_with_size(hist_dir)
                # A dir holding only avg files still contributes to TOTAL.
                grand_total += total
                info_lines.append(hist_info_line(model, hist_dir, files, total))
            sets = restart_sets(case, paths)
            rest_total = sum(dir_size_bytes(s[1]) for s in sets) if sets else 0
            grand_total += rest_total
            info_lines.append(f"  rest:      {len(sets):>4} folder(s)  ({fmt_size(rest_total)})")
            rundir = paths.get('rundir', '')
            if rundir:
                run_lines, run_total = _rundir_info(case, rundir)
                info_lines.extend(run_lines)
                grand_total += run_total
            info_lines.insert(0, f"  TOTAL:     {fmt_size(grand_total)}  (all locations)")

        energy_line = None
        if do_energy and archive:
            keep_path = None
            if keep_avg:
                stem = MODEL_STEM['atm']
                keep_name = f"{case}.{stem}.h0.{keep_suffix}.nc"
                keep_path = os.path.join(archive, case, 'atm', 'hist', keep_name)
            result = _energy_balance(case, archive, n_months=energy_months,
                                     keep_path=keep_path)
            if result is not None:
                (ts_mean, fsnt_mean, flnt_mean, n_used,
                 date_first, date_last, saved_path) = result
                etop = fsnt_mean - flnt_mean
                sign = '+' if etop >= 0 else ''
                if date_first and date_last:
                    span = date_first if date_first == date_last else f"{date_first}–{date_last}"
                    range_str = f"  [{span}]"
                else:
                    range_str = ""
                energy_line = (f"  Last {n_used}mo:  TS = {ts_mean:.1f} K    "
                               f"Etop = {sign}{etop:.1f} W/m²{range_str}")
                if saved_path:
                    energy_line += f"\n  Saved avg: {saved_path}"

        results.append((case, status_label, status_ts, info_lines, energy_line))

    if target_dir is not None:
        # Drill-down mode: list files in the named storage area for the single case.
        case = cases[0]
        resolver = _DIR_RESOLVERS.get(target_dir)
        abs_dir = resolver(case, paths) if resolver else None
        if not abs_dir:
            sys.exit(f"ERROR: path not configured for '{target_dir}'.")

        print(f"{abs_dir}")

        if not os.path.isdir(abs_dir):
            print("  (directory not found)")
            return

        # For 'rest', list subdirectory entries (restart sets are directories).
        if target_dir == 'rest':
            try:
                entries = sorted(os.scandir(abs_dir), key=lambda e: e.name)
            except OSError as exc:
                sys.exit(f"ERROR reading directory: {exc}")
            if not entries:
                print("  (empty)")
                return
            for entry in entries:
                try:
                    size = dir_size_bytes(entry.path) if entry.is_dir() else entry.stat().st_size
                    print(f"  {entry.name:<60}  {fmt_size(size):>10}")
                except OSError:
                    print(f"  {entry.name}")
            return

        # All other dirs: list files sorted by name with individual sizes.
        files, _ = list_files_with_size(abs_dir)
        if not files:
            print("  (empty or no files)")
            return

        files_sorted = sorted(files)
        sizes = {}
        try:
            with os.scandir(abs_dir) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False):
                        try:
                            sizes[entry.name] = entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            sizes[entry.name] = 0
        except OSError as exc:
            sys.exit(f"ERROR reading directory: {exc}")

        total = sum(sizes.get(f, 0) for f in files_sorted)
        for fname in files_sorted:
            sz = sizes.get(fname, 0)
            print(f"  {fname:<60}  {fmt_size(sz):>10}")
        print(f"  {'─' * 72}")
        print(f"  {'total':60}  {fmt_size(total):>10}")
        return

    # Columnar output: name left-justified to max_name_len, tag left-justified to 15.
    max_name_len = max(len(r[0]) for r in results)
    tag_width = 15  # fits [RESUBMITTED] (13) with room

    for case, status_label, status_ts, info_lines, energy_line in results:
        tag = f"[{status_label}]"
        print(f"{case:<{max_name_len}}  {tag:<{tag_width}}  {status_ts}".rstrip())
        for line in info_lines:
            print(line)
        if energy_line:
            print(energy_line)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='runmgr.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('--config-registry', default=DEFAULT_CONFIG, dest='config_registry',
                        help='Path to config_registry.yaml (default: config_registry.yaml '
                             'next to this script)')
    parser.add_argument('--caseroot',  help='Override paths.caseroot from config_registry')
    parser.add_argument('--rundir',    help='Override paths.rundir from config_registry')
    parser.add_argument('--archive',   help='Override paths.archive from config_registry')
    parser.add_argument('--long-term', dest='long_term',
                        help='Override paths.long_term from config_registry')

    top_sub = parser.add_subparsers(dest='group', metavar='SUBCOMMAND', help=argparse.SUPPRESS)
    top_sub.required = True

    # ---- check ----
    p_check = top_sub.add_parser(
        'check',
        help=argparse.SUPPRESS,
        description=cmd_check.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_check.add_argument('cases', nargs='*',
                         help='Case name(s) to check (default: all discoverable cases)')
    p_check.add_argument('--prefix', metavar='STR', default=None,
                         help='Case-insensitive prefix filter; '
                              'cannot combine with explicit case names')
    p_check.add_argument('--info', action='store_true',
                         help='Print per-model hist file count, year span, size, '
                              'and restart set count')
    p_check.add_argument('--energy', action='store_true',
                         help='Compute global-mean energy balance (TS, Etop=FSNT-FLNT) '
                              'from last 12 atm h0 files via ncra; requires ncra + netCDF4')
    p_check.add_argument('-n', '--energy-years', dest='energy_years',
                         type=int, default=None, metavar='N',
                         help='With --energy: average the last N model years '
                              '(12*N monthly h0 files) instead of the default '
                              'last 12 months')
    p_check.add_argument('--keep', action='store_true',
                         help='With --energy: keep the atm average instead of '
                              'discarding it. Written to archive/<case>/atm/hist/ '
                              'as <case>.cam.h0.avg_last{N}yr.nc (or avg_last12mo.nc '
                              'without -n), matching datamgr avg naming')
    p_check.add_argument('--dir', metavar='DIR', default=None,
                         choices=list(_DIR_RESOLVERS),
                         help=('Drill down into a specific storage area for a single case: '
                               f'{", ".join(_DIR_RESOLVERS)}. '
                               'Requires exactly one case name; incompatible with --prefix.'))

    # ---- xml ----
    p_xml = top_sub.add_parser(
        'xml',
        help=argparse.SUPPRESS,
        description=cmd_xml.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_xml.add_argument('cases', nargs='*',
                       help='Case name(s) to query/change (or use --prefix; no --all flag)')
    p_xml.add_argument('--prefix', metavar='STR', default=None,
                       help='Case-insensitive prefix filter; '
                            'cannot combine with explicit case names')
    p_xml.add_argument('--query', dest='query', action='append', metavar='VAR',
                       help='Print VAR\'s current value per case (repeatable, read-only); '
                            'e.g. --query STOP_N --query RESUBMIT')
    p_xml.add_argument('--change', dest='change', action='append', metavar='VAR=VALUE',
                       help='Set VAR=VALUE via xmlchange (repeatable); no CONTINUE_RUN, '
                            'no sbatch; e.g. --change STOP_N=12')
    p_xml.add_argument('--execute', action='store_true',
                       help='Actually apply --change (default is preview only)')

    # ---- continue ----
    p_cont = top_sub.add_parser(
        'continue',
        help=argparse.SUPPRESS,
        description=cmd_continue.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_cont.add_argument('cases', nargs='*',
                        help='Case name(s) to continue (or use --prefix; no --all flag)')
    p_cont.add_argument('--prefix', metavar='STR', default=None,
                        help='Case-insensitive prefix filter; cannot combine with explicit case names')
    p_cont.add_argument('--set', dest='set', action='append', metavar='VAR=VALUE',
                        help='Apply xmlchange VAR=VALUE before submitting (repeatable); '
                             'e.g. --set STOP_N=10 --set RESUBMIT=9')
    p_cont.add_argument('--execute', action='store_true',
                        help='Actually perform actions (default is preview only)')

    # ---- restart ----
    p_restart = top_sub.add_parser(
        'restart',
        help=argparse.SUPPRESS,
        description=cmd_restart.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_restart.add_argument('cases', nargs='*',
                           help='Case name(s) to restart (or use --prefix; no --all flag)')
    p_restart.add_argument('--prefix', metavar='STR', default=None,
                           help='Case-insensitive prefix filter; '
                                'cannot combine with explicit case names')
    p_restart.add_argument('--set', dest='set', action='append', metavar='VAR=VALUE',
                           help='Apply xmlchange VAR=VALUE before submitting (repeatable); '
                                'e.g. --set RUN_STARTDATE=0001-01-01 --set RESUBMIT=9')
    p_restart.add_argument('--execute', action='store_true',
                           help='Actually perform actions (default is preview only)')

    # ---- submit ----
    p_submit = top_sub.add_parser(
        'submit',
        help=argparse.SUPPRESS,
        description=cmd_submit.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_submit.add_argument('cases', nargs='*',
                          help='Case name(s) to submit (or use --prefix; no --all flag)')
    p_submit.add_argument('--prefix', metavar='STR', default=None,
                          help='Case-insensitive prefix filter; '
                               'cannot combine with explicit case names')
    p_submit.add_argument('--execute', action='store_true',
                          help='Actually perform actions (default is preview only)')

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    paths = load_paths(args)

    missing_paths = [k for k in ('caseroot', 'rundir', 'archive')
                     if not paths.get(k)]
    if missing_paths:
        print(f"WARNING: paths not configured: {', '.join(missing_paths)}. "
              f"Set them in config_registry.yaml.", file=sys.stderr)

    if args.group == 'check':
        cmd_check(args, paths)

    elif args.group == 'xml':
        cmd_xml(args, paths)

    elif args.group == 'continue':
        cmd_continue(args, paths)

    elif args.group == 'restart':
        cmd_restart(args, paths)

    elif args.group == 'submit':
        cmd_submit(args, paths)


if __name__ == '__main__':
    main()

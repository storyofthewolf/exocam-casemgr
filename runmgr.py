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
                        defaults to all discoverable cases when given no names
  ls                    List files in a storage area for a single case;
                        omit dir for a summary (like check --info)
  continue              Set CONTINUE_RUN=TRUE and sbatch the run script;
                        optionally apply --set VAR=VALUE xmlchange calls first
  restart               Set CONTINUE_RUN=FALSE, apply xmlchange calls, and sbatch;
                        use to fix and rerun from scratch after a completed or failed run

SAFETY
------
  continue and restart require explicit case names or --prefix.
  There is no --all flag. check and ls are read-only and need no --execute.

Run any subcommand with --help for full options, e.g.:
  python runmgr.py check --help
  python runmgr.py continue --help
  python runmgr.py restart --help
"""

import argparse
import math
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manage_utils import (
    ARCHIVE_MODELS, AVG_HIST_DEFAULT_MODELS,
    DEFAULT_CONFIG, load_paths,
    dir_size_bytes, fmt_size, list_files_with_size, discover_cases,
    _hist_year, restart_sets,
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


def cmd_continue(args, paths):
    """
    Set CONTINUE_RUN=TRUE and submit the run script via sbatch.

    Use --set VAR=VALUE (repeatable) to apply any xmlchange calls before
    submitting — e.g. --set STOP_N=10 --set RESUBMIT=9.

    Status gating (checked via CaseStatus + SLURM probe):
      RUNNING / RESUBMITTED  — hard block: skipped with error message
      COMPLETE               — proceeds without warning
      anything else          — soft block: per-case confirmation prompt

    Without --execute, prints a preview and exits. Requires explicit case
    names or --prefix — no --all flag.
    """
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    prefix_filter = getattr(args, 'prefix', None)
    explicit_cases = args.cases or []

    if explicit_cases and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")

    if prefix_filter:
        all_cases = discover_cases(paths)
        cases = [c for c in all_cases if c.lower().startswith(prefix_filter.lower())]
        if not cases:
            sys.exit(f"ERROR: no cases found matching prefix '{prefix_filter}'.")
    elif explicit_cases:
        cases = explicit_cases
    else:
        sys.exit("ERROR: continue requires explicit case names or --prefix. No --all flag.")

    # Parse --set VAR=VALUE pairs
    set_vars = []
    for item in (args.set or []):
        if '=' not in item:
            sys.exit(f"ERROR: --set requires VAR=VALUE format, got: {item!r}")
        var, _, val = item.partition('=')
        var = var.strip()
        if not var:
            sys.exit(f"ERROR: empty variable name in --set {item!r}")
        set_vars.append((var, val.strip()))

    for case in cases:
        case_dir = os.path.join(caseroot, case)
        if not os.path.isdir(case_dir):
            print(f"  {case}: ERROR: caseroot directory not found: {case_dir}")
            continue

        env_run = os.path.join(case_dir, 'env_run.xml')
        cur_continue = _read_xml_var(env_run, 'CONTINUE_RUN') or '?'
        cur_vals = {var: (_read_xml_var(env_run, var) or '?') for var, _ in set_vars}

        # Status gate
        casestatus_path = os.path.join(case_dir, 'CaseStatus')
        cs = _parse_casestatus(casestatus_path)
        if cs is None:
            status_label = 'NO_CASEDIR'
        else:
            status_label = cs['status']
            if cs['last_event'] and (
                cs['last_event'].startswith('run started') or
                cs['last_event'].startswith('run SUCCESSFUL')
            ):
                job_queued = _squeue_probe(case)
                if job_queued is True and cs['last_event'].startswith('run SUCCESSFUL'):
                    status_label = 'RESUBMITTED'
                elif job_queued is False and cs['last_event'].startswith('run started'):
                    status_label = 'RUNNING?'

        if status_label in ('RUNNING', 'RESUBMITTED'):
            print(f"  {case}: [{status_label}] — skipping (job already active)")
            continue

        run_script = os.path.join(case_dir, f'{case}.run')
        print(f"  {case}  [{status_label}]")
        print(f"    CONTINUE_RUN: {cur_continue} -> TRUE")
        for var, new_val in set_vars:
            print(f"    {var}: {cur_vals[var]} -> {new_val}")
        print(f"    sbatch: {run_script}")

        if not args.execute:
            continue

        # Soft block: warn and confirm for non-COMPLETE statuses
        if status_label != 'COMPLETE':
            print(f"    WARNING: status is [{status_label}], not COMPLETE.")
            try:
                answer = input(f"    Continue anyway for {case}? [yes/no]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                print(f"    Skipping {case}.")
                continue
            if answer not in ('yes', 'y'):
                print(f"    Skipping {case}.")
                continue

        # Apply xmlchange calls
        def _xmlchange(var, val):
            result = subprocess.run(
                ['./xmlchange', f'{var}={val}'],
                cwd=case_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"xmlchange {var}={val} failed: {result.stderr.strip()}")

        try:
            _xmlchange('CONTINUE_RUN', 'TRUE')
            for var, val in set_vars:
                _xmlchange(var, val)
        except RuntimeError as e:
            print(f"    ERROR: {e}")
            continue

        # sbatch
        try:
            result = subprocess.run(
                ['sbatch', f'{case}.run'],
                cwd=case_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except FileNotFoundError:
            print(f"    ERROR: sbatch not found in PATH")
            continue

        if result.returncode != 0:
            print(f"    ERROR: sbatch failed: {result.stderr.strip()}")
            continue

        import re as _re
        m = _re.search(r'Submitted batch job (\d+)', result.stdout)
        job_id = m.group(1) if m else result.stdout.strip()
        print(f"    submitted: job {job_id}")

    if not args.execute:
        print("\n(preview only — rerun with --execute to submit)")


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

    Status gating (checked via CaseStatus + SLURM probe):
      RUNNING / RESUBMITTED  — hard block: skipped with error message
      COMPLETE               — proceeds without warning (normal case)
      anything else          — soft block: per-case confirmation prompt

    Without --execute, prints a preview and exits. Requires explicit case
    names or --prefix — no --all flag.
    """
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    prefix_filter  = getattr(args, 'prefix', None)
    explicit_cases = args.cases or []

    if explicit_cases and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")

    if prefix_filter:
        all_cases = discover_cases(paths)
        cases = [c for c in all_cases if c.lower().startswith(prefix_filter.lower())]
        if not cases:
            sys.exit(f"ERROR: no cases found matching prefix '{prefix_filter}'.")
    elif explicit_cases:
        cases = explicit_cases
    else:
        sys.exit("ERROR: restart requires explicit case names or --prefix. No --all flag.")

    # Parse --set VAR=VALUE pairs
    set_vars = []  # list of (VAR, VALUE) in order
    for item in (args.set or []):
        if '=' not in item:
            sys.exit(f"ERROR: --set requires VAR=VALUE format, got: {item!r}")
        var, _, val = item.partition('=')
        var = var.strip()
        val = val.strip()
        if not var:
            sys.exit(f"ERROR: empty variable name in --set {item!r}")
        set_vars.append((var, val))

    for case in cases:
        case_dir = os.path.join(caseroot, case)
        if not os.path.isdir(case_dir):
            print(f"  {case}: ERROR: caseroot directory not found: {case_dir}")
            continue

        env_run = os.path.join(case_dir, 'env_run.xml')

        # Read current values for CONTINUE_RUN and each var being changed
        cur_continue = _read_xml_var(env_run, 'CONTINUE_RUN') or '?'
        cur_vals = {}
        for var, _ in set_vars:
            cur_vals[var] = _read_xml_var(env_run, var) or '?'

        # Status gate
        casestatus_path = os.path.join(case_dir, 'CaseStatus')
        cs = _parse_casestatus(casestatus_path)
        if cs is None:
            status_label = 'NO_CASEDIR'
        else:
            status_label = cs['status']
            if cs['last_event'] and (
                cs['last_event'].startswith('run started') or
                cs['last_event'].startswith('run SUCCESSFUL')
            ):
                job_queued = _squeue_probe(case)
                if job_queued is True and cs['last_event'].startswith('run SUCCESSFUL'):
                    status_label = 'RESUBMITTED'
                elif job_queued is False and cs['last_event'].startswith('run started'):
                    status_label = 'RUNNING?'

        if status_label in ('RUNNING', 'RESUBMITTED'):
            print(f"  {case}: [{status_label}] — skipping (job already active)")
            continue

        # Preview
        run_script = os.path.join(case_dir, f'{case}.run')
        print(f"  {case}  [{status_label}]")
        print(f"    CONTINUE_RUN: {cur_continue} -> FALSE")
        for var, new_val in set_vars:
            cur = cur_vals.get(var, '?')
            print(f"    {var}: {cur} -> {new_val}")
        print(f"    sbatch: {run_script}")

        if not args.execute:
            continue

        # Soft block for non-COMPLETE statuses
        if status_label != 'COMPLETE':
            print(f"    WARNING: status is [{status_label}], not COMPLETE.")
            try:
                answer = input(f"    Restart anyway for {case}? [yes/no]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                print(f"    Skipping {case}.")
                continue
            if answer not in ('yes', 'y'):
                print(f"    Skipping {case}.")
                continue

        # Apply xmlchange calls
        def _xmlchange(var, val):
            result = subprocess.run(
                ['./xmlchange', f'{var}={val}'],
                cwd=case_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"xmlchange {var}={val} failed: {result.stderr.strip()}")

        try:
            _xmlchange('CONTINUE_RUN', 'FALSE')
            for var, val in set_vars:
                _xmlchange(var, val)
        except RuntimeError as e:
            print(f"    ERROR: {e}")
            continue

        # sbatch
        try:
            result = subprocess.run(
                ['sbatch', f'{case}.run'],
                cwd=case_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except FileNotFoundError:
            print(f"    ERROR: sbatch not found in PATH")
            continue

        if result.returncode != 0:
            print(f"    ERROR: sbatch failed: {result.stderr.strip()}")
            continue

        import re as _re
        m = _re.search(r'Submitted batch job (\d+)', result.stdout)
        job_id = m.group(1) if m else result.stdout.strip()
        print(f"    submitted: job {job_id}")

    if not args.execute:
        print("\n(preview only — rerun with --execute to submit)")


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


def _squeue_probe(case):
    """Return True if a SLURM job named *case* is currently queued/running.

    Returns None if squeue is unavailable or returns a non-zero exit code for
    a reason other than the job not existing (graceful degradation).
    """
    try:
        result = subprocess.run(
            ['squeue', '--name', case, '-h'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _energy_balance(case, archive, n_months=12):
    """Compute global-mean energy balance from the last N atm h0 files.

    Returns (ts_mean, fsnt_mean, flnt_mean, n_used) or None on any failure.
    Prints a warning and returns None if ncra or netCDF4 is unavailable.
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

    input_paths = [os.path.join(hist_dir, f) for f in selected]
    tmp_path = os.path.join(tempfile.gettempdir(), f'runmgr_energy_{case}.nc')

    try:
        try:
            result = subprocess.run(
                ['ncra'] + input_paths + [tmp_path],
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
            return ts_mean, fsnt_mean, flnt_mean, n_used

        except Exception as e:
            print(f"  {case}: WARNING: error reading variables ({e}) — skipping --energy")
            try:
                ds.close()
            except Exception:
                pass
            return None

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _rundir_info(case, rundir):
    """Return info lines summarizing files in rundir/<case>/run/ (no individual filenames)."""
    import re
    run_dir = os.path.join(rundir, case, 'run')
    if not os.path.isdir(run_dir):
        return ["  run/:       (not found)"]
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
        return ["  run/:       (error reading directory)"]

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
    return [hist_line, rest_line, f"  run/total:         {fmt_size(total_run_size)}"]


def cmd_check(args, paths):
    """
    Show run status for cases based on CaseStatus file and SLURM queue probe.

    Defaults to all discoverable cases when no case names or --prefix are given.
    Read-only — no --execute flag required or accepted.

    Default output per case (single line):
      <case>  [STATUS]  (<timestamp of last CaseStatus line>)
      Status labels: RUNNING, COMPLETE, FAILED, BUILT, CLEANED, UNKNOWN,
      NO_CASEDIR, RESUBMITTED, RUNNING?
      Segment history counts are not reported — CaseStatus is inherited by
      cloned cases, making cumulative counts unreliable.

    SLURM probe: when the last CaseStatus event is 'run started' or
    'run SUCCESSFUL', squeue --name <case> -h is run. A queued job with a
    SUCCESSFUL last event is shown as RESUBMITTED. If squeue is unavailable or
    errors, the probe is silently omitted.

    --info: additionally print per-model hist file count, year span, and size
            (atm, lnd, ice) and restart set count.

    --energy: compute global-mean energy balance from the last 12 atm h0 files
              via ncra + netCDF4. Reports TS and Etop = FSNT - FLNT. Requires
              ncra in PATH and netCDF4 + numpy Python packages.
    """
    caseroot = paths.get('caseroot', '')
    archive  = paths.get('archive',  '')

    requested = getattr(args, 'cases', None) or []
    prefix_filter = getattr(args, 'prefix', None)

    if requested and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")

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
                    # Started but no longer queued — likely crashed without updating CaseStatus
                    status_label = 'RUNNING?'

        info_lines = []
        if do_info and archive:
            for model in AVG_HIST_DEFAULT_MODELS:
                hist_dir = os.path.join(archive, case, model, 'hist')
                files, total = list_files_with_size(hist_dir)
                non_avg = [f for f in files if 'avg' not in f]
                if not non_avg:
                    info_lines.append(f"  {model}/hist:    0 files")
                    continue
                years = sorted(y for y in (_hist_year(f) for f in non_avg) if y)
                span = f"years {years[0]}–{years[-1]}" if years else "years unknown"
                avg_note = ", avg present" if any('avg' in f for f in files) else ""
                info_lines.append(
                    f"  {model}/hist:  {len(non_avg):>4} files,  {span}  ({fmt_size(total)}){avg_note}")
            sets = restart_sets(case, paths)
            rest_total = sum(dir_size_bytes(s[1]) for s in sets) if sets else 0
            info_lines.append(f"  rest:      {len(sets):>4} folder(s)  ({fmt_size(rest_total)})")
            rundir = paths.get('rundir', '')
            if rundir:
                info_lines.extend(_rundir_info(case, rundir))

        energy_line = None
        if do_energy and archive:
            result = _energy_balance(case, archive)
            if result is not None:
                ts_mean, fsnt_mean, flnt_mean, n_used = result
                etop = fsnt_mean - flnt_mean
                sign = '+' if etop >= 0 else ''
                energy_line = (f"  Last {n_used}mo:  TS = {ts_mean:.1f} K    "
                               f"Etop = {sign}{etop:.1f} W/m²")

        results.append((case, status_label, status_ts, info_lines, energy_line))

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
# Subcommand: ls
# ---------------------------------------------------------------------------

# Maps the short label used on the CLI / in --info output to a callable that
# returns the absolute directory path given (case, paths).
_LS_DIR_RESOLVERS = {
    'atm/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'atm', 'hist'),
    'lnd/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'lnd', 'hist'),
    'ice/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'ice', 'hist'),
    'ocn/hist': lambda case, p: os.path.join(p.get('archive', ''), case, 'ocn', 'hist'),
    'rest':     lambda case, p: os.path.join(p.get('archive', ''), case, 'rest'),
    'run':      lambda case, p: os.path.join(p.get('rundir',  ''), case, 'run'),
}


def _ls_summary(case, paths):
    """Print the --info-style summary for a single case (no status line)."""
    archive = paths.get('archive', '')
    rundir  = paths.get('rundir',  '')

    for model in AVG_HIST_DEFAULT_MODELS:
        hist_dir = os.path.join(archive, case, model, 'hist')
        files, total = list_files_with_size(hist_dir)
        non_avg = [f for f in files if 'avg' not in f]
        if not non_avg:
            print(f"  {model}/hist:    0 files")
            continue
        years = sorted(y for y in (_hist_year(f) for f in non_avg) if y)
        span = f"years {years[0]}–{years[-1]}" if years else "years unknown"
        avg_note = ", avg present" if any('avg' in f for f in files) else ""
        print(f"  {model}/hist:  {len(non_avg):>4} files,  {span}  ({fmt_size(total)}){avg_note}")

    sets = restart_sets(case, paths)
    rest_total = sum(dir_size_bytes(s[1]) for s in sets) if sets else 0
    print(f"  rest:      {len(sets):>4} folder(s)  ({fmt_size(rest_total)})")

    if rundir:
        for line in _rundir_info(case, rundir):
            print(line)


def cmd_ls(args, paths):
    """
    List files in a storage area for a single case.

    With no DIR argument, prints the --info-style summary (file counts, year
    spans, sizes) for all storage areas — identical to 'check --info <case>'
    but without the status line.

    With a DIR argument, lists every file in that directory with its size,
    sorted by name, with a total at the bottom.  DIR is one of the short
    labels shown in the summary output:

      atm/hist   archive/<case>/atm/hist/
      lnd/hist   archive/<case>/lnd/hist/
      ice/hist   archive/<case>/ice/hist/
      ocn/hist   archive/<case>/ocn/hist/
      rest       archive/<case>/rest/          (top-level entries only)
      run        rundir/<case>/run/

    The absolute path is printed as a header line so you always know where
    you are.
    """
    case = args.case

    all_cases = discover_cases(paths)
    if case not in all_cases:
        sys.exit(f"ERROR: case '{case}' not found on disk.")

    target_dir = getattr(args, 'dir', None)

    if target_dir is None:
        print(f"{case}")
        _ls_summary(case, paths)
        return

    resolver = _LS_DIR_RESOLVERS.get(target_dir)
    if resolver is None:
        sys.exit(f"ERROR: unknown dir '{target_dir}'. "
                 f"Choices: {', '.join(_LS_DIR_RESOLVERS)}")

    abs_dir = resolver(case, paths)
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

    top_sub = parser.add_subparsers(dest='group', metavar='SUBCOMMAND')

    # ---- check ----
    p_check = top_sub.add_parser(
        'check',
        help='Show run status for cases (CaseStatus + SLURM probe); defaults to all cases',
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

    # ---- ls ----
    p_ls = top_sub.add_parser(
        'ls',
        help='List files in a storage area for a single case',
        description=cmd_ls.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_ls.add_argument('case', help='Case name')
    p_ls.add_argument('dir', nargs='?', default=None,
                      choices=list(_LS_DIR_RESOLVERS),
                      metavar='DIR',
                      help=('Storage area to list: '
                            f'{", ".join(_LS_DIR_RESOLVERS)} '
                            '(omit for summary)'))

    # ---- continue ----
    p_cont = top_sub.add_parser(
        'continue',
        help='Set CONTINUE_RUN=TRUE and sbatch the run script; optionally update STOP_N/RESUBMIT',
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
        help='Set CONTINUE_RUN=FALSE, apply xmlchange calls, and sbatch; rerun from scratch',
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

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.group is None:
        parser.print_help()
        sys.exit(0)

    paths = load_paths(args)

    missing_paths = [k for k in ('caseroot', 'rundir', 'archive')
                     if not paths.get(k)]
    if missing_paths:
        print(f"WARNING: paths not configured: {', '.join(missing_paths)}. "
              f"Set them in config_registry.yaml.", file=sys.stderr)

    if args.group == 'check':
        cmd_check(args, paths)

    elif args.group == 'ls':
        cmd_ls(args, paths)

    elif args.group == 'continue':
        cmd_continue(args, paths)

    elif args.group == 'restart':
        cmd_restart(args, paths)


if __name__ == '__main__':
    main()

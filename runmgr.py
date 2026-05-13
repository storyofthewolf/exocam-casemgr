#!/usr/bin/env python3
"""
runmgr.py — ExoCAM run supervision tool

Manages the active run environment and archive output for in-progress or
recently completed cases. Distinct from manage.py, which handles retirement
and lifecycle operations.

Paths are read from config_registry.yaml (paths.caseroot, paths.rundir,
paths.archive, paths.long_term). Override any path with --caseroot,
--rundir, --archive, or --long-term.

Cases are discovered by scanning those directories on disk — no separate
registry file is required.

ALL DESTRUCTIVE SUBCOMMANDS ARE NON-DESTRUCTIVE BY DEFAULT.
Add --execute to actually perform deletions or moves. Without --execute,
every command only reports what it would do.

SUBCOMMANDS
-----------
  check                 Show run status for cases (CaseStatus + SLURM probe);
                        defaults to all discoverable cases when given no names
  cata purge-bld        Delete build artifacts in rundir/<case>/bld/
  cata purge-restarts   Trim old restart sets in archive/<case>/rest/; keep last N
  cata purge-hist       Delete history NetCDF files in archive/<case>/<model>/hist/
  cata purge-logs       Delete log files from archive/<case>/<model>/logs/ and $CASE/logs/
  cata move-hist        Move history files to long-term storage

SAFETY
------
  All destructive subcommands (cata *) require explicit case names. There is
  no --all flag — bulk operations must be done by listing each case explicitly.
  Bare invocation without case names will exit with an error.

  purge-hist additionally requires --keep-years N or --models to prevent
  accidental deletion of all history files.

  check is read-only and safe to run bare — no case names means all cases.

Run any subcommand with --help for full options, e.g.:
  python runmgr.py check --help
  python runmgr.py cata purge-bld --help
"""

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manage_utils import (
    ARCHIVE_MODELS, HIST_MODELS, AVG_HIST_DEFAULT_MODELS,
    DEFAULT_CONFIG, load_paths,
    dir_size_bytes, fmt_size, list_files_with_size, discover_cases,
    _hist_year, _hist_keep_years_filter, restart_sets,
    confirm, _require_cases,
)

# ---------------------------------------------------------------------------
# Subcommand: purge-bld
# ---------------------------------------------------------------------------

def cmd_purge_bld(args, paths):
    """
    Delete build artifacts in rundir/<case>/bld/.

    The bld/ directory contains compiled .o/.mod files and build logs. It is
    safe to delete after a successful build — the model executable lives in
    run/ and is not affected.

    Use --logs-only to keep the bld/ directory but remove only the large
    binary object files (.o, .mod), preserving build logs.
    """
    rundir = paths.get('rundir', '')
    if not rundir:
        sys.exit("ERROR: rundir path not configured.")

    cases = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    for case in cases:
        bld = os.path.join(rundir, case, 'bld')
        if not os.path.exists(bld):
            print(f"  {case}: bld/ not found, skipping")
            continue
        size = dir_size_bytes(bld)
        if args.logs_only:
            obj_files = []
            for dirpath, _, filenames in os.walk(bld):
                for f in filenames:
                    if f.endswith(('.o', '.mod')):
                        obj_files.append(os.path.join(dirpath, f))
            obj_size = sum(os.path.getsize(f) for f in obj_files)
            action = f"delete {len(obj_files)} object files ({fmt_size(obj_size)}) from {bld}"
            if confirm(action, args.execute):
                for f in obj_files:
                    os.remove(f)
                print(f"  {case}: removed {len(obj_files)} object files ({fmt_size(obj_size)} freed)")
        else:
            action = f"delete entire bld/ directory ({fmt_size(size)}) for {case}"
            if confirm(action, args.execute):
                shutil.rmtree(bld)
                print(f"  {case}: bld/ deleted ({fmt_size(size)} freed)")


# ---------------------------------------------------------------------------
# Subcommand: purge-restarts
# ---------------------------------------------------------------------------

def cmd_purge_restarts(args, paths):
    """
    Trim old restart sets in archive/<case>/rest/, keeping the N most recent.

    Each restart set is a dated subdirectory (e.g. 0050-01-01). Keeping the
    last set is sufficient to resume or branch a simulation. Older sets are
    deleted oldest-first.

    Default: --keep 1 (keep only the most recent restart set).
    """
    archive = paths.get('archive', '')
    if not archive:
        sys.exit("ERROR: archive path not configured.")

    cases = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    for case in cases:
        sets = restart_sets(case, paths)
        if not sets:
            print(f"  {case}: no restart sets found, skipping")
            continue

        to_keep   = sets[-args.keep:]
        to_delete = sets[:-args.keep] if args.keep > 0 else sets

        keep_names   = [s[0] for s in to_keep]
        delete_names = [s[0] for s in to_delete]

        if not to_delete:
            print(f"  {case}: {len(sets)} set(s), nothing to purge (keep={args.keep})")
            continue

        delete_size = sum(dir_size_bytes(s[1]) for s in to_delete)
        print(f"  {case}: {len(sets)} restart set(s) — keeping {keep_names}, "
              f"purging {len(to_delete)} ({fmt_size(delete_size)}): {delete_names}")

        action = f"delete {len(to_delete)} restart set(s) ({fmt_size(delete_size)}) for {case}"
        if confirm(action, args.execute):
            for _, path in to_delete:
                shutil.rmtree(path)
            print(f"    deleted {len(to_delete)} sets ({fmt_size(delete_size)} freed)")


# ---------------------------------------------------------------------------
# Subcommand: purge-hist
# ---------------------------------------------------------------------------

def cmd_purge_hist(args, paths):
    """
    Delete history NetCDF files from archive/<case>/<model>/hist/.

    By default all model components are targeted. Use --models to restrict
    to specific components (e.g. --models atm lnd). The rest/ directory is
    never touched by this command.

    Use --keep-years N to retain files from the N most recent model years
    (cutoff determined across all targeted components jointly). Files whose
    year cannot be parsed from the filename are always kept.

    WARNING: history files are not recoverable once deleted. Without --execute
    this command only previews what would be removed.
    """
    archive = paths.get('archive', '')
    if not archive:
        sys.exit("ERROR: archive path not configured.")

    if args.keep_years is None and not args.models:
        sys.exit(
            "ERROR: purge-hist requires --keep-years N or --models to prevent accidental\n"
            "       deletion of all history files. To explicitly target all components,\n"
            "       pass: --models " + " ".join(HIST_MODELS)
        )

    models = args.models if args.models else HIST_MODELS
    cases  = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    for case in cases:
        archive_path = os.path.join(archive, case)

        if args.keep_years is not None:
            keep_years, per_model = _hist_keep_years_filter(
                archive_path, models, args.keep_years)
            if not per_model:
                print(f"  {case}: no hist/ directories found, skipping")
                continue
            print(f"  {case}: keeping years {keep_years if keep_years else '(none parsed)'}")
            targets = []
            case_total = 0
            for model, info in per_model.items():
                if not info['delete']:
                    continue
                size = sum(os.path.getsize(os.path.join(info['dir'], f))
                           for f in info['delete'])
                targets.append((info['dir'], info['delete'], size))
                case_total += size
        else:
            targets = []
            case_total = 0
            for model in models:
                hist_dir = os.path.join(archive_path, model, 'hist')
                files, total = list_files_with_size(hist_dir)
                if not files:
                    continue
                targets.append((hist_dir, files, total))
                case_total += total
            if not targets:
                print(f"  {case}: no hist/ directories found, skipping")
                continue

        if not targets:
            print(f"  {case}: nothing to delete, skipping")
            continue

        print(f"  {case}: {sum(len(t[1]) for t in targets)} file(s) to delete, "
              f"{fmt_size(case_total)} total")
        for hist, files, size in targets:
            print(f"    {hist}  ({len(files)} file(s), {fmt_size(size)})")

        action = f"DELETE {fmt_size(case_total)} of history files for {case}"
        if confirm(action, args.execute):
            for hist, files, _ in targets:
                for f in files:
                    os.remove(os.path.join(hist, f))
            print(f"    deleted ({fmt_size(case_total)} freed)")


# ---------------------------------------------------------------------------
# Subcommand: purge-logs
# ---------------------------------------------------------------------------

def cmd_purge_logs(args, paths):
    """
    Delete log files from archive/<case>/<model>/logs/ and $CASE/logs/.

    CESM writes logs to both locations. Both are safe to delete after a run
    completes — logs are never needed for restarting or analysis.

    By default both locations are targeted. Use --no-archive-logs to skip
    archive logs, or --no-case-logs to skip the case-directory logs.
    Use --models to restrict archive-side purging to specific components.

    WARNING: Without --execute this command only previews what would be removed.
    """
    archive  = paths.get('archive',  '')
    caseroot = paths.get('caseroot', '')

    if args.no_archive_logs and args.no_case_logs:
        sys.exit("ERROR: --no-archive-logs and --no-case-logs together leave "
                 "nothing to do.")

    models = args.models if args.models else HIST_MODELS
    cases  = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    for case in cases:
        case_total = 0
        targets = []  # (label, path, [files], size)

        if not args.no_archive_logs and archive:
            for model in models:
                logs_dir = os.path.join(archive, case, model, 'logs')
                files, size = list_files_with_size(logs_dir)
                if not files:
                    continue
                targets.append((f'archive/{model}/logs', logs_dir, files, size))
                case_total += size

        if not args.no_case_logs and caseroot:
            case_logs = os.path.join(caseroot, case, 'logs')
            if os.path.isdir(case_logs):
                all_files = []
                for dirpath, _, filenames in os.walk(case_logs):
                    for f in filenames:
                        all_files.append(os.path.join(dirpath, f))
                if all_files:
                    size = sum(os.path.getsize(f) for f in all_files)
                    targets.append(('casedir/logs', case_logs, all_files, size))
                    case_total += size

        if not targets:
            print(f"  {case}: no log files found, skipping")
            continue

        print(f"  {case}: {fmt_size(case_total)} across {len(targets)} log location(s)")
        for label, path, files, size in targets:
            print(f"    {label}  ({len(files)} file(s), {fmt_size(size)})")

        action = f"DELETE {fmt_size(case_total)} of log files for {case}"
        if confirm(action, args.execute):
            for label, path, files, _ in targets:
                for f in files:
                    fp = f if os.path.isabs(f) else os.path.join(path, f)
                    os.remove(fp)
            print(f"    deleted ({fmt_size(case_total)} freed)")


# ---------------------------------------------------------------------------
# Subcommand: move-hist
# ---------------------------------------------------------------------------

def cmd_move_hist(args, paths):
    """
    Move history NetCDF files from archive/<case>/<model>/hist/ to
    long-term storage, preserving the directory structure.

    Destination: <long_term>/<case>/<model>/hist/
    The source hist/ directory is left empty after the move (not deleted),
    so the archive structure remains intact.

    Use --models to restrict to specific components.
    """
    archive   = paths.get('archive', '')
    long_term = paths.get('long_term', '')
    if not archive:
        sys.exit("ERROR: archive path not configured.")
    if not long_term:
        sys.exit("ERROR: long_term path not configured. Set paths.long_term in "
                 "config_registry.yaml or use --long-term.")

    models = args.models if args.models else HIST_MODELS
    cases  = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    for case in cases:
        for model in models:
            src = os.path.join(archive, case, model, 'hist')
            files, total = list_files_with_size(src)
            if not files:
                continue
            dst = os.path.join(long_term, case, model, 'hist')
            print(f"  {case}/{model}/hist: {len(files)} file(s), {fmt_size(total)}")
            print(f"    -> {dst}")
            action = f"move {len(files)} file(s) ({fmt_size(total)}) to {dst}"
            if confirm(action, args.execute):
                os.makedirs(dst, exist_ok=True)
                for f in files:
                    shutil.move(os.path.join(src, f), os.path.join(dst, f))
                print(f"    moved {len(files)} file(s)")


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

    for case in cases:
        casestatus_path = os.path.join(caseroot, case, 'CaseStatus') if caseroot else ''
        cs = _parse_casestatus(casestatus_path) if casestatus_path else None

        # --- status line ---
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
                    # Job was started but no longer queued — likely crashed without writing status
                    status_label = 'RUNNING?'

        ts_suffix = f"  ({status_ts})" if status_ts else ''
        print(f"{case}  [{status_label}]{ts_suffix}")

        # --- --info: hist and restart breakdown ---
        if do_info and archive:
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

        # --- --energy ---
        if do_energy and archive:
            result = _energy_balance(case, archive)
            if result is not None:
                ts_mean, fsnt_mean, flnt_mean, n_used = result
                etop = fsnt_mean - flnt_mean
                sign = '+' if etop >= 0 else ''
                print(f"  Last {n_used}mo:  TS = {ts_mean:.1f} K    "
                      f"Etop = {sign}{etop:.1f} W/m²")


# ---------------------------------------------------------------------------
# Argparse helpers
# ---------------------------------------------------------------------------

def _add_destructive_args(p):
    """Add cases positional and --execute. No --all flag."""
    p.add_argument('cases', nargs='*',
                   help='Case name(s) to act on (required; no --all flag)')
    p.add_argument('--execute', action='store_true',
                   help='Actually perform actions (default is preview only)')


def _add_models_arg(p, help_prefix='Restrict to these model components'):
    p.add_argument('--models', nargs='+', metavar='MODEL',
                   choices=ARCHIVE_MODELS,
                   help=f'{help_prefix} (choices: {", ".join(ARCHIVE_MODELS)})')


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

    # ---- cata subcommand group ----
    p_cata = top_sub.add_parser(
        'cata',
        help='Catalog/archive output management (purge-bld, purge-restarts, '
             'purge-hist, purge-logs, move-hist)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cata_sub = p_cata.add_subparsers(dest='cata_command', metavar='CATA_SUBCOMMAND')

    # ---- cata purge-bld ----
    p_bld = cata_sub.add_parser(
        'purge-bld',
        help='Delete build artifacts in rundir/<case>/bld/',
        description=cmd_purge_bld.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_bld)
    p_bld.add_argument('--logs-only', action='store_true',
                       help='Remove only .o/.mod binary files, keep log files')

    # ---- cata purge-restarts ----
    p_rest = cata_sub.add_parser(
        'purge-restarts',
        help='Trim old restart sets in archive/<case>/rest/, keep last N',
        description=cmd_purge_restarts.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_rest)
    p_rest.add_argument('--keep', type=int, default=1, metavar='N',
                        help='Number of most-recent restart sets to keep (default: 1)')

    # ---- cata purge-hist ----
    p_hist = cata_sub.add_parser(
        'purge-hist',
        help='Delete history NetCDF files from archive/<case>/<model>/hist/',
        description=cmd_purge_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_hist)
    _add_models_arg(p_hist)
    p_hist.add_argument('--keep-years', type=int, default=None, metavar='N',
                        dest='keep_years',
                        help='Keep files from the N most recent model years; '
                             'cutoff is shared across all targeted components')

    # ---- cata purge-logs ----
    p_logs = cata_sub.add_parser(
        'purge-logs',
        help='Delete log files from archive/<case>/<model>/logs/ and $CASE/logs/',
        description=cmd_purge_logs.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_logs)
    _add_models_arg(p_logs, help_prefix='Restrict archive-side purging to these components')
    p_logs.add_argument('--no-archive-logs', action='store_true', dest='no_archive_logs',
                        help='Skip archive/<case>/<model>/logs/ (only purge casedir logs)')
    p_logs.add_argument('--no-case-logs', action='store_true', dest='no_case_logs',
                        help='Skip $CASE/logs/ (only purge archive logs)')

    # ---- cata move-hist ----
    p_mvhist = cata_sub.add_parser(
        'move-hist',
        help='Move history files to long-term storage (preserves archive structure)',
        description=cmd_move_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_mvhist)
    _add_models_arg(p_mvhist)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

CATA_COMMANDS = {
    'purge-bld':      cmd_purge_bld,
    'purge-restarts': cmd_purge_restarts,
    'purge-hist':     cmd_purge_hist,
    'purge-logs':     cmd_purge_logs,
    'move-hist':      cmd_move_hist,
}


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

    elif args.group == 'cata':
        if args.cata_command is None:
            # Find and print cata subparser help
            for action in parser._subparsers._actions:
                if hasattr(action, '_name_parser_map'):
                    cata_parser = action._name_parser_map.get('cata')
                    if cata_parser:
                        cata_parser.print_help()
                        break
            sys.exit(0)
        CATA_COMMANDS[args.cata_command](args, paths)


if __name__ == '__main__':
    main()

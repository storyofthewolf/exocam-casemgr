#!/usr/bin/env python3
"""
datamgr.py — ExoCAM case data management tool

Manages data for ExoCAM cases: disk reporting, surgical purging of output,
history averaging, and retirement to long-term storage. Run control
(status, continue, restart) lives in runmgr.py.

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
  report                Show disk usage per case; saves snapshot to usage.yaml
                        (--cached prints last snapshot without scanning disk)
  clean                 Surgical output housekeeping (subcommand group):
                          purge-bld       Delete build artifacts in rundir/<case>/bld/
                          purge-restarts  Trim old restart sets; keep last N
                          purge-hist      Delete history NetCDF files
                          purge-logs      Delete log files (archive + $CASE/logs/)
                          move-hist       Move history files to long-term storage
  avg                   Inspect or compute permanent time-averaged history files using ncra
  retire                Retire a case to long-term storage, then delete from
                        cesm_scratch. Three tiers:
                          bare        write case.yaml tombstone only
                          --keep-*    case.yaml + selected artifacts
                          --purge     COMPLETE ERASURE (no record written)

SAFETY
------
  All destructive subcommands require explicit case names. There is no --all
  flag — bulk operations across all cases must be done by listing each case.
  Bare invocation without case names will exit with an error.

  retire --purge is complete erasure and writes nothing to long-term.
  Prominent warnings are shown in both preview and at the confirmation prompt.
  --purge is mutually exclusive with all --keep-* flags.

  report is read-only and safe to run bare — no case names means all cases.

Run any subcommand with --help for full options, e.g.:
  python datamgr.py report --help
  python datamgr.py clean purge-bld --help
  python datamgr.py retire --help
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan import inspect_case, _rows_to_ordered, find_case_dirs
from manage_utils import (
    ARCHIVE_MODELS, HIST_MODELS, MODEL_STEM, AVG_HIST_DEFAULT_MODELS,
    DEFAULT_CONFIG, load_paths,
    dir_size_bytes, fmt_size, list_files_with_size, discover_cases,
    _hist_year, _hist_keep_years_filter, restart_sets,
    confirm, _require_cases,
)

DEFAULT_USAGE_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'usage.yaml')

CLEAN_GROUP_DESC = """\
clean — surgical output housekeeping for one or more cases

Selectively purge or relocate individual data artifacts of a case, more
finely than `retire` (which archives/erases a whole case). Every subcommand
takes explicit case name(s) or a --prefix bulk filter, previews by default,
and requires --execute to act.

SUBCOMMANDS
-----------
  purge-bld       Delete build artifacts in rundir/<case>/bld/
  purge-restarts  Trim old restart sets in archive/<case>/rest/; keep last N
  purge-hist      Delete history NetCDF files in archive/<case>/<model>/hist/
  purge-logs      Delete log files from archive/<case>/<model>/logs/ and $CASE/logs/
  move-hist       Move history files to long-term storage

Run any subcommand with --help for full options, e.g.:
  datamgr.py clean purge-bld --help
"""


# ---------------------------------------------------------------------------
# case_sizes (datamgr.py-local; not shared with runmgr.py)
# ---------------------------------------------------------------------------

def case_sizes(case, paths):
    """
    Return dict of size_bytes for each storage area of a case.
    Keys: casedir, bld, run, hist, logs, rest, archive_total
    """
    caseroot = paths.get('caseroot', '')
    rundir   = paths.get('rundir', '')
    archive  = paths.get('archive', '')

    casedir_path = os.path.join(caseroot, case) if caseroot else ''
    bld_path     = os.path.join(rundir, case, 'bld') if rundir else ''
    run_path     = os.path.join(rundir, case, 'run') if rundir else ''
    archive_path = os.path.join(archive, case) if archive else ''

    hist_bytes = 0
    logs_bytes = 0
    for model in HIST_MODELS:
        hist_bytes += dir_size_bytes(os.path.join(archive_path, model, 'hist'))
        logs_bytes += dir_size_bytes(os.path.join(archive_path, model, 'logs'))

    rest_bytes = dir_size_bytes(os.path.join(archive_path, 'rest'))

    return {
        'casedir':       dir_size_bytes(casedir_path),
        'bld':           dir_size_bytes(bld_path),
        'run':           dir_size_bytes(run_path),
        'hist':          hist_bytes,
        'logs':          logs_bytes,
        'rest':          rest_bytes,
        'archive_total': hist_bytes + logs_bytes + rest_bytes,
    }


# ---------------------------------------------------------------------------
# usage.yaml helpers
# ---------------------------------------------------------------------------

def save_usage_yaml(path, cases_data, generated_ts):
    """Clobber-write usage.yaml with cases_data as the complete snapshot.

    cases_data    : {case_name: {casedir_bytes, bld_bytes, run_bytes,
                                  hist_bytes, logs_bytes, rest_bytes, updated}}
    generated_ts  : ISO-format string written as the top-level 'generated' key.
    """
    doc = {'generated': generated_ts, 'cases': cases_data}
    with open(path, 'w') as f:
        yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_usage_yaml(path):
    """Load usage.yaml; exit with an error if the file is missing."""
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} not found. Run 'datamgr.py report' first to generate it.")
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    return doc


# ---------------------------------------------------------------------------
# Subcommand: clean purge-bld
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
# Subcommand: clean purge-restarts
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
# Subcommand: clean purge-hist
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
            "       pass: --models all   (or list them: --models " + " ".join(HIST_MODELS) + ")"
        )

    models = _resolve_models(args, HIST_MODELS)
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
# Subcommand: clean purge-logs
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

    models = _resolve_models(args, HIST_MODELS)
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
# Subcommand: clean move-hist
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

    models = _resolve_models(args, HIST_MODELS)
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
# Subcommand: report
# ---------------------------------------------------------------------------

def cmd_report(args, paths):
    """
    Show disk usage per case across cases/, rundir/, and archive/.

      report                 Full disk scan of all cases; clobbers usage.yaml
      report my_case         Diagnostic scan of one case; prints only, no yaml write
      report --prefix prox   Prefix filter; prints only, no yaml write
      report --cached        Print last saved usage.yaml without scanning disk

    Columns: CASE | CASEDIR | BLD | RUN | HIST | LOGS | REST | TOTAL
    """
    usage_path = getattr(args, 'usage_yaml', None) or DEFAULT_USAGE_YAML
    cached = getattr(args, 'cached', False)
    requested = getattr(args, 'cases', None) or []
    prefix_filter = getattr(args, 'prefix', None)

    if requested and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")
    if cached and requested:
        sys.exit("ERROR: --cached cannot be combined with explicit case names.")

    if cached:
        doc = load_usage_yaml(usage_path)
        generated = doc.get('generated', '(unknown)')
        cases_data = doc.get('cases', {}) or {}
        if not cases_data:
            print("No cases in usage.yaml.")
            return
        cases = sorted(cases_data.keys())
        print(f"(cached snapshot from {generated})")
        _print_report_table(cases, cases_data)
        return

    # Live scan
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

    col_w = max(len(c) for c in cases) + 2
    cw = 11
    header = (f"{'CASE':<{col_w}}  {'CASEDIR':>{cw}}  {'BLD':>{cw}}  {'RUN':>{cw}}  "
              f"{'HIST':>{cw}}  {'LOGS':>{cw}}  {'REST':>{cw}}  {'TOTAL':>{cw}}")
    print(header)
    print('-' * len(header))

    now_ts = datetime.datetime.now().replace(microsecond=0).isoformat()
    cases_data = {}
    grand = {k: 0 for k in ('casedir_bytes', 'bld_bytes', 'run_bytes',
                             'hist_bytes', 'logs_bytes', 'rest_bytes')}
    for case in cases:
        sz = case_sizes(case, paths)
        cd, bl, ru, hi, lo, re = (sz['casedir'], sz['bld'], sz['run'],
                                   sz['hist'], sz['logs'], sz['rest'])
        cases_data[case] = {
            'casedir_bytes': cd,
            'bld_bytes':     bl,
            'run_bytes':     ru,
            'hist_bytes':    hi,
            'logs_bytes':    lo,
            'rest_bytes':    re,
            'updated':       now_ts,
        }
        for k, v in (('casedir_bytes', cd), ('bld_bytes', bl), ('run_bytes', ru),
                     ('hist_bytes', hi), ('logs_bytes', lo), ('rest_bytes', re)):
            grand[k] += v
        print(f"{case:<{col_w}}  {fmt_size(cd):>{cw}}  {fmt_size(bl):>{cw}}  "
              f"{fmt_size(ru):>{cw}}  {fmt_size(hi):>{cw}}  "
              f"{fmt_size(lo):>{cw}}  {fmt_size(re):>{cw}}  "
              f"{fmt_size(cd + bl + ru + hi + lo + re):>{cw}}")

    grand_total = sum(grand.values())
    total_label = f"TOTAL ({len(cases_data)} cases)"
    print('-' * len(header))
    print(f"{total_label:<{col_w}}  "
          f"{fmt_size(grand['casedir_bytes']):>{cw}}  "
          f"{fmt_size(grand['bld_bytes']):>{cw}}  "
          f"{fmt_size(grand['run_bytes']):>{cw}}  "
          f"{fmt_size(grand['hist_bytes']):>{cw}}  "
          f"{fmt_size(grand['logs_bytes']):>{cw}}  "
          f"{fmt_size(grand['rest_bytes']):>{cw}}  "
          f"{fmt_size(grand_total):>{cw}}")

    # Bare invocation: clobber usage.yaml with the full snapshot.
    # Named-case or --prefix filtered invocation: diagnostic print only, no yaml write.
    if not requested and not prefix_filter:
        caseroot = paths.get('caseroot', '')
        no_caseroot = sum(1 for c in cases if not os.path.isdir(os.path.join(caseroot, c)))
        if no_caseroot:
            print(f"Note: {no_caseroot} of {len(cases)} cases have no caseroot directory (archive/rundir only).")
        save_usage_yaml(usage_path, cases_data, now_ts)


def _print_report_table(cases, cases_data):
    """Print the aligned disk-usage table from a {case: bytes-dict} mapping."""
    col_w = max(len(c) for c in cases) + 2
    cw = 11
    header = (f"{'CASE':<{col_w}}  {'CASEDIR':>{cw}}  {'BLD':>{cw}}  {'RUN':>{cw}}  "
              f"{'HIST':>{cw}}  {'LOGS':>{cw}}  {'REST':>{cw}}  {'TOTAL':>{cw}}")
    print(header)
    print('-' * len(header))

    grand = {k: 0 for k in ('casedir_bytes', 'bld_bytes', 'run_bytes',
                             'hist_bytes', 'logs_bytes', 'rest_bytes')}
    for case in cases:
        d = cases_data.get(case, {})
        cd = d.get('casedir_bytes', 0)
        bl = d.get('bld_bytes',     0)
        ru = d.get('run_bytes',     0)
        hi = d.get('hist_bytes',    0)
        lo = d.get('logs_bytes',    0)
        re = d.get('rest_bytes',    0)
        total = cd + bl + ru + hi + lo + re
        for k, v in (('casedir_bytes', cd), ('bld_bytes', bl), ('run_bytes', ru),
                     ('hist_bytes', hi), ('logs_bytes', lo), ('rest_bytes', re)):
            grand[k] += v
        print(f"{case:<{col_w}}  {fmt_size(cd):>{cw}}  {fmt_size(bl):>{cw}}  "
              f"{fmt_size(ru):>{cw}}  {fmt_size(hi):>{cw}}  "
              f"{fmt_size(lo):>{cw}}  {fmt_size(re):>{cw}}  "
              f"{fmt_size(total):>{cw}}")

    grand_total = sum(grand.values())
    total_label = f"TOTAL ({len(cases)} cases)"
    print('-' * len(header))
    print(f"{total_label:<{col_w}}  "
          f"{fmt_size(grand['casedir_bytes']):>{cw}}  "
          f"{fmt_size(grand['bld_bytes']):>{cw}}  "
          f"{fmt_size(grand['run_bytes']):>{cw}}  "
          f"{fmt_size(grand['hist_bytes']):>{cw}}  "
          f"{fmt_size(grand['logs_bytes']):>{cw}}  "
          f"{fmt_size(grand['rest_bytes']):>{cw}}  "
          f"{fmt_size(grand_total):>{cw}}")


# ---------------------------------------------------------------------------
# Subcommand: retire-case
# ---------------------------------------------------------------------------

DEFAULT_RETIRE_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'active.yaml')


def _load_registry_entry(case, registry_path):
    """Return the raw grouped entry dict for *case* from registry_path, or None."""
    if not registry_path or not os.path.exists(registry_path):
        return None
    with open(registry_path) as f:
        data = yaml.safe_load(f) or {}
    for entry in data.get('cases', []):
        if (entry.get('meta') or {}).get('case_name') == case:
            return entry
    return None


def _scan_source(case, casedir_path, registry_path):
    """Determine which tier will be used to source case.yaml content (no writes).

    Returns 'live', 'registry', or 'stub'.
    """
    if casedir_path and find_case_dirs(casedir_path):
        return 'live'
    if _load_registry_entry(case, registry_path) is not None:
        return 'registry'
    return 'stub'


def _write_case_yaml(case, lt_case_dir, casedir_path, registry_path):
    """Write case.yaml into lt_case_dir using a three-tier fallback.

    Tier 1 (live): casedir_path exists and is a valid ExoCAM case — inspect_case() is called.
    Tier 2 (registry): fall back to the entry in active.yaml via _load_registry_entry().
    Tier 3 (stub): write minimal {case_name, retired_date}.

    Returns 'live', 'registry', or 'stub'.
    """
    os.makedirs(lt_case_dir, exist_ok=True)
    dst = os.path.join(lt_case_dir, 'case.yaml')

    if casedir_path and find_case_dirs(casedir_path):
        row = inspect_case(casedir_path)
        doc = _rows_to_ordered([row])
        with open(dst, 'w') as f:
            yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return 'live'

    entry = _load_registry_entry(case, registry_path)
    if entry is not None:
        with open(dst, 'w') as f:
            yaml.dump({'cases': [entry]}, f,
                      default_flow_style=False, allow_unicode=True, sort_keys=False)
        return 'registry'

    stub = {'case_name': case, 'retired_date': datetime.date.today().isoformat()}
    with open(dst, 'w') as f:
        yaml.dump(stub, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return 'stub'


def _copy_case_config(casedir_path, lt_case_dir):
    """Copy SourceMods/, user_* files, and env_* files from casedir to lt_case_dir.

    Returns list of (label, src, dst) describing what was copied.
    """
    actions = []

    # SourceMods/
    src_sm = os.path.join(casedir_path, 'SourceMods')
    if os.path.isdir(src_sm):
        dst_sm = os.path.join(lt_case_dir, 'SourceMods')
        actions.append(('SourceMods/', src_sm, dst_sm))

    # user_* files
    nl_dst = os.path.join(lt_case_dir, 'namelists')
    try:
        for name in sorted(os.listdir(casedir_path)):
            if name.startswith('user_') and os.path.isfile(
                    os.path.join(casedir_path, name)):
                actions.append((f'namelists/{name}',
                                 os.path.join(casedir_path, name),
                                 os.path.join(nl_dst, name)))
    except OSError:
        pass

    # env_* files
    env_dst = os.path.join(lt_case_dir, 'env')
    try:
        for name in sorted(os.listdir(casedir_path)):
            if name.startswith('env_') and os.path.isfile(
                    os.path.join(casedir_path, name)):
                actions.append((f'env/{name}',
                                 os.path.join(casedir_path, name),
                                 os.path.join(env_dst, name)))
    except OSError:
        pass

    return actions


def _execute_copy_case_config(actions):
    """Perform the copies described by _copy_case_config actions list."""
    for label, src, dst in actions:
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def cmd_retire_case(args, paths):
    """
    Retire one or more cases from cesm_scratch. Three retirement tiers:

    Tier 1 — bare retire (no flags):
      Write case.yaml tombstone to long-term, then delete everything.
      Use for completed cases where no files need to be preserved.

    Tier 2 — --keep-* flags (one or more):
      case.yaml is always written implicitly. Additionally preserve:
        --keep-config      Copy SourceMods/, user_*, and env_* to long-term.
        --keep-years N     Move hist files from the N most recent model years.
        --keep-restarts    Move the most recent restart set.
      --keep-* flags are freely combinable. Then delete everything from cesm_scratch.

    Tier 3 — --purge:
      COMPLETE ERASURE. No case.yaml, no config, no data — nothing is preserved
      in long-term storage. Use only when you are certain no record is needed.
      Mutually exclusive with all --keep-* flags.

    In all modes except --purge, case.yaml is written to long_term/<case>/case.yaml.
    If the case is found in --registry (default: active.yaml), the full registry
    entry is written; otherwise a minimal stub (case_name, retired_date) is written.

    Avg files (filenames containing "avg") found in any archive/<case>/<model>/hist/
    are always moved to long-term storage regardless of which flags are used.

    Long-term layout:
      long_term/<case>/case.yaml
      long_term/<case>/SourceMods/          (--keep-config only)
      long_term/<case>/namelists/           (--keep-config only)
      long_term/<case>/env/                 (--keep-config only)
      long_term/<case>/<model>/hist/        (--keep-years and/or avg files)
      long_term/<case>/rest/<date>/         (--keep-restarts only)

    SAFEGUARDS:
      - --execute required; default is preview only.
      - Explicit case names required; no --all flag.
      - Each case requires individual yes/no confirmation before execution.
      - When --prefix is used, a single batch yes/no confirmation is shown
        for all matched cases before any action is taken.

    Examples:
      retire mycase --execute
          Write case.yaml tombstone only; delete everything from cesm_scratch.

      retire mycase --keep-config --execute
          Save SourceMods/, user_*, and env_* (plus case.yaml); delete everything.

      retire mycase --keep-config --keep-years 1 --keep-restarts --execute
          Save config files, 1 year of history, and most recent restart;
          delete everything else from cesm_scratch.

      retire mycase --purge --execute
          COMPLETE ERASURE: delete everything, write nothing to long-term.

      retire --prefix hazyCHAMPS_case23 --purge --execute
          Retire all cases whose name starts with hazyCHAMPS_case23.
          Single yes/no confirmation shown for the full matched batch.

    WARNING: deletions are permanent.
    """
    caseroot  = paths.get('caseroot', '')
    rundir    = paths.get('rundir',   '')
    archive   = paths.get('archive',  '')
    long_term = paths.get('long_term', '')

    if not any([caseroot, rundir, archive]):
        sys.exit("ERROR: no storage paths configured.")

    if not long_term:
        sys.exit("ERROR: retire-case requires long_term path. "
                 "Set paths.long_term in config_registry.yaml or use --long-term.")

    has_keep = args.keep_config or args.keep_years is not None or args.keep_restarts
    if args.purge and has_keep:
        sys.exit("ERROR: --purge is mutually exclusive with --keep-config, "
                 "--keep-years, and --keep-restarts.")

    prefix_filter = getattr(args, 'prefix', None)

    registry_path = getattr(args, 'registry', None) or DEFAULT_RETIRE_REGISTRY

    # _require_cases enforces prefix/names mutual exclusion and the no-selection
    # error; prefix_filter is retained below as the batch-vs-per-case mode flag.
    cases = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    # In prefix mode, build all plans first, print them all, then confirm once.
    # In non-prefix mode, confirm per-case after printing each plan.
    # plans[case] caches the built plan so the execute pass doesn't rebuild.
    plans = {}

    # Pass 1: build and print all plans.
    for case in cases:
        print(f"\n{'='*60}")
        print(f"  CASE: {case}")
        print(f"{'='*60}")

        casedir_path = os.path.join(caseroot, case) if caseroot else ''
        rundir_path  = os.path.join(rundir,   case) if rundir   else ''
        archive_path = os.path.join(archive,  case) if archive  else ''
        lt_case_dir  = os.path.join(long_term, case)

        sz = case_sizes(case, paths)
        total_on_disk = sum(sz[k] for k in ('casedir', 'bld', 'run', 'hist', 'logs', 'rest'))

        # --- build plan ---

        if args.purge:
            print(f"\n  *** WARNING: --purge ***")
            print(f"  *** COMPLETE ERASURE — no case.yaml, no config, nothing will be")
            print(f"  *** preserved in long-term storage. This is irreversible.")

        # case.yaml source tier (skipped for --purge)
        yaml_source = None
        if not args.purge:
            yaml_source = _scan_source(case, casedir_path, registry_path)
            if yaml_source == 'stub':
                print(f"  WARNING: '{case}' casedir not found and not in registry {registry_path}.")
                print(f"           A minimal case.yaml stub will be written.")

        # config copy (--keep-config only)
        config_actions = []
        if args.keep_config and casedir_path and os.path.isdir(casedir_path):
            config_actions = _copy_case_config(casedir_path, lt_case_dir)

        # hist preservation
        preserve_hist = []  # (src_file, dst_file)
        if args.keep_years is not None:
            keep_years, per_model = _hist_keep_years_filter(
                archive_path, HIST_MODELS, args.keep_years)
            keep_set = set(keep_years)
            for model, info in per_model.items():
                for f in info['keep']:
                    y = _hist_year(f)
                    if y and y in keep_set:
                        src = os.path.join(info['dir'], f)
                        dst = os.path.join(lt_case_dir, model, 'hist', f)
                        preserve_hist.append((src, dst))
            if not per_model:
                hist_dir = os.path.join(archive_path)
                print(f"  WARNING: --keep-years specified but no history files found in {hist_dir}")

        # restart preservation
        preserve_restart = []  # (src_dir, dst_dir)
        if args.keep_restarts:
            sets = restart_sets(case, paths)
            if sets:
                date_str, rest_path = sets[-1]
                preserve_restart.append(
                    (rest_path, os.path.join(lt_case_dir, 'rest', date_str)))
            else:
                rest_dir = os.path.join(paths.get('archive', ''), case, 'rest')
                print(f"  WARNING: --keep-restarts specified but no restart sets found in {rest_dir}")

        # avg file preservation (skipped under --purge; otherwise unconditional)
        preserve_avg = []  # (src, dst)
        for model in (HIST_MODELS if not args.purge else []):
            hist_dir = os.path.join(archive_path, model, 'hist')
            files, _ = list_files_with_size(hist_dir)
            for f in files:
                if 'avg' in f:
                    preserve_avg.append((
                        os.path.join(hist_dir, f),
                        os.path.join(lt_case_dir, model, 'hist', f),
                    ))

        # --- print plan ---
        yaml_source_label = {
            'live':     'live scan',
            'registry': 'registry (active.yaml)',
            'stub':     'minimal stub',
        }.get(yaml_source, '')
        print(f"\n  Total on cesm_scratch: {fmt_size(total_on_disk)}")
        if args.purge:
            print(f"  NO files will be written to long-term.")
        else:
            print(f"  COPY to long-term: {lt_case_dir}/case.yaml "
                  f"(source: {yaml_source_label})")
        if config_actions:
            print(f"  COPY to long-term: SourceMods/, namelists/, env/ "
                  f"({len(config_actions)} item(s))")
            for label, src, dst in config_actions:
                print(f"    {src}  ->  {dst}")
        if preserve_hist:
            hist_size = sum(os.path.getsize(s) for s, _ in preserve_hist)
            print(f"  MOVE to long-term: {len(preserve_hist)} hist file(s) "
                  f"from last {args.keep_years} year(s) ({fmt_size(hist_size)})")
        if preserve_restart:
            rest_size = sum(dir_size_bytes(s) for s, _ in preserve_restart)
            print(f"  MOVE to long-term: most recent restart set ({fmt_size(rest_size)})")
        if preserve_avg:
            print(f"  MOVE to long-term: {len(preserve_avg)} avg file(s)")
            for src, dst in preserve_avg:
                print(f"    {src}  ->  {dst}")
        print(f"  DELETE from cesm_scratch:")
        for label, p in [('casedir', casedir_path),
                         ('rundir',  rundir_path),
                         ('archive', archive_path)]:
            if not p:
                continue
            if os.path.exists(p):
                print(f"    {p}")
            else:
                print(f"    {p}  (not found on disk)")

        if not args.execute:
            print(f"\n  [preview] add --execute to perform these actions")
            continue

        plans[case] = dict(
            casedir_path=casedir_path,
            rundir_path=rundir_path,
            archive_path=archive_path,
            lt_case_dir=lt_case_dir,
            total_on_disk=total_on_disk,
            yaml_source=yaml_source,
            config_actions=config_actions,
            preserve_hist=preserve_hist,
            preserve_restart=preserve_restart,
            preserve_avg=preserve_avg,
        )

    if not args.execute:
        return

    # In prefix mode, show batch summary and confirm once for all cases.
    if prefix_filter:
        combined_bytes = sum(p['total_on_disk'] for p in plans.values())
        print(f"\n{'='*60}")
        print(f"  BATCH: {len(plans)} case(s) matched prefix '{prefix_filter}'")
        print(f"  Combined footprint: {fmt_size(combined_bytes)}")
        if args.purge:
            print(f"\n  *** WARNING: --purge — COMPLETE ERASURE ***")
            print(f"  *** Nothing will be written to long-term. This is irreversible.")
        answer = input(f"\n  Confirm retire-case for ALL {len(plans)} matched case(s)? [yes/no]: ").strip().lower()
        if answer != 'yes':
            print("  Aborted.")
            return

    # Pass 2: execute using cached plans.
    for case in list(plans.keys()):
        p = plans[case]
        casedir_path   = p['casedir_path']
        rundir_path    = p['rundir_path']
        archive_path   = p['archive_path']
        lt_case_dir    = p['lt_case_dir']
        yaml_source    = p['yaml_source']
        config_actions = p['config_actions']
        preserve_hist  = p['preserve_hist']
        preserve_restart = p['preserve_restart']
        preserve_avg   = p['preserve_avg']

        if not prefix_filter:
            if args.purge:
                print(f"\n  *** WARNING: --purge — COMPLETE ERASURE ***")
                print(f"  *** Nothing will be written to long-term. This is irreversible.")
            answer = input(f"\n  Confirm retire-case for '{case}'? [yes/no]: ").strip().lower()
            if answer != 'yes':
                print(f"  Skipped.")
                continue

        # Write case.yaml (skipped for --purge)
        if not args.purge:
            actual_source = _write_case_yaml(case, lt_case_dir, casedir_path, registry_path)
            actual_source_label = {
                'live':     'live scan',
                'registry': 'registry (active.yaml)',
                'stub':     'minimal stub',
            }[actual_source]
            print(f"  Written: {lt_case_dir}/case.yaml")
            print(f"  case.yaml written from: {actual_source_label}")

        # Copy config files (--keep-config only)
        if config_actions:
            print(f"  Copying SourceMods/, namelists/, env/...")
            _execute_copy_case_config(config_actions)

        # Move hist files
        if preserve_hist:
            print(f"  Moving {len(preserve_hist)} hist file(s) to long-term...")
            for src, dst in preserve_hist:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        # Move restart set
        if preserve_restart:
            print(f"  Moving restart set to long-term...")
            for src, dst in preserve_restart:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        # Move avg files
        if preserve_avg:
            print(f"  Moving {len(preserve_avg)} avg file(s) to long-term...")
            for src, dst in preserve_avg:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        # Delete from cesm_scratch
        deleted_bytes = 0
        print(f"  Deleting from cesm_scratch...")
        for p_path in [casedir_path, rundir_path, archive_path]:
            if p_path and os.path.exists(p_path):
                deleted_bytes += dir_size_bytes(p_path)
                shutil.rmtree(p_path)
                print(f"    deleted {p_path}")

        # Tally what landed in long-term
        kept_bytes = dir_size_bytes(lt_case_dir)

        print(f"  Done: {case}  "
              f"(freed {fmt_size(deleted_bytes)} from cesm_scratch, "
              f"kept {fmt_size(kept_bytes)} in long-term)")


# ---------------------------------------------------------------------------
# Subcommand: avg-hist
# ---------------------------------------------------------------------------

def cmd_avg_hist(args, paths):
    """
    Compute or inspect time-averaged history files using ncra (NCO).

    Operates on archive/<case>/<model>/hist/ for each targeted model.
    Default models: atm, lnd, ice.

    Modes (mutually exclusive, exactly one required):

      --info             Print file count, year span, and total size per model.
                         Read-only; --execute is not needed or used.

      --last N           Average the N most recent model years using ncra.
                         Avg files (filenames containing "avg") are excluded
                         from inputs. Output is written into the same hist/
                         directory as the inputs.

    Output filename format:
      <case>.<model_stem>.h0.avg_last{N}yr.nc

    Example:
      avg-hist mycase --info
      avg-hist mycase --last 10 --models atm lnd
      avg-hist mycase --last 10 --execute
    """
    archive = paths.get('archive', '')
    if not archive:
        sys.exit("ERROR: archive path not configured.")

    has_info = getattr(args, 'info', False)
    last_n   = getattr(args, 'last', None)
    prefix_filter = getattr(args, 'prefix', None)

    if has_info and last_n is not None:
        sys.exit("ERROR: --info and --last are mutually exclusive.")
    if not has_info and last_n is None:
        sys.exit("ERROR: avg-hist requires --info or --last N.")

    if args.cases and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")
    if not args.cases and not prefix_filter:
        sys.exit("ERROR: avg-hist requires explicit case name(s) or --prefix.")

    all_on_disk = discover_cases(paths)

    if prefix_filter:
        cases = [c for c in all_on_disk if c.lower().startswith(prefix_filter.lower())]
        if not cases:
            print(f"No cases matching prefix '{prefix_filter}'.")
            return
    else:
        missing = [c for c in args.cases if c not in all_on_disk]
        if missing:
            print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
        cases = [c for c in args.cases if c in all_on_disk]
        if not cases:
            print("No cases found on disk.")
            return

    models = _resolve_models(args, AVG_HIST_DEFAULT_MODELS)

    # --- --info mode ---
    if has_info:
        for case in cases:
            print(f"\n{case}")
            for model in models:
                hist_dir = os.path.join(archive, case, model, 'hist')
                files, total = list_files_with_size(hist_dir)
                non_avg = [f for f in files if 'avg' not in f]
                if not non_avg:
                    print(f"  {model}/hist:    0 files")
                    continue
                years = sorted(y for y in (_hist_year(f) for f in non_avg) if y)
                if years:
                    span = f"years {years[0]}–{years[-1]}"
                else:
                    span = "years unknown"
                avg_note = ", avg file present" if any('avg' in f for f in files) else ""
                print(f"  {model}/hist:  {len(non_avg):>4} files,  {span}  ({fmt_size(total)}){avg_note}")
            sets = restart_sets(case, paths)
            rest_count = len(sets)
            if rest_count:
                rest_total = sum(dir_size_bytes(s[1]) for s in sets)
                print(f"  rest:      {rest_count:>4} folders present  ({fmt_size(rest_total)})")
            else:
                print(f"  rest:      {0:>4} folders present")
        return

    # --- --last N mode ---
    for case in cases:
        archive_path = os.path.join(archive, case)
        print(f"\n{case}")
        _, per_model = _hist_keep_years_filter(archive_path, models, last_n)

        all_years_count = len(set(
            _hist_year(f)
            for info in per_model.values()
            for f in info['keep'] + info['delete']
            if _hist_year(f)
        ))
        if all_years_count < last_n:
            print(f"  WARNING: only {all_years_count} year(s) available, "
                  f"averaging all {all_years_count} (requested --last {last_n})")

        for model in models:
            if model not in per_model:
                print(f"  {model}/hist: no files found, skipping")
                continue

            info = per_model[model]
            hist_dir = info['dir']
            inputs = sorted(f for f in info['keep'] if 'avg' not in f)
            if not inputs:
                print(f"  {model}/hist: no non-avg files in last {last_n} year(s), skipping")
                continue

            stem = MODEL_STEM.get(model, model)
            outfile = f"{case}.{stem}.h0.avg_last{last_n}yr.nc"
            outpath = os.path.join(hist_dir, outfile)
            input_paths = [os.path.join(hist_dir, f) for f in inputs]
            cmd = ['ncra'] + input_paths + [outpath]

            print(f"  {model}/hist: {len(inputs)} input file(s) -> {outfile}")
            if not args.execute:
                print(f"  [preview] would run: {' '.join(cmd)}")
                continue

            print(f"  Running ncra ({len(inputs)} file(s))...")
            try:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            except FileNotFoundError:
                sys.exit("ERROR: ncra not found in PATH. Install NCO tools.")
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                sys.exit(f"ERROR: ncra exited with code {result.returncode}")
            print(f"  Written: {outpath}")

    if not args.execute and last_n is not None:
        print("\n[preview] add --execute to perform these actions")


# ---------------------------------------------------------------------------
# Argparse helpers (shared across destructive subcommands)
# ---------------------------------------------------------------------------

def _add_destructive_args(p):
    """Add cases positional, --prefix bulk filter, and --execute. No --all flag."""
    p.add_argument('cases', nargs='*',
                   help='Case name(s) to act on (or use --prefix; no --all flag)')
    p.add_argument('--prefix', metavar='STR', default=None,
                   help='Case-insensitive prefix filter; cannot combine with '
                        'explicit case names')
    p.add_argument('--execute', action='store_true',
                   help='Actually perform actions (default is preview only)')


def _add_models_arg(p, help_prefix='Restrict to these model components'):
    p.add_argument('--models', nargs='+', metavar='MODEL',
                   choices=ARCHIVE_MODELS + ['all'],
                   help=f'{help_prefix} (choices: {", ".join(ARCHIVE_MODELS)}; '
                        f'or "all" for every component this command targets by default)')


def _resolve_models(args, default):
    """Resolve --models into a concrete component list.

    Returns `default` when --models is omitted or given as the literal "all"
    (so `--models all` explicitly targets every component the verb handles by
    default — for purge-hist/move-hist that is HIST_MODELS, i.e. rest/ excluded).
    Otherwise returns the explicit component list as given.
    """
    if not args.models or args.models == ['all']:
        return default
    return args.models


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='datamgr.py',
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

    sub = parser.add_subparsers(dest='command', metavar='SUBCOMMAND', help=argparse.SUPPRESS)
    sub.required = True

    # ---- report (read-only; no --execute; empty cases = all) ----
    p_report = sub.add_parser(
        'report',
        help=argparse.SUPPRESS,
        description=cmd_report.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_report.add_argument('cases', nargs='*',
                          help='Case name(s) to report (default: all discovered cases)')
    p_report.add_argument('--prefix', metavar='STR', default=None,
                          help='Case-insensitive prefix filter; prints only, no yaml write '
                               '(cannot combine with explicit case names)')
    p_report.add_argument('--cached', action='store_true',
                          help='Print last saved usage.yaml without scanning disk '
                               '(cannot combine with case names)')
    p_report.add_argument('--usage-yaml', metavar='FILE', default=None, dest='usage_yaml',
                          help=f'Path to usage.yaml snapshot '
                               f'(default: usage.yaml next to this script)')

    # ---- clean subcommand group ----
    p_clean = sub.add_parser(
        'clean',
        help=argparse.SUPPRESS,
        description=CLEAN_GROUP_DESC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    clean_sub = p_clean.add_subparsers(dest='clean_command', metavar='CLEAN_SUBCOMMAND')
    clean_sub.required = True

    # ---- clean purge-bld ----
    p_bld = clean_sub.add_parser(
        'purge-bld',
        help='Delete build artifacts in rundir/<case>/bld/',
        description=cmd_purge_bld.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_bld)
    p_bld.add_argument('--logs-only', action='store_true',
                       help='Remove only .o/.mod binary files, keep log files')

    # ---- clean purge-restarts ----
    p_rest = clean_sub.add_parser(
        'purge-restarts',
        help='Trim old restart sets in archive/<case>/rest/; keep last N',
        description=cmd_purge_restarts.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_rest)
    p_rest.add_argument('--keep', type=int, default=1, metavar='N',
                        help='Number of most-recent restart sets to keep (default: 1)')

    # ---- clean purge-hist ----
    p_hist = clean_sub.add_parser(
        'purge-hist',
        help='Delete history NetCDF files in archive/<case>/<model>/hist/',
        description=cmd_purge_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_hist)
    _add_models_arg(p_hist)
    p_hist.add_argument('--keep-years', type=int, default=None, metavar='N',
                        dest='keep_years',
                        help='Keep files from the N most recent model years; '
                             'cutoff is shared across all targeted components')

    # ---- clean purge-logs ----
    p_logs = clean_sub.add_parser(
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

    # ---- clean move-hist ----
    p_mvhist = clean_sub.add_parser(
        'move-hist',
        help='Move history files to long-term storage',
        description=cmd_move_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_mvhist)
    _add_models_arg(p_mvhist)

    # ---- avg ----
    p_avg = sub.add_parser(
        'avg',
        help=argparse.SUPPRESS,
        description=cmd_avg_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_avg.add_argument('cases', nargs='*',
                       help='Case name(s) to process (or use --prefix)')
    p_avg.add_argument('--prefix', metavar='STR', default=None,
                       help='Case-insensitive prefix filter; cannot combine with explicit case names')
    p_avg.add_argument('--execute', action='store_true',
                       help='Actually run ncra (default is preview only)')
    _add_models_arg(p_avg, help_prefix=f'Models to process (default: {", ".join(AVG_HIST_DEFAULT_MODELS)})')
    mode = p_avg.add_mutually_exclusive_group()
    mode.add_argument('--info', action='store_true',
                      help='Print file count, year span, and size per model (read-only)')
    mode.add_argument('--last', type=int, metavar='N',
                      help='Average the N most recent model years using ncra')

    # ---- retire ----
    p_arc = sub.add_parser(
        'retire',
        help=argparse.SUPPRESS,
        description=cmd_retire_case.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_arc)
    p_arc.add_argument('--purge', action='store_true',
                       help='COMPLETE ERASURE: delete everything from cesm_scratch and write '
                            'nothing to long-term. Mutually exclusive with all --keep-* flags.')
    p_arc.add_argument('--keep-config', action='store_true', dest='keep_config',
                       help='Copy SourceMods/, user_*, and env_* to long-term (case.yaml is '
                            'always written implicitly). Combinable with --keep-years and '
                            '--keep-restarts.')
    p_arc.add_argument('--keep-years', type=int, metavar='N', default=None,
                       dest='keep_years',
                       help='Move hist files from the N most recent model years to long-term '
                            '(case.yaml always written). Combinable with --keep-config and '
                            '--keep-restarts.')
    p_arc.add_argument('--keep-restarts', action='store_true', dest='keep_restarts',
                       help='Move the most recent restart set to long-term (case.yaml always '
                            'written). Combinable with --keep-config and --keep-years.')
    p_arc.add_argument('--registry', metavar='FILE', default=None,
                       help=f'Path to active.yaml for case.yaml export '
                            f'(default: {DEFAULT_RETIRE_REGISTRY})')

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

CLEAN_COMMANDS = {
    'purge-bld':      cmd_purge_bld,
    'purge-restarts': cmd_purge_restarts,
    'purge-hist':     cmd_purge_hist,
    'purge-logs':     cmd_purge_logs,
    'move-hist':      cmd_move_hist,
}

COMMANDS = {
    'report':  cmd_report,
    'avg':     cmd_avg_hist,
    'retire':  cmd_retire_case,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    paths = load_paths(args)

    missing_paths = [k for k in ('caseroot', 'rundir', 'archive')
                     if not paths.get(k)]
    if missing_paths:
        print(f"WARNING: paths not configured: {', '.join(missing_paths)}. "
              f"Set them in config_registry.yaml.", file=sys.stderr)

    if args.command == 'clean':
        CLEAN_COMMANDS[args.clean_command](args, paths)
    else:
        COMMANDS[args.command](args, paths)


if __name__ == '__main__':
    main()

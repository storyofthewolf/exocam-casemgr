"""
exo_data.py — ExoCAM data management tool

Inspect, manage, and purge GCM data across the three primary storage areas:
  cases/    CESM case directories (build scripts, SourceMods, namelists)
  rundir/   Active run environment (bld/ and run/ subdirectories)
  archive/  Model output (hist/, logs/, rest/ per component)

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
  report              Show disk usage per case across all three areas (default;
                      bare invocation reports on every discovered case)
  purge-bld           Delete build artifacts in rundir/<case>/bld/
  purge-restarts      Trim old restart sets in archive/<case>/rest/; keep last N
  purge-hist          Delete history NetCDF files in archive/<case>/<model>/hist/
  purge-logs          Delete log files from archive/<case>/<model>/logs/ and $CASE/logs/
  move-hist           Move history files to long-term storage
  retire-case         Retire a case: preserve data to long-term and/or delete from cesm_scratch

SAFETY
------
  All destructive subcommands require explicit case names. There is no --all
  flag — bulk operations across all cases must be done by listing each case.
  Bare invocation without case names will exit with an error.

  purge-hist additionally requires --keep-years N or --models to prevent
  accidental deletion of all history files.

  retire-case requires one of --keep-years N, --keep-restarts, --keep-case,
  or --purge-only to force stating intent explicitly. --keep-case moves the
  entire case tree to long-term storage without deleting anything.

  report is read-only and safe to run bare — no case names means all cases.

Run any subcommand with --help for full options, e.g.:
  python exo_data.py purge-bld --help
"""

import argparse
import os
import re
import shutil
import sys
import yaml

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

ARCHIVE_MODELS = ['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']
HIST_MODELS = [m for m in ARCHIVE_MODELS if m != 'rest']

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'config_registry.yaml')


def load_paths(args):
    """Load paths from config_registry.yaml, then apply any CLI overrides."""
    paths = {}
    cfg_path = getattr(args, 'config_registry', DEFAULT_CONFIG)
    if cfg_path and os.path.exists(cfg_path):
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        paths = data.get('paths', {})

    overrides = {
        'caseroot':  getattr(args, 'caseroot',  None),
        'rundir':    getattr(args, 'rundir',    None),
        'archive':   getattr(args, 'archive',   None),
        'long_term': getattr(args, 'long_term', None),
    }
    for k, v in overrides.items():
        if v:
            paths[k] = v
    return paths


# ---------------------------------------------------------------------------
# Disk usage helpers
# ---------------------------------------------------------------------------

def dir_size_bytes(path):
    """Return total bytes under path, or 0 if path doesn't exist.

    Uses os.scandir so each entry's stat() is fetched once (one syscall).
    """
    if not os.path.exists(path):
        return 0
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += dir_size_bytes(entry.path)
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def fmt_size(nbytes):
    """Format bytes as human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def list_files_with_size(directory):
    """Return (filenames, total_bytes) for files directly inside directory.

    Subdirectories are ignored. Returns ([], 0) if directory is missing.
    """
    if not os.path.isdir(directory):
        return [], 0
    files = []
    total = 0
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        files.append(entry.name)
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        return [], 0
    return files, total


def discover_cases(paths):
    """
    Return sorted list of case names that appear in at least one of
    caseroot, rundir, or archive.
    """
    names = set()
    for key in ('caseroot', 'rundir', 'archive'):
        d = paths.get(key, '')
        if d and os.path.isdir(d):
            for name in os.listdir(d):
                if os.path.isdir(os.path.join(d, name)):
                    names.add(name)
    return sorted(names)


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


def restart_sets(case, paths):
    """
    Return sorted list of (date_str, path) for restart sets in
    archive/<case>/rest/, oldest first.
    """
    archive = paths.get('archive', '')
    rest_dir = os.path.join(archive, case, 'rest')
    if not os.path.isdir(rest_dir):
        return []
    sets = []
    for name in os.listdir(rest_dir):
        full = os.path.join(rest_dir, name)
        if os.path.isdir(full):
            sets.append((name, full))
    return sorted(sets, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Hist year filtering (shared by purge-hist and archive-case)
# ---------------------------------------------------------------------------

_RE_HIST_YEAR = re.compile(r'\.(\d{4})-\d{2}')


def _hist_year(filename):
    """Extract model year string from hist filename, e.g. '0050' from case.cam.h0.0050-01.nc."""
    m = _RE_HIST_YEAR.search(filename)
    return m.group(1) if m else None


def _hist_keep_years_filter(archive_path, models, keep_n):
    """
    Partition hist files across *models* under archive_path into keep/delete
    based on retaining the *keep_n* most recent model years.

    Returns (keep_years, per_model) where:
      keep_years : sorted list of year strings to retain
      per_model  : {model: {'dir': path, 'keep': [files], 'delete': [files]}}

    Files whose year cannot be parsed are placed in 'keep' (never deleted).
    """
    all_years = set()
    listings = {}
    for model in models:
        hist_dir = os.path.join(archive_path, model, 'hist')
        files, _ = list_files_with_size(hist_dir)
        if not files:
            continue
        listings[model] = (hist_dir, files)
        for f in files:
            y = _hist_year(f)
            if y:
                all_years.add(y)

    keep_years = sorted(all_years)[-keep_n:] if all_years and keep_n > 0 else []
    keep_set = set(keep_years)

    per_model = {}
    for model, (hist_dir, files) in listings.items():
        keep_files, delete_files = [], []
        for f in files:
            y = _hist_year(f)
            if y is None or y in keep_set:
                keep_files.append(f)
            else:
                delete_files.append(f)
        per_model[model] = {'dir': hist_dir, 'keep': keep_files, 'delete': delete_files}

    return keep_years, per_model


# ---------------------------------------------------------------------------
# Confirmation helper
# ---------------------------------------------------------------------------

def confirm(prompt, execute):
    """Return True if the action should proceed."""
    if not execute:
        print(f"  [preview] would: {prompt}")
        return False
    answer = input(f"  Confirm: {prompt} [yes/no]: ").strip().lower()
    return answer == 'yes'


# ---------------------------------------------------------------------------
# Case selection helper (destructive subcommands only)
# ---------------------------------------------------------------------------

def _require_cases(all_cases, args):
    """Return cases from args.cases that exist on disk.

    Exits with an error if no case names are provided. There is no --all flag.
    """
    requested = getattr(args, 'cases', None) or []
    if not requested:
        sys.exit("ERROR: specify case name(s). No --all flag is provided for "
                 "destructive operations — list cases explicitly.")
    missing = [c for c in requested if c not in all_cases]
    if missing:
        print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
    return [c for c in requested if c in all_cases]


# ---------------------------------------------------------------------------
# Subcommand: report
# ---------------------------------------------------------------------------

def cmd_report(args, paths):
    """
    Show disk usage per case across cases/, rundir/, and archive/.

    Read-only. With no case names, reports on every discovered case.

    Columns: CASE | CASEDIR | BLD | RUN | HIST | LOGS | REST | TOTAL
    """
    all_cases = discover_cases(paths)
    requested = getattr(args, 'cases', None) or []
    if requested:
        missing = [c for c in requested if c not in all_cases]
        if missing:
            print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
        cases = [c for c in requested if c in all_cases]
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

    grand = {k: 0 for k in ('casedir', 'bld', 'run', 'hist', 'logs', 'rest')}
    for case in cases:
        sz = case_sizes(case, paths)
        total = sum(sz[k] for k in grand)
        for k in grand:
            grand[k] += sz[k]
        print(f"{case:<{col_w}}  {fmt_size(sz['casedir']):>{cw}}  {fmt_size(sz['bld']):>{cw}}  "
              f"{fmt_size(sz['run']):>{cw}}  {fmt_size(sz['hist']):>{cw}}  "
              f"{fmt_size(sz['logs']):>{cw}}  {fmt_size(sz['rest']):>{cw}}  "
              f"{fmt_size(total):>{cw}}")

    grand_total = sum(grand.values())
    print('-' * len(header))
    print(f"{'TOTAL':<{col_w}}  {fmt_size(grand['casedir']):>{cw}}  {fmt_size(grand['bld']):>{cw}}  "
          f"{fmt_size(grand['run']):>{cw}}  {fmt_size(grand['hist']):>{cw}}  "
          f"{fmt_size(grand['logs']):>{cw}}  {fmt_size(grand['rest']):>{cw}}  "
          f"{fmt_size(grand_total):>{cw}}")


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
# Subcommand: retire-case
# ---------------------------------------------------------------------------

def _check_registry(case, registry_path):
    """Return True if case is found in registry_path, False if not, None if registry unavailable."""
    if not registry_path or not os.path.exists(registry_path):
        return None
    with open(registry_path) as f:
        data = yaml.safe_load(f) or {}
    for entry in data.get('cases', []):
        if (entry.get('meta') or {}).get('case_name') == case:
            return True
    return False


def cmd_retire_case(args, paths):
    """
    Retire one or more cases. Intent must be stated explicitly via one or more flags.

    Intent flags (at least one required):

      --purge-only       Delete caseroot, rundir, and archive entirely.
                         Mutually exclusive with all other intent flags.

      --keep-case        Move the complete case tree (caseroot + rundir + archive)
                         to long-term storage intact. No deletions. Mutually
                         exclusive with --purge-only.

      --keep-years N     Move hist files from the N most recent model years
                         to long-term storage, then delete everything else.

      --keep-restarts    Move the single most recent restart set to long-term
                         storage, then delete everything else.

    --keep-years and --keep-restarts may be combined with each other and with
    --keep-case (preserving selected data while also keeping the full tree).
    --purge-only is mutually exclusive with all three.

    All moves use shutil.move — no intermediate copy, so peak disk usage
    stays flat.

    SAFEGUARDS:
      - --execute required; default is preview only.
      - Explicit case names required; no --all flag.
      - At least one intent flag required.
      - If --registry is supplied, warns if the case is not found in cases.yaml.
      - Each case requires individual yes/no confirmation.

    WARNING: deletions are permanent. Ensure cases.yaml is current before running.
    """
    caseroot  = paths.get('caseroot', '')
    rundir    = paths.get('rundir',   '')
    archive   = paths.get('archive',  '')
    long_term = paths.get('long_term', '')

    if not any([caseroot, rundir, archive]):
        sys.exit("ERROR: no storage paths configured.")

    has_preserve = args.keep_years is not None or args.keep_restarts or args.keep_case
    if args.purge_only and has_preserve:
        sys.exit("ERROR: --purge-only is mutually exclusive with "
                 "--keep-years, --keep-restarts, and --keep-case.")
    if not args.purge_only and not has_preserve:
        sys.exit("ERROR: retire-case requires at least one of --keep-years N, "
                 "--keep-restarts, --keep-case, or --purge-only. "
                 "State your intent explicitly.")

    needs_long_term = args.keep_years is not None or args.keep_restarts or args.keep_case
    if needs_long_term and not long_term:
        sys.exit("ERROR: --keep-years, --keep-restarts, and --keep-case require "
                 "long_term path. Set paths.long_term in config_registry.yaml "
                 "or use --long-term.")

    cases_requested = args.cases
    if not cases_requested:
        sys.exit("ERROR: retire-case requires explicit case name(s).")

    all_on_disk = discover_cases(paths)
    missing = [c for c in cases_requested if c not in all_on_disk]
    if missing:
        print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
    cases = [c for c in cases_requested if c in all_on_disk]
    if not cases:
        print("No cases found on disk.")
        return

    for case in cases:
        print(f"\n{'='*60}")
        print(f"  CASE: {case}")
        print(f"{'='*60}")

        reg_status = _check_registry(case, getattr(args, 'registry', None))
        if reg_status is False:
            print(f"  WARNING: '{case}' not found in registry {args.registry}.")
            print(f"           Run exo_inspect.py first to preserve case metadata.")
        elif reg_status is True:
            print(f"  Registry: found in {args.registry}")

        casedir_path = os.path.join(caseroot, case) if caseroot else ''
        rundir_path  = os.path.join(rundir,   case) if rundir   else ''
        archive_path = os.path.join(archive,  case) if archive  else ''

        sz = case_sizes(case, paths)
        total_on_disk = sum(sz[k] for k in ('casedir', 'bld', 'run', 'hist', 'logs', 'rest'))

        preserve_hist    = []  # (src_file, dst_file)
        preserve_restart = []  # (src_dir, dst_dir)
        keep_case_moves  = []  # (src_dir, dst_dir) for --keep-case whole-tree move

        if args.keep_case:
            for path_key, lt_subdir in [('caseroot', 'cases'), ('rundir', 'rundir'),
                                         ('archive', 'archive')]:
                src = os.path.join(paths.get(path_key, ''), case)
                if os.path.exists(src):
                    dst = os.path.join(long_term, lt_subdir, case)
                    keep_case_moves.append((src, dst, dir_size_bytes(src)))

        if args.keep_years is not None:
            keep_years, per_model = _hist_keep_years_filter(
                archive_path, HIST_MODELS, args.keep_years)
            for model, info in per_model.items():
                for f in info['keep']:
                    y = _hist_year(f)
                    if y and y in set(keep_years):
                        src = os.path.join(info['dir'], f)
                        dst = os.path.join(long_term, case, model, 'hist', f)
                        preserve_hist.append((src, dst))

        if args.keep_restarts:
            sets = restart_sets(case, paths)
            if sets:
                date_str, rest_path = sets[-1]
                preserve_restart.append((rest_path,
                                         os.path.join(long_term, case, 'rest', date_str)))

        # Print plan
        print(f"\n  Total on cesm_scratch: {fmt_size(total_on_disk)}")
        if keep_case_moves:
            kc_total = sum(m[2] for m in keep_case_moves)
            print(f"  MOVE to long-term (--keep-case): entire case tree "
                  f"({fmt_size(kc_total)})")
            for src, dst, size in keep_case_moves:
                print(f"    {src}  ->  {dst}  ({fmt_size(size)})")
        if preserve_hist:
            hist_size = sum(os.path.getsize(s) for s, _ in preserve_hist)
            print(f"  MOVE to long-term: {len(preserve_hist)} hist file(s) "
                  f"from last {args.keep_years} year(s) ({fmt_size(hist_size)})")
        if preserve_restart:
            rest_size = sum(dir_size_bytes(s) for s, _ in preserve_restart)
            print(f"  MOVE to long-term: most recent restart set ({fmt_size(rest_size)})")
        if args.purge_only:
            print(f"  Mode: --purge-only (no preservation)")
        if not args.keep_case:
            print(f"  DELETE from cesm_scratch:")
            for path in [casedir_path, rundir_path, archive_path]:
                if path and os.path.exists(path):
                    print(f"    {path}")

        if not args.execute:
            print(f"\n  [preview] add --execute to perform these actions")
            continue

        answer = input(f"\n  Confirm retire-case for '{case}'? [yes/no]: ").strip().lower()
        if answer != 'yes':
            print(f"  Skipped.")
            continue

        # --keep-case: move entire tree, then stop (no deletions)
        if keep_case_moves:
            print(f"  Moving entire case tree to long-term...")
            for src, dst, _ in keep_case_moves:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            print(f"  Done: {case}")
            continue

        # Selective preservation moves
        if preserve_hist:
            print(f"  Moving {len(preserve_hist)} hist file(s) to long-term...")
            for src, dst in preserve_hist:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        if preserve_restart:
            print(f"  Moving restart set to long-term...")
            for src, dst in preserve_restart:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        print(f"  Deleting from cesm_scratch...")
        for path in [casedir_path, rundir_path, archive_path]:
            if path and os.path.exists(path):
                shutil.rmtree(path)
                print(f"    deleted {path}")

        print(f"  Done: {case}")


# ---------------------------------------------------------------------------
# Argparse helpers (shared across destructive subcommands)
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
        prog='exo_data.py',
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

    sub = parser.add_subparsers(dest='command', metavar='SUBCOMMAND')

    # ---- report (read-only; no --execute; empty cases = all) ----
    p_report = sub.add_parser(
        'report',
        help='Show disk usage per case across cases/, rundir/, and archive/',
        description=cmd_report.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_report.add_argument('cases', nargs='*',
                          help='Case name(s) to report (default: all discovered cases)')

    # ---- purge-bld ----
    p_bld = sub.add_parser(
        'purge-bld',
        help='Delete build artifacts in rundir/<case>/bld/',
        description=cmd_purge_bld.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_bld)
    p_bld.add_argument('--logs-only', action='store_true',
                       help='Remove only .o/.mod binary files, keep log files')

    # ---- purge-restarts ----
    p_rest = sub.add_parser(
        'purge-restarts',
        help='Trim old restart sets in archive/<case>/rest/, keep last N',
        description=cmd_purge_restarts.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_rest)
    p_rest.add_argument('--keep', type=int, default=1, metavar='N',
                        help='Number of most-recent restart sets to keep (default: 1)')

    # ---- purge-hist ----
    p_hist = sub.add_parser(
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

    # ---- purge-logs ----
    p_logs = sub.add_parser(
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

    # ---- move-hist ----
    p_mvhist = sub.add_parser(
        'move-hist',
        help='Move history files to long-term storage (preserves archive structure)',
        description=cmd_move_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_mvhist)
    _add_models_arg(p_mvhist)

    # ---- retire-case ----
    p_arc = sub.add_parser(
        'retire-case',
        help='Retire a case: move to long-term storage and/or delete from cesm_scratch',
        description=cmd_retire_case.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_destructive_args(p_arc)
    p_arc.add_argument('--keep-case', action='store_true', dest='keep_case',
                       help='Move the entire case tree (caseroot + rundir + archive) to '
                            'long-term storage intact; no deletions. Mutually exclusive '
                            'with --purge-only.')
    p_arc.add_argument('--keep-years', type=int, metavar='N', default=None,
                       help='Move hist files from the N most recent model years to long-term storage')
    p_arc.add_argument('--keep-restarts', action='store_true',
                       help='Move the most recent restart set to long-term storage before deleting')
    p_arc.add_argument('--purge-only', action='store_true', dest='purge_only',
                       help='Delete everything with no preservation (mutually exclusive '
                            'with --keep-case / --keep-years / --keep-restarts)')
    p_arc.add_argument('--registry', metavar='FILE', default=None,
                       help='Path to cases.yaml — warns if case not found (does not require it)')

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    'report':          cmd_report,
    'purge-bld':       cmd_purge_bld,
    'purge-restarts':  cmd_purge_restarts,
    'purge-hist':      cmd_purge_hist,
    'purge-logs':      cmd_purge_logs,
    'move-hist':       cmd_move_hist,
    'retire-case':     cmd_retire_case,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        args.command = 'report'
        args.cases = []

    paths = load_paths(args)

    missing_paths = [k for k in ('caseroot', 'rundir', 'archive')
                     if not paths.get(k)]
    if missing_paths:
        print(f"WARNING: paths not configured: {', '.join(missing_paths)}. "
              f"Set them in config_registry.yaml.", file=sys.stderr)

    COMMANDS[args.command](args, paths)


if __name__ == '__main__':
    main()

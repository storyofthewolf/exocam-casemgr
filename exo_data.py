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
  report              Show disk usage per case across all three areas (default)
  purge-bld           Delete build artifacts in rundir/<case>/bld/
  purge-restarts      Trim old restart sets in archive/<case>/rest/; keep last N
  purge-hist          Delete history NetCDF files in archive/<case>/<model>/hist/
  move-hist           Move history files to long-term storage
  move-case           Move an entire case tree (cases + rundir + archive) to long-term storage

Run any subcommand with --help for full options, e.g.:
  python exo_data.py purge-bld --help
"""

import argparse
import os
import shutil
import sys
import yaml

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

ARCHIVE_MODELS = ['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']

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
        'caseroot': getattr(args, 'caseroot', None),
        'rundir':   getattr(args, 'rundir',   None),
        'archive':  getattr(args, 'archive',  None),
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
    """Return total bytes under path, or 0 if path doesn't exist."""
    if not os.path.exists(path):
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def fmt_size(nbytes):
    """Format bytes as human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


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

    casedir_path  = os.path.join(caseroot, case) if caseroot else ''
    bld_path      = os.path.join(rundir, case, 'bld') if rundir else ''
    run_path      = os.path.join(rundir, case, 'run') if rundir else ''
    archive_path  = os.path.join(archive, case) if archive else ''

    # hist and logs across all model components
    hist_bytes = 0
    logs_bytes = 0
    for model in ARCHIVE_MODELS:
        if model == 'rest':
            continue
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
# Confirmation helper
# ---------------------------------------------------------------------------

def confirm(prompt, execute):
    """Return True if the action should proceed."""
    if not execute:
        print(f"  [preview] would: {prompt}")
        return False
    answer = input(f"  Confirm: {prompt} [yes/N]: ").strip().lower()
    return answer == 'yes'


# ---------------------------------------------------------------------------
# Subcommand: report
# ---------------------------------------------------------------------------

def cmd_report(args, paths):
    """
    Show disk usage per case across cases/, rundir/, and archive/.

    Columns: CASE | CASEDIR | BLD | RUN | HIST | LOGS | REST | TOTAL
    """
    cases = _filter_cases(discover_cases(paths), args)
    if not cases:
        print("No cases found.")
        return

    col_w = max(len(c) for c in cases) + 2
    cw = 11  # data column width
    header = (f"{'CASE':<{col_w}}  {'CASEDIR':>{cw}}  {'BLD':>{cw}}  {'RUN':>{cw}}  "
              f"{'HIST':>{cw}}  {'LOGS':>{cw}}  {'REST':>{cw}}  {'TOTAL':>{cw}}")
    print(header)
    print('-' * len(header))

    grand = {k: 0 for k in ('casedir', 'bld', 'run', 'hist', 'logs', 'rest')}
    for case in cases:
        sz = case_sizes(case, paths)
        total = sum(sz[k] for k in ('casedir', 'bld', 'run', 'hist', 'logs', 'rest'))
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

    cases = _filter_cases(discover_cases(paths), args)
    if not cases:
        print("No cases found.")
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

    cases = _filter_cases(discover_cases(paths), args)
    if not cases:
        print("No cases found.")
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

    WARNING: history files are not recoverable once deleted. Without --execute
    this command only previews what would be removed.
    """
    archive = paths.get('archive', '')
    if not archive:
        sys.exit("ERROR: archive path not configured.")

    models = args.models if args.models else [m for m in ARCHIVE_MODELS if m != 'rest']
    cases  = _filter_cases(discover_cases(paths), args)
    if not cases:
        print("No cases found.")
        return

    for case in cases:
        case_total = 0
        targets = []
        for model in models:
            hist = os.path.join(archive, case, model, 'hist')
            if not os.path.isdir(hist):
                continue
            size = dir_size_bytes(hist)
            if size > 0:
                targets.append((hist, size))
                case_total += size

        if not targets:
            print(f"  {case}: no hist/ directories found, skipping")
            continue

        print(f"  {case}: {len(targets)} hist/ director(ies), {fmt_size(case_total)} total")
        for hist, size in targets:
            print(f"    {hist}  ({fmt_size(size)})")

        action = f"DELETE {fmt_size(case_total)} of history files for {case}"
        if confirm(action, args.execute):
            for hist, _ in targets:
                shutil.rmtree(hist)
                os.makedirs(hist)  # recreate empty dir so archive structure stays intact
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

    models = args.models if args.models else [m for m in ARCHIVE_MODELS if m != 'rest']
    cases  = _filter_cases(discover_cases(paths), args)
    if not cases:
        print("No cases found.")
        return

    for case in cases:
        for model in models:
            src = os.path.join(archive, case, model, 'hist')
            if not os.path.isdir(src):
                continue
            files = [f for f in os.listdir(src)
                     if os.path.isfile(os.path.join(src, f))]
            if not files:
                continue
            total = sum(os.path.getsize(os.path.join(src, f)) for f in files)
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
# Subcommand: move-case
# ---------------------------------------------------------------------------

def cmd_move_case(args, paths):
    """
    Move a complete case tree to long-term storage.

    Moves all three areas for each specified case:
      cases/<case>         -> <long_term>/cases/<case>
      rundir/<case>/       -> <long_term>/rundir/<case>/
      archive/<case>/      -> <long_term>/archive/<case>/

    Use --no-rundir or --no-archive to skip those areas.
    Intended for cases that are fully complete and no longer active.
    """
    long_term = paths.get('long_term', '')
    if not long_term:
        sys.exit("ERROR: long_term path not configured. Set paths.long_term in "
                 "config_registry.yaml or use --long-term.")

    cases = _filter_cases(discover_cases(paths), args)
    if not cases:
        print("No cases found.")
        return

    areas = []
    if not args.no_casedir:
        areas.append(('caseroot', 'cases'))
    if not args.no_rundir:
        areas.append(('rundir', 'rundir'))
    if not args.no_archive:
        areas.append(('archive', 'archive'))

    for case in cases:
        moves = []
        for path_key, lt_subdir in areas:
            src = os.path.join(paths.get(path_key, ''), case)
            if os.path.exists(src):
                dst = os.path.join(long_term, lt_subdir, case)
                size = dir_size_bytes(src)
                moves.append((src, dst, size))

        if not moves:
            print(f"  {case}: nothing found to move, skipping")
            continue

        total = sum(m[2] for m in moves)
        print(f"  {case}: {fmt_size(total)} across {len(moves)} area(s)")
        for src, dst, size in moves:
            print(f"    {src}  ->  {dst}  ({fmt_size(size)})")

        action = f"move {fmt_size(total)} for case '{case}' to long-term storage"
        if confirm(action, args.execute):
            for src, dst, _ in moves:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            print(f"    moved {case}")


# ---------------------------------------------------------------------------
# Case filtering helper
# ---------------------------------------------------------------------------

def _filter_cases(all_cases, args):
    """Return the case list filtered by args.cases (if specified)."""
    requested = getattr(args, 'cases', None)
    if not requested:
        return all_cases
    missing = [c for c in requested if c not in all_cases]
    if missing:
        print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
    return [c for c in requested if c in all_cases]


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='exo_data.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global options
    parser.add_argument('--config-registry', default=DEFAULT_CONFIG, dest='config_registry',
                        help='Path to config_registry.yaml (default: config_registry.yaml '
                             'next to this script)')
    parser.add_argument('--caseroot',  help='Override paths.caseroot from config_registry')
    parser.add_argument('--rundir',    help='Override paths.rundir from config_registry')
    parser.add_argument('--archive',   help='Override paths.archive from config_registry')
    parser.add_argument('--long-term', dest='long_term',
                        help='Override paths.long_term from config_registry')

    sub = parser.add_subparsers(dest='command', metavar='SUBCOMMAND')

    # ---- report ----
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
    p_bld.add_argument('cases', nargs='*',
                       help='Case name(s) to purge bld/ for (default: all)')
    p_bld.add_argument('--logs-only', action='store_true',
                       help='Remove only .o/.mod binary files, keep log files')
    p_bld.add_argument('--execute', action='store_true',
                       help='Actually perform deletions (default is preview only)')

    # ---- purge-restarts ----
    p_rest = sub.add_parser(
        'purge-restarts',
        help='Trim old restart sets in archive/<case>/rest/, keep last N',
        description=cmd_purge_restarts.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_rest.add_argument('cases', nargs='*',
                        help='Case name(s) to trim restart sets for (default: all)')
    p_rest.add_argument('--keep', type=int, default=1, metavar='N',
                        help='Number of most-recent restart sets to keep (default: 1)')
    p_rest.add_argument('--execute', action='store_true',
                        help='Actually perform deletions (default is preview only)')

    # ---- purge-hist ----
    p_hist = sub.add_parser(
        'purge-hist',
        help='Delete history NetCDF files from archive/<case>/<model>/hist/',
        description=cmd_purge_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_hist.add_argument('cases', nargs='*',
                        help='Case name(s) to purge hist/ for (default: all)')
    p_hist.add_argument('--models', nargs='+', metavar='MODEL',
                        choices=ARCHIVE_MODELS,
                        help=f'Restrict to these model components '
                             f'(choices: {", ".join(ARCHIVE_MODELS)})')
    p_hist.add_argument('--execute', action='store_true',
                        help='Actually perform deletions (default is preview only)')

    # ---- move-hist ----
    p_mvhist = sub.add_parser(
        'move-hist',
        help='Move history files to long-term storage (preserves archive structure)',
        description=cmd_move_hist.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_mvhist.add_argument('cases', nargs='*',
                          help='Case name(s) to move hist/ for (default: all)')
    p_mvhist.add_argument('--models', nargs='+', metavar='MODEL',
                          choices=ARCHIVE_MODELS,
                          help=f'Restrict to these model components '
                               f'(choices: {", ".join(ARCHIVE_MODELS)})')
    p_mvhist.add_argument('--execute', action='store_true',
                          help='Actually perform moves (default is preview only)')

    # ---- move-case ----
    p_mvcase = sub.add_parser(
        'move-case',
        help='Move a complete case tree (cases + rundir + archive) to long-term storage',
        description=cmd_move_case.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_mvcase.add_argument('cases', nargs='*',
                          help='Case name(s) to move (default: all — use with care)')
    p_mvcase.add_argument('--no-casedir', action='store_true',
                          help='Skip moving cases/<case>')
    p_mvcase.add_argument('--no-rundir', action='store_true',
                          help='Skip moving rundir/<case>')
    p_mvcase.add_argument('--no-archive', action='store_true',
                          help='Skip moving archive/<case>')
    p_mvcase.add_argument('--execute', action='store_true',
                          help='Actually perform moves (default is preview only)')

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    'report':          cmd_report,
    'purge-bld':       cmd_purge_bld,
    'purge-restarts':  cmd_purge_restarts,
    'purge-hist':      cmd_purge_hist,
    'move-hist':       cmd_move_hist,
    'move-case':       cmd_move_case,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Default to report when called with no subcommand
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

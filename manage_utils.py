#!/usr/bin/env python3
"""
manage_utils.py — Shared utility layer for datamgr.py and runmgr.py

Constants, path-loading, disk helpers, and case-selection primitives used by
both tools. No subcommand logic lives here.
"""

import os
import re
import subprocess
import sys
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARCHIVE_MODELS = ['atm', 'cpl', 'dart', 'glc', 'ice', 'lnd', 'ocn', 'rest', 'rof', 'wav']
HIST_MODELS = [m for m in ARCHIVE_MODELS if m != 'rest']

MODEL_STEM = {
    'atm': 'cam', 'lnd': 'clm2', 'ice': 'cice', 'ocn': 'pop',
    'rof': 'mosart', 'glc': 'cism', 'wav': 'ww3', 'cpl': 'cpl',
}
AVG_HIST_DEFAULT_MODELS = ['atm', 'lnd', 'ice']

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'config_registry.yaml')

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------


def load_paths(args):
    """Load paths from config_registry.yaml, then apply any CLI overrides."""
    paths = {}
    # `or DEFAULT_CONFIG`, not just getattr's default: an argparse flag declared
    # without a default leaves the attribute *present* and None, which getattr
    # happily returns. Falling back on a None keeps a subcommand that forgot
    # `default=DEFAULT_CONFIG` from silently loading no paths at all.
    cfg_path = getattr(args, 'config_registry', None) or DEFAULT_CONFIG
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


# ---------------------------------------------------------------------------
# Hist year filtering (shared by purge-hist and retire)
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

def preview_hint(execute):
    """Print a single --execute reminder at the end of a preview run.

    Call once after a destructive verb's per-case loop. No-op when executing
    (the user already confirmed) so the hint only appears after the last
    [preview] block.
    """
    if not execute:
        print("\n  (preview only — rerun with --execute to perform these actions)")


def batch_confirm(action, n):
    """Single batch [yes/no] gate covering a whole case set.

    The caller prints all per-case previews first, then calls this once before
    acting on the entire batch — one confirmation instead of one per case.
    `action` is a verb phrase; rendered as "<action> N case(s)? [yes/no]:".
    Returns True to proceed. EOF/interrupt is treated as 'no'.
    """
    try:
        answer = input(f"\n  {action} {n} case(s)? [yes/no]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ('yes', 'y')


# ---------------------------------------------------------------------------
# Case selection helper (destructive subcommands only)
# ---------------------------------------------------------------------------

def _require_cases(all_cases, args):
    """Return cases to act on, from explicit args.cases or a --prefix filter.

    Selection is either explicit case names or a case-insensitive --prefix bulk
    filter — the two are mutually exclusive. Exits with an error if neither is
    given (there is no --all flag for destructive operations).
    """
    requested = getattr(args, 'cases', None) or []
    prefix_filter = getattr(args, 'prefix', None)

    if requested and prefix_filter:
        sys.exit("ERROR: --prefix cannot be combined with explicit case names.")
    if not requested and not prefix_filter:
        sys.exit("ERROR: specify case name(s) or --prefix. No --all flag is "
                 "provided for destructive operations — select cases explicitly.")

    if prefix_filter:
        cases = [c for c in all_cases if c.lower().startswith(prefix_filter.lower())]
        if not cases:
            print(f"No cases matching prefix '{prefix_filter}'.")
        return cases

    missing = [c for c in requested if c not in all_cases]
    if missing:
        print(f"WARNING: case(s) not found on disk: {', '.join(missing)}", file=sys.stderr)
    return [c for c in requested if c in all_cases]


# ---------------------------------------------------------------------------
# Job submission (shared by runmgr.py submit and build.py make --send-it)
# ---------------------------------------------------------------------------

def submit_case(case_dir, case_name):
    """sbatch <case_name>.run from case_dir.

    The single submission code path. Callers handle their own status gating,
    XML edits, and output formatting; this only runs sbatch and reports the
    outcome.

    Returns (ok, detail):
      (True,  job_id)      on success — job_id parsed from sbatch stdout
      (False, message)     on failure — sbatch not found, nonzero exit, etc.
    """
    try:
        result = subprocess.run(
            ['sbatch', f'{case_name}.run'],
            cwd=case_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except FileNotFoundError:
        return False, 'sbatch not found in PATH'
    if result.returncode != 0:
        return False, f'sbatch failed: {result.stderr.strip()}'
    m = re.search(r'Submitted batch job (\d+)', result.stdout)
    job_id = m.group(1) if m else result.stdout.strip()
    return True, job_id

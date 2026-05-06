#!/usr/bin/env python3
"""
diff.py — SourceMods diff tool for ExoCAM cases

Compares a case's SourceMods/ directories against the ExoCAM reference source
to answer one question before retiring: does this case contain custom Fortran
worth preserving?

Usage:
  python diff.py my_case
  python diff.py my_case --full physpkg.F90
  python diff.py my_case --config-registry /path/to/config_registry.yaml
"""

import argparse
import os
import subprocess
import sys
import yaml

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'config_registry.yaml')

COMPONENTS = ['src.cam', 'src.share', 'src.drv', 'src.clm', 'src.cice']

SKIP_FILES = {'exoplanet_mod.F90'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_size(nbytes):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def load_paths(config_registry):
    if not config_registry or not os.path.exists(config_registry):
        sys.exit(f"ERROR: config_registry not found: {config_registry}")
    with open(config_registry) as f:
        data = yaml.safe_load(f) or {}
    return data.get('paths', {})


def discover_sourcemods_files(case_sourcemods_root):
    """Return {component: [(rel_path, abs_path), ...]} for all files under
    case SourceMods/, grouped by component directory.

    rel_path is the path relative to the component directory (e.g. 'sub/file.F90').
    Skips editor backup files (names ending in ~).
    """
    result = {comp: [] for comp in COMPONENTS}
    for comp in COMPONENTS:
        comp_dir = os.path.join(case_sourcemods_root, comp)
        if not os.path.isdir(comp_dir):
            continue
        for dirpath, _, filenames in os.walk(comp_dir):
            for fname in sorted(filenames):
                if fname.endswith('~'):
                    continue
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, comp_dir)
                result[comp].append((rel_path, abs_path))
        result[comp].sort(key=lambda x: x[0])
    return result


def find_exocam_counterpart(filename, component, exocam_sourcemods_root):
    """Search the top level of exocam_sourcemods_root/<component>/ for filename.

    Returns the absolute path if found, None otherwise.
    """
    comp_dir = os.path.join(exocam_sourcemods_root, component)
    if not os.path.isdir(comp_dir):
        return None
    candidate = os.path.join(comp_dir, filename)
    if os.path.isfile(candidate):
        return candidate
    return None


def diff_summary(case_lines, exo_lines):
    """Return (added, removed) line counts relative to ExoCAM source.

    Added: lines in case but not in ExoCAM.
    Removed: lines in ExoCAM but not in case.
    Uses multiset comparison so duplicate lines are counted correctly.
    """
    from collections import Counter
    case_counts = Counter(case_lines)
    exo_counts  = Counter(exo_lines)
    added   = sum(max(0, case_counts[l] - exo_counts[l]) for l in case_counts)
    removed = sum(max(0, exo_counts[l]  - case_counts[l]) for l in exo_counts)
    return added, removed


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_summary(args, paths):
    caseroot         = paths.get('caseroot', '')
    exocam_root      = paths.get('exocam_root', '')

    if not caseroot:
        sys.exit("ERROR: paths.caseroot not set in config_registry.yaml")
    if not exocam_root:
        sys.exit("ERROR: paths.exocam_root not set in config_registry.yaml")

    case             = args.case
    case_dir         = os.path.join(caseroot, case)
    case_sm_root     = os.path.join(case_dir, 'SourceMods')
    exocam_sm_root   = os.path.join(exocam_root, 'cesm1.2.1', 'configs',
                                    'cam_aqua_fv', 'SourceMods')

    if not os.path.isdir(case_dir):
        sys.exit(f"ERROR: case not found: {case_dir}")
    if not os.path.isdir(case_sm_root):
        sys.exit(f"ERROR: SourceMods not found in case: {case_sm_root}")
    if not os.path.isdir(exocam_sm_root):
        sys.exit(f"ERROR: ExoCAM reference SourceMods not found: {exocam_sm_root}")

    files_by_comp = discover_sourcemods_files(case_sm_root)

    print(f"Comparing: {case}  vs  ExoCAM source")
    print(f"Excluding: exoplanet_mod.F90  (captured by scan.py)")
    print()

    n_modified = 0
    n_case_only = 0
    n_identical = 0

    for comp in COMPONENTS:
        entries = files_by_comp[comp]
        print(comp)
        if not entries:
            print("  (no files)")
            print()
            continue

        for rel_path, abs_path in entries:
            fname = os.path.basename(rel_path)

            if fname in SKIP_FILES:
                print(f"  {'':12}  {fname:<40}  [skipped]")
                continue

            exo_path = find_exocam_counterpart(fname, comp, exocam_sm_root)

            if exo_path is None:
                size = os.path.getsize(abs_path)
                print(f"  {'CASE ONLY':<12}  {fname:<40}  ({fmt_size(size)})")
                n_case_only += 1
            else:
                with open(abs_path,  'rb') as f:
                    case_bytes = f.read()
                with open(exo_path, 'rb') as f:
                    exo_bytes = f.read()

                if case_bytes == exo_bytes:
                    print(f"  {'IDENTICAL':<12}  {fname}")
                    n_identical += 1
                else:
                    case_lines = case_bytes.decode('utf-8', errors='replace').splitlines()
                    exo_lines  = exo_bytes.decode('utf-8',  errors='replace').splitlines()
                    added, removed = diff_summary(case_lines, exo_lines)
                    print(f"  {'MODIFIED':<12}  {fname:<40}  (+{added} / -{removed} lines)")
                    n_modified += 1
        print()

    # Summary line
    if n_modified == 0 and n_case_only == 0:
        print("Summary: all identical  →  safe to retire without preserving SourceMods")
    else:
        parts = []
        if n_modified:
            parts.append(f"{n_modified} modified")
        if n_case_only:
            parts.append(f"{n_case_only} case-only")
        if n_identical:
            parts.append(f"{n_identical} identical")
        print(f"Summary: {', '.join(parts)}  →  review before retiring")


def cmd_full(args, paths):
    caseroot       = paths.get('caseroot', '')
    exocam_root    = paths.get('exocam_root', '')

    if not caseroot:
        sys.exit("ERROR: paths.caseroot not set in config_registry.yaml")
    if not exocam_root:
        sys.exit("ERROR: paths.exocam_root not set in config_registry.yaml")

    case           = args.case
    target_name    = args.full
    case_dir       = os.path.join(caseroot, case)
    case_sm_root   = os.path.join(case_dir, 'SourceMods')
    exocam_sm_root = os.path.join(exocam_root, 'cesm1.2.1', 'configs',
                                  'cam_aqua_fv', 'SourceMods')

    if not os.path.isdir(case_dir):
        sys.exit(f"ERROR: case not found: {case_dir}")
    if not os.path.isdir(case_sm_root):
        sys.exit(f"ERROR: SourceMods not found in case: {case_sm_root}")

    files_by_comp = discover_sourcemods_files(case_sm_root)

    # Find the target file in the case
    case_path = None
    found_comp = None
    for comp in COMPONENTS:
        for rel_path, abs_path in files_by_comp[comp]:
            if os.path.basename(rel_path) == target_name:
                case_path  = abs_path
                found_comp = comp
                break
        if case_path:
            break

    if case_path is None:
        sys.exit(f"ERROR: '{target_name}' not found in SourceMods of case '{case}'")

    exo_path = find_exocam_counterpart(target_name, found_comp, exocam_sm_root)

    if exo_path is None:
        print(f"CASE ONLY — {target_name} has no ExoCAM counterpart")
        print(f"  {case_path}")
        print()
        with open(case_path) as f:
            sys.stdout.write(f.read())
    else:
        result = subprocess.run(
            ['diff', exo_path, case_path],
            capture_output=False,
        )
        if result.returncode == 0:
            print(f"(files are identical)")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='diff.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('case',
                        help='Case name (resolved relative to caseroot in config_registry.yaml)')
    parser.add_argument('--full', metavar='FILENAME',
                        help='Print full diff (or file contents if CASE ONLY) for this filename')
    parser.add_argument('--config-registry', default=DEFAULT_CONFIG, dest='config_registry',
                        help='Path to config_registry.yaml '
                             '(default: config_registry.yaml next to this script)')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    paths = load_paths(args.config_registry)

    if args.full:
        cmd_full(args, paths)
    else:
        cmd_summary(args, paths)


if __name__ == '__main__':
    main()

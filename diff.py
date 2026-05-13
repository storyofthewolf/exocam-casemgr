#!/usr/bin/env python3
"""
diff.py — SourceMods diff tool for ExoCAM cases

Compare a case's SourceMods/ against the ExoCAM reference source, or compare
two cases' SourceMods/ directly.

Usage:
  python diff.py my_case                              # case vs ExoCAM source
  python diff.py my_case --full physpkg.F90           # full diff for one file
  python diff.py case1 --case2 case2                  # case vs case summary
  python diff.py case1 --case2 case2 --full physpkg.F90
  python diff.py my_case --config-registry /path/to/config_registry.yaml
  python diff.py my_case --registry active.yaml
  python diff.py my_case --verbose
"""

import argparse
import os
import subprocess
import sys
import yaml
from collections import Counter

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'config_registry.yaml')
DEFAULT_CASES_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'active.yaml')

COMPONENTS = ['src.cam', 'src.share', 'src.drv', 'src.clm', 'src.cice']
SKIP_FILES = {'exoplanet_mod.F90'}

NAMELIST_FILES = [
    'user_nl_cam',
    'user_nl_clm',
    'user_nl_cice',
    'user_docn.streams.txt.som',
    'user_nl_cpl',
    'user_nl_docn',
    'user_nl_rtm',
]


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


def load_case_meta(case, cases_yaml_path):
    """Return {'config_type': ..., 'exort_pkg': ...} for case from active.yaml."""
    if not os.path.exists(cases_yaml_path):
        sys.exit(f"ERROR: {cases_yaml_path} not found.\n"
                 f"Run 'python scan.py' to generate it before using diff.py.")
    with open(cases_yaml_path) as f:
        data = yaml.safe_load(f) or {}
    for entry in data.get('cases', []):
        meta = entry.get('meta', {})
        if meta.get('case_name') == case:
            exort_pkg = (meta.get('exort_pkg') or '').rstrip('*') or None
            return {'config_type': meta.get('config_type'),
                    'exort_pkg':   exort_pkg}
    sys.exit(f"ERROR: case '{case}' not found in {cases_yaml_path}.\n"
             f"Run 'python scan.py {case}' first to register it before diffing.")


def build_exort_fileset(exort_root, exort_pkg):
    """Return {filename: filepath} for all files in exort_root/3dmodels/src.cam.{exort_pkg}/."""
    pkg_dir = os.path.join(exort_root, '3dmodels', f'src.cam.{exort_pkg}')
    if not os.path.isdir(pkg_dir):
        return {}
    fileset = {}
    for root, _, files in os.walk(pkg_dir):
        for f in files:
            fileset[f] = os.path.join(root, f)
    return fileset


def _load_exort_fileset(paths, exort_pkg):
    """Resolve ExoRT fileset, printing a warning and returning {} on any failure."""
    exort_root = paths.get('exort_root', '')
    if not exort_root:
        print("WARNING: paths.exort_root not set in config_registry.yaml; RT file detection disabled.")
        return {}
    if not exort_pkg:
        print("WARNING: exort_pkg not found in active.yaml for this case; RT file detection disabled.")
        return {}
    fileset = build_exort_fileset(exort_root, exort_pkg)
    if not fileset:
        pkg_dir = os.path.join(exort_root, '3dmodels', f'src.cam.{exort_pkg}')
        print(f"WARNING: ExoRT package directory not found: {pkg_dir}; RT file detection disabled.")
    return fileset


def walk_sourcemods(sourcemods_root):
    """Return {component: {filename: abs_path}}. Skips ~ files; shallowest wins."""
    result = {comp: {} for comp in COMPONENTS}
    for comp in COMPONENTS:
        comp_dir = os.path.join(sourcemods_root, comp)
        if not os.path.isdir(comp_dir):
            continue
        for dirpath, _, filenames in os.walk(comp_dir):
            for fname in sorted(filenames):
                if not fname.endswith('~') and fname not in result[comp]:
                    result[comp][fname] = os.path.join(dirpath, fname)
    return result


def find_exocam_counterpart(filename, component, exocam_sm_root):
    candidate = os.path.join(exocam_sm_root, component, filename)
    return candidate if os.path.isfile(candidate) else None

def normalize_lines(text):
    """Strip trailing whitespace from each line. Used to ignore cosmetic whitespace-only diffs."""
    return [line.rstrip() for line in text.splitlines()]

def read_normalized(path):
    return normalize_lines(open(path, 'rb').read().decode('utf-8', errors='replace'))

def diff_counts(path_a, path_b):
    """Return (added, removed) line counts after trailing-whitespace normalization; b is reference."""
    a = read_normalized(path_a)
    b = read_normalized(path_b)
    ca, cb = Counter(a), Counter(b)
    return (sum(max(0, ca[l] - cb[l]) for l in ca),
            sum(max(0, cb[l] - ca[l]) for l in cb))

def _sm_root(caseroot, case):
    case_dir = os.path.join(caseroot, case)
    if not os.path.isdir(case_dir):
        sys.exit(f"ERROR: case not found: {case_dir}")
    sm = os.path.join(case_dir, 'SourceMods')
    if not os.path.isdir(sm):
        sys.exit(f"ERROR: SourceMods not found in case: {sm}")
    return sm


def _case_dir(caseroot, case):
    d = os.path.join(caseroot, case)
    if not os.path.isdir(d):
        sys.exit(f"ERROR: case not found: {d}")
    return d


def walk_namelists(casedir):
    """Return {filename: abs_path} for namelist files that exist in casedir."""
    result = {}
    for fname in NAMELIST_FILES:
        p = os.path.join(casedir, fname)
        if os.path.isfile(p):
            result[fname] = p
    return result


def _print_namelist_section(nl1, nl2, label1, label2, verbose):
    """Print namelist diff section; nl1/nl2 are {filename: path} dicts. Returns counts."""
    names = sorted(set(nl1) | set(nl2))
    if not names:
        print("  (no namelist files found)\n")
        return 0, 0, 0, 0
    n_mod = n_c1 = n_c2 = n_eq = 0
    for fname in names:
        in1, in2 = fname in nl1, fname in nl2
        if in1 and in2:
            if read_normalized(nl1[fname]) == read_normalized(nl2[fname]):
                if verbose:
                    print(f"  {'IDENTICAL':<14}  {fname}")
                n_eq += 1
            else:
                ad, rm = diff_counts(nl1[fname], nl2[fname])
                print(f"  {'MODIFIED':<14}  {fname:<40}  (+{ad} / -{rm} lines)")
                n_mod += 1
        elif in1:
            print(f"  {label1+' ONLY':<14}  {fname:<40}  ({fmt_size(os.path.getsize(nl1[fname]))})")
            n_c1 += 1
        else:
            print(f"  {label2+' ONLY':<14}  {fname:<40}  ({fmt_size(os.path.getsize(nl2[fname]))})")
            n_c2 += 1
    print()
    return n_mod, n_c1, n_c2, n_eq


def cmd_summary(args, paths):
    caseroot = paths.get('caseroot', '') or sys.exit(
        "ERROR: paths.caseroot not set in config_registry.yaml")
    case1_sm = _sm_root(caseroot, args.case)

    if args.case2:
        case2_sm  = _sm_root(caseroot, args.case2)
        case1_dir = _case_dir(caseroot, args.case)
        case2_dir = _case_dir(caseroot, args.case2)
        files1    = walk_sourcemods(case1_sm)
        files2    = walk_sourcemods(case2_sm)
        nl1       = walk_namelists(case1_dir)
        nl2       = walk_namelists(case2_dir)
        print(f"Comparing: {args.case}  vs  {args.case2}\n")
        n_mod = n_c1 = n_c2 = n_eq = 0
        for comp in COMPONENTS:
            f1, f2 = files1[comp], files2[comp]
            names = sorted(set(f1) | set(f2))
            print(comp)
            if not names:
                print("  (no files in either case)\n")
                continue
            for fname in names:
                if fname in SKIP_FILES:
                    print(f"  {'':14}  {fname:<40}  [skipped]")
                    continue
                in1, in2 = fname in f1, fname in f2
                if in1 and in2:
                    if read_normalized(f1[fname]) == read_normalized(f2[fname]):
                        if args.verbose:
                            print(f"  {'IDENTICAL':<14}  {fname}")
                        n_eq += 1
                    else:
                        ad, rm = diff_counts(f1[fname], f2[fname])
                        print(f"  {'MODIFIED':<14}  {fname:<40}  (+{ad} / -{rm} lines)"); n_mod += 1
                elif in1:
                    print(f"  {'CASE1 ONLY':<14}  {fname:<40}  ({fmt_size(os.path.getsize(f1[fname]))})"); n_c1 += 1
                else:
                    print(f"  {'CASE2 ONLY':<14}  {fname:<40}  ({fmt_size(os.path.getsize(f2[fname]))})"); n_c2 += 1
            print()
        print("namelists")
        nl_mod, nl_c1, nl_c2, nl_eq = _print_namelist_section(nl1, nl2, 'CASE1', 'CASE2', args.verbose)
        n_mod += nl_mod; n_c1 += nl_c1; n_c2 += nl_c2; n_eq += nl_eq
        if n_mod == 0 and n_c1 == 0 and n_c2 == 0:
            print("Summary: all identical  →  SourceMods and namelists are equivalent")
        else:
            parts = ([f"{n_mod} modified"]   if n_mod else []) + \
                    ([f"{n_c1} case1-only"]  if n_c1  else []) + \
                    ([f"{n_c2} case2-only"]  if n_c2  else []) + \
                    ([f"{n_eq} identical"]   if n_eq  else [])
            print(f"Summary: {', '.join(parts)}")
        cases_yaml = getattr(args, 'registry', None) or paths.get('cases_yaml') or DEFAULT_CASES_YAML
        print(f"\n(registry: {cases_yaml})")
    else:
        exocam_root = paths.get('exocam_root', '') or sys.exit(
            "ERROR: paths.exocam_root not set in config_registry.yaml")
        cases_yaml  = getattr(args, 'registry', None) or paths.get('cases_yaml') or DEFAULT_CASES_YAML
        meta        = load_case_meta(args.case, cases_yaml)
        config_type = meta['config_type']
        exort_files = _load_exort_fileset(paths, meta['exort_pkg'])
        exocam_sm   = os.path.join(exocam_root, 'cesm1.2.1', 'configs',
                                   config_type, 'SourceMods')
        if not os.path.isdir(exocam_sm):
            sys.exit(f"ERROR: ExoCAM reference SourceMods not found: {exocam_sm}")
        case1_dir = _case_dir(caseroot, args.case)
        nl1       = walk_namelists(case1_dir)
        files = walk_sourcemods(case1_sm)
        print(f"Comparing: {args.case}  vs  ExoCAM source")
        print(f"Excluding: exoplanet_mod.F90  (captured by scan.py)\n")
        n_mod = n_co = n_eq = n_rt_mod = n_rt_eq = 0
        for comp in COMPONENTS:
            comp_files = files[comp]
            print(comp)
            if not comp_files:
                print("  (no files)\n")
                continue
            for fname, abs_path in sorted(comp_files.items()):
                if fname in SKIP_FILES:
                    print(f"  {'':14}  {fname:<40}  [skipped]")
                    continue
                exo_path = find_exocam_counterpart(fname, comp, exocam_sm)
                if exo_path is not None:
                    if read_normalized(abs_path) == read_normalized(exo_path):
                        if args.verbose:
                            print(f"  {'IDENTICAL':<14}  {fname}")
                        n_eq += 1
                    else:
                        ad, rm = diff_counts(abs_path, exo_path)
                        print(f"  {'MODIFIED':<14}  {fname:<40}  (+{ad} / -{rm} lines)"); n_mod += 1
                elif fname in exort_files:
                    rt_path = exort_files[fname]
                    if read_normalized(abs_path) == read_normalized(rt_path):
                        if args.verbose:
                            print(f"  {'RT IDENTICAL':<14}  {fname}")
                        n_rt_eq += 1
                    else:
                        ad, rm = diff_counts(abs_path, rt_path)
                        print(f"  {'RT MODIFIED':<14}  {fname:<40}  (+{ad} / -{rm} lines)"); n_rt_mod += 1
                else:
                    print(f"  {'CASE ONLY':<14}  {fname:<40}  ({fmt_size(os.path.getsize(abs_path))})"); n_co += 1
            print()
        nl_ref_dir = os.path.join(exocam_root, 'cesm1.2.1', 'configs',
                                   config_type, 'namelist_files')
        print("namelists")
        if nl1:
            for fname, abs_path in sorted(nl1.items()):
                ref_path = os.path.join(nl_ref_dir, fname)
                if os.path.isfile(ref_path):
                    if read_normalized(abs_path) == read_normalized(ref_path):
                        if args.verbose:
                            print(f"  {'IDENTICAL':<14}  {fname}")
                    else:
                        ad, rm = diff_counts(abs_path, ref_path)
                        print(f"  {'MODIFIED':<14}  {fname:<40}  (+{ad} / -{rm} lines)")
                else:
                    print(f"  {'CASE ONLY':<14}  {fname:<40}  ({fmt_size(os.path.getsize(abs_path))})")
        else:
            print("  (no namelist files found)")
        print()
        if n_mod == 0 and n_rt_mod == 0 and n_co == 0:
            print("Summary: all identical  →  safe to retire without preserving SourceMods")
        else:
            parts = ([f"{n_mod} modified"]        if n_mod    else []) + \
                    ([f"{n_rt_mod} RT-modified"]   if n_rt_mod else []) + \
                    ([f"{n_rt_eq} RT-identical"]   if n_rt_eq  else []) + \
                    ([f"{n_co} case-only"]         if n_co     else []) + \
                    ([f"{n_eq} identical"]         if n_eq     else [])
            print(f"Summary: {', '.join(parts)}  →  review before retiring")
        print(f"\n(registry: {cases_yaml})")


def cmd_full(args, paths):
    caseroot = paths.get('caseroot', '') or sys.exit(
        "ERROR: paths.caseroot not set in config_registry.yaml")
    case1_sm  = _sm_root(caseroot, args.case)
    case1_dir = _case_dir(caseroot, args.case)
    files1    = walk_sourcemods(case1_sm)
    nl1       = walk_namelists(case1_dir)
    target    = args.full
    path1     = next((files1[c][target] for c in COMPONENTS if target in files1[c]), None)
    if path1 is None:
        path1 = nl1.get(target)

    if args.case2:
        case2_sm  = _sm_root(caseroot, args.case2)
        case2_dir = _case_dir(caseroot, args.case2)
        files2    = walk_sourcemods(case2_sm)
        nl2       = walk_namelists(case2_dir)
        path2     = next((files2[c][target] for c in COMPONENTS if target in files2[c]), None)
        if path2 is None:
            path2 = nl2.get(target)
        if path1 is None and path2 is None:
            sys.exit(f"ERROR: '{target}' not found in SourceMods or namelists of "
                     f"'{args.case}' or '{args.case2}'")
        if path1 and path2:
            r = subprocess.run(['diff', '-b', path1, path2])
            if r.returncode == 0:
                print("(files are identical)")
        elif path1:
            print(f"CASE1 ONLY — {target} exists only in {args.case}\n  {path1}\n")
            sys.stdout.write(open(path1).read())
        else:
            print(f"CASE2 ONLY — {target} exists only in {args.case2}\n  {path2}\n")
            sys.stdout.write(open(path2).read())
        cases_yaml = getattr(args, 'registry', None) or paths.get('cases_yaml') or DEFAULT_CASES_YAML
        print(f"\n(registry: {cases_yaml})")
    else:
        exocam_root = paths.get('exocam_root', '') or sys.exit(
            "ERROR: paths.exocam_root not set in config_registry.yaml")
        cases_yaml  = getattr(args, 'registry', None) or paths.get('cases_yaml') or DEFAULT_CASES_YAML
        meta        = load_case_meta(args.case, cases_yaml)
        config_type = meta['config_type']
        exort_files = _load_exort_fileset(paths, meta['exort_pkg'])
        exocam_sm   = os.path.join(exocam_root, 'cesm1.2.1', 'configs',
                                   config_type, 'SourceMods')
        if path1 is None:
            sys.exit(f"ERROR: '{target}' not found in SourceMods or namelists of case '{args.case}'")
        if target in nl1:
            nl_ref_dir = os.path.join(exocam_root, 'cesm1.2.1', 'configs',
                                      config_type, 'namelist_files')
            ref_path = os.path.join(nl_ref_dir, target)
            if os.path.isfile(ref_path):
                r = subprocess.run(['diff', '-b', ref_path, path1])
                if r.returncode == 0:
                    print("(files are identical)")
            else:
                print(f"CASE ONLY — {target} has no ExoCAM namelist reference\n  {path1}\n")
                sys.stdout.write(open(path1).read())
            print(f"\n(registry: {cases_yaml})")
            return
        found_comp = next(c for c in COMPONENTS if target in files1[c])
        exo_path   = find_exocam_counterpart(target, found_comp, exocam_sm)
        if exo_path is not None:
            r = subprocess.run(['diff', '-b', exo_path, path1])
            if r.returncode == 0:
                print("(files are identical)")
        elif target in exort_files:
            rt_path = exort_files[target]
            r = subprocess.run(['diff', '-b', rt_path, path1])
            if r.returncode == 0:
                print("(RT file is identical to ExoRT source)")
        else:
            print(f"CASE ONLY — {target} has no ExoCAM or ExoRT counterpart\n  {path1}\n")
            sys.stdout.write(open(path1).read())
        print(f"\n(registry: {cases_yaml})")


def build_parser():
    parser = argparse.ArgumentParser(
        prog='diff.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('case',
                        help='Case name (resolved relative to caseroot)')
    parser.add_argument('--case2', metavar='CASE',
                        help='Compare against this case instead of ExoCAM reference source')
    parser.add_argument('--full', metavar='FILENAME',
                        help='Print full diff (or file contents if one-sided) for this filename')
    parser.add_argument('--config-registry', default=DEFAULT_CONFIG, dest='config_registry',
                        help='Path to config_registry.yaml '
                             '(default: config_registry.yaml next to this script)')
    parser.add_argument('--registry', default=None, metavar='PATH',
                        help='Path to cases registry yaml (e.g. active.yaml or archived.yaml). '
                             'Overrides paths.cases_yaml from config_registry.yaml; '
                             'falls back to active.yaml next to this script.')
    parser.add_argument('--verbose', action='store_true',
                        help='Show all files including IDENTICAL matches '
                             '(default: show only differences)')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    paths = load_paths(args.config_registry)
    cmd_full(args, paths) if args.full else cmd_summary(args, paths)

if __name__ == '__main__': main()

#!/usr/bin/env python3
"""
query.py — search active.yaml and generate experiment matrices

SUBCOMMANDS
-----------
  search      List cases matching filter criteria (name, config_type, exort_pkg, nlev)
  show        Print all parameters for one or more cases by exact name or prefix
  export      Write an experiment_matrix.yaml from one or more registry cases

Examples
--------
  python query.py search                                    # all cases
  python query.py search ExoCAM_thai_ben1_L51_n68equiv     # exact name
  python query.py search --prefix ExoCAM_thai              # prefix filter
  python query.py search --config-type cam_land_fv
  python query.py search --exort-pkg n68equiv --nlev 51
  python query.py show ExoCAM_thai_ben1_L51_n68equiv       # exact name
  python query.py show --prefix ExoCAM_thai                # prefix filter
  python query.py export ExoCAM_thai_ben1_L51_n68equiv -o my_run.yaml
  python query.py export case_a case_b -o sweep.yaml
  python query.py export my_base_case -o clone.yaml --clone
  python query.py --retired search                           # search retired cases
  python query.py --retired show ExoCAM_thai_ben1_L51_n68equiv
"""

import argparse
import datetime
import os
import re
import sys

import yaml

# Registry group order — mirrors scan._REGISTRY_GROUPS
_REGISTRY_GROUPS = [
    'meta', 'atmosphere', 'geophysical', 'model_options', 'special', 'diagnostics',
]

# Script-dir-absolute, matching RETIRED_REGISTRY: scan.py writes active.yaml
# next to itself, so a CWD-relative default only resolved when query.py was
# run from the repo directory.
DEFAULT_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'active.yaml')
RETIRED_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'retired.yaml')
DEFAULT_EXP_MATRIX_DIR = 'exp_matrices'
DEFAULT_CONFIG   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'config_registry.yaml')

# ---------------------------------------------------------------------------
# Registry I/O  (mirrors scan.load_registry)
# ---------------------------------------------------------------------------

def load_registry(path):
    """Load active.yaml and return list of flat dicts (one per case)."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    rows = []
    for entry in data.get('cases', []):
        row = {}
        for group in _REGISTRY_GROUPS:
            row.update(entry.get(group, {}) or {})
        rows.append(row)
    return rows


def load_registry_raw(path):
    """Load active.yaml and return list of raw grouped entry dicts (preserves group structure)."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get('cases', [])


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def _match(row, cases, prefix, config_type, exort_pkg, nlev):
    case_name = row.get('case_name') or ''
    if cases and case_name not in cases:
        return False
    if prefix and not case_name.lower().startswith(prefix.lower()):
        return False
    if config_type and row.get('config_type') != config_type:
        return False
    if exort_pkg and row.get('exort_pkg') != exort_pkg:
        return False
    if nlev is not None and row.get('nlev') != nlev:
        return False
    return True


def _entry_match(entry, cases, prefix):
    """Match a raw grouped registry entry against a cases set or prefix."""
    case_name = (entry.get('meta') or {}).get('case_name') or ''
    if cases and case_name not in cases:
        return False
    if prefix and not case_name.lower().startswith(prefix.lower()):
        return False
    return True


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------

def cmd_search(args, rows):
    if args.cases and args.prefix:
        sys.exit("ERROR: cannot combine explicit case names with --prefix")
    matches = [r for r in rows
               if _match(r, set(args.cases), args.prefix, args.config_type, args.exort_pkg, args.nlev)]
    if not matches:
        no_filters = not (args.cases or args.prefix or args.config_type or args.exort_pkg or args.nlev is not None)
        print("No cases found matching criteria.")
        if no_filters:
            print(f"Note: registry appears to be empty: {args.registry}")
        return

    # Column widths
    name_w   = max(len(r.get('case_name', '')) for r in matches)
    ct_w     = max(len(r.get('config_type', '') or '') for r in matches)
    exort_w  = max(len(r.get('exort_pkg', '') or '') for r in matches)

    show_config_saved = any('config_saved' in r for r in matches)
    date_w   = len('INSPECT_DATE')   # 12; YYYY-MM-DD (10) left-padded to header width
    config_w = len('CONFIG')         # 6

    header = (f"{'CASE':<{name_w}}  {'CONFIG_TYPE':<{ct_w}}  "
              f"{'EXORT_PKG':<{exort_w}}  {'NLEV':>4}  {'INSPECT_DATE':<{date_w}}"
              + (f"  {'CONFIG':<{config_w}}" if show_config_saved else ""))
    print(header)
    print('-' * len(header))
    for r in matches:
        config_col = ''
        if show_config_saved:
            config_col = f"  {'yes' if r.get('config_saved') else '-':<{config_w}}"
        print(f"{r.get('case_name',''):<{name_w}}  "
              f"{r.get('config_type',''):<{ct_w}}  "
              f"{r.get('exort_pkg',''):<{exort_w}}  "
              f"{str(r.get('nlev','') or ''):>4}  "
              f"{r.get('inspect_date',''):<{date_w}}"
              f"{config_col}")
    print(f"\n{len(matches)} case(s) found.")


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------

def cmd_show(args, raw_entries):
    known = {(e.get('meta') or {}).get('case_name') for e in raw_entries}
    errors = [n for n in args.cases if n not in known]
    for name in errors:
        print(f"ERROR: case '{name}' not found in registry.")
    if errors:
        sys.exit(1)
    cases_set = set(args.cases)
    matches = [e for e in raw_entries if _entry_match(e, cases_set, None)]
    for i, entry in enumerate(matches):
        if i > 0:
            print('---')
        print(yaml.dump({'cases': [entry]}, Dumper=_NoAliasDumper,
                        default_flow_style=False, sort_keys=False).rstrip())


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------

# Fields written to the matrix base block, in order
_BASE_FIELD_ORDER = [
    # CESM config
    'config_type', 'exort_pkg', 'cloud_scheme', 'nlev',
    'mach', 'stop_option', 'stop_n', 'rest_option', 'rest_n', 'ntasks', 'account',
    'run_type', 'run_refcase', 'run_refdate', 'brnch_retain_casename', 'run_startdate',
    # atmosphere
    'exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_nh3bar',
    'exo_cobar', 'exo_h2bar', 'exo_o2bar',
    'exo_scon', 'exo_solar_file',
    # geophysical
    'exo_surface_gravity', 'exo_planet_radius',
    'exo_ndays', 'exo_porb', 'exo_sday',
    'exo_eccen', 'exo_obliq', 'exo_mvelp', 'exo_ve',
    'exo_albdif', 'exo_albdir',
    # model options
    'do_exo_atmconst', 'do_exo_rt', 'do_exo_synchronous',
    'do_exo_gw', 'do_exo_simplevolc',
    'exo_convect_plim', 'exo_rad_step',
    'do_exo_rt_clearsky', 'do_exo_rt_spectral', 'do_exo_rt_carma',
    'do_carma_exort', 'Tmax', 'swFluxLimit', 'lwFluxLimit',
    # land/ocean files
    'finidat', 'fsurdat', 'som_pop_frc_file',
    # special
    'carma_params', 'volc_params', 'cice_params',
]

# Fields included in a clone export base (explicit allowlist — all others omitted)
_CLONE_BASE_FIELDS = {
    'clone', 'config_type', 'exort_pkg', 'nlev',
    'mach', 'stop_option', 'stop_n', 'rest_option', 'rest_n', 'resubmit', 'ntasks', 'account',
    'run_type', 'run_refcase', 'run_refdate', 'brnch_retain_casename', 'run_startdate',
}

# Registry keys not forwarded to the matrix
_SKIP_KEYS = {
    'case_name', 'casedir', 'inspect_date',
    'ncdata_pressure_str', 'ncdata_levels',
    'exo_n2bar', 'exo_n2bar_expr',         # N2 is implicit or set via exo_n2bar_explicit
    'exo_sday_expr',
    'exo_pstd_computed_bar',
    'warnings',
    'config_saved',
}

# Registry key -> matrix key renames
_KEY_RENAMES = {
    'clm_finidat': 'finidat',
    'clm_fsurdat': 'fsurdat',
}
# Reverse: matrix key -> registry key (for ordered field lookup)
_KEY_RENAMES_REV = {v: k for k, v in _KEY_RENAMES.items()}


def _row_to_base(row):
    """Convert a flat registry row to a matrix base dict."""
    base = {}
    # Build in _BASE_FIELD_ORDER first (controls key order in output)
    seen = set()
    for field in _BASE_FIELD_ORDER:
        reg_key = _KEY_RENAMES_REV.get(field, field)
        if reg_key in row and row[reg_key] is not None and reg_key not in _SKIP_KEYS:
            base[field] = row[reg_key]
            seen.add(reg_key)
        elif field in row and row[field] is not None and field not in _SKIP_KEYS:
            base[field] = row[field]
            seen.add(field)
    # Append any remaining keys not in the ordered list
    for k, v in row.items():
        if k in _SKIP_KEYS or k in seen or v is None:
            continue
        out_key = _KEY_RENAMES.get(k, k)
        base[out_key] = v
    return base


def _load_registry_defaults(config_registry_path):
    """Read machine name and defaults block from config_registry.yaml.

    Returns (machine, defaults_dict) where defaults_dict contains any of:
    resubmit, stop_option, stop_n, rest_n, ntasks, account.
    """
    try:
        with open(config_registry_path) as f:
            data = yaml.safe_load(f) or {}
        defaults = data.get('defaults', {}) or {}
        return data.get('machine'), defaults
    except (OSError, yaml.YAMLError):
        return None, {}


def cmd_export(args, rows, config_registry_path):
    case_names = args.case_names
    matched = {}
    for name in case_names:
        hits = [r for r in rows if r.get('case_name') == name]
        if not hits:
            sys.exit(f"ERROR: case '{name}' not found in registry.")
        matched[name] = hits[0]

    clone = args.clone

    # --clone is only valid against active cases
    if clone and os.path.basename(args.registry) == 'retired.yaml':
        sys.exit("ERROR: --clone requires an active registry; retired.yaml cases cannot be cloned.")

    # For --clone, all positional cases must resolve to a single clone source
    if clone:
        unique_sources = set(case_names)
        if len(unique_sources) > 1:
            sys.exit(
                "ERROR: --clone requires all positional cases to share the same source, "
                f"but got: {', '.join(sorted(unique_sources))}"
            )
        clone_source = case_names[0]

    if len(matched) == 1:
        # Single case: put everything in base, one stub entry in cases
        row  = next(iter(matched.values()))
        base = _row_to_base(row)
        cases_list = [{'name': ''}]
    else:
        # Multiple cases: compute common base, per-case overrides
        all_rows  = list(matched.values())
        all_bases = [_row_to_base(r) for r in all_rows]

        # Keys with identical values across all cases go in base
        all_keys = set(k for b in all_bases for k in b)
        base = {}
        for k in all_keys:
            vals = [b.get(k) for b in all_bases]
            if all(v == vals[0] for v in vals) and vals[0] is not None:
                base[k] = vals[0]

        # Per-case: only keys that differ from base
        cases_list = []
        for b in all_bases:
            entry = {'name': ''}
            for k, v in b.items():
                if k not in base or base[k] != v:
                    entry[k] = v
            cases_list.append(entry)

    # --- inject clone source into base if requested ---
    if clone:
        base['clone'] = clone_source
        # Restrict base to the clone allowlist — scientific params are inherited
        base = {k: v for k, v in base.items() if k in _CLONE_BASE_FIELDS}

    # --- default run_type for old registry rows that predate this field ---
    base.setdefault('run_type', 'startup')

    # --- inject required run/machine fields into base ---
    # CLI flags take priority; registry defaults fill in what's still missing.
    reg_mach, reg_defaults = _load_registry_defaults(config_registry_path)

    def _cli_or_default(attr, default_key=None):
        v = getattr(args, attr, None)
        if v is not None:
            return v
        return reg_defaults.get(default_key or attr)

    mach        = getattr(args, 'mach', None) or reg_mach
    resubmit    = _cli_or_default('resubmit')
    stop_option = _cli_or_default('stop_option')
    stop_n      = _cli_or_default('stop_n')
    rest_option = _cli_or_default('rest_option')
    rest_n      = _cli_or_default('rest_n')
    ntasks      = _cli_or_default('ntasks')
    account     = _cli_or_default('account') or ''

    base['mach']        = mach        or ''
    base['stop_option'] = stop_option or ''
    base['stop_n']      = stop_n      if stop_n      is not None else ''
    base['rest_option'] = rest_option or ''
    base['rest_n']      = rest_n      if rest_n      is not None else ''
    base['resubmit']    = resubmit    if resubmit    is not None else ''
    base['ntasks']      = ntasks      if ntasks      is not None else ''
    if account:
        base['account'] = account

    # collect any fields left blank so we can warn the user
    _REQUIRED_LABELS = {
        'mach':        'mach          — CESM machine name (e.g. discover)',
        'stop_option': 'stop_option   — run length unit (e.g. nyears)',
        'stop_n':      'stop_n        — run length value (e.g. 20)',
        'rest_option': 'rest_option   — restart frequency unit (e.g. nyears)',
        'rest_n':      'rest_n        — restart interval (e.g. 5)',
        'resubmit':    'resubmit      — number of automatic resubmissions (e.g. 1)',
        'ntasks':      'ntasks        — processor count (e.g. 126)',
    }
    missing = [label for key, label in _REQUIRED_LABELS.items() if not base.get(key)]

    matrix = {
        'meta': {
            'description': '',
            'author': '',
            'created': datetime.date.today().isoformat(),
            'source_registry': args.registry,
        },
        'config_registry': config_registry_path,
        'base': base,
        'cases': cases_list,
    }

    yaml_text = _dump_matrix(matrix)

    if missing:
        warning = (
            "# ============================================================\n"
            "# FIXME: the following required fields are blank.\n"
            "# Fill them in before running build.py.\n"
            "#\n"
            + "".join(f"#   {label}\n" for label in missing)
            + "# ============================================================\n\n"
        )
        out_text = warning + yaml_text
    else:
        out_text = yaml_text

    if args.output:
        if not os.path.dirname(args.output):
            os.makedirs(DEFAULT_EXP_MATRIX_DIR, exist_ok=True)
            args.output = os.path.join(DEFAULT_EXP_MATRIX_DIR, args.output)
        with open(args.output, 'w') as f:
            f.write(out_text)
        if missing:
            print(f"Wrote {args.output} ({len(cases_list)} case(s)) — "
                  f"WARNING: {len(missing)} required field(s) need values (see FIXME header)")
        else:
            print(f"Wrote {args.output} ({len(cases_list)} case(s))")
    else:
        print(out_text)
        print("  (output above printed to stdout — use -o FILE to write experiment matrix)",
              file=sys.stderr)

    # Warn about exort_pkg '*' suffix after output so the warning is visible at the end.
    # Suppressed in clone mode — RT source is inherited from the clone source, not via -usr_src.
    for name, row in matched.items():
        pkg = row.get('exort_pkg', '') or ''
        if pkg.endswith('*') and not clone:
            print(
                f"\nWARNING: case '{name}' has exort_pkg='{pkg}'\n"
                f"  The '*' indicates RT source was copied into SourceMods of the originating case\n"
                f"  rather than referenced via -usr_src. create_newcase cannot replicate this.\n"
                f"  Use --clone against the originating case instead of exporting a newcase matrix.\n"
                f"  The exported matrix will contain exort_pkg='{pkg}' — edit before use.",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# YAML output — preserve key order, no aliases
# ---------------------------------------------------------------------------

class _NoAliasDumper(yaml.Dumper):
    def ignore_aliases(self, data):
        return True


def _fmt_float_yaml(value):
    """
    Format a float so it round-trips cleanly through PyYAML without !!float tags.
    PyYAML's plain-float resolver requires a decimal point AND a sign in the exponent
    (e.g. '2.0e+10', not '2e10' or '2.0e10'). We use %e to guarantee both, then strip
    trailing zeros after the decimal (keeping at least one digit).
    Threshold: scientific notation when |v| >= 1e6 or |v| < 1e-4 (nonzero),
    or when repr() already uses 'e' notation.
    """
    s = f'{value:.6e}'                            # '2.000000e+10'
    s = re.sub(r'(\.\d*?)0+(e)', r'\1\2', s)     # '2.e+10'
    s = re.sub(r'\.(e)', r'.0\1', s)             # '2.0e+10'
    return s


def _float_representer(dumper, value):
    if 'e' in repr(value) or abs(value) >= 1e6 or (value != 0 and abs(value) < 1e-4):
        s = _fmt_float_yaml(value)
    else:
        s = repr(value)
    return dumper.represent_scalar('tag:yaml.org,2002:float', s)


_NoAliasDumper.add_representer(float, _float_representer)


def _dump_matrix(matrix):
    text = yaml.dump(
        matrix,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return text.replace("name: ''\n", "name: ''  # FIXME: set new case name\n")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='query.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--registry', default=DEFAULT_REGISTRY, metavar='PATH',
                        help=f'Path to active.yaml (default: {DEFAULT_REGISTRY})')
    parser.add_argument('--retired', action='store_true',
                        help='Query retired.yaml instead of active.yaml '
                             '(shorthand for --registry retired.yaml)')

    sub = parser.add_subparsers(dest='command', metavar='SUBCOMMAND', help=argparse.SUPPRESS)
    sub.required = True

    # ---- search ----
    p_search = sub.add_parser(
        'search',
        help=argparse.SUPPRESS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_search.add_argument('cases', nargs='*', metavar='CASE_NAME',
                          help='Exact case name(s) to match (optional)')
    p_search.add_argument('--prefix', metavar='STR',
                          help='Filter by case name prefix (case-insensitive)')
    p_search.add_argument('--config-type', dest='config_type', metavar='TYPE',
                          help='Exact match on config_type (e.g. cam_land_fv)')
    p_search.add_argument('--exort-pkg', dest='exort_pkg', metavar='PKG',
                          help='Exact match on exort_pkg (e.g. n68equiv)')
    p_search.add_argument('--nlev', type=int, metavar='N',
                          help='Exact match on nlev')

    # ---- show ----
    p_show = sub.add_parser(
        'show',
        help=argparse.SUPPRESS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_show.add_argument('cases', nargs='+', metavar='CASE_NAME',
                        help="Exact case name(s) as stored in the registry. "
                             "Use 'query.py search [--prefix STR] [--nlev N] "
                             "[--config-type TYPE] [--exort-pkg PKG]' to find case names.")

    # ---- export ----
    p_export = sub.add_parser(
        'export',
        help=argparse.SUPPRESS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_export.add_argument('case_names', nargs='+', metavar='CASE_NAME',
                          help='Exact case name(s) to export')
    p_export.add_argument('-o', '--output', metavar='PATH',
                          help='Output file path (default: print to stdout)')
    p_export.add_argument('--clone', action='store_true',
                          help='Write clone: <source_case> into base; the positional case '
                               'argument(s) must all be the same case (the clone source)')
    p_export.add_argument('--config-registry', dest='config_registry',
                          default=DEFAULT_CONFIG, metavar='PATH',
                          help='config_registry path written into the matrix '
                               f'(default: {DEFAULT_CONFIG})')
    p_export.add_argument('--mach', metavar='NAME',
                          help='CESM machine name (default: read from config_registry.yaml)')
    p_export.add_argument('--stop-option', dest='stop_option', metavar='STR',
                          help='Run length unit, e.g. nyears or ndays')
    p_export.add_argument('--rest-option', dest='rest_option', metavar='STR',
                          help='Restart frequency unit (nyears or ndays)')
    p_export.add_argument('--stop-n', dest='stop_n', type=int, metavar='N',
                          help='Run length value')
    p_export.add_argument('--rest-n', dest='rest_n', type=int, metavar='N',
                          help='Restart write interval')
    p_export.add_argument('--resubmit', type=int, metavar='N',
                          help='Number of automatic resubmissions '
                               '(default: read from config_registry.yaml)')
    p_export.add_argument('--ntasks', type=int, metavar='N',
                          help='Processor count')
    p_export.add_argument('--account', metavar='STR',
                          help='SLURM charge account (#SBATCH --account)')

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.retired and args.registry != DEFAULT_REGISTRY:
        sys.exit("ERROR: --retired and --registry are mutually exclusive")
    if args.retired:
        args.registry = RETIRED_REGISTRY

    if not os.path.exists(args.registry):
        sys.exit(f"ERROR: registry not found: {args.registry}")

    rows = load_registry(args.registry)

    if args.command == 'search':
        cmd_search(args, rows)
    elif args.command == 'show':
        cmd_show(args, load_registry_raw(args.registry))
    elif args.command == 'export':
        cmd_export(args, rows, args.config_registry)
    registry_label = '--retired' if args.retired else f'--registry {args.registry}'
    print(f"\n(case information from: {registry_label})")


if __name__ == '__main__':
    main()

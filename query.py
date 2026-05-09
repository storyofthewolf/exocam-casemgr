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
"""

import argparse
import datetime
import os
import sys

import yaml

# Registry group order — mirrors scan._REGISTRY_GROUPS
_REGISTRY_GROUPS = [
    'meta', 'atmosphere', 'geophysical', 'model_options', 'special', 'diagnostics',
]

DEFAULT_REGISTRY = 'active.yaml'
DEFAULT_BLUEPRINT_DIR = 'blueprints'
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
        print("No cases found matching criteria.")
        return

    # Column widths
    name_w   = max(len(r.get('case_name', '')) for r in matches)
    ct_w     = max(len(r.get('config_type', '') or '') for r in matches)
    exort_w  = max(len(r.get('exort_pkg', '') or '') for r in matches)

    show_config_saved = any('config_saved' in r for r in matches)

    header = (f"{'CASE':<{name_w}}  {'CONFIG_TYPE':<{ct_w}}  "
              f"{'EXORT_PKG':<{exort_w}}  {'NLEV':>4}  {'INSPECT_DATE'}"
              + (f"  {'CONFIG'}" if show_config_saved else ""))
    print(header)
    print('-' * len(header))
    for r in matches:
        config_col = ''
        if show_config_saved:
            config_col = f"  {'yes' if r.get('config_saved') else '-'}"
        print(f"{r.get('case_name',''):<{name_w}}  "
              f"{r.get('config_type',''):<{ct_w}}  "
              f"{r.get('exort_pkg',''):<{exort_w}}  "
              f"{str(r.get('nlev','') or ''):>4}  "
              f"{r.get('inspect_date','')}"
              f"{config_col}")
    print(f"\n{len(matches)} case(s) found.")


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------

def cmd_show(args, raw_entries):
    if args.cases and args.prefix:
        sys.exit("ERROR: cannot combine explicit case names with --prefix")
    cases_set = set(args.cases)
    prefix = args.prefix
    matches = [
        e for e in raw_entries
        if _entry_match(e, cases_set, prefix)
    ]
    if not matches:
        sys.exit("ERROR: no cases found matching criteria.")
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
    'mach', 'stop_option', 'stop_n', 'rest_n', 'ntasks', 'account',
    # atmosphere
    'exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_nh3bar',
    'exo_cobar', 'exo_h2bar', 'exo_o2bar',
    'exo_scon', 'exo_solar_file',
    # geophysical
    'exo_surface_gravity', 'exo_planet_radius',
    'exo_ndays', 'exo_porb', 'exo_sday',
    'exo_eccen', 'exo_obliq',
    # model options
    'do_exo_atmconst', 'do_exo_rt', 'do_exo_synchronous',
    'do_exo_gw', 'do_exo_simplevolc',
    'exo_convect_plim', 'exo_rad_step',
    'do_exo_rt_clearsky', 'do_exo_rt_spectral', 'do_exo_rt_carma',
    # land/ocean files
    'finidat', 'fsurdat', 'som_pop_frc_file',
    # special
    'carma_params', 'volc_params',
]

# Fields stripped from base in --bare mode (atmosphere, geophysical, model options, special)
_BARE_STRIP_KEYS = {
    'exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_nh3bar', 'exo_cobar',
    'exo_h2bar', 'exo_o2bar', 'exo_scon', 'exo_solar_file',
    'exo_surface_gravity', 'exo_planet_radius',
    'exo_ndays', 'exo_porb', 'exo_sday', 'exo_eccen', 'exo_obliq',
    'do_exo_atmconst', 'do_exo_rt', 'do_exo_synchronous',
    'do_exo_gw', 'do_exo_simplevolc', 'exo_convect_plim', 'exo_rad_step',
    'do_exo_rt_clearsky', 'do_exo_rt_spectral', 'do_exo_rt_carma',
    'finidat', 'fsurdat', 'som_pop_frc_file', 'ncdata_override',
    'carma_params', 'volc_params',
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
    'ncdata':      'ncdata_override',
}
# Reverse: matrix key -> registry key (for ordered field lookup)
_KEY_RENAMES_REV = {v: k for k, v in _KEY_RENAMES.items()}


def _row_to_base(row, bare=False):
    """Convert a flat registry row to a matrix base dict.

    bare=True strips atmosphere, geophysical, model_options, and special fields,
    leaving only CESM config and run/machine fields.
    """
    skip = _SKIP_KEYS | (_BARE_STRIP_KEYS if bare else set())
    base = {}
    # Build in _BASE_FIELD_ORDER first (controls key order in output)
    seen = set()
    for field in _BASE_FIELD_ORDER:
        if bare and field in _BARE_STRIP_KEYS:
            continue
        reg_key = _KEY_RENAMES_REV.get(field, field)
        if reg_key in row and row[reg_key] is not None and reg_key not in skip:
            base[field] = row[reg_key]
            seen.add(reg_key)
        elif field in row and row[field] is not None and field not in skip:
            base[field] = row[field]
            seen.add(field)
    # Append any remaining keys not in the ordered list
    for k, v in row.items():
        if k in skip or k in seen or v is None:
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

    # bare mode: default True when --clone is set, False otherwise; --full overrides
    clone = getattr(args, 'clone', None)
    full  = getattr(args, 'full',  False)
    bare  = bool(clone) and not full

    if len(matched) == 1:
        # Single case: put everything in base, one stub entry in cases
        row  = next(iter(matched.values()))
        name = next(iter(matched.keys()))
        base = _row_to_base(row, bare=bare)
        cases_list = [{'name': name}]
    else:
        # Multiple cases: compute common base, per-case overrides
        all_rows = list(matched.values())
        all_bases = [_row_to_base(r, bare=bare) for r in all_rows]

        # Keys with identical values across all cases go in base
        all_keys = set(k for b in all_bases for k in b)
        base = {}
        for k in all_keys:
            vals = [b.get(k) for b in all_bases]
            if all(v == vals[0] for v in vals) and vals[0] is not None:
                base[k] = vals[0]

        # Per-case: only keys that differ from base
        cases_list = []
        for name, b in zip(case_names, all_bases):
            entry = {'name': name}
            for k, v in b.items():
                if k not in base or base[k] != v:
                    entry[k] = v
            cases_list.append(entry)

    # --- inject clone source into base if provided ---
    if clone:
        base['clone'] = clone

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
    rest_n      = _cli_or_default('rest_n')
    ntasks      = _cli_or_default('ntasks')
    account     = _cli_or_default('account') or ''

    base['mach']        = mach        or ''
    base['stop_option'] = stop_option or ''
    base['stop_n']      = stop_n      if stop_n      is not None else ''
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
            os.makedirs(DEFAULT_BLUEPRINT_DIR, exist_ok=True)
            args.output = os.path.join(DEFAULT_BLUEPRINT_DIR, args.output)
        with open(args.output, 'w') as f:
            f.write(out_text)
        if missing:
            print(f"Wrote {args.output} ({len(cases_list)} case(s)) — "
                  f"WARNING: {len(missing)} required field(s) need values (see FIXME header)")
        else:
            print(f"Wrote {args.output} ({len(cases_list)} case(s))")
    else:
        print(out_text)
        print("  (output above printed to stdout — use -o FILE to write blueprint)", 
              file=sys.stderr)


# ---------------------------------------------------------------------------
# YAML output — preserve key order, no aliases
# ---------------------------------------------------------------------------

class _NoAliasDumper(yaml.Dumper):
    def ignore_aliases(self, data):
        return True


def _dump_matrix(matrix):
    return yaml.dump(
        matrix,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


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

    sub = parser.add_subparsers(dest='command', metavar='SUBCOMMAND')

    # ---- search ----
    p_search = sub.add_parser(
        'search',
        help='List cases matching filter criteria',
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
        help='Print all parameters for one case by exact name',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_show.add_argument('cases', nargs='*', metavar='CASE_NAME',
                        help='Exact case name(s) as stored in active.yaml')
    p_show.add_argument('--prefix', metavar='STR',
                        help='Filter by case name prefix (case-insensitive; '
                             'cannot combine with explicit case names)')

    # ---- export ----
    p_export = sub.add_parser(
        'export',
        help='Generate an experiment_matrix.yaml from one or more registry cases',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_export.add_argument('case_names', nargs='+', metavar='CASE_NAME',
                          help='Exact case name(s) to export')
    p_export.add_argument('-o', '--output', metavar='PATH',
                          help='Output file path (default: print to stdout)')
    p_export.add_argument('--clone', metavar='CASE_NAME',
                          help='Source case to clone; written as clone: in base')
    p_export.add_argument('--full', action='store_true',
                          help='When used with --clone, include all scientific parameters in '
                               'base instead of the default bare (clone-source-inherits) output')
    p_export.add_argument('--config-registry', dest='config_registry',
                          default=DEFAULT_CONFIG, metavar='PATH',
                          help='config_registry path written into the matrix '
                               f'(default: {DEFAULT_CONFIG})')
    p_export.add_argument('--mach', metavar='NAME',
                          help='CESM machine name (default: read from config_registry.yaml)')
    p_export.add_argument('--stop-option', dest='stop_option', metavar='STR',
                          help='Run length unit, e.g. nyears or ndays')
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
    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if not os.path.exists(args.registry):
        sys.exit(f"ERROR: registry not found: {args.registry}")

    rows = load_registry(args.registry)

    if args.command == 'search':
        cmd_search(args, rows)
    elif args.command == 'show':
        cmd_show(args, load_registry_raw(args.registry))
    elif args.command == 'export':
        cmd_export(args, rows, args.config_registry)
    print(f"\n(case information from: --registry {args.registry})")


if __name__ == '__main__':
    main()

"""
exo_query.py — search cases.yaml and generate experiment matrices

SUBCOMMANDS
-----------
  search      List cases matching filter criteria (name, config_type, exort_pkg, nlev)
  show        Print all parameters for a single case by exact name
  export      Write an experiment_matrix.yaml from one or more registry cases

Examples
--------
  python exo_query.py search --config-type cam_land_fv
  python exo_query.py search --exort-pkg n68equiv --nlev 51
  python exo_query.py search --name thai              # substring match
  python exo_query.py show ExoCAM_thai_ben1_L51_n68equiv
  python exo_query.py export ExoCAM_thai_ben1_L51_n68equiv -o my_run.yaml
  python exo_query.py export case_a case_b -o sweep.yaml
"""

import argparse
import datetime
import os
import sys

import yaml

# Registry group order — mirrors exo_inspect._REGISTRY_GROUPS
_REGISTRY_GROUPS = [
    'meta', 'atmosphere', 'geophysical', 'model_options', 'special', 'diagnostics',
]

DEFAULT_REGISTRY = 'cases.yaml'
DEFAULT_CONFIG   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'config_registry.yaml')

# ---------------------------------------------------------------------------
# Registry I/O  (mirrors exo_inspect.load_registry)
# ---------------------------------------------------------------------------

def load_registry(path):
    """Load cases.yaml and return list of flat dicts (one per case)."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    rows = []
    for entry in data.get('cases', []):
        row = {}
        for group in _REGISTRY_GROUPS:
            row.update(entry.get(group, {}) or {})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

def _match(row, name, config_type, exort_pkg, nlev):
    if name and name.lower() not in (row.get('case_name') or '').lower():
        return False
    if config_type and row.get('config_type') != config_type:
        return False
    if exort_pkg and row.get('exort_pkg') != exort_pkg:
        return False
    if nlev is not None and row.get('nlev') != nlev:
        return False
    return True


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------

def cmd_search(args, rows):
    matches = [r for r in rows
               if _match(r, args.name, args.config_type, args.exort_pkg, args.nlev)]
    if not matches:
        print("No cases found matching criteria.")
        return

    # Column widths
    name_w   = max(len(r.get('case_name', '')) for r in matches)
    ct_w     = max(len(r.get('config_type', '') or '') for r in matches)
    exort_w  = max(len(r.get('exort_pkg', '') or '') for r in matches)

    header = (f"{'CASE':<{name_w}}  {'CONFIG_TYPE':<{ct_w}}  "
              f"{'EXORT_PKG':<{exort_w}}  {'NLEV':>4}  {'INSPECT_DATE'}")
    print(header)
    print('-' * len(header))
    for r in matches:
        print(f"{r.get('case_name',''):<{name_w}}  "
              f"{r.get('config_type',''):<{ct_w}}  "
              f"{r.get('exort_pkg',''):<{exort_w}}  "
              f"{str(r.get('nlev','') or ''):>4}  "
              f"{r.get('inspect_date','')}")
    print(f"\n{len(matches)} case(s) found.")


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------

def cmd_show(args, rows):
    target = args.case_name
    matches = [r for r in rows if r.get('case_name') == target]
    if not matches:
        sys.exit(f"ERROR: case '{target}' not found in registry.")
    row = matches[0]
    print(yaml.dump({target: row}, default_flow_style=False, sort_keys=False).rstrip())


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

# Registry keys not forwarded to the matrix
_SKIP_KEYS = {
    'case_name', 'casedir', 'inspect_date',
    'ncdata_pressure_str', 'ncdata_levels',
    'exo_n2bar', 'exo_n2bar_expr',         # N2 is implicit or set via exo_n2bar_explicit
    'exo_sday_expr',
    'exo_pstd_computed_bar',
    'warnings',
}

# Registry key -> matrix key renames
_KEY_RENAMES = {
    'clm_finidat': 'finidat',
    'clm_fsurdat': 'fsurdat',
    'ncdata':      'ncdata_override',
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


def cmd_export(args, rows, config_registry_path):
    case_names = args.case_names
    matched = {}
    for name in case_names:
        hits = [r for r in rows if r.get('case_name') == name]
        if not hits:
            sys.exit(f"ERROR: case '{name}' not found in registry.")
        matched[name] = hits[0]

    if len(matched) == 1:
        # Single case: put everything in base, one stub entry in cases
        row  = next(iter(matched.values()))
        name = next(iter(matched.keys()))
        base = _row_to_base(row)
        cases_list = [{'name': name}]
    else:
        # Multiple cases: compute common base, per-case overrides
        all_rows = list(matched.values())
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
        for name, b in zip(case_names, all_bases):
            entry = {'name': name}
            for k, v in b.items():
                if k not in base or base[k] != v:
                    entry[k] = v
            cases_list.append(entry)

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

    out_text = _dump_matrix(matrix)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(out_text)
        print(f"Wrote {args.output} ({len(cases_list)} case(s))")
    else:
        print(out_text)


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
        prog='exo_query.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--registry', default=DEFAULT_REGISTRY, metavar='PATH',
                        help=f'Path to cases.yaml (default: {DEFAULT_REGISTRY})')

    sub = parser.add_subparsers(dest='command', metavar='SUBCOMMAND')

    # ---- search ----
    p_search = sub.add_parser(
        'search',
        help='List cases matching filter criteria',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_search.add_argument('--name', metavar='STR',
                          help='Substring match on case name (case-insensitive)')
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
    p_show.add_argument('case_name', metavar='CASE_NAME',
                        help='Exact case name as stored in cases.yaml')

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
    p_export.add_argument('--config-registry', dest='config_registry',
                          default=DEFAULT_CONFIG, metavar='PATH',
                          help='config_registry path written into the matrix '
                               f'(default: {DEFAULT_CONFIG})')

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
        cmd_show(args, rows)
    elif args.command == 'export':
        cmd_export(args, rows, args.config_registry)


if __name__ == '__main__':
    main()

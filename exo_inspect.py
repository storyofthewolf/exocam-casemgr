"""
ExoCAM case inspector. Walks CASE directories, extracts scientific metadata,
writes a queryable YAML registry.

Usage:
  python exo_inspect.py PATH [PATH ...] [--registry cases.yaml] [--update]

Each PATH is either a CASE dir (contains SourceMods/) or a parent dir
(its children are scanned for CASE dirs).
"""

import argparse
import datetime
import os
import sys
import yaml

# allow running from any directory
sys.path.insert(0, os.path.dirname(__file__))
from exo_parse import (
    parse_exoplanet_mod, parse_user_nl_cam, parse_cam_config_opts,
    compute_pstd_bar, pressure_str_to_bar
)

SOLAR_STEM_MAP = {
    'n68equiv':   'n68',
    'n84equiv':   'n84',
    'n28archean': 'n28',
    'n42h2o':     'n42',
}

REGISTRY_FIELDS = [
    'case_name', 'casedir', 'inspect_date',
    'config_type', 'exort_pkg', 'cloud_scheme', 'nlev',
    'ncdata', 'ncdata_pressure_str', 'ncdata_levels',
    'exo_co2bar', 'exo_ch4bar', 'exo_h2bar', 'exo_o2bar',
    'exo_c2h6bar', 'exo_nh3bar', 'exo_cobar', 'exo_n2bar',
    'exo_n2bar_expr', 'exo_pstd_computed_bar',
    'exo_scon', 'exo_solar_file', 'do_exo_synchronous',
    'exo_ndays', 'exo_porb', 'exo_sday', 'exo_sday_expr',
    'exo_surface_gravity', 'exo_planet_radius', 'exo_eccen', 'exo_obliq',
    'carma_params', 'volc_params',
    'warnings',
]


def find_case_dirs(path):
    marker = os.path.join('SourceMods', 'src.share', 'exoplanet_mod.F90')
    if os.path.exists(os.path.join(path, marker)):
        return [path]
    # treat as parent: scan one level of children
    found = []
    try:
        for name in sorted(os.listdir(path)):
            child = os.path.join(path, name)
            if os.path.isdir(child) and os.path.exists(os.path.join(child, marker)):
                found.append(child)
    except PermissionError:
        pass
    return found


def inspect_case(casedir):
    row = {'casedir': os.path.abspath(casedir),
           'case_name': os.path.basename(casedir.rstrip('/\\')),
           'inspect_date': datetime.date.today().isoformat()}

    # exoplanet_mod.F90
    exo_path = os.path.join(casedir, 'SourceMods', 'src.share', 'exoplanet_mod.F90')
    exo = {}
    if os.path.exists(exo_path):
        exo = parse_exoplanet_mod(exo_path)

    for key in ['exo_co2bar', 'exo_ch4bar', 'exo_h2bar', 'exo_o2bar',
                'exo_c2h6bar', 'exo_nh3bar', 'exo_cobar', 'exo_n2bar',
                'exo_scon', 'exo_solar_file', 'do_exo_synchronous',
                'exo_ndays', 'exo_porb', 'exo_surface_gravity',
                'exo_planet_radius', 'exo_eccen', 'exo_obliq']:
        row[key] = exo.get(key)

    row['exo_n2bar_expr'] = exo.get('exo_n2bar_expr')

    # exo_sday: prefer literal value; fall back to expression string
    if exo.get('exo_sday') is not None:
        row['exo_sday'] = exo['exo_sday']
        row['exo_sday_expr'] = None
    else:
        row['exo_sday'] = None
        row['exo_sday_expr'] = exo.get('exo_sday_expr')

    pstd, _ = compute_pstd_bar(exo)
    row['exo_pstd_computed_bar'] = round(pstd, 6) if pstd is not None else None

    # user_nl_cam
    nl_path = os.path.join(casedir, 'user_nl_cam')
    nl = {}
    if os.path.exists(nl_path):
        nl = parse_user_nl_cam(nl_path)
    row['ncdata'] = nl.get('ncdata')
    row['ncdata_pressure_str'] = nl.get('ncdata_pressure_str')
    row['ncdata_levels'] = nl.get('ncdata_levels')
    row['carma_params'] = nl.get('carma_params') or None
    row['volc_params'] = nl.get('volc_params') or None

    # env_build.xml (may not exist pre-cesm_setup)
    xml_path = os.path.join(casedir, 'env_build.xml')
    if not os.path.exists(xml_path):
        xml_path = os.path.join(casedir, 'env_run.xml')
    cam = parse_cam_config_opts(xml_path)
    row['nlev'] = cam.get('nlev')
    row['exort_pkg'] = cam.get('exort_pkg')
    row['cloud_scheme'] = cam.get('cloud_scheme')

    # infer config_type from SourceMods structure
    row['config_type'] = _infer_config_type(casedir)

    warnings = check_consistency(row)
    row['warnings'] = warnings or None

    return row


def _infer_config_type(casedir):
    srcmods = os.path.join(casedir, 'SourceMods')
    has_cice = os.path.isdir(os.path.join(srcmods, 'src.cice'))
    has_clm  = os.path.isdir(os.path.join(srcmods, 'src.clm'))
    has_docn = os.path.isdir(os.path.join(srcmods, 'src.docn'))
    if has_cice and has_clm:
        return 'cam_mixed_fv'
    if has_cice and not has_clm:
        return 'cam_aqua_fv'
    if has_clm and not has_cice:
        return 'cam_land_fv'
    return 'unknown'


def check_consistency(meta):
    warnings = []

    # pressure match
    pstd = meta.get('exo_pstd_computed_bar')
    nc_pstr = meta.get('ncdata_pressure_str')
    if pstd is not None and nc_pstr is not None:
        nc_p = pressure_str_to_bar(nc_pstr)
        if nc_p and nc_p > 0:
            if abs(pstd - nc_p) / nc_p > 0.05:
                warnings.append(
                    f"pressure mismatch: exoplanet_mod pstd={pstd:.4f}bar "
                    f"but ncdata implies {nc_p}bar"
                )

    # level match
    nc_lev = meta.get('ncdata_levels')
    nlev = meta.get('nlev')
    if nc_lev is not None and nlev is not None:
        if int(nc_lev) != int(nlev):
            warnings.append(
                f"level mismatch: ncdata has L{nc_lev} but CAM_CONFIG_OPTS has -nlev {nlev}"
            )

    # solar file / exort package
    solar = meta.get('exo_solar_file') or ''
    exort_pkg = meta.get('exort_pkg')
    if solar and exort_pkg and exort_pkg in SOLAR_STEM_MAP:
        stem = SOLAR_STEM_MAP[exort_pkg]
        if stem not in os.path.basename(solar):
            warnings.append(
                f"solar file mismatch: exort_pkg={exort_pkg} expects stem '{stem}' "
                f"but solar file is {os.path.basename(solar)}"
            )

    return warnings


def load_registry(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get('cases', []) if data else []


def write_registry(rows, path):
    # Build ordered dicts so YAML output follows REGISTRY_FIELDS order
    ordered = []
    for row in rows:
        entry = {}
        for field in REGISTRY_FIELDS:
            val = row.get(field)
            if val is not None:
                entry[field] = val
        ordered.append(entry)

    with open(path, 'w') as f:
        yaml.dump({'cases': ordered}, f,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"Registry written: {path}  ({len(rows)} cases)")


def main():
    parser = argparse.ArgumentParser(description='Inspect ExoCAM CASE directories and write YAML registry')
    parser.add_argument('paths', nargs='+', help='CASE dir(s) or parent dir(s) to scan')
    parser.add_argument('--registry', default='cases.yaml', help='Output YAML path (default: cases.yaml)')
    parser.add_argument('--update', action='store_true',
                        help='Merge with existing registry instead of overwriting')
    args = parser.parse_args()

    # collect all case dirs
    all_case_dirs = []
    for p in args.paths:
        found = find_case_dirs(p)
        if not found:
            print(f"WARNING: no CASE dirs found under {p}", file=sys.stderr)
        all_case_dirs.extend(found)

    if not all_case_dirs:
        print("No CASE directories found.", file=sys.stderr)
        sys.exit(1)

    rows = []
    for casedir in all_case_dirs:
        print(f"Inspecting: {casedir}")
        try:
            row = inspect_case(casedir)
            rows.append(row)
            if row['warnings']:
                for w in row['warnings']:
                    print(f"  WARNING: {w}")
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)

    if args.update:
        existing = load_registry(args.registry)
        existing_by_name = {r['case_name']: r for r in existing}
        for row in rows:
            existing_by_name[row['case_name']] = row
        rows = list(existing_by_name.values())

    write_registry(rows, args.registry)

    # print summary table
    print(f"\n{'CASE':<30} {'CONFIG':<16} {'PSTD':>8} {'NLEV':>5}  WARNINGS")
    print('-' * 75)
    for r in rows:
        pstd = r.get('exo_pstd_computed_bar')
        pstd_s = f"{float(pstd):.3f}" if pstd else '?'
        warn_s = '; '.join(r['warnings']) if r.get('warnings') else ''
        print(f"{r['case_name']:<30} {str(r.get('config_type','')):<16} "
              f"{pstd_s:>8} {str(r.get('nlev','?')):>5}  {warn_s[:40]}")


if __name__ == '__main__':
    main()

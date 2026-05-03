"""
ExoCAM build script generator. Reads an experiment matrix YAML and a config
registry YAML, validates each case, writes one shell script per case plus a
staged exoplanet_mod.F90.

Usage:
  python exo_build.py experiment_matrix.yaml [--outdir scripts/] [--execute]

Default is dry-run: scripts are written but not executed.
--execute runs each script via bash and tees output to <case>.build.log.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("pyyaml is required: pip install pyyaml")

sys.path.insert(0, os.path.dirname(__file__))
from exo_parse import compute_pstd_bar, _RE_KIND, _RE_PURE_NUM

# Parameters that map directly to exoplanet_mod.F90 Fortran parameter names
EXO_PARAMS = {
    'exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_nh3bar', 'exo_cobar',
    'exo_h2bar', 'exo_o2bar',
    'exo_surface_gravity', 'exo_planet_radius',
    'exo_ndays', 'exo_porb', 'exo_sday',
    'exo_scon', 'exo_eccen', 'exo_obliq', 'exo_mvelp', 'exo_ve',
    'exo_convect_plim', 'exo_rad_step',
    'exo_albdif', 'exo_albdir',
    'do_exo_synchronous', 'do_exo_rt', 'do_exo_atmconst',
    'do_exo_rt_clearsky', 'do_exo_rt_spectral', 'do_exo_gw', 'do_carma_exort',
    'Tmax', 'swFluxLimit', 'lwFluxLimit',
}

REQUIRED_FIELDS = ['config_type', 'exort_pkg', 'nlev', 'mach',
                   'stop_option', 'stop_n', 'rest_n', 'ntasks_atm']

SOLAR_FILE_STEMS = {
    'n68equiv':   'n68',
    'n84equiv':   'n84',
    'n28archean': 'n28',
    'n42h2o':     'n42',
}

# Fortran parameter line pattern for replacement
_RE_PARAM_LINE = re.compile(
    r'^(\s+(?:real\(r8\)|integer|logical)[^:]*parameter\s*::\s*)(\w+)(\s*=\s*)([^!\n]+)(.*)',
    re.IGNORECASE
)


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_case(base, overrides):
    spec = dict(base)
    spec.update(overrides)
    return spec


def compute_n2bar(spec):
    """Return explicit n2bar if set, else 1 - sum(others) for <=1 bar atmospheres."""
    if 'exo_n2bar_explicit' in spec:
        return float(spec['exo_n2bar_explicit'])
    gas_others = ['exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_o2bar',
                  'exo_h2bar', 'exo_nh3bar', 'exo_cobar']
    others = sum(float(spec.get(k, 0.0)) for k in gas_others)
    if others <= 1.0:
        return 1.0 - others
    return None


def compute_pstd_from_spec(spec):
    """Compute total pressure in bar from spec dict."""
    gas_keys = ['exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_o2bar',
                'exo_h2bar', 'exo_nh3bar', 'exo_cobar']
    others = sum(float(spec.get(k, 0.0)) for k in gas_keys)
    if 'exo_n2bar_explicit' in spec:
        return others + float(spec['exo_n2bar_explicit'])
    if others <= 1.0:
        return 1.0
    return others


def bar_to_pressure_str(bar_val):
    """Convert 1.0 -> '1bar', 0.1 -> '0.1bar', 10.0 -> '10bar'."""
    # round to 6 significant figures to avoid float noise, then strip trailing zeros
    s = f"{round(bar_val, 6):g}"
    return s + 'bar'


def find_ic_file(spec, registry):
    """
    Look up IC filename in registry.ic_files[config_type][pressure_str][nlev].
    Returns (ic_filename, pressure_str) or raises ValueError.
    """
    if 'ncdata_override' in spec:
        return spec['ncdata_override'], None

    config_type = spec['config_type']
    nlev = int(spec['nlev'])
    pstd = compute_pstd_from_spec(spec)
    pressure_str = bar_to_pressure_str(pstd)

    ic_table = registry.get('ic_files', {}).get(config_type, {})
    if pressure_str not in ic_table:
        raise ValueError(
            f"No IC file entry for {config_type} / {pressure_str} in config_registry.yaml. "
            f"Add it or use ncdata_override."
        )
    level_table = ic_table[pressure_str]
    if nlev not in level_table:
        available = list(level_table.keys())
        raise ValueError(
            f"No IC file for {config_type} / {pressure_str} / L{nlev}. "
            f"Available levels: {available}. Use nlev from that list or add an entry."
        )
    return level_table[nlev], pressure_str


def validate_case(spec, registry):
    """Return list of error strings. Empty list = valid."""
    errors = []

    for field in REQUIRED_FIELDS:
        if field not in spec:
            errors.append(f"missing required field: {field}")

    # IC file lookup
    try:
        ic_file, pressure_str = find_ic_file(spec, registry)
    except ValueError as e:
        errors.append(str(e))
        ic_file, pressure_str = None, None

    # solar file / exort package consistency
    solar = spec.get('exo_solar_file', '')
    exort_pkg = spec.get('exort_pkg', '')
    if solar and exort_pkg and exort_pkg in SOLAR_FILE_STEMS:
        stem = SOLAR_FILE_STEMS[exort_pkg]
        if stem not in os.path.basename(solar):
            errors.append(
                f"solar file '{os.path.basename(solar)}' doesn't match "
                f"exort_pkg='{exort_pkg}' (expected stem '{stem}')"
            )

    # synchronous rotation consistency
    if str(spec.get('do_exo_synchronous', 'false')).lower() == 'true':
        sday = spec.get('exo_sday')
        ndays = spec.get('exo_ndays')
        if sday is not None and ndays is not None:
            expected = 86400.0 * float(ndays)
            if abs(float(sday) - expected) / expected > 0.01:
                errors.append(
                    f"synchronous rotation: exo_sday={sday} but 86400*exo_ndays={expected:.0f}. "
                    f"Set exo_sday = 86400 * exo_ndays for synchronous mode."
                )

    return errors


def _fortran_value(name, value):
    """Format a Python value as a Fortran parameter RHS."""
    if isinstance(value, bool) or str(value).lower() in ('true', 'false'):
        v = str(value).lower()
        return f'.{v}.'
    if isinstance(value, int) and name == 'exo_rad_step':
        return str(value)
    try:
        f = float(value)
        # use scientific notation for very small/large values
        if abs(f) != 0 and (abs(f) < 1e-3 or abs(f) >= 1e8):
            s = f"{f:.6e}"
        else:
            # ensure there's always a decimal point so Fortran parses as real
            s = f"{f:g}"
            if '.' not in s and 'e' not in s:
                s += '.0'
        return f"{s}_r8"
    except (ValueError, TypeError):
        return str(value)


def render_exoplanet_mod(template_path, spec):
    """
    Read exoplanet_mod.F90 template, substitute values from spec.
    Returns modified file content as string.
    Only touches active (uncommented) parameter lines for params in EXO_PARAMS.
    The derived constants block is passed through unchanged.
    """
    n2bar = compute_n2bar(spec)
    substitutions = {}
    for k, v in spec.items():
        if k in EXO_PARAMS:
            substitutions[k] = v
    if n2bar is not None:
        # we don't rewrite the n2bar expression line; leave it as-is since
        # it's derived from the other gas values we set. The Fortran compiler
        # recalculates it at compile time.
        pass

    lines_out = []
    with open(template_path) as f:
        for line in f:
            stripped = line.lstrip()
            if stripped.startswith('!') or not stripped.strip():
                lines_out.append(line)
                continue

            m = _RE_PARAM_LINE.match(line)
            if m:
                param_name = m.group(2)
                if param_name in substitutions:
                    prefix   = m.group(1)   # type declaration + '::'
                    spaces   = m.group(3)   # ' = '
                    old_rhs  = m.group(4)
                    suffix   = m.group(5)   # inline comment (!! ...) or empty

                    new_val = _fortran_value(param_name, substitutions[param_name])
                    # preserve spacing before comment
                    lines_out.append(f"{prefix}{param_name}{spaces}{new_val}{suffix}\n")
                    continue

            lines_out.append(line)

    return ''.join(lines_out)


def generate_shell_script(case_name, spec, registry, ic_file, outdir, staging_dir):
    """Write <outdir>/<case_name>_build.sh. Return the script path."""
    paths = dict(registry.get('paths', {}))
    # per-matrix path overrides (passed in via spec)
    for k in ['cesm_scripts', 'caseroot', 'exocam_root', 'exort_root']:
        if k in spec.get('_paths_override', {}):
            paths[k] = spec['_paths_override'][k]

    config_type = spec['config_type']
    cfg = registry.get('cesm_config', {}).get(config_type, {})

    exort_pkg   = spec['exort_pkg']
    nlev        = spec['nlev']
    phys        = cfg.get('phys', 'cam4')
    cloud_opts  = '-chem none -microphys mg1' if spec.get('cloud_scheme') == 'mg' else ''
    pstd        = compute_pstd_from_spec(spec)

    # solar file: use spec override, or build default from registry
    solar_file = spec.get('exo_solar_file', '')
    if not solar_file:
        stem = SOLAR_FILE_STEMS.get(exort_pkg, exort_pkg)
        solar_file = f"{paths.get('exort_root','$EXORT')}/data/solar/G2V_SUN_{stem}.nc"

    ic_path = (f"{paths.get('exocam_root','$EXOCAM')}/cesm1.2.1/initial_files"
               f"/{config_type}/{ic_file}")

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    script_path = os.path.join(outdir, f"{case_name}_build.sh")

    lines = [
        "#!/bin/bash",
        f"# ExoCAM build script: {case_name}",
        f"# Generated: {now} by exo_build.py",
        f"# VALIDATION: pstd={pstd:.4g}bar | ncdata={ic_file} | nlev={nlev} | exort={exort_pkg}",
        "#",
        "# Review this script before running.",
        "# To run:  bash " + os.path.basename(script_path),
        "# To check syntax only:  bash -n " + os.path.basename(script_path),
        "",
        "set -e   # exit on first error",
        "set -x   # print every command before executing (the log)",
        "",
        f"CASE={case_name}",
        f"CESM_SCRIPTS={paths.get('cesm_scripts', 'EDIT_ME')}",
        f"CASEROOT={paths.get('caseroot', 'EDIT_ME')}",
        f"EXOCAM={paths.get('exocam_root', 'EDIT_ME')}",
        f"EXORT={paths.get('exort_root', 'EDIT_ME')}",
        f"STAGING={os.path.abspath(staging_dir)}",
        f"CONFIG_TYPE={config_type}",
        "",
        "# -----------------------------------------------------------",
        "# STEP 1: create case",
        "# -----------------------------------------------------------",
        "cd ${CESM_SCRIPTS}",
        (f"./create_newcase -case ${{CASEROOT}}/${{CASE}}"
         f" -res {cfg.get('res','EDIT_ME')}"
         f" -mach {spec['mach']}"
         f" -compset {cfg.get('compset','EDIT_ME')}"),
        "",
        "# -----------------------------------------------------------",
        "# STEP 2: copy ExoCAM SourceMods and namelists",
        "# -----------------------------------------------------------",
        "cd ${CASEROOT}/${CASE}",
        "cp -r ${EXOCAM}/cesm1.2.1/configs/${CONFIG_TYPE}/SourceMods .",
        "cp    ${EXOCAM}/cesm1.2.1/configs/${CONFIG_TYPE}/namelist_files/* .",
        "",
        "# -----------------------------------------------------------",
        "# STEP 3: install modified exoplanet_mod.F90 and update paths",
        "# -----------------------------------------------------------",
        "cp ${STAGING}/exoplanet_mod.F90 SourceMods/src.share/exoplanet_mod.F90",
        "",
        "# Update ncdata path in user_nl_cam",
        f"sed -i \"s|ncdata = '.*'|ncdata = '{ic_path}'|\" user_nl_cam",
        "",
        "# Update solar file path in exoplanet_mod.F90",
        f"sed -i \"s|exo_solar_file = '.*'|exo_solar_file = '{solar_file}'|\" "
        "SourceMods/src.share/exoplanet_mod.F90",
        "",
        "# -----------------------------------------------------------",
        "# STEP 4: processor counts",
        "# -----------------------------------------------------------",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ATM -val {spec['ntasks_atm']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ICE -val {spec['ntasks_atm']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_LND -val {spec['ntasks_atm']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_OCN -val {spec['ntasks_atm']}",
        "",
        "# -----------------------------------------------------------",
        "# STEP 5: run length and CAM configuration",
        "# -----------------------------------------------------------",
        f"./xmlchange STOP_OPTION={spec['stop_option']}",
        f"./xmlchange STOP_N={spec['stop_n']}",
        f"./xmlchange REST_N={spec['rest_n']}",
        (f"./xmlchange CAM_CONFIG_OPTS="
         f"\"-nlev {nlev} -phys {phys}"
         + (f" {cloud_opts}" if cloud_opts else "")
         + f" -usr_src ${{EXORT}}/ExoRT/3dmodels/src.cam.{exort_pkg}\""),
        "",
        "# -----------------------------------------------------------",
        "# STEP 6: cesm_setup",
        "# -----------------------------------------------------------",
        "./cesm_setup",
        "",
        "# -----------------------------------------------------------",
        "# STEP 7: branch restart files (uncomment if needed)",
        "# -----------------------------------------------------------",
        "# cp /path/to/restart/files ${CASEROOT}/${CASE}/run/",
        "",
        "# -----------------------------------------------------------",
        "# STEP 8: build  (submission is always manual)",
        "# -----------------------------------------------------------",
        "./${CASE}.build",
        "",
        "echo \"Build complete: ${CASE}\"",
        "echo \"To submit: cd ${CASEROOT}/${CASE} && ./${CASE}.run\"",
    ]

    with open(script_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.chmod(script_path, 0o755)
    return script_path


def main():
    parser = argparse.ArgumentParser(description='Generate ExoCAM build shell scripts from experiment matrix')
    parser.add_argument('matrix', help='experiment_matrix.yaml')
    parser.add_argument('--outdir', default='scripts', help='Output directory for scripts (default: scripts/)')
    parser.add_argument('--execute', action='store_true',
                        help='Execute generated scripts via bash (default is dry-run)')
    args = parser.parse_args()

    matrix = load_yaml(args.matrix)

    registry_path = matrix.get('config_registry')
    if not registry_path:
        sys.exit("experiment matrix must specify 'config_registry' path")
    if not os.path.exists(registry_path):
        sys.exit(f"config_registry not found: {registry_path}")
    registry = load_yaml(registry_path)

    # matrix-level path overrides
    paths_override = matrix.get('paths', {}) or {}

    base = matrix.get('base', {})
    cases = matrix.get('cases', [])

    os.makedirs(args.outdir, exist_ok=True)

    # find exoplanet_mod.F90 template from registry exocam_root
    exocam_root = paths_override.get('exocam_root') or registry.get('paths', {}).get('exocam_root', '')
    template_base = (f"{exocam_root}/cesm1.2.1/configs"
                     if exocam_root else None)

    generated = []
    errors_total = 0

    for case_def in cases:
        case_name = case_def.get('name')
        if not case_name:
            print("WARNING: case missing 'name', skipping", file=sys.stderr)
            continue

        spec = resolve_case(base, case_def)
        spec['_paths_override'] = paths_override

        errors = validate_case(spec, registry)
        if errors:
            print(f"\nERROR: {case_name}")
            for e in errors:
                print(f"  - {e}")
            errors_total += 1
            continue

        ic_file, _ = find_ic_file(spec, registry)

        # staging dir for this case's exoplanet_mod.F90
        staging_dir = os.path.join(args.outdir, 'staging', case_name)
        os.makedirs(staging_dir, exist_ok=True)

        # find template exoplanet_mod.F90 for this config
        config_type = spec['config_type']
        # strip _ne5/_ne16 suffix for se configs — they use the same source dir
        src_config = config_type.replace('_ne5', '').replace('_ne16', '')
        if template_base:
            template_path = os.path.join(
                template_base, src_config,
                'SourceMods', 'src.share', 'exoplanet_mod.F90'
            )
        else:
            template_path = None

        if template_path and os.path.exists(template_path):
            content = render_exoplanet_mod(template_path, spec)
            staged_path = os.path.join(staging_dir, 'exoplanet_mod.F90')
            with open(staged_path, 'w') as f:
                f.write(content)
        else:
            print(f"  WARNING: template exoplanet_mod.F90 not found for {config_type}; "
                  f"staging dir will be empty — edit manually")

        script_path = generate_shell_script(
            case_name, spec, registry, ic_file, args.outdir, staging_dir
        )
        generated.append((case_name, script_path))
        print(f"Generated: {script_path}")

    print(f"\n{len(generated)} script(s) generated, {errors_total} case(s) skipped due to errors.")

    if not args.execute:
        print("\nDry-run complete. To execute a script:")
        for name, path in generated:
            print(f"  bash {path}")
        return

    # execute mode
    for case_name, script_path in generated:
        log_path = os.path.join(args.outdir, f"{case_name}.build.log")
        print(f"\nExecuting: {script_path}")
        print(f"  Log: {log_path}")
        with open(log_path, 'w') as log:
            result = subprocess.run(
                ['bash', script_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True
            )
            log.write(result.stdout)
            # also echo to terminal
            for line in result.stdout.splitlines():
                print(f"  {line}")
        if result.returncode != 0:
            print(f"  FAILED (exit {result.returncode}) — see {log_path}")
        else:
            print(f"  OK")


if __name__ == '__main__':
    main()

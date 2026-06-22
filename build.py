#!/usr/bin/env python3
"""
ExoCAM build script generator. Reads an experiment matrix YAML and a config
registry YAML, validates each case, and writes one self-contained shell script
per case. Each script embeds the rendered exoplanet_mod.F90 as an inline
heredoc so no external staging directory is required.
"""

import argparse
import datetime
import os
import random
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("pyyaml is required: pip install pyyaml")

sys.path.insert(0, os.path.dirname(__file__))
from parse_utils import compute_pstd_bar
from manage_utils import submit_case

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
    'do_exo_rt_clearsky', 'do_exo_rt_spectral', 'do_exo_rt_carma',
    'do_exo_gw', 'do_exo_simplevolc', 'do_carma_exort',
    'Tmax', 'swFluxLimit', 'lwFluxLimit',
}

REQUIRED_FIELDS = ['config_type', 'exort_pkg', 'nlev', 'mach',
                   'stop_option', 'stop_n', 'rest_option', 'rest_n', 'resubmit', 'ntasks']
# Fields required for clone mode (config/compset/mach are inherited from the source case)
REQUIRED_FIELDS_CLONE = ['clone', 'stop_option', 'stop_n', 'rest_option', 'rest_n', 'resubmit', 'ntasks']

SOLAR_FILE_STEMS = {
    'n68equiv':   'n68',
    'n84equiv':   'n84',
    'n28archean': 'n28',
    'n42h2o':     'n42',
}

# --- Type verification (build.py generate --verify) -------------------------
# Authoritative type tags for matrix values, checked by --verify. Keys not
# listed here are not type-checked. 'bool' accepts python bool or the strings
# true/false (any case); 'int' accepts ints or integral-valued strings; 'real'
# accepts any numeric (int or float); 'str' must not be numeric/bool.
PARAM_TYPES = {
    # exoplanet_mod reals (gas bars, geophysical, orbital, RT tuning)
    'exo_co2bar': 'real', 'exo_ch4bar': 'real', 'exo_c2h6bar': 'real',
    'exo_nh3bar': 'real', 'exo_cobar': 'real', 'exo_h2bar': 'real',
    'exo_o2bar': 'real', 'exo_n2bar_explicit': 'real',
    'exo_surface_gravity': 'real', 'exo_planet_radius': 'real',
    'exo_ndays': 'real', 'exo_porb': 'real', 'exo_sday': 'real',
    'exo_scon': 'real', 'exo_eccen': 'real', 'exo_obliq': 'real',
    'exo_mvelp': 'real', 'exo_ve': 'real',
    'exo_convect_plim': 'real', 'exo_albdif': 'real', 'exo_albdir': 'real',
    'Tmax': 'real', 'swFluxLimit': 'real', 'lwFluxLimit': 'real',
    # exoplanet_mod ints
    'exo_rad_step': 'int',
    # exoplanet_mod logicals
    'do_exo_synchronous': 'bool', 'do_exo_rt': 'bool', 'do_exo_atmconst': 'bool',
    'do_exo_rt_clearsky': 'bool', 'do_exo_rt_spectral': 'bool',
    'do_exo_rt_carma': 'bool', 'do_exo_gw': 'bool', 'do_exo_simplevolc': 'bool',
    'do_carma_exort': 'bool',
    # matrix run/control fields
    'nlev': 'int', 'stop_n': 'int', 'rest_n': 'int', 'resubmit': 'int',
    'ntasks': 'int',
    'config_type': 'str', 'exort_pkg': 'str', 'mach': 'str',
    'stop_option': 'str', 'rest_option': 'str', 'clone': 'str',
    'run_type': 'str', 'run_refcase': 'str', 'run_refdate': 'str',
}


def _check_type(value, type_tag):
    """Return None if value matches type_tag, else a short reason string."""
    if type_tag == 'bool':
        if isinstance(value, bool):
            return None
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            return None
        return f"expected boolean, got {type(value).__name__} {value!r}"
    if type_tag == 'int':
        if isinstance(value, bool):
            return f"expected int, got boolean {value!r}"
        if isinstance(value, int):
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return f"expected int, got {value!r}"
        if f != int(f):
            return f"expected int, got non-integral {value!r}"
        return None
    if type_tag == 'real':
        if isinstance(value, bool):
            return f"expected real, got boolean {value!r}"
        if isinstance(value, (int, float)):
            return None
        try:
            float(value)
        except (TypeError, ValueError):
            return f"expected real, got {value!r}"
        return None
    if type_tag == 'str':
        if isinstance(value, bool):
            return f"expected string, got boolean {value!r}"
        if isinstance(value, (int, float)):
            return f"expected string, got numeric {value!r}"
        return None
    return None


def _expand_local_path(path):
    """Expand $VARS and ~ in a path. Return (expanded, resolvable) where
    resolvable is False if the result still contains an unexpanded shell var
    (e.g. $EXOCAM not set in the local env) — such paths point at the HPC and
    cannot be existence-checked locally."""
    expanded = os.path.expandvars(os.path.expanduser(path))
    return expanded, '$' not in expanded


# NetCDF file fields and how each resolves to a path, mirroring the build
# blocks. (field, resolver) where resolver(value, spec, paths) -> path str.
def _resolve_ncdata_field(value, spec, paths):
    return resolve_ic_path(value, spec.get('config_type', ''), paths)


def _resolve_clm_field(value, spec, paths):
    if value.startswith('/'):
        return value
    exocam = paths.get('exocam_root', '$EXOCAM')
    return f"{exocam}/cesm1.2.1/initial_files/cam_land_fv/{value}"


def _resolve_verbatim_field(value, spec, paths):
    # exo_solar_file and som_pop_frc_file are used verbatim by the build blocks.
    return value


# (field, resolver, restrict_config_types or None for all)
NCFILE_FIELDS = [
    ('ncdata',          _resolve_ncdata_field,   None),
    ('exo_solar_file',  _resolve_verbatim_field, None),
    ('som_pop_frc_file', _resolve_verbatim_field,
     ('cam_aqua_fv', 'cam_aqua_se_ne5', 'cam_aqua_se_ne16', 'cam_mixed_fv')),
    ('finidat', _resolve_clm_field, ('cam_land_fv', 'cam_mixed_fv')),
    ('fsurdat', _resolve_clm_field, ('cam_land_fv', 'cam_mixed_fv')),
]


def verify_case(spec, registry, paths):
    """Coherency check for a single resolved case spec.

    Checks (no geophysical/scientific validation):
      1. Type tags: every matrix value with a PARAM_TYPES entry matches its type.
      2. NetCDF existence: every nc-file field resolves to an existing file.
         --verify is intended to run on the HPC, where every input file should
         live, so a var-free path whose file (or directory) is absent is a hard
         FAILURE. Only paths that still contain an unexpanded $VAR (env var not
         set) are SKIPPED — those genuinely can't be checked.

    Returns (errors, notes): both lists of strings. errors are hard failures;
    notes are informational (skipped/unresolvable file checks).
    """
    errors = []
    notes = []

    # 1. Type checks
    for key, type_tag in PARAM_TYPES.items():
        if key not in spec:
            continue
        reason = _check_type(spec[key], type_tag)
        if reason:
            errors.append(f"type: {key}: {reason}")

    # 2. NetCDF file existence
    config_type = spec.get('config_type', '')
    for field, resolver, restrict in NCFILE_FIELDS:
        if field not in spec or not spec[field]:
            continue
        if restrict is not None and config_type not in restrict:
            # field present but irrelevant for this config — note it, don't check
            notes.append(
                f"nc: {field} present but config_type={config_type or '?'} "
                f"does not use it (ignored at build time)")
            continue
        raw = resolver(str(spec[field]), spec, paths)
        local, resolvable = _expand_local_path(raw)
        if not resolvable:
            notes.append(f"nc: {field}: SKIPPED (unresolved path, runs on HPC): {raw}")
            continue
        # --verify is intended to run on the HPC, where every input file should be
        # present. A var-free path whose file isn't there is a broken path that
        # needs fixing — a hard failure, whether the parent dir is missing or the
        # file alone is. (A missing parent dir is the more common symptom of a
        # mistyped/transposed path.)
        if not os.path.isfile(local):
            parent = os.path.dirname(local)
            if parent and not os.path.isdir(parent):
                errors.append(f"nc: {field}: directory not found: {local}")
            else:
                errors.append(f"nc: {field}: file not found: {local}")

    return errors, notes


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


def resolve_ic_path(ic_file, config_type, paths):
    """
    Resolve an IC file reference to a full ncdata path.

    A bare filename (from the registry ic_files table) is placed under the
    config-type IC directory. An explicit ncdata value that is already an
    absolute path (or contains a directory component) is used verbatim —
    prepending the base dir would mangle it into a double path.
    """
    if ic_file.startswith('/') or '/' in ic_file:
        return ic_file
    return (f"{paths.get('exocam_root', '$EXOCAM')}/cesm1.2.1/initial_files"
            f"/{config_type}/{ic_file}")


def find_ic_file(spec, registry):
    """
    Look up IC filename in registry.ic_files[config_type][pressure_str][nlev].
    Returns (ic_filename, pressure_str) or raises ValueError.
    """
    if 'ncdata' in spec:
        return spec['ncdata'], None

    config_type = spec['config_type']
    nlev = int(spec['nlev'])
    pstd = compute_pstd_from_spec(spec)
    pressure_str = bar_to_pressure_str(pstd)

    ic_table = registry.get('ic_files', {}).get(config_type, {})
    if pressure_str not in ic_table:
        raise ValueError(
            f"No IC file entry for {config_type} / {pressure_str} in config_registry.yaml. "
            f"Add it or use ncdata."
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

    if spec.get('clone'):
        for field in REQUIRED_FIELDS_CLONE:
            if field not in spec:
                errors.append(f"missing required field: {field}")
        # IC file lookup still runs if enough info is present (nlev + config_type inherited)
        # but is optional for clone mode — skip if config_type or nlev are absent
        if spec.get('config_type') and spec.get('nlev'):
            try:
                find_ic_file(spec, registry)
            except ValueError as e:
                errors.append(str(e))
    else:
        for field in REQUIRED_FIELDS:
            if field not in spec:
                errors.append(f"missing required field: {field}")

        # IC file lookup
        try:
            find_ic_file(spec, registry)
        except ValueError as e:
            errors.append(str(e))

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

    # exort_pkg asterisk: custom RT copied into SourceMods — create_newcase cannot replicate.
    # Clone mode is exempt: RT source is inherited from the clone source, not via -usr_src.
    if spec.get('exort_pkg', '').endswith('*') and not spec.get('clone'):
        pkg = spec['exort_pkg']
        errors.append(
            f"exort_pkg='{pkg}': the '*' suffix indicates custom RT source copied into "
            f"SourceMods of the originating case. create_newcase cannot replicate this. "
            f"Use clone mode (add 'clone: <source_case>' to base) or manually strip '*' "
            f"and copy RT files into SourceMods after case creation."
        )

    # branch/hybrid: run_refcase and run_refdate required
    if spec.get('run_type') in ('branch', 'hybrid'):
        for field in ('run_refcase', 'run_refdate'):
            if not spec.get(field):
                errors.append(f"missing required field for run_type={spec['run_type']}: {field}")

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
            s = f"{f:.10e}"
        else:
            # ensure there's always a decimal point so Fortran parses as real.
            # 12 sig figs preserves full input precision (matters for exo_n2bar,
            # the pressure-balancing fill gas) without float noise.
            s = f"{f:.12g}"
            if '.' not in s and 'e' not in s:
                s += '.0'
        return f"{s}_r8"
    except (ValueError, TypeError):
        return str(value)


# Radiatively-active gas bar params (excludes N2, which is the filling gas).
GAS_BAR_PARAMS = (
    'exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_nh3bar', 'exo_cobar',
    'exo_h2bar', 'exo_o2bar',
)


def render_exoplanet_mod(template_path, spec, is_clone=False):
    """
    Read exoplanet_mod.F90 template, substitute values from spec.
    Returns modified file content as string.
    Only touches active (uncommented) parameter lines for params in EXO_PARAMS.
    The derived constants block is passed through unchanged.

    Newcase (is_clone=False): start from a clean slate — every radiatively-active
    gas not named in the matrix is forced to 0.0 (the template's modern-Earth
    defaults, e.g. exo_o2bar=0.2095, must not leak in), and N2 is always emitted
    as an explicit numeric fill = target_pressure - sum(specified gases).

    Clone (is_clone=True): preserve the source case's composition — only the gas
    params named in the matrix are substituted; unspecified gases and N2 keep
    whatever the clone-source template has.
    """
    substitutions = {}
    for k, v in spec.items():
        if k in EXO_PARAMS:
            substitutions[k] = v

    if is_clone:
        # Only patch exo_n2bar when explicitly set (high-pressure atmospheres).
        # For <=1 bar cases the Fortran expression line is correct as-is.
        n2bar = compute_n2bar(spec)
        if 'exo_n2bar_explicit' in spec and n2bar is not None:
            substitutions['exo_n2bar'] = n2bar
    else:
        # Clean slate: zero every unspecified gas, then fill with explicit N2.
        for gas in GAS_BAR_PARAMS:
            substitutions.setdefault(gas, 0.0)
        if 'exo_n2bar_explicit' in spec:
            substitutions['exo_n2bar'] = float(spec['exo_n2bar_explicit'])
        else:
            target = compute_pstd_from_spec(spec)
            specified = sum(float(spec.get(g, 0.0)) for g in GAS_BAR_PARAMS)
            substitutions['exo_n2bar'] = target - specified

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


def _build_nl_append_block(spec):
    """
    Return shell lines to append carma_params, volc_params, and/or nl_cam_params
    to user_nl_cam (and cice_params to user_nl_cice) via echo >>. Returns an
    empty list if none are present.
    Used by generate_shell_script (newcase path) where the namelist is a fresh
    template that never contains these entries, so plain append is correct.
    """
    lines = []
    for group_key in ('carma_params', 'volc_params', 'nl_cam_params'):
        params = spec.get(group_key)
        if params:
            lines.append(f"")
            lines.append(f"# Append {group_key} to user_nl_cam")
            lines.extend(_nl_append_lines(params))
    cice_params = spec.get('cice_params')
    if cice_params:
        lines.append(f"")
        lines.append(f"# Append cice_params to user_nl_cice")
        lines.extend(_nl_append_lines(cice_params, target='user_nl_cice'))
    return lines


def _nl_upsert_lines(param_dict, target='user_nl_cam'):
    """
    Return shell lines that upsert key = value entries into a namelist file
    (default user_nl_cam; pass target='user_nl_cice' etc. for others).
    For each key: replace the existing line if present, otherwise append.
    Used by generate_clone_script because create_clone copies the namelist
    verbatim from the source case, so appending a key that already exists
    would create duplicate entries.
    """
    lines = []
    for key, val in param_dict.items():
        nl_val = _format_nl_value(val)
        escaped_val = nl_val.replace('|', r'\|')
        # Match the key at start-of-line, allowing leading whitespace and any
        # whitespace around '=', and rewrite the whole line. Anchoring on the
        # key (^[[:space:]]*KEY[[:space:]]*=) avoids matching a different key
        # that merely contains this one as a substring, and tolerates source
        # formatting (extra spaces/tabs, trailing inline comments).
        pat = f'^[[:space:]]*{key}[[:space:]]*='
        lines.append(
            f'if grep -qE "{pat}" {target}; then '
            f'sed -i -E "s|{pat}.*|{key} = {escaped_val}|" {target}; '
            f'else echo "{key} = {nl_val}" >> {target}; fi'
        )
    return lines


def _build_nl_upsert_block(spec):
    """
    Return shell lines to upsert carma_params, volc_params, and/or nl_cam_params
    into user_nl_cam (and cice_params into user_nl_cice) using replace-or-append
    semantics.
    Used by generate_clone_script where the namelist already contains entries
    inherited from the clone source.
    """
    lines = []
    for group_key in ('carma_params', 'volc_params', 'nl_cam_params'):
        params = spec.get(group_key)
        if params:
            lines.append(f"")
            lines.append(f"# Upsert {group_key} in user_nl_cam")
            lines.extend(_nl_upsert_lines(params))
    cice_params = spec.get('cice_params')
    if cice_params:
        lines.append(f"")
        lines.append(f"# Upsert cice_params in user_nl_cice")
        lines.extend(_nl_upsert_lines(cice_params, target='user_nl_cice'))
    return lines


def _format_nl_value(val):
    """Format a Python value as a CESM namelist RHS (for use inside echo "...")."""
    # bool must be checked before int (bool is a subclass of int in Python)
    if isinstance(val, bool):
        return '.true.' if val else '.false.'
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        s = f'{val:g}'
        if '.' not in s and 'e' not in s:
            s += '.0'
        return s
    # str: pass through Fortran logicals unquoted; try numeric coercion; otherwise single-quote
    s = str(val)
    if s.lower() in ('.true.', '.false.'):
        return s
    try:
        f = float(s)
        formatted = f'{f:g}'
        if '.' not in formatted and 'e' not in formatted:
            formatted += '.0'
        return formatted
    except ValueError:
        pass
    # Genuine string (file path etc.) — single-quote it
    return f"'{s.replace(chr(39), chr(92) + chr(39))}'"


def _nl_append_lines(param_dict, target='user_nl_cam'):
    """
    Return a list of shell lines that append key = value entries to a namelist
    file (default user_nl_cam; pass target='user_nl_cice' etc. for others).
    Type dispatch via _format_nl_value:
    - bool        -> .true. / .false.  (unquoted Fortran logical)
    - int/float   -> bare number       (no quotes)
    - str logical -> .true. / .false.  (unquoted, passed through)
    - str numeric -> bare number       (unquoted, coerced)
    - str other   -> 'value'           (single-quoted, e.g. file paths)
    """
    lines = []
    for key, val in param_dict.items():
        nl_val = _format_nl_value(val)
        lines.append(f'echo "{key} = {nl_val}" >> {target}')
    return lines


def _build_clm_update_block(spec, paths):
    """
    For land/mixed configs, return sed lines to update finidat and fsurdat
    in user_nl_clm if those keys are present in the spec.
    """
    if spec.get('config_type') not in ('cam_land_fv', 'cam_mixed_fv'):
        return []
    lines = []
    exocam = paths.get('exocam_root', '$EXOCAM')
    land_ic_base = f"{exocam}/cesm1.2.1/initial_files/cam_land_fv"
    for key in ('finidat', 'fsurdat'):
        val = spec.get(key)
        if val:
            # allow absolute paths through unchanged; prefix relative names
            path_val = val if val.startswith('/') else f"{land_ic_base}/{val}"
            if not lines:
                lines.append("")
                lines.append("# Update CLM initial and surface data file paths in user_nl_clm")
            lines.append(
                f'sed -i \'s|{key} = ".*"|{key} = "{path_val}"|\' user_nl_clm'
            )
    return lines


def _build_run_script_block(spec):
    """
    Return shell lines to patch SBATCH directives into ${CASE}.run after cesm_setup.
    account: upsert — replace existing #SBATCH --account= line, or append if absent.
    job_name (-J): upsert — replace existing #SBATCH -J line, or append if absent.
    Both are skipped if absent from the spec.
    """
    lines = []
    account  = spec.get('account')
    job_name = spec.get('job_name')
    if not account and not job_name:
        return lines
    lines += [
        "",
        "# -----------------------------------------------------------",
        "# Patch SBATCH directives in ${CASE}.run",
        "# -----------------------------------------------------------",
    ]
    if account:
        lines.append(
            f"if grep -q '^#SBATCH --account=' ${{CASE}}.run; then\n"
            f"    sed -i 's|^#SBATCH --account=.*|#SBATCH --account={account}|' ${{CASE}}.run\n"
            f"else\n"
            f"    sed -i '0,/^#SBATCH /s|^#SBATCH |#SBATCH --account={account}\\n#SBATCH |' ${{CASE}}.run\n"
            f"fi"
        )
    if job_name:
        lines.append(
            f"if grep -q '^#SBATCH -J ' ${{CASE}}.run; then\n"
            f"    sed -i 's|^#SBATCH -J .*|#SBATCH -J {job_name}|' ${{CASE}}.run\n"
            f"else\n"
            f"    sed -i '0,/^#SBATCH /s|^#SBATCH |#SBATCH -J {job_name}\\n#SBATCH |' ${{CASE}}.run\n"
            f"fi"
        )
    return lines


def _build_usr_src_fix_block():
    """
    Return shell lines to update -usr_src in CAM_CONFIG_OPTS so it points to
    this case's own SourceMods/src.cam/ rather than the clone source's path.

    Only emitted when exort_pkg ends with '*', meaning the RT source was copied
    into the clone source's SourceMods and create_clone inherited that -usr_src
    path verbatim.

    xmlquery returns 'env_build.xml: CAM_CONFIG_OPTS = <value>'; sed
    's/^[^=]*= //' strips the prefix to get the bare value. The new -usr_src
    path is inlined directly into the sed replacement string with double quotes
    so the shell expands $CASEROOT/$CASE/$USR_SRC_DIR at runtime before sed
    sees the string. xmlchange then receives a fully-expanded plain path.
    Note: CESM 1.2.1's xmlquery does not support --value.
    """
    return [
        "",
        "# -----------------------------------------------------------",
        "# Fix stale -usr_src in CAM_CONFIG_OPTS (custom RT clone)",
        "# create_clone inherited -usr_src pointing to the source case;",
        "# update it to point to this case's own SourceMods directory.",
        "# xmlquery output: 'env_build.xml: CAM_CONFIG_OPTS = <value>'",
        "# sed strips everything up to and including the first ' = '.",
        "# $CASEROOT and $CASE are set by CESM before this script runs.",
        "# -----------------------------------------------------------",
        "OLD_CAM_OPTS=$(./xmlquery CAM_CONFIG_OPTS | sed 's/^[^=]*= //')",
        "OLD_USR_SRC=$(echo \"${OLD_CAM_OPTS}\" | grep -oP '(?<=-usr_src )\\S+')",
        "USR_SRC_DIR=$(basename \"${OLD_USR_SRC}\")",
        "NEW_CAM_OPTS=$(echo \"${OLD_CAM_OPTS}\" | sed \"s|-usr_src [^ ]*|-usr_src ${CASEROOT}/${CASE}/SourceMods/src.cam/${USR_SRC_DIR}|\")",
        "./xmlchange CAM_CONFIG_OPTS=\"${NEW_CAM_OPTS}\"",
    ]


def _build_docn_update_block(spec):
    """
    For aqua/mixed configs, return a sed line to update the pop_frc* filename
    in user_docn.streams.txt.som if som_pop_frc_file is present in the spec.
    """
    if spec.get('config_type') not in ('cam_aqua_fv', 'cam_aqua_se_ne5',
                                        'cam_aqua_se_ne16', 'cam_mixed_fv'):
        return []
    val = spec.get('som_pop_frc_file')
    if not val:
        return []
    dirname  = os.path.dirname(val)
    basename = os.path.basename(val)
    return [
        "",
        "# Update SOM ocean forcing file in user_docn.streams.txt.som",
        f"sed -i 's|<filePath>.*</filePath>|<filePath>\\n            {dirname}\\n         </filePath>|2' "
        "user_docn.streams.txt.som",
        f"sed -i 's|<fileNames>.*pop_frc.*</fileNames>|<fileNames>\\n            {basename}\\n         </fileNames>|' "
        "user_docn.streams.txt.som",
    ]


def _heredoc_exoplanet_mod(exoplanet_mod_content):
    """
    Return shell lines that write exoplanet_mod.F90 inline via heredoc,
    or a comment if the content is None (template was not found).
    """
    if exoplanet_mod_content is None:
        return [
            "# WARNING: exoplanet_mod.F90 template not found at generation time.",
            "# Install it manually before building:",
            "# cp /path/to/exoplanet_mod.F90 SourceMods/src.share/exoplanet_mod.F90",
        ]
    lines = ["cat > SourceMods/src.share/exoplanet_mod.F90 << 'EXOPLANET_MOD_EOF'"]
    lines.extend(exoplanet_mod_content.splitlines())
    lines.append("EXOPLANET_MOD_EOF")
    return lines


def _branch_var_block(spec):
    """Return shell variable lines for branch/hybrid cases (RUN_REFCASE, RUN_REFDATE, RUN_REFDIR).

    RUN_REFDATE is YYYY-MM-DD (from env_run.xml), but CESM names restart directories
    YYYY-MM-DD-SSSSS (date + seconds). The seconds field is always 00000, so -00000
    is appended unconditionally when constructing RUN_REFDIR.
    """
    return [
        f"RUN_REFCASE={spec['run_refcase']}",
        f"RUN_REFDATE={spec['run_refdate']}",
        "# Set RUN_REFDIR to the location of the reference restart files:",
        "#   active refcase:   ${ARCHIVE}/${RUN_REFCASE}/rest/${RUN_REFDATE}-00000",
        "#   retired refcase:  ${LONG_TERM}/${RUN_REFCASE}/rest/${RUN_REFDATE}-00000",
        "RUN_REFDIR=${ARCHIVE}/${RUN_REFCASE}/rest/${RUN_REFDATE}-00000",
    ]


def _build_branch_pre_setup(spec):
    """Lines to insert before cesm_setup for branch/hybrid cases."""
    if spec.get('run_type') not in ('branch', 'hybrid'):
        return []
    return [
        "# -----------------------------------------------------------",
        "# cesm_setup requires RUN_TYPE=startup; switch to branch after",
        "# -----------------------------------------------------------",
        "./xmlchange RUN_TYPE=startup",
    ]


def _build_branch_post_setup(spec, paths):
    """Lines to insert after cesm_setup (and SBATCH patches) for branch/hybrid cases."""
    run_type = spec.get('run_type')
    if run_type not in ('branch', 'hybrid'):
        return []
    retain = spec.get('brnch_retain_casename', 'false').upper()
    rundir = paths.get('rundir', 'EDIT_ME')
    return [
        "",
        "# -----------------------------------------------------------",
        "# copy restart files to rundir",
        "# -----------------------------------------------------------",
        f"cp ${{RUN_REFDIR}}/* {rundir}/${{CASE}}/run",
        "",
        "# -----------------------------------------------------------",
        "# apply branch/hybrid configuration",
        "# -----------------------------------------------------------",
        f"./xmlchange RUN_TYPE={run_type}",
        "./xmlchange CONTINUE_RUN=FALSE",
        f"./xmlchange BRNCH_RETAIN_CASENAME={retain}",
        "./xmlchange RUN_REFCASE=${RUN_REFCASE}",
        "./xmlchange RUN_REFDATE=${RUN_REFDATE}",
    ]


def generate_shell_script(case_name, spec, registry, ic_file, outdir, exoplanet_mod_content):
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

    ic_path = resolve_ic_path(ic_file, config_type, paths)

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    script_path = os.path.join(outdir, f"{case_name}_build.sh")

    lines = [
        "#!/bin/bash",
        f"# ExoCAM build script: {case_name}",
        f"# Generated: {now} by build.py",
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
        f"RUNDIR={paths.get('rundir', 'EDIT_ME')}",
        f"ARCHIVE={paths.get('archive', 'EDIT_ME')}",
        f"LONG_TERM={paths.get('long_term', 'EDIT_ME')}",
        f"CONFIG_TYPE={config_type}",
        f"EXORT_PKG={exort_pkg}",
        *(_branch_var_block(spec) if spec.get('run_type') in ('branch', 'hybrid') else []),
        "",
        "# Guard: exort_pkg with '*' suffix indicates custom RT — cannot build via create_newcase",
        "if [[ \"$EXORT_PKG\" == *\\* ]]; then",
        "  echo \"ERROR: EXORT_PKG='$EXORT_PKG' contains '*' — custom RT source cannot be used with create_newcase.\"",
        "  echo \"Use clone mode or manually copy RT files into SourceMods.\"",
        "  exit 1",
        "fi",
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
        *_heredoc_exoplanet_mod(exoplanet_mod_content),
        "",
        "# Update ncdata path in user_nl_cam",
        f"sed -i \"s|ncdata = '.*'|ncdata = '{ic_path}'|\" user_nl_cam",
        *_build_nl_append_block(spec),
        *_build_clm_update_block(spec, paths),
        *_build_docn_update_block(spec),
        "",
        "# Update solar file path in exoplanet_mod.F90",
        f"sed -i \"s|exo_solar_file = '.*'|exo_solar_file = '{solar_file}'|\" "
        "SourceMods/src.share/exoplanet_mod.F90",
        "",
        "# -----------------------------------------------------------",
        "# STEP 4: processor counts",
        "# -----------------------------------------------------------",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ATM -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_LND -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ICE -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_OCN -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_GLC -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ROF -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_WAV -val {spec['ntasks']}",
        "",
        "# -----------------------------------------------------------",
        "# STEP 5: run length and CAM configuration",
        "# -----------------------------------------------------------",
        f"./xmlchange STOP_OPTION={spec['stop_option']}",
        f"./xmlchange STOP_N={spec['stop_n']}",
        f"./xmlchange REST_OPTION={spec['rest_option']}",
        f"./xmlchange REST_N={spec['rest_n']}",
        f"./xmlchange RESUBMIT={spec['resubmit']}",
        *([ f"./xmlchange RUN_STARTDATE={spec['run_startdate']}" ]
          if spec.get('run_startdate') else []),
        (f"./xmlchange CAM_CONFIG_OPTS="
         f"\"-nlev {nlev} -phys {phys}"
         + (f" {cloud_opts}" if cloud_opts else "")
         + f" -usr_src ${{EXORT}}/3dmodels/src.cam.{exort_pkg}\""),
        "",
        *_build_branch_pre_setup(spec),
        "# -----------------------------------------------------------",
        "# STEP 6: cesm_setup",
        "# -----------------------------------------------------------",
        "./cesm_setup",
        *_build_run_script_block(spec),
        *_build_branch_post_setup(spec, paths),
        "",
        "# -----------------------------------------------------------",
        "# STEP 7: build  (submission is always manual)",
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


def generate_clone_script(case_name, spec, registry, ic_file, outdir, exoplanet_mod_content):
    """
    Write <outdir>/<case_name>_build.sh using create_clone instead of create_newcase.
    Steps 3-8 are identical to generate_shell_script; Steps 1-2 are replaced by
    create_clone + cd into the new case (SourceMods and namelists are inherited).
    """
    paths = dict(registry.get('paths', {}))
    for k in ['cesm_scripts', 'caseroot', 'exocam_root', 'exort_root']:
        if k in spec.get('_paths_override', {}):
            paths[k] = spec['_paths_override'][k]

    clone_of    = spec['clone']
    config_type = spec.get('config_type', '')
    exort_pkg   = spec.get('exort_pkg', '')
    nlev        = spec.get('nlev', '?')
    pstd        = compute_pstd_from_spec(spec) if config_type else None

    # solar_file: only override if explicitly set in spec — never construct a default for clones
    solar_file = spec.get('exo_solar_file', '')

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    script_path = os.path.join(outdir, f"{case_name}_build.sh")

    pstd_label = f"{pstd:.4g}bar" if pstd else "inherited"
    ic_label   = ic_file if ic_file else "inherited"

    lines = [
        "#!/bin/bash",
        f"# ExoCAM clone build script: {case_name}",
        f"# Cloned from: {clone_of}",
        f"# Generated: {now} by build.py",
        f"# VALIDATION: pstd={pstd_label} | ncdata={ic_label} | nlev={nlev} | exort={exort_pkg}",
        "#",
        "# Review this script before running.",
        "# To run:  bash " + os.path.basename(script_path),
        "# To check syntax only:  bash -n " + os.path.basename(script_path),
        "",
        "set -e   # exit on first error",
        "set -x   # print every command before executing (the log)",
        "",
        f"CASE={case_name}",
        f"CLONE_OF={clone_of}",
        f"CESM_SCRIPTS={paths.get('cesm_scripts', 'EDIT_ME')}",
        f"CASEROOT={paths.get('caseroot', 'EDIT_ME')}",
        f"EXOCAM={paths.get('exocam_root', 'EDIT_ME')}",
        f"EXORT={paths.get('exort_root', 'EDIT_ME')}",
        f"RUNDIR={paths.get('rundir', 'EDIT_ME')}",
        f"ARCHIVE={paths.get('archive', 'EDIT_ME')}",
        f"LONG_TERM={paths.get('long_term', 'EDIT_ME')}",
        *(_branch_var_block(spec) if spec.get('run_type') in ('branch', 'hybrid') else []),
        "",
        "# -----------------------------------------------------------",
        "# STEP 1: create clone",
        "# -----------------------------------------------------------",
        "cd ${CESM_SCRIPTS}",
        "./create_clone -clone ${CASEROOT}/${CLONE_OF} -case ${CASEROOT}/${CASE}",
        "",
        "# -----------------------------------------------------------",
        "# STEP 2: install modified exoplanet_mod.F90 and update paths",
        "# -----------------------------------------------------------",
        "cd ${CASEROOT}/${CASE}",
        *_heredoc_exoplanet_mod(exoplanet_mod_content),
        *(_build_usr_src_fix_block() if exort_pkg.endswith('*') else []),
    ]

    if ic_file:
        ic_path = resolve_ic_path(ic_file, config_type, paths)
        lines += [
            "",
            "# Update ncdata path in user_nl_cam",
            f"sed -i \"s|ncdata = '.*'|ncdata = '{ic_path}'|\" user_nl_cam",
        ]

    lines += [
        *_build_nl_upsert_block(spec),
        *_build_clm_update_block(spec, paths),
        *_build_docn_update_block(spec),
    ]

    if solar_file:
        lines += [
            "",
            "# Update solar file path in exoplanet_mod.F90",
            f"sed -i \"s|exo_solar_file = '.*'|exo_solar_file = '{solar_file}'|\" "
            "SourceMods/src.share/exoplanet_mod.F90",
        ]

    lines += [
        "",
        "# -----------------------------------------------------------",
        "# STEP 3: processor counts",
        "# -----------------------------------------------------------",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ATM -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_LND -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ICE -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_OCN -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_GLC -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_ROF -val {spec['ntasks']}",
        f"./xmlchange -file env_mach_pes.xml -id NTASKS_WAV -val {spec['ntasks']}",
        "",
        "# -----------------------------------------------------------",
        "# STEP 4: run length and CAM configuration",
        "# -----------------------------------------------------------",
        f"./xmlchange STOP_OPTION={spec['stop_option']}",
        f"./xmlchange STOP_N={spec['stop_n']}",
        f"./xmlchange REST_OPTION={spec['rest_option']}",
        f"./xmlchange REST_N={spec['rest_n']}",
        f"./xmlchange RESUBMIT={spec['resubmit']}",
        *([ f"./xmlchange RUN_STARTDATE={spec['run_startdate']}" ]
          if spec.get('run_startdate') else []),
    ]

    lines += [
        "",
        *_build_branch_pre_setup(spec),
        "# -----------------------------------------------------------",
        "# STEP 5: cesm_setup",
        "# -----------------------------------------------------------",
        "./cesm_setup",
        *_build_run_script_block(spec),
        *_build_branch_post_setup(spec, paths),
        "",
        "# -----------------------------------------------------------",
        "# STEP 6: build  (submission is always manual)",
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


def cmd_generate(args):
    blueprints_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'blueprints')

    if args.list:
        yamls = sorted(f for f in os.listdir(blueprints_dir)
                       if f.endswith('.yaml')) if os.path.isdir(blueprints_dir) else []
        if yamls:
            print('\n'.join(yamls))
        else:
            print(f"No .yaml files found in {blueprints_dir}")
        sys.exit(0)

    if not args.matrix:
        sys.exit("error: the following arguments are required: matrix")

    if not os.path.exists(args.matrix):
        candidate = os.path.join(blueprints_dir, args.matrix)
        if os.path.exists(candidate):
            args.matrix = candidate
        else:
            sys.exit(f"matrix file not found: {args.matrix}\n"
                     f"  also checked: {candidate}")

    matrix = load_yaml(args.matrix)

    registry_path = matrix.get('config_registry')
    if not registry_path:
        sys.exit("experiment matrix must specify 'config_registry' path")
    if not os.path.exists(registry_path):
        sys.exit(f"config_registry not found: {registry_path}")
    registry = load_yaml(registry_path)

    # matrix-level path overrides
    paths_override = matrix.get('paths', {}) or {}

    # Apply registry defaults for run fields not set in the matrix base.
    reg_defaults = registry.get('defaults', {}) or {}
    base = matrix.get('base', {})
    for key, val in reg_defaults.items():
        if key not in base:
            base[key] = val

    cases = matrix.get('cases', [])

    scripts_dir = args.scripts_dir
    os.makedirs(scripts_dir, exist_ok=True)

    # find exoplanet_mod.F90 template from registry exocam_root
    exocam_root = paths_override.get('exocam_root') or registry.get('paths', {}).get('exocam_root', '')
    template_base = (f"{exocam_root}/cesm1.2.1/configs"
                     if exocam_root else None)

    verify_only = getattr(args, 'verify', False)

    generated = []
    errors_total = 0
    verify_failed = 0

    for case_def in cases:
        case_name = case_def.get('name')
        if not case_name:
            print("WARNING: case missing 'name', skipping", file=sys.stderr)
            continue

        spec = resolve_case(base, case_def)
        spec['_paths_override'] = paths_override

        if verify_only:
            paths = dict(registry.get('paths', {}))
            for k in ['cesm_scripts', 'caseroot', 'exocam_root', 'exort_root']:
                if k in paths_override:
                    paths[k] = paths_override[k]
            # Type + nc-file checks run first: they degrade gracefully on bad
            # input, whereas validate_case coerces values to float and would
            # raise on a mistyped numeric. Only run validate_case if types pass.
            v_errors, v_notes = verify_case(spec, registry, paths)
            if not v_errors:
                try:
                    v_errors = validate_case(spec, registry)
                except (ValueError, TypeError) as exc:
                    v_errors = [f"validation crashed (likely a bad value): {exc}"]
            if v_errors:
                verify_failed += 1
                print(f"\nFAIL: {case_name}")
                for e in v_errors:
                    print(f"  - {e}")
                for n in v_notes:
                    print(f"  · {n}")
            else:
                print(f"OK:   {case_name}" + (f"  ({len(v_notes)} skipped)" if v_notes else ""))
                for n in v_notes:
                    print(f"  · {n}")
            continue

        errors = validate_case(spec, registry)
        if errors:
            print(f"\nERROR: {case_name}")
            for e in errors:
                print(f"  - {e}")
            errors_total += 1
            continue

        is_clone = bool(spec.get('clone'))

        # IC file: required for newcase; for clone, only if ncdata is explicitly in spec
        ic_file = None
        if not is_clone:
            ic_file, _ = find_ic_file(spec, registry)
        elif spec.get('ncdata'):
            ic_file, _ = find_ic_file(spec, registry)

        # find and render exoplanet_mod.F90 template into memory
        config_type = spec.get('config_type', '')
        src_config = config_type.replace('_ne5', '').replace('_ne16', '')
        if is_clone:
            # use the source case's exoplanet_mod.F90 as the template so any
            # custom parameter baselines in the clone source are preserved
            caseroot  = paths_override.get('caseroot') or registry.get('paths', {}).get('caseroot', '')
            clone_of  = spec.get('clone', '')
            template_path = os.path.join(
                caseroot, clone_of, 'SourceMods', 'src.share', 'exoplanet_mod.F90'
            ) if caseroot and clone_of else None
        elif template_base and src_config:
            template_path = os.path.join(
                template_base, src_config,
                'SourceMods', 'src.share', 'exoplanet_mod.F90'
            )
        else:
            template_path = None

        if template_path and os.path.exists(template_path):
            exoplanet_mod_content = render_exoplanet_mod(template_path, spec, is_clone=is_clone)
        else:
            src_label = spec.get('clone') if is_clone else (config_type or 'unknown')
            print(f"  WARNING: template exoplanet_mod.F90 not found for {src_label}; "
                  f"script will contain a placeholder comment — install manually")
            exoplanet_mod_content = None

        if is_clone:
            script_path = generate_clone_script(
                case_name, spec, registry, ic_file, scripts_dir, exoplanet_mod_content
            )
        else:
            script_path = generate_shell_script(
                case_name, spec, registry, ic_file, scripts_dir, exoplanet_mod_content
            )
        generated.append((case_name, script_path))
        print(f"Generated: {script_path}")

    if verify_only:
        named = sum(1 for c in cases if c.get('name'))
        print(f"\nVerify: {named - verify_failed} OK, {verify_failed} failed "
              f"of {named} case(s). No scripts generated.")
        if verify_failed:
            sys.exit(1)
        return

    print(f"\n{len(generated)} script(s) generated, {errors_total} case(s) skipped due to errors.")


def cmd_make(args):
    import glob

    scripts_dir = args.scripts_dir
    pattern = os.path.join(scripts_dir, '*_build.sh')
    all_scripts = sorted(glob.glob(pattern))

    if args.prefix:
        prefix_lower = args.prefix.lower()
        all_scripts = [s for s in all_scripts
                       if os.path.basename(s).lower().startswith(prefix_lower)]

    if not all_scripts:
        sys.exit(f"No matching *_build.sh scripts found in {scripts_dir}")

    print("Scripts to run:")
    for s in all_scripts:
        print(f"  {os.path.basename(s)}")
    print()

    try:
        answer = input("Run these scripts? [yes/no]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit("Aborted.")
    if answer not in ('yes', 'y'):
        sys.exit("Aborted.")

    logs_dir = os.path.join(scripts_dir, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    passed = []
    failed = []

    t_start = datetime.datetime.now()

    for script_path in all_scripts:
        basename = os.path.basename(script_path)
        # strip _build.sh suffix to get case name
        case_name = basename[:-len('_build.sh')]
        log_path = os.path.join(logs_dir, f"{case_name}.build.log")
        print(f"Building: {case_name} ... ", end='', flush=True)
        result = subprocess.run(
            ['bash', script_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        with open(log_path, 'w') as f:
            f.write(result.stdout)
        if result.returncode == 0:
            print("OK")
            passed.append(case_name)
        else:
            print(f"FAILED (see {log_path})")
            failed.append(case_name)

    t_end = datetime.datetime.now()
    elapsed = t_end - t_start
    total_seconds = int(elapsed.total_seconds())
    elapsed_str = f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m {total_seconds % 60}s"

    print(f"\n{len(passed)} passed, {len(failed)} failed.")
    print(f"Started:  {t_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Finished: {t_end.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Elapsed:  {elapsed_str}")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")

    if getattr(args, 'send_it', False) and passed:
        _cmd_send_it(passed, scripts_dir, all_scripts)

    if failed:
        sys.exit(1)


_SEND_IT_VERBS = [
    'Launching', 'Shredding', 'Hucking', 'Schussing',
    'Slaloming', 'Sending', 'Jibbing', 'Carving', 'Ripping',
]
_VERB_WIDTH = max(len(v) for v in _SEND_IT_VERBS)


def _extract_caseroot(script_path):
    """Return the CASEROOT= value from a *_build.sh script, or None."""
    with open(script_path) as f:
        for line in f:
            m = re.match(r'^CASEROOT=(.+)', line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None


def _cmd_send_it(passed_cases, scripts_dir, all_scripts):
    script_map = {
        os.path.basename(s)[:-len('_build.sh')]: s for s in all_scripts
    }
    bar = '━' * 40
    print(f"\n  SEND IT")
    print(bar)
    submitted = 0
    for case_name in passed_cases:
        verb = random.choice(_SEND_IT_VERBS)
        script_path = script_map.get(case_name)
        if not script_path:
            print(f"  {'ERROR':<{_VERB_WIDTH}}  {case_name} → script not found, skipping")
            continue
        caseroot_base = _extract_caseroot(script_path)
        if not caseroot_base:
            print(f"  {'ERROR':<{_VERB_WIDTH}}  {case_name} → CASEROOT not found in script, skipping")
            continue
        caseroot = os.path.join(caseroot_base, case_name)
        ok, detail = submit_case(caseroot, case_name)
        if not ok:
            print(f"  {verb:<{_VERB_WIDTH}}  {case_name} → {detail}")
            continue
        print(f"  {verb:<{_VERB_WIDTH}}  {case_name} → job {detail}")
        submitted += 1
    print(bar)
    print(f"  {submitted} job{'s' if submitted != 1 else ''} submitted.")


def main():
    parser = argparse.ArgumentParser(description='ExoCAM build script generator and runner')
    parser.add_argument('--scripts-dir', default='build_scripts',
                        metavar='DIR',
                        help='Directory for generated scripts and logs (default: build_scripts/)')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    sub.required = True

    p_gen = sub.add_parser('generate', help='Generate build scripts from an experiment matrix')
    p_gen.add_argument('matrix', nargs='?', help='experiment_matrix.yaml')
    p_gen.add_argument('--list', action='store_true',
                       help='List available blueprints and exit')
    p_gen.add_argument('--verify', action='store_true',
                       help='Check matrix coherency (value types + netCDF file '
                            'existence) without generating any scripts')

    p_make = sub.add_parser('make', help='Run generated *_build.sh scripts in scripts-dir')
    p_make.add_argument('--prefix', metavar='PREFIX',
                        help='Only run scripts whose filename starts with PREFIX (case-insensitive)')
    p_make.add_argument('--send-it', action='store_true',
                        help='Submit each successfully built case via sbatch after building')

    args = parser.parse_args()

    if args.command == 'generate':
        cmd_generate(args)
    elif args.command == 'make':
        cmd_make(args)


if __name__ == '__main__':
    main()

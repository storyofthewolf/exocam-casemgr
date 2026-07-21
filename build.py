#!/usr/bin/env python3
"""
ExoCAM case build tool. Three subcommands:

  generate  Read one or more experiment matrix YAMLs (+ each matrix's config
            registry YAML), validate each case (types, required fields, IC
            lookup), and write one self-contained shell build script per case
            (create_newcase or, in clone mode, create_clone + build). The
            rendered exoplanet_mod.F90 is embedded as an inline heredoc — no
            staging directory. Scripts are only written, never executed. When
            several matrices are passed, each is processed in turn under its own
            header and a combined total is reported. --verify checks matrix
            coherency (types, netCDF file existence, scientific-consistency
            warnings) and generates nothing.

  make      Run generated *_build.sh scripts from scripts-dir: explicit NAME
            args, --prefix, or --all (a bare `make` just lists the scripts).
            Builds but does not submit; --send-it also sbatches each case
            (submission otherwise belongs to runmgr.py submit).

  patch     Edit exoplanet_mod.F90 parameters in place in existing cases and
            rerun <case>.build — the only way to change a compiled-in Fortran
            parameter without recreating the case (generate would destroy the
            run via create_newcase/create_clone). Preview by default;
            --execute gates a single batch [yes/no].

For a newcase the experiment matrix is the sole arbiter of atmospheric
composition: unspecified gases render as 0.0 and silence on ozone injects the
zeroVMR default. Clones preserve their source case's composition. See
CLAUDE.md for the full rules.
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
from manage_utils import (submit_case, load_paths, discover_cases,
                          _require_cases, batch_confirm, preview_hint,
                          DEFAULT_CONFIG)

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


# --- REST_N <= STOP_N guard -------------------------------------------------
# Approximate length in days of each CESM *_OPTION unit, used only to compare
# a restart interval against a run length. The values need not be exact -- they
# only have to order correctly across units (an nmonth is longer than an nday,
# shorter than an nyear), and every real matrix uses matching units anyway,
# where the comparison is exact regardless.
#
# 'nsteps'/'nstep' are deliberately absent: a step is the model timestep, which
# depends on the resolution and is not knowable from the matrix. 'date'/'ifdays0'
# are absent because they are absolute markers, not intervals. A case using any
# of these is skipped by _verify_rest_stop rather than guessed at.
_OPTION_DAYS = {
    'nsecond': 1.0 / 86400.0, 'nseconds': 1.0 / 86400.0,
    'nminute': 1.0 / 1440.0,  'nminutes': 1.0 / 1440.0,
    'nhour':   1.0 / 24.0,    'nhours':   1.0 / 24.0,
    'nday':    1.0,           'ndays':    1.0,
    'nmonth':  30.4375,       'nmonths':  30.4375,
    'nyear':   365.0,         'nyears':   365.0,
}


def _verify_rest_stop(spec):
    """Error when the restart interval exceeds the run length (REST_N > STOP_N).

    A run whose restart interval is longer than the run itself never reaches a
    restart write during the segment. CESM still emits an end-of-run restart,
    but the fileset it leaves behind is incomplete, and a subsequent
    CONTINUE_RUN=TRUE crashes because the model cannot find the full set it
    needs to resume. The failure surfaces only on the *next* submission, long
    after the build, so it is caught here at generate time.

    Units are normalized through _OPTION_DAYS before comparing, so a legitimate
    cross-unit pairing (stop_option=nyears/stop_n=5 with rest_option=nmonths/
    rest_n=12) is not false-failed. When either option is a unit that cannot be
    expressed in days (nsteps -- timestep-dependent; date/ifdays0 -- absolute
    markers), the check is skipped rather than guessed at.

    Returns a list of error strings. Assumes types already passed _type_errors,
    which validate_case runs (and returns early on) before calling this.
    """
    for field in ('stop_option', 'stop_n', 'rest_option', 'rest_n'):
        if spec.get(field) is None:
            return []  # a missing field is already reported as such

    stop_unit = str(spec['stop_option']).strip().lower()
    rest_unit = str(spec['rest_option']).strip().lower()
    if stop_unit not in _OPTION_DAYS or rest_unit not in _OPTION_DAYS:
        return []  # nsteps/date/ifdays0 -- not expressible as a fixed interval

    try:
        stop_n = int(spec['stop_n'])
        rest_n = int(spec['rest_n'])
    except (TypeError, ValueError):
        return []  # a bad type is already reported by the type check

    stop_days = stop_n * _OPTION_DAYS[stop_unit]
    rest_days = rest_n * _OPTION_DAYS[rest_unit]
    if rest_days <= stop_days:
        return []

    if stop_unit == rest_unit:
        detail = (f"rest_n={rest_n} exceeds stop_n={stop_n} "
                  f"(both {stop_unit})")
    else:
        detail = (f"rest_n={rest_n} {rest_unit} (~{rest_days:g} days) exceeds "
                  f"stop_n={stop_n} {stop_unit} (~{stop_days:g} days)")
    return [f"rest/stop: {detail}. The run ends before a restart interval is "
            f"reached, so the restart fileset written is incomplete and a "
            f"later CONTINUE_RUN=TRUE will crash. Set rest_n <= stop_n."]


# No-ozone default for newcase builds. exocam-casemgr takes the experiment
# matrix as the sole arbiter of atmospheric composition, so a newcase inherits
# nothing: unspecified gases are forced to 0.0 (see render_exoplanet_mod), and
# ozone is likewise forced off unless the matrix asks for it. Without this, a
# cam_mixed_fv newcase would silently inherit modern-Earth ozone from the
# shipped namelist_files/user_nl_cam while its O2 was being zeroed -- an
# incoherent atmosphere (ozone is photochemically produced from O2).
#
# The zeroVMR file is a single shared file living under the cam_aqua_fv IC
# directory, used by every config_type. If the ExoCAM initial_files tree is
# ever reorganized, this is the only place that needs to change.
ZERO_OZONE_IC_DIR = 'cam_aqua_fv'
ZERO_OZONE_FILE = 'ozone_1.9x2.5_L26_zeroVMR.nc'


def _zero_ozone_defaults(paths):
    """Return the prescribed_ozone_* keys that turn ozone off for a newcase.

    Only _datapath and _file are defaulted; _cycle_yr, _name and _type ship with
    namelist_files/user_nl_cam and are left alone. The datapath is derived from
    paths.exocam_root rather than hardcoded, so it tracks config_registry.yaml.
    """
    root = paths.get('exocam_root', '$EXOCAM')
    return {
        'prescribed_ozone_datapath': f"{root}/cesm1.2.1/initial_files/{ZERO_OZONE_IC_DIR}",
        'prescribed_ozone_file': ZERO_OZONE_FILE,
    }


# Approximate lower bound on exo_convect_plim (Pa) when ozone is present. With
# a stratospheric inversion, convection must be cut off near the tropopause;
# allowing it higher is a numerical stability hazard. Values above this are
# safe (convection is simply clamped lower), so this is a floor, not an
# equality. Without ozone the parameter is freely tunable.
OZONE_CONVECT_PLIM_FLOOR = 4.0e3


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


def _type_errors(spec):
    """Type-check every spec value with a PARAM_TYPES entry.

    Returns a list of error strings. Shared by verify_case and validate_case
    so plain `generate` is exactly as strict as `--verify` and `patch --set`:
    an unvalidated value must never reach _fortran_value, whose int branch
    would silently truncate a non-integral (26.5 -> 26) or crash on a
    non-numeric.
    """
    errors = []
    for key, type_tag in PARAM_TYPES.items():
        if key not in spec:
            continue
        reason = _check_type(spec[key], type_tag)
        if reason:
            errors.append(f"type: {key}: {reason}")
    return errors


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


def _effective_ozone_file(spec):
    """The prescribed_ozone_file a build will actually end up with, or None.

    Newcase: the matrix value if it sets one, else the zeroVMR no-ozone
    default that generate_shell_script injects — silence is checkable, not
    unknown, and the consistency checks can reason about it.

    Clone: the matrix value if set, else None. generate_clone_script applies
    no ozone default (a clone inherits its source's composition), so a clone
    matrix silent on ozone is genuinely unknown here — the consistency checks
    must skip it rather than assume the zeroVMR default and raise false
    warnings.
    """
    nl_cam = spec.get('nl_cam_params') or {}
    explicit = nl_cam.get('prescribed_ozone_file')
    if explicit:
        return explicit
    if spec.get('clone'):
        return None
    return ZERO_OZONE_FILE


def _verify_o2_ozone(spec):
    """Warn on an O2 / ozone combination that is scientifically contradictory.

    Ozone is photochemically produced from O2, so the two must agree:

      exo_o2bar == 0.0  ->  prescribed_ozone_file must be a zeroVMR file
      exo_o2bar >  0.0  ->  prescribed_ozone_file must NOT be a zeroVMR file

    Keyed on the presence of the substring 'zeroVMR' in the filename, which is
    the canonical marker for a no-ozone run. The stock ozone filename is
    deliberately not matched against -- that tag drifts between input datasets,
    whereas the zeroVMR convention is stable.

    This is a WARNING, not a failure: it flags a combination the user should
    justify, and --verify does not presume to know the science.

    A newcase matrix silent on prescribed_ozone_file gets no ozone, since
    newcase injects the zeroVMR default (_zero_ozone_defaults). Silence is
    therefore checkable, not unknown, and a silent matrix with O2 present
    still warns. Skipped: a matrix silent on exo_o2bar (the gas is zeroed and
    ozone is off — coherent), and a CLONE matrix silent on ozone (the clone
    inherits ozone from its source, so silence is unknown, not zeroVMR).
    """
    if 'exo_o2bar' not in spec:
        return []

    try:
        o2 = float(spec['exo_o2bar'])
    except (TypeError, ValueError):
        return []  # a bad type is already reported by the type check

    ozone_file = _effective_ozone_file(spec)
    if ozone_file is None:
        return []  # clone silent on ozone — inherited from source, unknown here
    is_zero_vmr = 'zeroVMR' in str(ozone_file)

    if o2 == 0.0 and not is_zero_vmr:
        return [f"o2/ozone: exo_o2bar = 0.0 (no O2) but prescribed_ozone_file = "
                f"'{ozone_file}' supplies ozone. Ozone is produced from O2 -- "
                f"expected a zeroVMR file. Verify this is intended."]
    if o2 > 0.0 and is_zero_vmr:
        default_note = '' if 'prescribed_ozone_file' in (spec.get('nl_cam_params') or {}) \
            else ' (the newcase no-ozone default; set prescribed_ozone_file to add ozone)'
        return [f"o2/ozone: exo_o2bar = {o2} (O2 present) but "
                f"prescribed_ozone_file = '{ozone_file}' is a zeroVMR (no ozone) "
                f"file{default_note}. Verify this is intended."]
    return []


def _verify_ozone_convect_plim(spec):
    """Warn when ozone is present but exo_convect_plim sits below its floor.

    With ozone the stratospheric temperature inversion demands that convection
    be cut off around the tropopause; letting it reach higher is a numerical
    stability hazard. 4.e3 Pa is the established approximate floor. Values at or
    above it are fine (convection is merely clamped lower).

    A newcase matrix silent on prescribed_ozone_file gets the zeroVMR default,
    i.e. no ozone, so exo_convect_plim is then freely tunable and nothing is
    warned. A clone matrix silent on ozone inherits it from the source —
    unknown here, so the check is skipped rather than assumed.
    """
    if 'exo_convect_plim' not in spec:
        return []
    ozone_file = _effective_ozone_file(spec)
    if ozone_file is None:
        return []  # clone silent on ozone — inherited from source, unknown here
    if 'zeroVMR' in str(ozone_file):
        return []  # no ozone -> exo_convect_plim is free to be tuned

    try:
        plim = float(spec['exo_convect_plim'])
    except (TypeError, ValueError):
        return []

    if plim < OZONE_CONVECT_PLIM_FLOOR:
        return [f"ozone/plim: prescribed_ozone_file supplies ozone but "
                f"exo_convect_plim = {plim} is below the ~{OZONE_CONVECT_PLIM_FLOOR:g} Pa "
                f"floor; convection reaching the stratosphere is a numerical "
                f"stability hazard."]
    return []


def verify_case(spec, registry, paths):
    """Coherency check for a single resolved case spec.

    Checks (no geophysical/scientific validation):
      1. Type tags: every matrix value with a PARAM_TYPES entry matches its type.
      2. NetCDF existence: every nc-file field resolves to an existing file.
         --verify is intended to run on the HPC, where every input file should
         live, so a var-free path whose file (or directory) is absent is a hard
         FAILURE. Only paths that still contain an unexpanded $VAR (env var not
         set) are SKIPPED — those genuinely can't be checked.
      3. Consistency warnings: O2 vs ozone, and ozone vs exo_convect_plim.
         These are questions, not verdicts — they never fail a case.

    Returns (errors, warnings, notes): all lists of strings. errors are hard
    failures; warnings flag combinations to justify; notes are informational
    (skipped/unresolvable file checks).
    """
    errors = []
    warnings = []
    notes = []

    # 1. Type checks
    errors.extend(_type_errors(spec))

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

    # 3. Scientific consistency warnings (never fail the case)
    nl_cam = spec.get('nl_cam_params') or {}
    if (not spec.get('clone')
            and 'prescribed_ozone_datapath' in nl_cam
            and 'prescribed_ozone_file' not in nl_cam):
        warnings.append(
            "ozone: prescribed_ozone_datapath set without "
            "prescribed_ozone_file — the pair is owned as a unit; generate "
            "will force the zeroVMR no-ozone default for both")
    warnings.extend(_verify_o2_ozone(spec))
    warnings.extend(_verify_ozone_convect_plim(spec))

    return errors, warnings, notes


# Fortran parameter line pattern for replacement
_RE_PARAM_LINE = re.compile(
    r'^(\s+(?:real\(r8\)|integer|logical)[^:]*parameter\s*::\s*)(\w+)(\s*=\s*)([^!\n]+)(.*)',
    re.IGNORECASE
)


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


# Namelist param-group dicts stored under a single matrix key. These merge
# one level deep in resolve_case rather than replacing wholesale.
NL_GROUP_KEYS = ('carma_params', 'volc_params', 'nl_cam_params', 'cice_params')


def resolve_case(base, overrides):
    """Merge a per-case matrix entry over the base block.

    Scalar keys: the per-case value replaces the base value.

    Namelist param-group dicts (NL_GROUP_KEYS): merge one level deep — a
    per-case block overrides only the inner keys it names and inherits the
    rest of the base block. Base-specified keys are never silently dropped
    (a case naming just `nhtfrq` must not shed the base's ozone keys and
    silently flip to the zero-ozone newcase default). To remove an inherited
    inner key for one case, set it to an explicit `null` in the per-case
    block — null-valued inner keys are deleted after the merge. A bare
    `nl_cam_params:` stub (the whole group null, e.g. every inner key
    commented out) inherits the base group unchanged — deletion is per
    inner key only, never wholesale.
    """
    spec = dict(base)
    spec.update(overrides)
    for group in NL_GROUP_KEYS:
        base_grp = base.get(group)
        over_grp = overrides.get(group)
        if isinstance(base_grp, dict) and isinstance(over_grp, dict):
            spec[group] = {**base_grp, **over_grp}
        elif isinstance(base_grp, dict) and over_grp is None:
            # Group absent from the per-case block, or present as a bare
            # null stub: inherit the base group. Without this, the stub's
            # None (stamped by spec.update above) would silently shed the
            # base keys — the exact zero-ozone flip the merge prevents.
            spec[group] = dict(base_grp)
        if spec.get(group) is None and group in spec:
            # Bare null stub with no base group: drop the key entirely so
            # None never reaches the namelist rendering or the registry.
            del spec[group]
            continue
        if isinstance(spec.get(group), dict):
            # Drop explicit nulls (the deletion marker) so they neither
            # reach the namelist upsert nor linger in the resolved spec.
            spec[group] = {k: v for k, v in spec[group].items()
                           if v is not None}
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
    # Type tags first, and alone: the checks below (and rendering after them)
    # coerce values to float, so a mistyped param must fail here with a clean
    # message rather than crash the coercion or silently truncate in
    # _fortran_value's int branch. Under --verify this never fires —
    # verify_case runs the same check and only calls validate_case when it
    # passes.
    errors = _type_errors(spec)
    if errors:
        return errors

    if spec.get('clone'):
        for field in REQUIRED_FIELDS_CLONE:
            if field not in spec:
                errors.append(f"missing required field: {field}")
        # No IC table lookup for clones. The build script only touches ncdata
        # when the matrix sets it explicitly (find_ic_file then returns the
        # value verbatim, which cannot fail), and a table lookup keyed on
        # matrix-only gases is wrong by construction: the clone inherits its
        # unspecified gases from the source case, so the pressure computed
        # here (and hence the ic_files key) does not reflect the real
        # atmosphere. It used to hard-fail coherent clone gas sweeps whose
        # partial sum had no table entry.
    else:
        for field in REQUIRED_FIELDS:
            if field not in spec:
                errors.append(f"missing required field: {field}")

        # IC file lookup
        try:
            find_ic_file(spec, registry)
        except ValueError as e:
            errors.append(str(e))

    # restart interval must not outrun the segment length (applies to both
    # newcase and clone — the xmlchange block below is identical for each)
    errors.extend(_verify_rest_stop(spec))

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
    if PARAM_TYPES.get(name) == 'int':
        # Integer parameters render as bare integers — a decimal or _r8
        # suffix contradicts the declared Fortran type. Keyed on PARAM_TYPES
        # rather than the value's Python type so string values arriving from
        # `patch --set exo_rad_step=4` take this branch too.
        return str(int(float(value)))
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


# ---------------------------------------------------------------------------
# In-place SourceMods patching (build.py patch)
# ---------------------------------------------------------------------------
#
# `generate` renders exoplanet_mod.F90 from a template into a fresh build
# script whose first act is create_newcase/create_clone -- running it against a
# live case would recreate it and destroy the run. `patch` is the in-place
# counterpart: it edits <case>/SourceMods/src.share/exoplanet_mod.F90 directly
# and recompiles via <case>.build. exo_convect_plim and friends are Fortran
# `parameter` constants baked into the binary, so there is no xmlchange or
# user_nl path that could change them -- editing + rebuilding is the only way.
#
# Per project convention: no clean_build. Changing a parameter in a file that
# already exists under SourceMods only requires <case>.build.

EXO_MOD_RELPATH = os.path.join('SourceMods', 'src.share', 'exoplanet_mod.F90')


def patch_exoplanet_mod(exo_path, updates):
    """Rewrite `parameter ::` lines in an existing exoplanet_mod.F90 in place.

    `updates` maps param name -> new value. Only active (uncommented) parameter
    lines are touched, using the same regex and value formatter as
    render_exoplanet_mod, so declaration spacing and trailing !! comments are
    preserved exactly.

    Returns (new_text, applied) where `applied` maps param name -> (old_rhs,
    new_rhs) for each line actually rewritten. Params in `updates` that never
    matched a line are absent from `applied` -- the caller reports those.
    """
    applied = {}
    lines_out = []
    with open(exo_path) as f:
        for line in f:
            stripped = line.lstrip()
            if stripped.startswith('!') or not stripped.strip():
                lines_out.append(line)
                continue

            m = _RE_PARAM_LINE.match(line)
            if m:
                param_name = m.group(2)
                if param_name in updates:
                    prefix  = m.group(1)
                    spaces  = m.group(3)
                    old_rhs = m.group(4)
                    suffix  = m.group(5)

                    # group(4) is [^!]+, so it greedily absorbs the run of
                    # spaces separating the value from a trailing !! comment.
                    # Hand that gap back to the suffix, or the rewritten line
                    # reads `5.0_r8!! Sets the minimum...`.
                    gap = old_rhs[len(old_rhs.rstrip()):]
                    new_val = _fortran_value(param_name, updates[param_name])
                    applied[param_name] = (old_rhs.strip(), new_val)
                    lines_out.append(
                        f"{prefix}{param_name}{spaces}{new_val}{gap}{suffix}\n")
                    continue

            lines_out.append(line)

    return ''.join(lines_out), applied


def rebuild_case(case_dir, case_name):
    """Run ./<case_name>.build in case_dir. Returns (ok, detail).

    No clean_build: for parameters in a file already present under SourceMods,
    the CESM dependency scan picks up the change and recompiles dependents.
    """
    script = f'./{case_name}.build'
    if not os.path.exists(os.path.join(case_dir, f'{case_name}.build')):
        return False, f'{case_name}.build not found'
    try:
        result = subprocess.run(
            [script],
            cwd=case_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
    except OSError as e:
        return False, f'build failed to launch: {e}'
    if result.returncode != 0:
        tail = (result.stdout or '').strip().splitlines()
        tail = tail[-1] if tail else 'no output'
        return False, f'build failed (exit {result.returncode}): {tail}'
    return True, 'built'


def _sed_escape_replacement(s):
    """Escape a value for use in a sed s|pat|repl| replacement string.

    Backslash first, then '&' (re-inserts the match) and the '|' delimiter.
    Theoretical hardening — real namelist/path values contain none of these —
    but corruption here would be silent, so every interpolated replacement
    goes through this.
    """
    return s.replace('\\', r'\\').replace('&', r'\&').replace('|', r'\|')


def _nl_upsert_lines(param_dict, target='user_nl_cam'):
    """
    Return shell lines that upsert key = value entries into a namelist file
    (default user_nl_cam; pass target='user_nl_cice' etc. for others).
    For each key: delete every existing line for it, then append exactly one.
    Guarantees exactly one line per key -- never a duplicate -- including
    collapsing duplicates inherited from a clone source built by pre-2026-06
    scripts.

    Type dispatch via _format_nl_value:
    - bool        -> .true. / .false.  (unquoted Fortran logical)
    - int/float   -> bare number       (no quotes)
    - str logical -> .true. / .false.  (unquoted, passed through)
    - str numeric -> bare number       (unquoted, coerced)
    - str other   -> 'value'           (single-quoted, e.g. file paths)
    - list/tuple  -> comma-separated array, each element by the rules above
    """
    lines = []
    for key, val in param_dict.items():
        nl_val = _format_nl_value(val)
        # Delete every existing line for the key, then append exactly one.
        # Anchoring on the key (^[[:space:]]*KEY[[:space:]]*=) avoids
        # matching a different key that merely contains this one as a
        # substring, and tolerates source formatting (extra spaces/tabs,
        # trailing inline comments). Delete-then-append (rather than
        # replace-in-place) also collapses pre-existing duplicates — clones
        # inherit their source namelist verbatim, and cases built by
        # pre-2026-06 scripts carry appended duplicates (CESM reads the last
        # value, but one line per key is the contract here). The [ -f ]
        # guard keeps sed from failing under set -e when the namelist
        # doesn't exist yet.
        pat = f'^[[:space:]]*{key}[[:space:]]*='
        lines.append(
            f'if [ -f {target} ]; then sed -i -E "/{pat}/d" {target}; fi; '
            f'echo "{key} = {nl_val}" >> {target}'
        )
    return lines


def _build_nl_upsert_block(spec):
    """
    Return shell lines to upsert carma_params, volc_params, and/or nl_cam_params
    into user_nl_cam (and cice_params into user_nl_cice) using
    delete-then-append semantics (one line per key, duplicates collapsed).

    Used by BOTH build paths. Clone needs it because create_clone copies the
    namelist verbatim from the source case. Newcase needs it too: the namelist
    copied from ${EXOCAM}/.../${CONFIG_TYPE}/namelist_files/ is not empty --
    cam_mixed_fv ships prescribed_ozone_* set to modern Earth. A plain append
    there would leave two lines for the same key, and the resulting value would
    depend on how the namelist reader treats duplicates. Upsert guarantees one
    line per key, so a matrix nl_cam_params entry always overrides the shipped
    default rather than racing it.
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
    """
    Format a Python value as a CESM namelist RHS (for use inside echo "...").
    Multi-valued entries (nhtfrq, mfilt, fincl*, ...) may be given as a YAML
    list — each element is formatted by the scalar rules and joined with
    commas, so [0, -24] -> 0, -24 and [TS, FSNT] -> 'TS', 'FSNT'.
    A plain string containing commas (YAML reads nhtfrq: 0,-24 as a string)
    is treated as an array only if every piece coerces to a number or Fortran
    logical; otherwise it is quoted whole like any genuine string. String
    arrays must therefore use YAML list syntax, not a comma-joined string.
    """
    if isinstance(val, (list, tuple)):
        return ', '.join(_format_nl_scalar(v) for v in val)
    if isinstance(val, str) and ',' in val:
        parts = [p.strip() for p in val.split(',')]
        if all(parts):
            formatted = [_format_nl_scalar(p) for p in parts]
            if not any(f.startswith("'") for f in formatted):
                return ', '.join(formatted)
    return _format_nl_scalar(val)


def _format_nl_scalar(val):
    """Format a single scalar value as a namelist RHS token."""
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
    # integer-looking strings stay integers: Fortran reads 1 into a real
    # fine, but errors reading 1.0 into an integer (nhtfrq, mfilt, ...)
    try:
        return str(int(s))
    except ValueError:
        pass
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
                f'sed -i \'s|{key} = ".*"|{key} = "{_sed_escape_replacement(path_val)}"|\' user_nl_clm'
            )
    return lines


def _build_run_script_block(spec):
    """
    Return shell lines to patch SBATCH directives into ${CASE}.run after cesm_setup.
    account: upsert — replace existing #SBATCH --account= line, or append if absent.
    job_name (-J): upsert — replace existing #SBATCH -J line, or append if absent.
    The job name always defaults to the full case name (${CASE}) so that
    `squeue --name <case>` uniquely identifies the job; an explicit matrix
    job_name overrides it. account is skipped if absent from the spec.
    """
    lines = []
    account  = spec.get('account')
    # Default -J to the full case name (CESM otherwise truncates it to a short,
    # non-unique label that runmgr's squeue --name probe can't match).
    job_name = spec.get('job_name') or '${CASE}'
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
        # Double-quote the sed expressions so a ${CASE} default expands at
        # runtime; an explicit literal job_name is unaffected by double-quoting.
        lines.append(
            f"if grep -q '^#SBATCH -J ' ${{CASE}}.run; then\n"
            f'    sed -i "s|^#SBATCH -J .*|#SBATCH -J {job_name}|" ${{CASE}}.run\n'
            f"else\n"
            f'    sed -i "0,/^#SBATCH /s|^#SBATCH |#SBATCH -J {job_name}\\n#SBATCH |" ${{CASE}}.run\n'
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

    # Force ozone off unless the matrix asks for it, so a newcase inherits no
    # composition from the shipped namelist. prescribed_ozone_file is the
    # owner key, and the pair is set as a unit: a matrix that names the file
    # owns the whole ozone setting and must supply its own datapath rather
    # than silently keeping the zeroVMR directory; a datapath alone cannot
    # hold ozone on (a stray datapath — e.g. the file deleted per-case via
    # `prescribed_ozone_file: null` — would otherwise suppress the zeroVMR
    # default and silently resurrect the shipped template's ozone). Copied,
    # not mutated in place -- spec is the caller's and is read again for
    # verification and reporting.
    spec = dict(spec)
    nl_cam = dict(spec.get('nl_cam_params') or {})
    if 'prescribed_ozone_file' not in nl_cam:
        if 'prescribed_ozone_datapath' in nl_cam:
            print(f"  WARNING: {case_name}: prescribed_ozone_datapath set "
                  f"without prescribed_ozone_file — the pair is owned as a "
                  f"unit; forcing the zeroVMR no-ozone default for both.")
        nl_cam.update(_zero_ozone_defaults(paths))
    spec['nl_cam_params'] = nl_cam

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
        f"sed -i \"s|ncdata = '.*'|ncdata = '{_sed_escape_replacement(ic_path)}'|\" user_nl_cam",
        *_build_nl_upsert_block(spec),
        *_build_clm_update_block(spec, paths),
        *_build_docn_update_block(spec),
        "",
        "# Update solar file path in exoplanet_mod.F90",
        f"sed -i \"s|exo_solar_file = '.*'|exo_solar_file = '{_sed_escape_replacement(solar_file)}'|\" "
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
            f"sed -i \"s|ncdata = '.*'|ncdata = '{_sed_escape_replacement(ic_path)}'|\" user_nl_cam",
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
            f"sed -i \"s|exo_solar_file = '.*'|exo_solar_file = '{_sed_escape_replacement(solar_file)}'|\" "
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
    exp_matrices_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exp_matrices')

    if args.list:
        yamls = sorted(f for f in os.listdir(exp_matrices_dir)
                       if f.endswith('.yaml')) if os.path.isdir(exp_matrices_dir) else []
        if yamls:
            print('\n'.join(yamls))
        else:
            print(f"No .yaml files found in {exp_matrices_dir}")
        sys.exit(0)

    if not args.matrix:
        sys.exit("error: the following arguments are required: matrix")

    # Resolve every matrix path up front so a typo aborts before any work.
    matrix_paths = []
    for m in args.matrix:
        if os.path.exists(m):
            matrix_paths.append(m)
            continue
        candidate = os.path.join(exp_matrices_dir, m)
        if os.path.exists(candidate):
            matrix_paths.append(candidate)
        else:
            sys.exit(f"matrix file not found: {m}\n"
                     f"  also checked: {candidate}")

    verify_only = getattr(args, 'verify', False)

    # Package-wide tallies across all matrices for the final summary/exit code.
    grand_generated = 0
    grand_errors = 0
    grand_named = 0
    grand_verify_failed = 0
    grand_verify_warned = 0

    multi = len(matrix_paths) > 1
    for matrix_path in matrix_paths:
        if multi:
            print(f"\n=== {matrix_path} ===")
        g, e, n, vf, vw = _generate_one_matrix(
            matrix_path, args, exp_matrices_dir, verify_only)
        grand_generated += g
        grand_errors += e
        grand_named += n
        grand_verify_failed += vf
        grand_verify_warned += vw

    if verify_only:
        summary = (f"\nVerify total: {grand_named - grand_verify_failed} OK, "
                   f"{grand_verify_failed} failed of {grand_named} case(s) "
                   f"across {len(matrix_paths)} matrix file(s).")
        if grand_verify_warned:
            summary += f" {grand_verify_warned} case(s) raised warnings."
        print(summary + " No scripts generated.")
        if grand_verify_failed:
            sys.exit(1)
        return

    print(f"\nTotal: {grand_generated} script(s) generated, {grand_errors} "
          f"case(s) skipped due to errors across {len(matrix_paths)} matrix file(s).")


def _generate_one_matrix(matrix_file, args, exp_matrices_dir, verify_only):
    """Process a single experiment matrix. Returns
    (generated, errors_total, named, verify_failed, verify_warned)."""
    matrix = load_yaml(matrix_file)

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

    generated = []
    errors_total = 0
    verify_failed = 0
    verify_warned = 0

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
            v_errors, v_warnings, v_notes = verify_case(spec, registry, paths)
            if not v_errors:
                try:
                    v_errors = validate_case(spec, registry)
                except (ValueError, TypeError) as exc:
                    v_errors = [f"validation crashed (likely a bad value): {exc}"]
            if v_warnings:
                verify_warned += 1
            if v_errors:
                verify_failed += 1
                print(f"\nFAIL: {case_name}")
                for e in v_errors:
                    print(f"  - {e}")
            else:
                tags = []
                if v_warnings:
                    tags.append(f"{len(v_warnings)} warning(s)")
                if v_notes:
                    tags.append(f"{len(v_notes)} skipped")
                print(f"OK:   {case_name}" + (f"  ({', '.join(tags)})" if tags else ""))
            for w in v_warnings:
                print(f"  ! {w}")
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

    named = sum(1 for c in cases if c.get('name'))
    if verify_only:
        summary = (f"\nVerify: {named - verify_failed} OK, {verify_failed} failed "
                   f"of {named} case(s).")
        if verify_warned:
            # Warnings are questions for the user, not verdicts — they never
            # affect the exit code.
            summary += f" {verify_warned} case(s) raised warnings."
        print(summary + " No scripts generated.")
    else:
        print(f"\n{len(generated)} script(s) generated, {errors_total} "
              f"case(s) skipped due to errors.")

    return len(generated), errors_total, named, verify_failed, verify_warned


def cmd_make(args):
    import glob

    scripts_dir = args.scripts_dir
    pattern = os.path.join(scripts_dir, '*_build.sh')
    all_scripts = sorted(glob.glob(pattern))

    if args.names:
        wanted = []
        for name in args.names:
            wanted.append(name if name.endswith('_build.sh') else f"{name}_build.sh")
        by_basename = {os.path.basename(s): s for s in all_scripts}
        all_scripts = []
        missing = []
        for basename in wanted:
            script_path = by_basename.get(basename)
            if script_path:
                all_scripts.append(script_path)
            else:
                missing.append(basename)
        if missing:
            sys.exit(f"Scripts not found in {scripts_dir}: {', '.join(missing)}")
    elif args.prefix:
        prefix_lower = args.prefix.lower()
        all_scripts = [s for s in all_scripts
                       if os.path.basename(s).lower().startswith(prefix_lower)]
    elif not args.all:
        if not all_scripts:
            sys.exit(f"No *_build.sh scripts found in {scripts_dir}")
        print(f"{len(all_scripts)} script(s) in {scripts_dir}:")
        for s in all_scripts:
            print(f"  {os.path.basename(s)}")
        print("\nNo NAME, --prefix, or --all given — nothing will be run. "
              "Pass --all to run every script above, or list names / use --prefix to select a subset.")
        return

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


def _parse_patch_pairs(items):
    """Parse ['VAR=VALUE', ...] into an ordered dict, validating against
    EXO_PARAMS and PARAM_TYPES. Exits on unknown param, bad format, or type
    mismatch -- the same type tags --verify enforces on the matrix."""
    updates = {}
    for item in (items or []):
        if '=' not in item:
            sys.exit(f"ERROR: --set requires VAR=VALUE format, got: {item!r}")
        var, _, val = item.partition('=')
        var, val = var.strip(), val.strip()
        if not var:
            sys.exit(f"ERROR: empty variable name in --set {item!r}")
        if var not in EXO_PARAMS:
            sys.exit(f"ERROR: {var!r} is not a known exoplanet_mod parameter "
                     f"(not in EXO_PARAMS).")
        type_tag = PARAM_TYPES.get(var)
        if type_tag:
            reason = _check_type(val, type_tag)
            if reason:
                sys.exit(f"ERROR: --set {var}={val!r}: {reason}")
        updates[var] = val
    if not updates:
        sys.exit("ERROR: patch requires at least one --set VAR=VALUE.")
    return updates


def cmd_patch(args):
    # Validate the flags before touching the registry, so a malformed --set
    # reports the real problem rather than a missing-caseroot error.
    updates = _parse_patch_pairs(args.set)

    paths = load_paths(args)
    caseroot = paths.get('caseroot', '')
    if not caseroot:
        sys.exit("ERROR: caseroot path not configured.")

    cases = _require_cases(discover_cases(paths), args)
    if not cases:
        return

    # Gas bars are the only EXO_PARAMS coupled to another rendered value:
    # exo_n2bar was computed at generate time as target - sum(gases). Patching a
    # gas in place leaves n2bar fixed, so total surface pressure shifts by the
    # delta. Harmless at ppm (the model self-adjusts); not at 0.1 bar. Warn with
    # the magnitude rather than refusing -- the caller decides.
    gas_touched = [g for g in updates if g in GAS_BAR_PARAMS]
    if gas_touched:
        print("  WARNING: patching gas bar param(s): " + ', '.join(gas_touched))
        print("           exo_n2bar is NOT recomputed; total surface pressure "
              "shifts by the delta.")
        print("           Safe at trace (ppm) magnitudes. For a composition "
              "change, regenerate instead.\n")

    from runmgr import _probe_status

    actions, flagged = [], []
    for case in cases:
        case_dir = os.path.join(caseroot, case)
        exo_path = os.path.join(case_dir, EXO_MOD_RELPATH)
        if not os.path.exists(exo_path):
            print(f"  {case}: ERROR: not found: {EXO_MOD_RELPATH}")
            continue

        new_text, applied = patch_exoplanet_mod(exo_path, updates)
        missing = [p for p in updates if p not in applied]
        if missing:
            print(f"  {case}: ERROR: no active parameter line for: "
                  f"{', '.join(missing)}")
            continue

        status = _probe_status(case_dir, case)
        note = ''
        if status in ('RUNNING', 'RUNNING?'):
            note = f'  <- {status}: job is live, recompiling swaps the binary mid-run'
            flagged.append(case)
        elif status == 'RESUBMITTED':
            note = f'  <- {status}: queued; next segment picks up the new binary'
            flagged.append(case)

        changes = ', '.join(f"{p}: {o} -> {n}" for p, (o, n) in applied.items())
        print(f"  [{'patch' if args.execute else 'preview'}] {case}: {changes}{note}")
        actions.append((case, case_dir, exo_path, new_text))

    if not actions:
        return
    if not args.execute:
        preview_hint(args.execute)
        return

    if flagged:
        print(f"\n  {len(flagged)} case(s) have an active or queued job "
              f"(see flags above).")
    if not batch_confirm(f"Patch exoplanet_mod.F90 and rebuild", len(actions)):
        print("Aborted.")
        return

    built = 0
    failed = []
    for case, case_dir, exo_path, new_text in actions:
        with open(exo_path, 'w') as f:
            f.write(new_text)
        ok, detail = rebuild_case(case_dir, case)
        print(f"  {case}: {'OK' if ok else 'ERROR'}: {detail}")
        if ok:
            built += 1
        else:
            failed.append(case)

    print(f"\n  Patched {', '.join(updates)} in {len(actions)} case(s); "
          f"{built} rebuilt.")
    if failed:
        # The F90 edit landed even where the build failed -- rerunning
        # <case>.build after fixing the cause is enough; no re-patch needed.
        print(f"  {len(failed)} build(s) FAILED: {', '.join(failed)}")
        print("  The source edit was written; rerun <case>.build once the "
              "build error is resolved.")
    print("  NOTE: experiment matrices are NOT updated -- edit the matrix "
          "`base:` block to keep future regenerates consistent.")


def main():
    parser = argparse.ArgumentParser(description='ExoCAM build script generator and runner')
    parser.add_argument('--scripts-dir', default='build_scripts',
                        metavar='DIR',
                        help='Directory for generated scripts and logs (default: build_scripts/)')
    # Path overrides live on the top-level parser with an explicit default, the
    # same shape datamgr.py and runmgr.py use — so no subcommand has to be given
    # --caseroot / --config-registry per invocation.
    parser.add_argument('--config-registry', default=DEFAULT_CONFIG,
                        dest='config_registry', metavar='FILE',
                        help='Path to config_registry.yaml')
    parser.add_argument('--caseroot', metavar='DIR',
                        help='Override paths.caseroot from config_registry')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    sub.required = True

    p_gen = sub.add_parser('generate', help='Generate build scripts from one or more experiment matrices')
    p_gen.add_argument('matrix', nargs='*', metavar='MATRIX',
                       help='one or more experiment_matrix.yaml files')
    p_gen.add_argument('--list', action='store_true',
                       help='List available experiment matrices and exit')
    p_gen.add_argument('--verify', action='store_true',
                       help='Check matrix coherency (value types + netCDF file '
                            'existence) without generating any scripts')

    p_make = sub.add_parser('make', help='Run generated *_build.sh scripts in scripts-dir')
    p_make.add_argument('names', nargs='*', metavar='NAME',
                        help='Explicit case names or *_build.sh filenames to run')
    p_make.add_argument('--prefix', metavar='PREFIX',
                        help='Only run scripts whose filename starts with PREFIX (case-insensitive); '
                             'ignored if NAME arguments are given')
    p_make.add_argument('--all', action='store_true',
                        help='Run every *_build.sh script in scripts-dir; required if no NAME '
                             'or --prefix is given (a bare call with none of these just lists scripts)')
    p_make.add_argument('--send-it', action='store_true',
                        help='Submit each successfully built case via sbatch after building')

    p_patch = sub.add_parser(
        'patch',
        help='Edit exoplanet_mod.F90 in existing case(s) in place and rebuild')
    p_patch.add_argument('cases', nargs='*', metavar='CASE',
                         help='Explicit case names to patch')
    p_patch.add_argument('--prefix', metavar='PREFIX',
                         help='Patch all cases whose name starts with PREFIX '
                              '(case-insensitive); cannot be combined with CASE names')
    p_patch.add_argument('--set', action='append', metavar='VAR=VALUE',
                         help='exoplanet_mod parameter to set; repeatable')
    p_patch.add_argument('--execute', action='store_true',
                         help='Apply the edit and rebuild (default: preview only)')

    args = parser.parse_args()

    if args.command == 'generate':
        cmd_generate(args)
    elif args.command == 'make':
        cmd_make(args)
    elif args.command == 'patch':
        cmd_patch(args)


if __name__ == '__main__':
    main()

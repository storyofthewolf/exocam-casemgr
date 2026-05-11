"""
Parsing primitives for ExoCAM case inspection and build automation.
No filesystem side effects — pure parse functions only.
"""

import os
import re
import xml.etree.ElementTree as ET

try:
    import netCDF4 as _nc4
    _HAVE_NC4 = True
except ImportError:
    _HAVE_NC4 = False

# Fortran parameter regexes
_RE_REAL = re.compile(
    r'^\s+(real\(r8\)|integer),\s*public,\s*parameter\s*::\s*(\w+)\s*=\s*([^!\n]+)',
    re.IGNORECASE
)
_RE_LOGICAL = re.compile(
    r'^\s+logical[^:]*parameter\s*::\s*(\w+)\s*=\s*\.(true|false)\.',
    re.IGNORECASE
)
_RE_STRING = re.compile(
    r"^\s+character[^:]*parameter\s*::\s*(\w+)\s*=\s*'([^']+)'"
)
_RE_KIND = re.compile(r'_[rR]8\b')
# Matches expressions that are safe to eval: digits, operators, parens, dots, sci notation
_RE_SAFE_EXPR = re.compile(r'^[0-9eE.+\-*/() ]+$')


def _try_eval_expr(rhs, known_params):
    """
    Try to evaluate a Fortran parameter RHS as a Python float.
    Substitutes previously resolved numeric params by name, then evals.
    Returns float on success, None on failure (unknown symbol, bad syntax, etc.).
    """
    expr = rhs
    # Substitute known numeric symbols longest-name-first to avoid partial matches
    for sym, val in sorted(known_params.items(), key=lambda kv: -len(kv[0])):
        if isinstance(val, (int, float)) and sym in expr:
            expr = re.sub(r'\b' + re.escape(sym) + r'\b', repr(val), expr)
    # Only eval if the result is a safe arithmetic expression
    if not _RE_SAFE_EXPR.match(expr):
        return None
    try:
        return float(eval(expr, {"__builtins__": {}}))  # noqa: S307
    except Exception:
        return None


# user_nl_* key=value patterns — single or double quoted strings, and bare values
_RE_NL_STR = re.compile(r'(\w+)\s*=\s*["\']([^"\']+)["\']')
_RE_NL_VAL = re.compile(r"(\w+)\s*=\s*([^,'\s!][^,!\n]*)")


def _to_numeric(s):
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s

# IC filename pressure/level pattern
_RE_IC = re.compile(r'ic_([0-9.e+\-]+bar)_L(\d+)')


def parse_exoplanet_mod(path):
    """
    Parse exoplanet_mod.F90, return flat dict of parameter name -> value.
    Numeric literals are stored as floats. Logical as 'true'/'false' strings.
    String params as strings. Expression RHS (e.g. exo_n2bar, exo_pstd)
    stored as raw strings with an '_expr' suffix key and the original key
    set to None.
    """
    params = {}
    with open(path) as f:
        for line in f:
            stripped = line.lstrip()
            # skip comments and blank lines
            if stripped.startswith('!') or not stripped.strip():
                continue

            m = _RE_LOGICAL.match(line)
            if m:
                params[m.group(1)] = m.group(2).lower()
                continue

            m = _RE_STRING.match(line)
            if m:
                params[m.group(1)] = m.group(2)
                continue

            m = _RE_REAL.match(line)
            if m:
                is_int = m.group(1).lower() == 'integer'
                name = m.group(2)
                rhs = _RE_KIND.sub('', m.group(3)).strip().rstrip()
                val = _try_eval_expr(rhs, params)
                if val is not None:
                    params[name] = int(val) if is_int else val
                else:
                    # unevaluable expression — keep raw for caller inspection
                    params[name] = None
                    params[name + '_expr'] = rhs
    return params


def parse_user_nl_cam(path):
    """
    Parse user_nl_cam, return dict with ncdata, bnd_topo, gw_drag_file,
    ncdata_pressure_str / ncdata_levels extracted from IC filename, and
    carma_params / volc_params dicts for any carma_* / volc_* keys found.
    """
    result = {}
    keys = {'ncdata', 'bnd_topo', 'gw_drag_file'}
    carma = {}
    volc = {}
    with open(path) as f:
        for line in f:
            if line.lstrip().startswith('!'):
                continue
            for m in _RE_NL_STR.finditer(line):
                k, v = m.group(1), m.group(2)
                if k in keys:
                    result[k] = v
                elif k.startswith('carma_'):
                    carma[k] = _to_numeric(v)
                elif k.startswith('volc_'):
                    volc[k] = _to_numeric(v)
            # bare (non-string) values for carma_*/volc_* keys
            for m in _RE_NL_VAL.finditer(line):
                k, v = m.group(1), m.group(2).strip().rstrip(',')
                if k.startswith('carma_') and k not in carma:
                    carma[k] = _to_numeric(v)
                elif k.startswith('volc_') and k not in volc:
                    volc[k] = _to_numeric(v)
    if carma:
        result['carma_params'] = carma
    if volc:
        result['volc_params'] = volc

    ncdata = result.get('ncdata', '')
    basename = os.path.basename(ncdata)
    m = _RE_IC.search(basename)
    if m:
        result['ncdata_pressure_str'] = m.group(1)
        result['ncdata_levels'] = int(m.group(2))
    else:
        result['ncdata_pressure_str'] = None
        result['ncdata_levels'] = None

    return result


def parse_docn_som(path):
    """
    Parse user_docn.streams.txt.som (an XML fragment, no root element).
    Returns dict with som_pop_frc_file: the full path to the pop_frc* file
    found in the fieldInfo block, or None if not present.
    """
    # Wrap in a synthetic root so ElementTree can parse the fragment
    try:
        with open(path) as f:
            content = f.read()
        tree = ET.fromstring(f'<root>{content}</root>')
    except ET.ParseError:
        return {}

    for field_info in tree.iter('fieldInfo'):
        file_path = (field_info.findtext('filePath') or '').strip()
        file_names_el = field_info.find('fileNames')
        if file_names_el is None:
            continue
        for name in file_names_el.text.split():
            if name.startswith('pop_frc'):
                return {'som_pop_frc_file': os.path.join(file_path, name)}
    return {}


def parse_user_nl_clm(path):
    """
    Parse user_nl_clm, return dict with finidat and fsurdat paths.
    Only called for cam_land_fv and cam_mixed_fv config types.
    """
    result = {}
    keys = {'finidat', 'fsurdat'}
    with open(path) as f:
        for line in f:
            if line.lstrip().startswith('!'):
                continue
            for m in _RE_NL_STR.finditer(line):
                if m.group(1) in keys:
                    result[m.group(1)] = m.group(2)
    return result


def parse_cam_config_opts(xmlpath):
    """
    Parse env_build.xml (or env_run.xml) for CAM_CONFIG_OPTS.
    Returns dict: nlev, exort_pkg, cloud_scheme, raw_opts.
    Falls back to line scan if XML parse fails.
    """
    raw = _find_cam_config_opts(xmlpath)
    if raw is None:
        return {'nlev': None, 'exort_pkg': None, 'cloud_scheme': None, 'raw_opts': None}

    nlev = None
    m = re.search(r'-nlev\s+(\d+)', raw)
    if m:
        nlev = int(m.group(1))

    exort_pkg = None
    m = re.search(r'-usr_src\s+\S+/src\.cam\.([\w]+)', raw)
    if m:
        exort_pkg = m.group(1)

    cloud_scheme = 'mg' if '-microphys' in raw else 'rk'

    return {'nlev': nlev, 'exort_pkg': exort_pkg, 'cloud_scheme': cloud_scheme, 'raw_opts': raw}


def _find_cam_config_opts(xmlpath):
    if not os.path.exists(xmlpath):
        return None
    try:
        tree = ET.parse(xmlpath)
        for entry in tree.iter('entry'):
            if entry.get('id') == 'CAM_CONFIG_OPTS':
                val = entry.findtext('value') or entry.get('value') or ''
                # also check direct text
                if not val:
                    val = (entry.text or '').strip()
                return val
    except ET.ParseError:
        pass
    # fallback: line scan
    with open(xmlpath) as f:
        for line in f:
            if 'CAM_CONFIG_OPTS' in line:
                m = re.search(r'value="([^"]+)"', line)
                if m:
                    return m.group(1)
    return None


def parse_run_type_fields(xmlpath):
    """
    Parse env_run.xml for RUN_TYPE, RUN_REFCASE, RUN_REFDATE, BRNCH_RETAIN_CASENAME.
    Returns dict with lowercase keys. run_type defaults to 'startup'; others default to None.
    brnch_retain_casename is stored as a lowercase string ('true' or 'false'), not a bool.
    """
    fields = {
        'run_type':              'startup',
        'run_refcase':           None,
        'run_refdate':           None,
        'brnch_retain_casename': None,
    }
    id_map = {
        'RUN_TYPE':              'run_type',
        'RUN_REFCASE':           'run_refcase',
        'RUN_REFDATE':           'run_refdate',
        'BRNCH_RETAIN_CASENAME': 'brnch_retain_casename',
    }
    if not os.path.exists(xmlpath):
        return fields
    try:
        tree = ET.parse(xmlpath)
        for entry in tree.iter('entry'):
            eid = entry.get('id')
            if eid in id_map:
                val = entry.findtext('value') or entry.get('value') or (entry.text or '').strip()
                if val:
                    key = id_map[eid]
                    fields[key] = val.lower() if eid == 'BRNCH_RETAIN_CASENAME' else val
    except ET.ParseError:
        # fallback: line scan
        with open(xmlpath) as f:
            for line in f:
                for xml_id, key in id_map.items():
                    if xml_id in line:
                        m = re.search(r'value="([^"]+)"', line)
                        if m:
                            val = m.group(1)
                            fields[key] = val.lower() if xml_id == 'BRNCH_RETAIN_CASENAME' else val
    return fields


def compute_pstd_bar(params):
    """
    Compute total surface pressure in bar from gas bar parameters.
    exo_n2bar is always an expression in the Fortran source; we compute it
    as the remainder up to 1 bar only when total of others <= 1.0.
    If exo_n2bar is set as a float in params (e.g. from experiment matrix),
    it is included directly.
    Returns (pstd_bar, n2bar_computed) tuple.
    """
    gas_keys = ['exo_co2bar', 'exo_ch4bar', 'exo_c2h6bar', 'exo_o2bar',
                'exo_h2bar', 'exo_nh3bar', 'exo_cobar']
    others = sum(float(params[k]) for k in gas_keys if params.get(k) is not None)

    n2bar = params.get('exo_n2bar')
    if n2bar is not None and isinstance(n2bar, (int, float)):
        # explicit numeric value (from experiment matrix)
        pstd = others + float(n2bar)
        return pstd, float(n2bar)
    elif others <= 1.0:
        # standard case: N2 fills to 1 bar
        n2bar_computed = 1.0 - others
        pstd = 1.0
        return pstd, n2bar_computed
    else:
        # high-pressure atmosphere: need exo_n2bar_explicit
        return others, None


def pressure_str_to_bar(s):
    """Convert '1bar' -> 1.0, '0.1bar' -> 0.1, '10bar' -> 10.0."""
    if s is None:
        return None
    try:
        return float(s.replace('bar', '').strip())
    except ValueError:
        return None


def read_solar_nw(path):
    """
    Return the 'nw' dimension size from a NetCDF solar file, or None if
    the file doesn't exist, isn't readable, or has no 'nw' dimension.
    Requires netCDF4; returns None gracefully if the library is absent.
    """
    if not _HAVE_NC4 or not os.path.exists(path):
        return None
    try:
        with _nc4.Dataset(path, 'r') as ds:
            if 'nw' in ds.dimensions:
                return len(ds.dimensions['nw'])
    except Exception:
        pass
    return None

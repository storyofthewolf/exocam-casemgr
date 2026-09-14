#!/usr/bin/env python3
"""
derive_utils.py -- derived inputs for exocam-casemgr (`build.py prep`).

A utility module, not a user-facing tool: the `_utils` suffix marks it as
never invoked directly (same convention as parse_utils.py / manage_utils.py).
Imported by build.py (planning + the prep verb) and scan.py (manifest
read-back).

The matrix declares physics; casemgr derives the filenames. Two chains:

  Chain A (stellar)   star {teff, feh, logg}  --stellar-interpolator-->  raw SED
                      raw SED + exort_pkg     --ExoRT binner-->          exo_solar_file (.nc)
                      exo_solar_file          --ExoCAM albedo calc-->    cice_params (4 floats)
  Chain B (IC)        ic_template + pstd + nlev + ic_dry  --ExoCAM mod_ncdata-->  ncdata

Ownership: the engines live in their home repos (stellar-interpolator, ExoRT/tools,
ExoCAM/tools/py_progs) and are imported from the paths in config_registry.yaml.
This module owns orchestration, naming, caching, provenance and verification only.

Two layers, deliberately separate:

  * plan_derived_inputs(spec, registry, paths)  -- PURE. Resolves what a case needs
    and what each product is called. Used by `generate` (to emit the
    required-inputs block and the effective exo_solar_file / ncdata), by
    `--verify` (to report names), and by `prep` (to decide what to build).
    Touches no engine and reads no input file, so it runs anywhere.

  * derive_*(item, paths)  -- the engines. Run by `prep --execute` only, where the
    data lives. Each writes to a temporary sibling and renames at the end, and
    drops a `<file>.prep.yaml` manifest beside the product.

Explicit exo_solar_file / cice_params / ncdata in the matrix always win;
derivation fills gaps, never overrides.
"""
import datetime
import os
import subprocess
import sys

import yaml

ICE_SURFACES = ('blueice', 'mixed')
DEFAULT_ICE_SURFACE = 'blueice'
MANIFEST_SUFFIX = '.prep.yaml'

# Subdirectories of paths.derived_inputs
SOLAR_SUBDIR = 'solar'
SED_SUBDIR = 'solar/sed'
ALBEDO_SUBDIR = 'albedo'
IC_SUBDIR = 'ic'

# Shell variable names the albedo product defines, in namelist-key order.
ALBEDO_KEYS = ('albicev', 'albicei', 'albsnowv', 'albsnowi')
ALBEDO_SHELL_VARS = {k: k.upper() for k in ALBEDO_KEYS}

# Comment line the build script leaves in user_nl_cice naming the albedo
# product the values came from. Namelist parsers skip `!` lines; scan.py
# reads this one back to recover the ice surface and the star.
ALBEDO_PROVENANCE_PREFIX = '! casemgr derived albedos:'

# ExoRT resolution tag per exort_pkg (mirrors build.SOLAR_FILE_STEMS; kept
# here too so this module has no import of build.py).
EXORT_RES = {
    'n68equiv':   'n68',
    'n84equiv':   'n84',
    'n28archean': 'n28',
    'n42h2o':     'n42',
}


class DeriveError(ValueError):
    """A derivation that cannot be planned or run. Message is user-facing."""


class DerivedInput:
    """One product a case needs: what it is, where it goes, how it is made."""

    def __init__(self, kind, path, recipe, summary):
        self.kind = kind          # 'solar' | 'albedo' | 'ic'
        self.path = path          # absolute product path
        self.recipe = recipe      # dict, everything needed to (re)make it
        self.summary = summary    # one-line human description

    def __repr__(self):
        return f"DerivedInput({self.kind}, {self.path})"


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def fmt_num(x):
    """Compact, unambiguous number for a filename: 3400.0 -> '3400',
    4.5 -> '4.5', 0.0 -> '0.0', -0.5 -> '-0.5'."""
    x = float(x)
    if x == int(x) and abs(x) >= 10:
        return str(int(x))
    s = f"{x:.6g}"
    if '.' not in s and 'e' not in s:
        s += '.0'
    return s


def parse_star(value):
    """Normalize the matrix `star` key.

    None / absent -> None (matrix says nothing about the star).
    'sun'         -> 'sun'
    {teff, feh, logg} -> dict of floats (feh defaults 0.0, logg 4.5; both are
                          part of the product name so nothing is hidden).
    """
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() == 'sun':
            return 'sun'
        raise DeriveError(f"star: expected 'sun' or a {{teff, feh, logg}} mapping, got '{value}'")
    if isinstance(value, dict):
        if 'teff' not in value:
            raise DeriveError("star: mapping needs at least 'teff' (K)")
        try:
            star = {
                'teff': float(value['teff']),
                'feh': float(value.get('feh', 0.0)),
                'logg': float(value.get('logg', 4.5)),
            }
        except (TypeError, ValueError) as e:
            raise DeriveError(f"star: non-numeric value ({e})")
        extra = set(value) - {'teff', 'feh', 'logg', 'method'}
        if extra:
            raise DeriveError(f"star: unknown key(s) {sorted(extra)}")
        star['method'] = str(value.get('method', 'log'))
        if star['method'] not in ('log', 'linear'):
            raise DeriveError("star.method must be 'log' or 'linear'")
        return star
    raise DeriveError(f"star: unrecognized value {value!r}")


def star_tag(star):
    return f"T{fmt_num(star['teff'])}_g{fmt_num(star['logg'])}_m{fmt_num(star['feh'])}"


def sed_name(star):
    return f"btsettl_{star_tag(star)}.txt"


def solar_name(star, res):
    return f"btsettl_{star_tag(star)}_{res}.nc"


def albedo_product_name(solar_path, ice_surface):
    stem = os.path.splitext(os.path.basename(solar_path))[0]
    return f"{stem}__{ice_surface}.albedos.sh"


def ic_name(template_tag, pressure_str, nlev, dry):
    return f"ic_{template_tag}_{pressure_str}_L{int(nlev)}{'_dry' if dry else ''}_ic.nc"


def exort_res(exort_pkg):
    pkg = (exort_pkg or '').rstrip('*')
    if pkg not in EXORT_RES:
        raise DeriveError(f"cannot derive a solar file for exort_pkg='{exort_pkg}' "
                          f"(known: {', '.join(sorted(EXORT_RES))})")
    return EXORT_RES[pkg]


def sun_solar_file(exort_pkg, paths):
    """The shipped solar spectrum for this RT package -- the same default
    generate_shell_script has always applied."""
    res = exort_res(exort_pkg)
    return f"{paths.get('exort_root', '$EXORT')}/data/solar/G2V_SUN_{res}.nc"


def derived_root(paths):
    root = paths.get('derived_inputs')
    if not root:
        raise DeriveError("config_registry.yaml has no paths.derived_inputs -- "
                          "set it to the directory that holds derived solar, "
                          "albedo and IC products")
    return root


def ic_templates(registry):
    return registry.get('ic_templates', {}) or {}


def ic_template_path(tag, registry, paths):
    """Resolve an ic_templates tag to a path: absolute as given, else under
    {exocam_root}/cesm1.2.1/initial_files/."""
    table = ic_templates(registry)
    if tag not in table:
        raise DeriveError(f"ic_template '{tag}' is not in config_registry.yaml "
                          f"ic_templates (known: {', '.join(sorted(table)) or 'none'})")
    value = str(table[tag])
    if value.startswith('/'):
        return value
    return f"{paths.get('exocam_root', '$EXOCAM')}/cesm1.2.1/initial_files/{value}"


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------

def plan_derived_inputs(spec, registry, paths, pstd_bar=None, pressure_str=None):
    """Decide what a resolved case derives and what each product is called.

    Returns (items, effective, warnings):
      items     : list[DerivedInput] in dependency order (solar, albedo, ic)
      effective : {'exo_solar_file': path?, 'ncdata': path?, 'albedo_product': path?}
                  -- values generate should use where the matrix is silent
      warnings  : list[str]
    Raises DeriveError for a matrix that asks for something impossible.

    pstd_bar / pressure_str are supplied by build.py (compute_pstd_from_spec /
    bar_to_pressure_str) so this module never recomputes composition.
    """
    items, effective, warnings = [], {}, []
    is_clone = bool(spec.get('clone'))

    star = parse_star(spec.get('star'))
    exort_pkg = spec.get('exort_pkg', '')

    # ---- Chain A: solar file ------------------------------------------------
    explicit_solar = spec.get('exo_solar_file') or None
    solar_path = explicit_solar
    if star is not None and explicit_solar:
        warnings.append(
            f"star is set but exo_solar_file is explicit -- the explicit file "
            f"wins ({os.path.basename(explicit_solar)}); star is used only "
            f"to opt in to albedo derivation")
    elif star == 'sun':
        solar_path = sun_solar_file(exort_pkg, paths)
        effective['exo_solar_file'] = solar_path
    elif isinstance(star, dict):
        res = exort_res(exort_pkg)
        root = derived_root(paths)
        solar_path = os.path.join(root, SOLAR_SUBDIR, solar_name(star, res))
        sed_path = os.path.join(root, SED_SUBDIR, sed_name(star))
        items.append(DerivedInput(
            'solar', solar_path,
            {'star': dict(star), 'resolution': res, 'sed': sed_path,
             'exort_pkg': exort_pkg},
            f"exo_solar_file <- BT-Settl Teff={fmt_num(star['teff'])} K "
            f"logg={fmt_num(star['logg'])} [M/H]={fmt_num(star['feh'])} "
            f"({star['method']}), binned to {res}"))
        effective['exo_solar_file'] = solar_path

    # ---- Chain A: albedos ---------------------------------------------------
    derive_alb = spec.get('derive_albedos', True)
    if isinstance(derive_alb, str):
        derive_alb = derive_alb.strip().lower() == 'true'
    ice_surface = spec.get('ice_surface', DEFAULT_ICE_SURFACE)
    if ice_surface not in ICE_SURFACES:
        raise DeriveError(f"ice_surface '{ice_surface}' not one of {ICE_SURFACES}")
    explicit_cice = spec.get('cice_params')
    if star is not None and derive_alb:
        if explicit_cice:
            warnings.append("star is set and derive_albedos is true, but cice_params "
                            "is explicit -- the explicit albedos win, none derived")
        elif not solar_path:
            raise DeriveError("cannot derive albedos: no solar file (set star or exo_solar_file)")
        else:
            root = derived_root(paths)
            alb_path = os.path.join(root, ALBEDO_SUBDIR,
                                    albedo_product_name(solar_path, ice_surface))
            items.append(DerivedInput(
                'albedo', alb_path,
                {'solar_file': solar_path, 'ice_surface': ice_surface},
                f"cice albedos <- {os.path.basename(solar_path)} "
                f"(ice: {ice_surface}, snow: snow100um)"))
            effective['albedo_product'] = alb_path
    elif star is None and derive_alb and 'derive_albedos' in spec:
        warnings.append("derive_albedos is set but no 'star' key -- albedos are "
                        "derived only when the matrix names the star (set "
                        "star: sun to opt in for the Sun)")

    # ---- Chain B: IC --------------------------------------------------------
    tag = spec.get('ic_template')
    if tag:
        if spec.get('ncdata'):
            warnings.append("ic_template is set but ncdata is explicit -- the "
                            "explicit ncdata wins, no IC derived")
        elif is_clone and not spec.get('config_type'):
            raise DeriveError("ic_template on a clone needs config_type and nlev "
                              "(the pressure and level count come from the matrix)")
        else:
            if pstd_bar is None or pressure_str is None or not spec.get('nlev'):
                raise DeriveError("ic_template needs nlev and a computable pstd")
            template = ic_template_path(str(tag), registry, paths)
            dry = spec.get('ic_dry', True)
            if isinstance(dry, str):
                dry = dry.strip().lower() == 'true'
            config_type = spec.get('config_type', 'unknown')
            root = derived_root(paths)
            ic_path = os.path.join(root, IC_SUBDIR, config_type,
                                   ic_name(str(tag), pressure_str, spec['nlev'], dry))
            items.append(DerivedInput(
                'ic', ic_path,
                {'template_tag': str(tag), 'template': template,
                 'pstd_bar': float(pstd_bar), 'nlev': int(spec['nlev']),
                 'dry': bool(dry), 'ps_mode': 'flat', 'config_type': config_type},
                f"ncdata <- {os.path.basename(template)} @ {pressure_str}, "
                f"L{int(spec['nlev'])}{', dry' if dry else ''}"))
            effective['ncdata'] = ic_path

    return items, effective, warnings


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def _git_sha(directory):
    try:
        out = subprocess.run(['git', '-C', directory, 'rev-parse', '--short', 'HEAD'],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def manifest_path(product_path):
    return product_path + MANIFEST_SUFFIX


def write_manifest(item, extra, tools):
    """Sidecar YAML beside the product: kind, recipe, tool provenance."""
    data = {
        'kind': item.kind,
        'product': item.path,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'recipe': item.recipe,
        'result': extra or {},
        'tools': tools,
        'casemgr': _git_sha(os.path.dirname(os.path.abspath(__file__))),
    }
    with open(manifest_path(item.path), 'w') as f:
        yaml.safe_dump(data, f, sort_keys=False)


def read_manifest(product_path):
    p = manifest_path(product_path)
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as f:
            return yaml.safe_load(f) or None
    except (OSError, yaml.YAMLError):
        return None


# ---------------------------------------------------------------------------
# Engines (prep --execute only)
# ---------------------------------------------------------------------------

def _tool_dir(paths, key, default_rel=None, base_key=None):
    d = paths.get(key)
    if not d and base_key and default_rel:
        base = paths.get(base_key)
        if base:
            d = os.path.join(base, default_rel)
    if not d or not os.path.isdir(d):
        raise DeriveError(f"tool directory for '{key}' not found ({d or 'unset'}); "
                          f"set paths.{key} in config_registry.yaml")
    return d


def _import_from(directory, module):
    if directory not in sys.path:
        sys.path.insert(0, directory)
    try:
        return __import__(module)
    except ImportError as e:
        raise DeriveError(f"cannot import {module} from {directory}: {e}")


def derive_solar(item, paths, verbose=True):
    """Chain A steps 1-2: interpolate the SED, bin it to the ExoRT grid."""
    si_dir = _tool_dir(paths, 'stellar_interpolator')
    grid_dir = paths.get('btsettl_grid') or os.path.join(si_dir, 'grid')
    if not os.path.isdir(grid_dir):
        raise DeriveError(f"BT-Settl grid directory not found: {grid_dir} "
                          f"(paths.btsettl_grid)")
    exort_tools = _tool_dir(paths, 'exort_tools', 'tools', 'exort_root')

    btsettl_io = _import_from(si_dir, 'btsettl_io')
    interp = _import_from(si_dir, 'interp_btsettl')
    binner = _import_from(exort_tools, 'makeStellarSpectrum_fromSED')

    star = item.recipe['star']
    sed_path = item.recipe['sed']
    os.makedirs(os.path.dirname(sed_path), exist_ok=True)
    os.makedirs(os.path.dirname(item.path), exist_ok=True)

    if os.path.isfile(sed_path):
        if verbose:
            print(f"    sed present: {sed_path}")
        prov = None
    else:
        grid = btsettl_io.SpectralGrid.from_dir(grid_dir)
        try:
            wav, flux, prov = interp.interpolate(
                grid, star['teff'], star['feh'], star['logg'],
                method=star.get('method', 'log'), verbose=False)
        except (ValueError, FileNotFoundError) as e:
            raise DeriveError(f"stellar-interpolator: {e}")
        tmp = sed_path + '.partial'
        interp.write_sed(tmp, wav, flux, provenance=prov)
        os.replace(tmp, sed_path)
        if verbose:
            print(f"    sed written: {sed_path} ({len(wav)} rows)")

    tmp = item.path + '.partial'
    result = binner.make_stellar_file(sed_path, outname=tmp,
                                      resolution=item.recipe['resolution'],
                                      plot=False, verbose=False)
    os.replace(tmp, item.path)
    result['outname'] = item.path
    write_manifest(item, {'binner': result, 'grid_dir': grid_dir},
                   {'stellar_interpolator': {'dir': si_dir, 'git': _git_sha(si_dir)},
                    'exort_tools': {'dir': exort_tools, 'git': _git_sha(exort_tools)}})
    return item.path


def albedo_product_lines(values, item):
    """The shell fragment the build script sources: four variables, plus a
    comment block that makes the file self-describing."""
    lines = [
        "# Derived CICE broadband albedos -- written by exocam-casemgr `build.py prep`",
        f"# stellar file : {item.recipe['solar_file']}",
        f"# ice surface  : {item.recipe['ice_surface']}   (albice*)",
        f"# snow surface : snow100um                (albsnow*)",
        f"# manifest     : {os.path.basename(manifest_path(item.path))}",
        "# Sourced by the generated build script; the values are upserted into",
        "# user_nl_cice. Do not hand-edit -- change the matrix and re-run prep.",
    ]
    for key in ALBEDO_KEYS:
        lines.append(f"{ALBEDO_SHELL_VARS[key]}={values[key]:.6f}")
    return lines


def derive_albedos(item, paths, verbose=True):
    """Chain A step 3: SED-weighted broadband albedos for ice and snow."""
    py_progs = _tool_dir(paths, 'exocam_py_progs', 'tools/py_progs', 'exocam_root')
    calc = _import_from(py_progs, 'broadband_albedo_calculator')
    solar = item.recipe['solar_file']
    if not os.path.isfile(solar):
        raise DeriveError(f"solar file not found: {solar}")
    r = calc.compute_cice_albedos(solar, ice_surface=item.recipe['ice_surface'])
    values = {k: float(r[k]) for k in ALBEDO_KEYS}
    os.makedirs(os.path.dirname(item.path), exist_ok=True)
    tmp = item.path + '.partial'
    with open(tmp, 'w') as f:
        f.write('\n'.join(albedo_product_lines(values, item)) + '\n')
    os.replace(tmp, item.path)
    write_manifest(item,
                   {'values': values,
                    'ice': {k: r['ice'][k] for k in ('vis', 'ir', 'bond', 'albedo_file')},
                    'snow': {k: r['snow'][k] for k in ('vis', 'ir', 'bond', 'albedo_file')}},
                   {'exocam_py_progs': {'dir': py_progs, 'git': _git_sha(py_progs)}})
    if verbose:
        print("    " + "  ".join(f"{k}={values[k]:.4f}" for k in ALBEDO_KEYS))
    return item.path


def read_albedo_product(path):
    """Parse the four values back out of a product file. None if unreadable."""
    vals = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    for key, var in ALBEDO_SHELL_VARS.items():
                        if k.strip() == var:
                            vals[key] = float(v)
    except (OSError, ValueError):
        return None
    return vals if len(vals) == len(ALBEDO_KEYS) else None


def derive_ic(item, paths, verbose=True):
    """Chain B: template IC -> ncdata at the case's pressure and level count."""
    py_progs = _tool_dir(paths, 'exocam_py_progs', 'tools/py_progs', 'exocam_root')
    mod = _import_from(py_progs, 'mod_ncdata')
    r = item.recipe
    if not os.path.isfile(r['template']):
        raise DeriveError(f"IC template not found: {r['template']}")
    try:
        result = mod.modify_ncdata(r['template'], item.path, nlev=r['nlev'],
                                   pstd_bar=r['pstd_bar'], dry=r['dry'],
                                   ps_mode=r.get('ps_mode', 'flat'),
                                   exocam_root=paths.get('exocam_root'),
                                   overwrite=True, verbose=verbose)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        raise DeriveError(f"mod_ncdata: {e}")
    write_manifest(item, result,
                   {'exocam_py_progs': {'dir': py_progs, 'git': _git_sha(py_progs)}})
    return item.path


DERIVERS = {'solar': derive_solar, 'albedo': derive_albedos, 'ic': derive_ic}


def derive(item, paths, verbose=True):
    return DERIVERS[item.kind](item, paths, verbose=verbose)


# ---------------------------------------------------------------------------
# Provenance read-back (scan.py)
# ---------------------------------------------------------------------------

def albedo_product_from_nl(user_nl_cice_path):
    """The albedo product path recorded in user_nl_cice, or None."""
    try:
        with open(user_nl_cice_path) as f:
            for line in f:
                if line.startswith(ALBEDO_PROVENANCE_PREFIX):
                    return line[len(ALBEDO_PROVENANCE_PREFIX):].strip() or None
    except OSError:
        return None
    return None


def provenance_fields(solar_file=None, ncdata=None, user_nl_cice=None):
    """Registry fields recovered from prep manifests beside the products a
    case actually references. Every field is None when there is no manifest,
    so hand-made inputs read exactly as before."""
    out = {'star_teff': None, 'star_feh': None, 'star_logg': None,
           'star_method': None, 'ice_surface': None, 'albedo_product': None,
           'ic_template': None, 'ic_dry': None, 'ic_pstd_bar': None}
    m = read_manifest(solar_file) if solar_file else None
    if m and m.get('kind') == 'solar':
        star = (m.get('recipe') or {}).get('star') or {}
        out['star_teff'] = star.get('teff')
        out['star_feh'] = star.get('feh')
        out['star_logg'] = star.get('logg')
        out['star_method'] = star.get('method')
    product = albedo_product_from_nl(user_nl_cice) if user_nl_cice else None
    if product:
        out['albedo_product'] = product
        m = read_manifest(product)
        if m and m.get('kind') == 'albedo':
            out['ice_surface'] = (m.get('recipe') or {}).get('ice_surface')
    m = read_manifest(ncdata) if ncdata else None
    if m and m.get('kind') == 'ic':
        r = m.get('recipe') or {}
        out['ic_template'] = r.get('template_tag')
        out['ic_dry'] = r.get('dry')
        out['ic_pstd_bar'] = r.get('pstd_bar')
    return out

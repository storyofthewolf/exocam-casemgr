"""
fields_check.py — `runmgr.py check --fields`: semantic spot checks.

Over ONE window of monthly cam.h0 files a case gets one number per reported
field (area-weighted global mean) and a verdict per verified field (present,
finite, not all zero). No series, slopes, maps or profiles, and nothing is
written: those belong to exocam-trend and exocam-analysis. Design:
docs/DESIGN_check_fields.md.

Built agent-first: `run_fields_check` returns a plain dict per case (the
JSON record); the text line is rendered from that dict, never the other way
round. Self-contained: weights come from the file's own `gw` (fallback:
cell-edge weights from `lat`, computed here) — no dependency on any other
exocam repo.
"""

import hashlib
import math
import os
import re
import subprocess

import yaml

from utils.manage_utils import REPO_ROOT, select_last_monthly_hist

DEFAULT_FIELD_SETS = os.path.join(REPO_ROOT, 'field_sets.yaml')
RECORD_VERSION = 1
SET_KEYS = {'report', 'verify', 'derived', 'show', 'include'}
MAX_LISTED_FILES = 24          # failed_files list cap; n_failed is the full count
_NAME = r'[A-Za-z_][A-Za-z0-9_]*'


class FieldSpecError(ValueError):
    """A bad --fields spec or field_sets.yaml entry (usage error, exit 2)."""


# ---------------------------------------------------------------------------
# field_sets.yaml and spec resolution
# ---------------------------------------------------------------------------

def parse_expression(expr):
    """Parse 'A - B + C' into [(+1, 'A'), (-1, 'B'), (+1, 'C')].

    Only names joined by + and -; a leading sign is allowed. Anything else
    (numbers, *, /, parentheses, calls) is rejected: derived fields are
    parsed, never evaluated.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise FieldSpecError(f"derived expression must be a non-empty string, got {expr!r}")
    s = expr.strip()
    terms, pos, first = [], 0, True
    tok = re.compile(r'\s*([+-])?\s*(' + _NAME + r')\s*')
    while pos < len(s):
        m = tok.match(s, pos)
        if not m or m.end() == pos or (m.group(1) is None and not first):
            raise FieldSpecError(
                f"derived expression {expr!r}: only field names joined by + and - are allowed")
        terms.append((-1 if m.group(1) == '-' else 1, m.group(2)))
        pos, first = m.end(), False
    return terms


def _str_list(setname, key, val):
    if not isinstance(val, list) or not all(isinstance(v, str) and re.fullmatch(_NAME, v) for v in val):
        raise FieldSpecError(f"set '{setname}': '{key}' must be a list of field names")
    return list(val)


def load_field_sets(path=DEFAULT_FIELD_SETS):
    """Load and validate field_sets.yaml. Returns (sets, md5).

    sets: {name: {'report': [...], 'verify': [...], 'derived': {n: terms},
                  'show': [...] or None, 'include': [...]}}
    Raises FieldSpecError naming the file and key on any problem.
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        raise FieldSpecError(f"cannot read field sets file {path}: {e}")
    md5 = hashlib.md5(raw).hexdigest()
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise FieldSpecError(f"{path}: invalid YAML: {e}")
    if not isinstance(data, dict):
        raise FieldSpecError(f"{path}: top level must be a mapping of set names")
    sets = {}
    for name, body in data.items():
        if not isinstance(name, str) or not re.fullmatch(_NAME, name):
            raise FieldSpecError(f"{path}: bad set name {name!r}")
        if not isinstance(body, dict):
            raise FieldSpecError(f"{path}: set '{name}' must be a mapping")
        unknown = set(body) - SET_KEYS
        if unknown:
            raise FieldSpecError(f"{path}: set '{name}': unknown key(s) {sorted(unknown)}; "
                                 f"allowed: {sorted(SET_KEYS)}")
        derived_raw = body.get('derived') or {}
        if not isinstance(derived_raw, dict):
            raise FieldSpecError(f"{path}: set '{name}': 'derived' must be a mapping")
        try:
            derived = {}
            for dn, expr in derived_raw.items():
                if not isinstance(dn, str) or not re.fullmatch(_NAME, dn):
                    raise FieldSpecError(f"derived name {dn!r} is not a valid name")
                derived[dn] = parse_expression(expr)
        except FieldSpecError as e:
            raise FieldSpecError(f"{path}: set '{name}': {e}")
        sets[name] = {
            'report':  _str_list(name, 'report',  body.get('report')  or []),
            'verify':  _str_list(name, 'verify',  body.get('verify')  or []),
            'derived': derived,
            'show':    _str_list(name, 'show', body['show']) if body.get('show') is not None else None,
            'include': _str_list(name, 'include', body.get('include') or []),
        }
    # Validate includes and derived operands after every set is known.
    for name in sets:
        _expand_set(name, sets, path)
    return sets, md5


def _expand_set(name, sets, path, stack=()):
    """Return the flattened plan of one set (includes merged first)."""
    if name not in sets:
        where = f"included from '{stack[-1]}'" if stack else "requested"
        raise FieldSpecError(f"unknown field set '@{name}' ({where}); "
                             f"defined in {path}: {', '.join('@' + s for s in sorted(sets))}")
    if name in stack:
        raise FieldSpecError(f"{path}: include cycle: {' -> '.join(stack + (name,))}")
    body = sets[name]
    plan = _empty_plan()
    for inc in body['include']:
        _merge(plan, _expand_set(inc, sets, path, stack + (name,)))
    own = _empty_plan()
    own['report'] = list(body['report'])
    own['verify'] = list(body['verify'])
    own['derived'] = dict(body['derived'])
    _merge(plan, own, show=False)
    for dn, terms in plan['derived'].items():
        missing = [t for _, t in terms if t not in plan['report']]
        if missing:
            raise FieldSpecError(f"{path}: set '{name}': derived '{dn}' uses "
                                 f"{missing}, which the set does not report")
    if body['show'] is not None:
        bad = [s for s in body['show'] if s not in plan['report'] and s not in plan['derived']]
        if bad:
            raise FieldSpecError(f"{path}: set '{name}': show lists {bad}, "
                                 f"which are neither reported nor derived")
        plan['show'] = list(body['show'])
    else:
        plan['show'] = _dedup(plan['show'] + body['report'] + list(body['derived']))
    return plan


def _empty_plan():
    return {'report': [], 'verify': [], 'derived': {}, 'show': []}


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _merge(into, other, show=True):
    into['report'] = _dedup(into['report'] + other['report'])
    into['verify'] = _dedup(into['verify'] + other['verify'])
    for k, v in other['derived'].items():
        into['derived'].setdefault(k, v)
    if show:
        into['show'] = _dedup(into['show'] + other['show'])


def resolve_spec(spec, sets, path=DEFAULT_FIELD_SETS):
    """Resolve a --fields spec ('@energy,TMQ,@clearsky') into one plan.

    '@name' expands a set (includes first); a bare name is reported.
    Returns {'report', 'verify', 'derived', 'show'}.
    """
    items = [s.strip() for s in (spec or '').split(',') if s.strip()]
    if not items:
        raise FieldSpecError("--fields needs at least one field name or @set")
    plan = _empty_plan()
    for item in items:
        if item.startswith('@'):
            _merge(plan, _expand_set(item[1:], sets, path))
        elif re.fullmatch(_NAME, item):
            _merge(plan, {'report': [item], 'verify': [], 'derived': {}, 'show': [item]})
        else:
            raise FieldSpecError(f"--fields: {item!r} is neither a field name nor @set")
    return plan


# ---------------------------------------------------------------------------
# Weights and per-file reading
# ---------------------------------------------------------------------------

def lat_edge_weights(lat):
    """Cell-edge area weights per latitude row (edges at midpoints, poles at ±90°),
    normalised to sum 1. Equals CAM's gw on the FV grid."""
    import numpy as np
    lat = np.asarray(lat, dtype=float)
    rad = np.deg2rad(lat)
    edges = np.empty(len(lat) + 1)
    edges[0], edges[-1] = -math.pi / 2, math.pi / 2
    edges[1:-1] = 0.5 * (rad[:-1] + rad[1:])
    w = np.abs(np.sin(edges[1:]) - np.sin(edges[:-1]))
    return w / w.sum()


def area_weights(ds):
    """2-D (lat, lon) weights summing to 1, and which source was used."""
    import numpy as np
    lat_name = next((v for v in ('lat', 'latitude') if v in ds.variables), None)
    lon_name = next((v for v in ('lon', 'longitude') if v in ds.variables), None)
    if lat_name is None or lon_name is None:
        raise KeyError("lat/lon coordinates not found")
    nlon = ds.variables[lon_name].shape[0]
    if 'gw' in ds.variables:
        w1 = np.asarray(ds.variables['gw'][:], dtype=float)
        kind = 'gw'
    else:
        w1 = lat_edge_weights(ds.variables[lat_name][:])
        kind = 'lat-edges'
    w2 = np.repeat((w1 / w1.sum())[:, None], nlon, axis=1) / nlon
    return w2, kind


def _read(var):
    """Return (data ndarray float64, bad mask) for one variable, time squeezed."""
    import numpy as np
    raw = var[:]
    data = np.asarray(np.ma.getdata(raw), dtype=float)
    bad = np.ma.getmaskarray(raw) | ~np.isfinite(data)
    if data.ndim >= 1 and 'time' in var.dimensions and data.shape[var.dimensions.index('time')] == 1:
        ax = var.dimensions.index('time')
        data, bad = np.take(data, 0, axis=ax), np.take(bad, 0, axis=ax)
    return data, bad


def _stem(fname):
    m = re.search(r'\.(\d{4}-\d{2})\.nc$', fname)
    return m.group(1) if m else fname


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _git_describe():
    try:
        out = subprocess.run(['git', '-C', REPO_ROOT, 'describe', '--always', '--dirty'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_DESCRIBE = None


def provenance(spec, sets_md5, weights_kind):
    global _DESCRIBE
    if _DESCRIBE is None:
        _DESCRIBE = _git_describe() or 'unknown'
    return {'tool': 'runmgr.py check --fields', 'commit': _DESCRIBE,
            'weights': weights_kind, 'fields_spec': spec, 'field_sets_md5': sets_md5}


def _skipped(record, reason, fix=None):
    record['verdict'] = 'skipped'
    record['skip_reason'] = reason
    if fix:
        record['fix'] = fix
    return record


def run_fields_check(case, hist_dir, source, plan, spec, sets_md5,
                     years=1, expect_zero=False, status=None):
    """Run the check for one case over the last `years` model years.

    Returns the JSON record (dict). verdict: 'pass' | 'fail' | 'skipped'.
    Never raises for data problems; they land in the record.
    """
    n_req = 12 * years
    record = {'record_version': RECORD_VERSION, 'case': case, 'status': status,
              'verdict': None,
              'window': {'source': source, 'dir': hist_dir, 'first': None, 'last': None,
                         'files': 0, 'requested': n_req, 'short': False},
              'values': {}, 'checks': {}, 'warnings': [],
              'provenance': provenance(spec, sets_md5, None)}
    try:
        import numpy as np
        import netCDF4 as nc4
    except ImportError as e:
        return _skipped(record, f"python package missing: {e.name}",
                        'export PATH="$HPC_PYTHON_BIN:$PATH" in the payload (site Python env)')
    if not os.path.isdir(hist_dir):
        return _skipped(record, f"directory not found: {hist_dir}")
    files, first, last = select_last_monthly_hist(hist_dir, 'atm', n_req)
    if not files:
        return _skipped(record, f"no monthly cam.h0 files in {hist_dir}")
    win = record['window']
    win.update(first=first, last=last, files=len(files), short=len(files) < n_req)
    if win['short']:
        record['warnings'].append(f"only {len(files)} of {n_req} requested month(s) available")

    test = 'zero' if expect_zero else 'nonzero'
    monthly = {n: [] for n in plan['report']}
    units = {}
    rep_bad = {n: {} for n in plan['report']}       # name -> {stem: reason}
    ver_bad = {n: {} for n in plan['verify']}
    weights_kind = None
    w2 = None
    for fname in files:
        stem = _stem(fname)
        try:
            ds = nc4.Dataset(os.path.join(hist_dir, fname))
        except (OSError, RuntimeError) as e:
            for n in plan['report']:
                rep_bad[n][stem] = f"unreadable file ({e})"
            for n in plan['verify']:
                ver_bad[n][stem] = 'unreadable file'
            continue
        try:
            if w2 is None and plan['report']:
                try:
                    w2, weights_kind = area_weights(ds)
                except KeyError as e:
                    record['warnings'].append(str(e))
            for n in plan['report']:
                if n not in ds.variables:
                    rep_bad[n][stem] = 'missing'
                    continue
                v = ds.variables[n]
                units.setdefault(n, getattr(v, 'units', None))
                data, bad = _read(v)
                if data.ndim != 2:
                    rep_bad[n][stem] = (f"not 2-D (dims {v.dimensions}); report fields must be "
                                        f"(lat, lon) — use verify, or exocam-trend/analysis for levels")
                    continue
                if bad.any():
                    rep_bad[n][stem] = 'non-finite or fill values'
                    continue
                if w2 is None or w2.shape != data.shape:
                    rep_bad[n][stem] = 'no usable area weights'
                    continue
                monthly[n].append(float(np.sum(data * w2)))
            for n in plan['verify']:
                if n not in ds.variables:
                    ver_bad[n][stem] = 'missing'
                    continue
                data, bad = _read(ds.variables[n])
                if bad.any():
                    ver_bad[n][stem] = 'non-finite or fill values'
                elif expect_zero and np.any(data != 0):
                    ver_bad[n][stem] = 'not all zero'
                elif not expect_zero and not np.any(data != 0):
                    ver_bad[n][stem] = 'all zero'
        finally:
            ds.close()

    failed = False
    means = {}
    for n in plan['report']:
        if rep_bad[n] or not monthly[n]:
            means[n] = None
            failed = True
        else:
            means[n] = sum(monthly[n]) / len(monthly[n])
    for dn, terms in plan['derived'].items():
        vals = [means.get(t) for _, t in terms]
        means[dn] = None if any(v is None for v in vals) else sum(s * v for (s, _), v in zip(terms, vals))
        u = [units.get(t) for _, t in terms]
        units[dn] = u[0] if u and all(x == u[0] for x in u) else None

    for n in plan['show']:
        entry = {'mean': means.get(n), 'units': units.get(n)}
        if n in rep_bad:
            entry['ok'] = not rep_bad[n] and means.get(n) is not None
            if rep_bad[n]:
                entry.update(_failure_summary(rep_bad[n]))
        else:
            entry['ok'] = means.get(n) is not None
        record['values'][n] = entry
    # A reported field not shown still decides the verdict; surface its failure.
    for n in plan['report']:
        if n not in record['values'] and rep_bad[n]:
            record['values'][n] = {'mean': None, 'units': units.get(n), 'ok': False,
                                   'shown': False, **_failure_summary(rep_bad[n])}
    for n in plan['verify']:
        c = {'ok': not ver_bad[n], 'test': test}
        if ver_bad[n]:
            c.update(_failure_summary(ver_bad[n]))
            failed = True
        record['checks'][n] = c

    record['verdict'] = 'fail' if failed else 'pass'
    record['provenance'] = provenance(spec, sets_md5, weights_kind)
    return record


def _failure_summary(bad):
    stems = sorted(bad)
    reasons = sorted(set(bad.values()))
    return {'n_failed': len(stems), 'failed_files': stems[:MAX_LISTED_FILES],
            'reason': reasons[0] if len(reasons) == 1 else reasons}


# ---------------------------------------------------------------------------
# Text rendering (from the record)
# ---------------------------------------------------------------------------

def _fmt_value(name, entry, derived_names):
    v = entry.get('mean')
    if v is None:
        return f"{name} —"
    if name in derived_names:
        s = f"{v:+.1f}"
    elif abs(v) >= 10:
        s = f"{v:.1f}"
    else:
        s = f"{v:.3f}"
    u = entry.get('units')
    return f"{name} {s}" + (f" {u}" if u and len(u) <= 6 and name not in derived_names else "")


def render_text(record, derived_names=(), indent='  '):
    """Lines for one case, rendered from its record."""
    w = record['window']
    verdict = record['verdict'].upper()
    if record['verdict'] == 'skipped':
        line = f"{indent}fields: SKIPPED — {record['skip_reason']}"
        if record.get('fix'):
            line += f"  (fix: {record['fix']})"
        return [line]
    head = (f"{indent}fields {w['first']}..{w['last']} ({w['files']}/{w['requested']} "
            f"{w['source']})")
    vals = [_fmt_value(n, e, derived_names) for n, e in record['values'].items()
            if e.get('shown', True)]
    ver = ''
    if record['checks']:
        n_ok = sum(1 for c in record['checks'].values() if c['ok'])
        test = next(iter(record['checks'].values()))['test']
        ver = f"verify[{test}] {n_ok}/{len(record['checks'])} ok"
    lines = [f"{head}  " + "  ".join(vals + ([ver] if ver else [])) + f"  {verdict}"]
    for n, e in record['values'].items():
        if not e.get('ok', True) and e.get('n_failed'):
            lines.append(f"{indent}    {n}  {e['reason']} in {e['n_failed']} file(s) "
                         f"({', '.join(e['failed_files'][:6])}{', …' if e['n_failed'] > 6 else ''})")
    for n, c in record['checks'].items():
        if not c['ok']:
            lines.append(f"{indent}    {n}  {c['reason']} in {c['n_failed']} file(s) "
                         f"({', '.join(c['failed_files'][:6])}{', …' if c['n_failed'] > 6 else ''})")
    for wmsg in record['warnings']:
        lines.append(f"{indent}    WARNING: {wmsg}")
    return lines


def describe_sets(sets, path, md5):
    """The --list-sets payload."""
    out = {'field_sets': path, 'field_sets_md5': md5, 'sets': {}}
    for name in sets:
        plan = _expand_set(name, sets, path)
        out['sets'][name] = {
            'report': plan['report'], 'verify': plan['verify'],
            'derived': {k: ' '.join(('-' if s < 0 else '+') + ' ' + t for s, t in v).lstrip('+ ')
                        for k, v in plan['derived'].items()},
            'show': plan['show'], 'include': sets[name]['include'],
        }
    return out

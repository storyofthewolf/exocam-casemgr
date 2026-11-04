#!/usr/bin/env python3
"""
Tests for `runmgr.py check --fields` (utils/fields_check.py).

Synthetic monthly cam.h0 files on the FV 4x5 grid (46 lat incl. poles, 72
lon), written with netCDF4. Covers: weights (file gw vs lat-edge fallback,
non-zero polar rows), set resolution and the derived-expression parser,
verdicts (all zero, NaN, fill, missing, not 2-D, --expect zero), window
selection, and the CLI contract (JSON on stdout, exit codes, NOT_FOUND
records, --source run).

Run:  python3 tests/test_fields_check.py   (or: python3 -m unittest discover tests)
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

try:
    import numpy as np
    import netCDF4 as nc4
except ImportError:          # pragma: no cover
    np = nc4 = None

from utils import fields_check as fc
from utils.manage_utils import select_last_monthly_hist

LAT = [-90.0 + 4.0 * i for i in range(46)]
LON = [5.0 * i for i in range(72)]


def _gw():
    w = fc.lat_edge_weights(LAT)
    return w * 2.0          # CAM's gw sums to 2; normalisation must not care


def write_month(d, case, stem, fields, with_gw=True, fill=None):
    """fields: {name: array (lat,lon) or (ilev,lat,lon)}; fill: {name: value} -> masked."""
    path = os.path.join(d, f"{case}.cam.h0.{stem}.nc")
    ds = nc4.Dataset(path, 'w')
    ds.createDimension('time', None)
    ds.createDimension('lat', len(LAT))
    ds.createDimension('lon', len(LON))
    ds.createDimension('ilev', 3)
    ds.createVariable('lat', 'f8', ('lat',))[:] = LAT
    ds.createVariable('lon', 'f8', ('lon',))[:] = LON
    if with_gw:
        ds.createVariable('gw', 'f8', ('lat',))[:] = _gw()
    for name, arr in fields.items():
        arr = np.asarray(arr, dtype='f4')
        dims = ('time', 'lat', 'lon') if arr.ndim == 2 else ('time', 'ilev', 'lat', 'lon')
        kw = {'fill_value': np.float32(1e36)} if fill and name in fill else {}
        v = ds.createVariable(name, 'f4', dims, **kw)
        v.units = 'K' if name == 'TS' else 'W/m2'
        if fill and name in fill:
            arr = arr.copy()
            arr[0, 0] = 1e36
        v[0] = arr
    ds.close()
    return path


def const(x):
    return np.full((len(LAT), len(LON)), float(x))


def energy_fields(ts=300.0):
    return {'TS': const(ts), 'FSNT': const(250), 'FLNT': const(240),
            'FSNS': const(160), 'FLNS': const(60), 'LHFLX': const(80), 'SHFLX': const(15)}


def months(y0, y1):
    return [f"{y:04d}-{m:02d}" for y in range(y0, y1 + 1) for m in range(1, 13)]


SETS_YAML = os.path.join(REPO, 'field_sets.yaml')


@unittest.skipIf(np is None, "numpy/netCDF4 not available")
class WeightsCase(unittest.TestCase):
    def test_lat_edges_match_cam_gw_shape(self):
        w = fc.lat_edge_weights(LAT)
        self.assertAlmostEqual(w.sum(), 1.0, places=12)
        # polar row: edges -90 and -88 -> (1 - cos 2deg)/2 of the sphere
        self.assertAlmostEqual(w[0], (1 - math.cos(math.radians(2))) / 2, places=12)
        self.assertGreater(w[0], 0)          # cos(lat) at the point would give 0

    def test_gw_and_fallback_agree(self):
        with tempfile.TemporaryDirectory() as d:
            a = write_month(d, 'a', '0001-01', {'TS': const(1)}, with_gw=True)
            b = write_month(d, 'b', '0001-01', {'TS': const(1)}, with_gw=False)
            with nc4.Dataset(a) as da, nc4.Dataset(b) as db:
                wa, ka = fc.area_weights(da)
                wb, kb = fc.area_weights(db)
        self.assertEqual((ka, kb), ('gw', 'lat-edges'))
        self.assertTrue(np.allclose(wa, wb, rtol=0, atol=1e-15))
        self.assertAlmostEqual(wa.sum(), 1.0, places=12)


class SpecCase(unittest.TestCase):
    def test_expression_accepts_sums(self):
        self.assertEqual(fc.parse_expression('FSNT - FLNT'), [(1, 'FSNT'), (-1, 'FLNT')])
        self.assertEqual(fc.parse_expression('-A + B'), [(-1, 'A'), (1, 'B')])

    def test_expression_rejects_everything_else(self):
        for bad in ['A*B', '2*A', 'A -', 'A B', 'os.system("x")', '(A)', '', 'A + 1']:
            with self.assertRaises(fc.FieldSpecError, msg=bad):
                fc.parse_expression(bad)

    def test_repo_sets_load(self):
        sets, md5 = fc.load_field_sets(SETS_YAML)
        self.assertEqual(len(md5), 32)
        self.assertTrue({'energy', 'monitor', 'clearsky'} <= set(sets))

    def test_include_and_show(self):
        sets, _ = fc.load_field_sets(SETS_YAML)
        p = fc.resolve_spec('@monitor', sets)
        self.assertIn('FSNS', p['report'])               # from @energy, needed for energy_bot
        self.assertNotIn('FSNS', p['show'])               # but not printed
        self.assertEqual(p['show'][-2:], ['energy_top', 'energy_bot'])
        self.assertEqual(p['derived']['energy_top'], [(1, 'FSNT'), (-1, 'FLNT')])

    def test_bare_names_and_mixed(self):
        sets, _ = fc.load_field_sets(SETS_YAML)
        p = fc.resolve_spec('@energy,TMQ,@clearsky', sets)
        self.assertEqual(p['show'][-1], 'TMQ')
        self.assertIn('FUSC', p['verify'])
        self.assertEqual(fc.resolve_spec('TS', sets)['report'], ['TS'])

    def test_unknown_set_names_the_choices(self):
        sets, _ = fc.load_field_sets(SETS_YAML)
        with self.assertRaises(fc.FieldSpecError) as cm:
            fc.resolve_spec('@nope', sets)
        self.assertIn('@clearsky', str(cm.exception))

    def _load(self, text):
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            f.write(text)
        try:
            return fc.load_field_sets(f.name)
        finally:
            os.unlink(f.name)

    def test_bad_files(self):
        for text, needle in [
            ("a: {include: [b]}\nb: {include: [a]}\n", 'cycle'),
            ("a: {report: [X], derived: {d: X - Y}}\n", 'does not report'),
            ("a: {report: [X], show: [Z]}\n", 'neither reported'),
            ("a: {reprot: [X]}\n", 'unknown key'),
            ("a: {report: X}\n", 'list of field names'),
            ("a: {derived: {d: X * 2}, report: [X]}\n", 'only field names'),
            ("- a\n", 'mapping'),
        ]:
            with self.assertRaises(fc.FieldSpecError, msg=text) as cm:
                self._load(text)
            self.assertIn(needle, str(cm.exception), text)

    def test_spec_rejects_junk(self):
        sets, _ = fc.load_field_sets(SETS_YAML)
        for bad in ['', ' , ', 'TS;rm', '@']:
            with self.assertRaises(fc.FieldSpecError, msg=bad):
                fc.resolve_spec(bad, sets)


@unittest.skipIf(np is None, "numpy/netCDF4 not available")
class CheckCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.sets, self.md5 = fc.load_field_sets(SETS_YAML)

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, spec, years=1, expect_zero=False):
        plan = fc.resolve_spec(spec, self.sets)
        return fc.run_fields_check('c', self.d, 'archive', plan, spec, self.md5,
                                   years=years, expect_zero=expect_zero, status='COMPLETE')

    def test_energy_values_and_monthly_mean(self):
        for i, s in enumerate(months(1, 1)):
            write_month(self.d, 'c', s, energy_fields(ts=300.0 + i))
        r = self.run_check('@energy')
        self.assertEqual(r['verdict'], 'pass')
        self.assertAlmostEqual(r['values']['TS']['mean'], 305.5, places=4)
        self.assertAlmostEqual(r['values']['energy_top']['mean'], 10.0, places=4)
        self.assertAlmostEqual(r['values']['energy_bot']['mean'], 5.0, places=4)
        self.assertEqual(r['values']['TS']['units'], 'K')
        self.assertEqual(r['window']['files'], 12)
        self.assertEqual(r['provenance']['weights'], 'gw')
        self.assertEqual(list(r['values']), ['TS', 'FSNT', 'FLNT', 'energy_top', 'energy_bot'])

    def test_polar_row_counts(self):
        f = np.zeros((len(LAT), len(LON)))
        f[-1, :] = 1.0                                   # north polar row only
        write_month(self.d, 'c', '0001-01', {'X': f})
        r = self.run_check('X', years=1)
        expect = (1 - math.cos(math.radians(2))) / 2
        self.assertAlmostEqual(r['values']['X']['mean'], expect, places=7)

    def test_verify_all_zero_months_fail(self):
        for s in months(1, 1):
            z = s in ('0001-04', '0001-05')
            v = np.zeros((3, len(LAT), len(LON))) if z else np.ones((3, len(LAT), len(LON)))
            write_month(self.d, 'c', s, {'FUSC': v})
        plan = {'report': [], 'verify': ['FUSC'], 'derived': {}, 'show': []}
        r = fc.run_fields_check('c', self.d, 'archive', plan, 'FUSC', self.md5)
        self.assertEqual(r['verdict'], 'fail')
        c = r['checks']['FUSC']
        self.assertEqual((c['ok'], c['n_failed'], c['failed_files'], c['reason']),
                         (False, 2, ['0001-04', '0001-05'], 'all zero'))

    def test_clearsky_set_pass_and_expect_zero(self):
        ones = np.ones((3, len(LAT), len(LON)))
        names = self.sets['clearsky']['verify']
        for s in months(1, 1):
            write_month(self.d, 'c', s, {n: ones if n.startswith('FU') or n.startswith('FD')
                                         else const(1) for n in names})
        self.assertEqual(self.run_check('@clearsky')['verdict'], 'pass')
        r = self.run_check('@clearsky', expect_zero=True)
        self.assertEqual(r['verdict'], 'fail')
        self.assertEqual(r['checks']['FUSC']['reason'], 'not all zero')
        self.assertEqual(r['checks']['FUSC']['test'], 'zero')

    def test_nan_fill_missing_and_3d(self):
        nan = const(1); nan[3, 3] = np.nan
        write_month(self.d, 'c', '0001-01', {'TS': nan, 'Q': np.ones((3, len(LAT), len(LON)))})
        write_month(self.d, 'c', '0001-02', {'TS': const(1), 'Q': np.ones((3, len(LAT), len(LON))),
                                             'F': const(2)}, fill={'F': True})
        r = self.run_check('TS,F,Q')
        self.assertEqual(r['verdict'], 'fail')
        self.assertIsNone(r['values']['TS']['mean'])
        self.assertEqual(r['values']['TS']['failed_files'], ['0001-01'])
        self.assertEqual(r['values']['TS']['reason'], 'non-finite or fill values')
        self.assertEqual(sorted(r['values']['F']['reason']), ['missing', 'non-finite or fill values'])
        self.assertIn('not 2-D', r['values']['Q']['reason'])

    def test_window_and_short(self):
        for s in months(1, 3):
            write_month(self.d, 'c', s, {'TS': const(1)})
        r = self.run_check('TS', years=2)
        self.assertEqual((r['window']['first'], r['window']['last'], r['window']['files']),
                         ('0002-01', '0003-12', 24))
        sel, a, b = select_last_monthly_hist(self.d, 'atm', 24)
        self.assertEqual((a, b), ('0002-01', '0003-12'))      # same rule as datamgr avg
        r = self.run_check('TS', years=5)
        self.assertTrue(r['window']['short'])
        self.assertEqual(r['verdict'], 'pass')
        self.assertTrue(r['warnings'])

    def test_skipped_without_files(self):
        r = self.run_check('TS')
        self.assertEqual(r['verdict'], 'skipped')
        self.assertIn('no monthly cam.h0', r['skip_reason'])

    def test_render_text(self):
        for s in months(1, 1):
            write_month(self.d, 'c', s, energy_fields())
        r = self.run_check('@energy')
        line = fc.render_text(r, self.sets['energy']['derived'])[0]
        self.assertIn('0001-01..0001-12 (12/12 archive)', line)
        self.assertIn('TS 300.0 K', line)
        self.assertIn('energy_top +10.0', line)
        self.assertTrue(line.endswith('PASS'))


@unittest.skipIf(np is None, "numpy/netCDF4 not available")
class CliCase(unittest.TestCase):
    """The agent contract: JSON on stdout, exit codes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.caseroot, self.archive, self.rundir = (os.path.join(d, x) for x in
                                                    ('caseroot', 'archive', 'rundir'))
        for case in ('good', 'bad'):
            os.makedirs(os.path.join(self.caseroot, case))
            with open(os.path.join(self.caseroot, case, 'CaseStatus'), 'w') as f:
                f.write('run SUCCESSFUL 2026-11-03 10:00:00\n')
            h = os.path.join(self.archive, case, 'atm', 'hist')
            os.makedirs(h)
            for s in months(1, 1):
                fl = energy_fields()
                if case == 'bad' and s == '0001-07':
                    fl['TS'] = fl['TS'].copy(); fl['TS'][0, 0] = np.nan
                write_month(h, case, s, fl)
        run = os.path.join(self.rundir, 'good', 'run')
        os.makedirs(run)
        write_month(run, 'good', '0002-01', energy_fields(ts=310))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        env = dict(os.environ)
        env['PATH'] = os.path.dirname(sys.executable)    # no squeue/sacct reachable
        cmd = [sys.executable, os.path.join(REPO, 'runmgr.py'), '--caseroot', self.caseroot,
               '--archive', self.archive, '--rundir', self.rundir, 'check'] + list(argv)
        p = subprocess.run(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        return p.returncode, p.stdout, p.stderr

    def test_json_pass(self):
        code, out, _ = self.cli('good', '--fields', '@energy', '--json')
        self.assertEqual(code, 0)
        recs = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r['case'], r['verdict'], r['status']), ('good', 'pass', 'COMPLETE'))
        self.assertEqual(r['record_version'], 1)
        for k in ('tool', 'commit', 'weights', 'fields_spec', 'field_sets_md5'):
            self.assertIn(k, r['provenance'])

    def test_fail_exit_1_and_not_found(self):
        code, out, err = self.cli('good', 'bad', 'ghost', '--fields', '@energy', '--json')
        self.assertEqual(code, 1)
        recs = {json.loads(l)['case']: json.loads(l) for l in out.splitlines()}
        self.assertEqual(list(recs), ['good', 'bad', 'ghost'])          # request order
        self.assertEqual(recs['bad']['verdict'], 'fail')
        self.assertEqual(recs['bad']['values']['TS']['failed_files'], ['0001-07'])
        self.assertEqual(recs['ghost']['verdict'], 'skipped')
        self.assertEqual(recs['ghost']['status'], 'NOT_FOUND')

    def test_short_is_3_and_source_run(self):
        code, out, _ = self.cli('good', '--fields', 'TS', '--source', 'run', '--json')
        self.assertEqual(code, 3)                                       # 1 of 12 months
        r = json.loads(out)
        self.assertEqual(r['window']['source'], 'run')
        self.assertAlmostEqual(r['values']['TS']['mean'], 310.0, places=4)

    def test_text_mode(self):
        code, out, _ = self.cli('good', '--fields', '@energy')
        self.assertEqual(code, 0)
        self.assertIn('[COMPLETE]', out)
        self.assertIn('energy_top +10.0', out)

    def test_usage_errors_exit_2(self):
        for argv in (['good', '--json'], ['good', '--last', '2'], ['--fields', '@energy'],
                     ['good', '--fields', '@nope'], ['good', '--fields', 'TS', '--dir', 'run'],
                     ['good', '--fields', 'TS', '--expect', 'zero'],
                     ['good', '--fields', 'TS', '--energy', '--json']):
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, '', argv)                             # nothing on stdout
            self.assertIn('ERROR', err, argv)

    def test_list_sets_json(self):
        code, out, _ = self.cli('--list-sets', '--json')
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertIn('clearsky', d['sets'])

    def test_plain_check_unchanged(self):
        code, out, _ = self.cli('good')
        self.assertEqual(code, 0)
        self.assertNotIn('fields', out)


if __name__ == '__main__':
    unittest.main()

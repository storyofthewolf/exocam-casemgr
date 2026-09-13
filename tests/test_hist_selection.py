#!/usr/bin/env python3
"""
Regression tests for the shared history-file selection rule
(manage_utils.select_last_monthly_hist), used by both
`runmgr.py check --energy` and `datamgr.py avg --last`.

The bug these lock down (found in review, 2026-09-13): the two tools selected
different inputs for the same avg_last{N}yr filename. check --energy took the
last 12*N cam.h0 files; datamgr avg took the last N calendar-year labels over
every file in hist/, so a partial final year shortened the window to a few
months and any h1/h2 stream was averaged in with the monthly h0 files.

Run:  python3 tests/test_hist_selection.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manage_utils import select_last_monthly_hist


def _touch(d, names):
    for n in names:
        open(os.path.join(d, n), 'w').close()


def _months(stem_stream, y0, y1, case='c'):
    return [f"{case}.{stem_stream}.{y:04d}-{m:02d}.nc"
            for y in range(y0, y1 + 1) for m in range(1, 13)]


class SelectionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_final_year_still_yields_twelve_months(self):
        # Three full years plus Jan-Mar of the next: --last 1 must be 12
        # months ending in 0004-03, not the 3 months labelled year 0004.
        _touch(self.d, _months('cam.h0', 1, 3) + [f"c.cam.h0.0004-{m:02d}.nc" for m in (1, 2, 3)])
        files, first, last = select_last_monthly_hist(self.d, 'atm', 12)
        self.assertEqual(len(files), 12)
        self.assertEqual((first, last), ('0003-04', '0004-03'))

    def test_other_streams_never_enter(self):
        _touch(self.d, _months('cam.h0', 1, 2) + _months('cam.h1', 1, 2)
               + ['c.cam.h2.0002-12-01-00000.nc'])
        files, _, _ = select_last_monthly_hist(self.d, 'atm', 24)
        self.assertEqual(len(files), 24)
        self.assertTrue(all('.cam.h0.' in f for f in files))

    def test_avg_products_excluded(self):
        _touch(self.d, _months('cam.h0', 1, 1) + ['c.cam.h0.avg_last1yr.nc',
                                                   'c.cam.h0.avg_last12mo.nc'])
        files, _, _ = select_last_monthly_hist(self.d, 'atm', 12)
        self.assertEqual(len(files), 12)
        self.assertFalse(any('avg' in f for f in files))

    def test_cice_uses_bare_h_stream(self):
        _touch(self.d, _months('cice.h', 1, 1))
        files, first, last = select_last_monthly_hist(self.d, 'ice', 12)
        self.assertEqual(len(files), 12)
        self.assertEqual((first, last), ('0001-01', '0001-12'))

    def test_clm_uses_h0(self):
        _touch(self.d, _months('clm2.h0', 1, 1))
        self.assertEqual(len(select_last_monthly_hist(self.d, 'lnd', 12)[0]), 12)

    def test_fewer_than_requested_returns_all(self):
        _touch(self.d, _months('cam.h0', 1, 1))
        files, _, _ = select_last_monthly_hist(self.d, 'atm', 120)
        self.assertEqual(len(files), 12)

    def test_chronological_order(self):
        _touch(self.d, _months('cam.h0', 9, 11))
        files, _, _ = select_last_monthly_hist(self.d, 'atm', 36)
        self.assertEqual(files, sorted(files))
        self.assertEqual(files[0], 'c.cam.h0.0009-01.nc')

    def test_missing_dir(self):
        self.assertEqual(select_last_monthly_hist(os.path.join(self.d, 'nope'), 'atm', 12),
                         ([], None, None))

    def test_no_matching_files(self):
        _touch(self.d, ['c.cam.h1.0001-01.nc'])
        self.assertEqual(select_last_monthly_hist(self.d, 'atm', 12), ([], None, None))


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Tests for the angular-momentum fixer flags (ExoCAM PR #14, cam_aqua_fv only)
and the generate-time warning for EXO_PARAMS the template does not declare.

do_am_fixes / do_am_fix_lbl are logical parameters in the cam_aqua_fv
exoplanet_mod.F90 template only. render_exoplanet_mod substitutes by line
match, so setting them against any other template would silently do nothing;
_unmatched_exo_params names such keys so generate can warn.

Run:  python3 tests/test_am_fix_params.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build

AQUA_F90 = """module exoplanet_mod
  logical, public, parameter :: do_am_fixes = .false.            !! Toniazzo et al. 2020 AM correction
  logical, public, parameter :: do_am_fix_lbl = .false.          !! level by level
  integer, public, parameter :: exo_rad_step = 3!! freq. of radiation calc
end module exoplanet_mod
"""

MIXED_F90 = """module exoplanet_mod
  !logical, public, parameter :: do_am_fixes = .false.
  integer, public, parameter :: exo_rad_step = 3!! freq. of radiation calc
end module exoplanet_mod
"""


def _write(text):
    fd, path = tempfile.mkstemp(suffix='.F90')
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    return path


class AmFixParams(unittest.TestCase):
    def setUp(self):
        self.aqua = _write(AQUA_F90)
        self.mixed = _write(MIXED_F90)

    def tearDown(self):
        os.unlink(self.aqua)
        os.unlink(self.mixed)

    def test_registered_as_bool_exo_params(self):
        for k in ('do_am_fixes', 'do_am_fix_lbl'):
            self.assertIn(k, build.EXO_PARAMS)
            self.assertEqual(build.PARAM_TYPES[k], 'bool')

    def test_type_check_rejects_non_bool(self):
        self.assertTrue(build._type_errors({'do_am_fixes': 3.0}))
        self.assertFalse(build._type_errors({'do_am_fixes': True, 'do_am_fix_lbl': 'false'}))

    def test_render_sets_logicals(self):
        out = build.render_exoplanet_mod(self.aqua, {'do_am_fixes': True,
                                                     'do_am_fix_lbl': True}, is_clone=True)
        self.assertIn('do_am_fixes = .true.', out)
        self.assertIn('do_am_fix_lbl = .true.', out)
        self.assertIn('!! Toniazzo', out)

    def test_patch_sets_logical(self):
        text = open(self.aqua).read()
        new, applied = build.patch_exoplanet_mod(self.aqua, {'do_am_fixes': 'true'})
        self.assertEqual(applied['do_am_fixes'], ('.false.', '.true.'))
        self.assertIn('do_am_fixes = .true.            !! Toniazzo', new)
        self.assertEqual(open(self.aqua).read(), text)   # returns text, does not write

    def test_declared_params_are_not_flagged(self):
        self.assertEqual(build._unmatched_exo_params(
            self.aqua, {'do_am_fixes': True, 'exo_rad_step': 2}), [])

    def test_undeclared_params_are_named(self):
        # commented-out declaration does not count
        self.assertEqual(build._unmatched_exo_params(
            self.mixed, {'do_am_fixes': True, 'do_am_fix_lbl': True,
                         'exo_rad_step': 2, 'stop_n': 5}),
            ['do_am_fix_lbl', 'do_am_fixes'])


if __name__ == '__main__':
    unittest.main()

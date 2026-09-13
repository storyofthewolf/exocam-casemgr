#!/usr/bin/env python3
"""
Regression tests for the missing exoplanet_mod.F90 template path (build.py).

The bug these lock down (found in review, 2026-09-13): when the F90 template
could not be read at generate time, build.py warned and emitted a script with
an "install manually" comment in place of the heredoc. The script still
compiled, so a clone generated locally (where the clone source lives only on
the HPC) silently dropped every exoplanet_mod parameter the matrix requested
— the same silent-no-op class as the 2026-08-26 ncdata bug.

Run:  python3 tests/test_missing_template.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build


class MissingTemplateCase(unittest.TestCase):
    def test_newcase_is_always_an_error(self):
        err = build._missing_template_error({'config_type': 'cam_aqua_fv'}, False,
                                            '/nope/exoplanet_mod.F90')
        self.assertIsNotNone(err)
        self.assertIn('/nope/exoplanet_mod.F90', err)
        self.assertIn('sole arbiter', err)

    def test_clone_requesting_exo_params_is_an_error_naming_them(self):
        spec = {'clone': 'src', 'exo_co2bar': 0.01, 'exo_convect_plim': 5.e3}
        err = build._missing_template_error(spec, True, '/hpc/src/SourceMods/src.share/exoplanet_mod.F90')
        self.assertIsNotNone(err)
        self.assertIn('exo_co2bar', err)
        self.assertIn('exo_convect_plim', err)

    def test_clone_with_explicit_n2_is_an_error(self):
        err = build._missing_template_error({'clone': 'src', 'exo_n2bar_explicit': 2.0}, True, '/x')
        self.assertIsNotNone(err)
        self.assertIn('exo_n2bar_explicit', err)

    def test_clone_requesting_nothing_may_inherit(self):
        spec = {'clone': 'src', 'ncdata': 'ic.nc', 'stop_n': 5, 'nl_cam_params': {}}
        self.assertIsNone(build._missing_template_error(spec, True, '/x'))

    def test_no_placeholder_install_manually_comment(self):
        lines = build._heredoc_exoplanet_mod(None)
        joined = '\n'.join(lines)
        self.assertNotIn('install', joined.lower())
        self.assertIn('inherited unchanged', joined)
        self.assertTrue(all(l.startswith('#') for l in lines))

    def test_rendered_content_still_heredocs(self):
        lines = build._heredoc_exoplanet_mod('module x\nend module x')
        self.assertEqual(lines[0], "cat > SourceMods/src.share/exoplanet_mod.F90 << 'EXOPLANET_MOD_EOF'")
        self.assertEqual(lines[-1], 'EXOPLANET_MOD_EOF')


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Land/mixed hybrid runs must not set finidat (found 2026-11-22, reg0_land_hyb).

For RUN_TYPE=hybrid, CESM's clm.buildnml.csh starts CLM as a "startup" from
RUN_REFCASE's own clm2.r restart, passed as -clm_startfile; CLM build-namelist
turns that into finidat and dies if user_nl_clm sets finidat as well ("Can NOT
set both -clm_startfile option AND finidat on namelist"). The shipped
cam_land_fv / cam_mixed_fv user_nl_clm carries a finidat line, so a hybrid
newcase failed at its first build. The working exovolc_* hybrids have no
finidat in user_nl_clm, and their lnd_in resolves finidat to the refcase
restart. A branch passes the restart as nrevsn instead, so finidat is harmless
there and left alone.

Locks down: the hybrid script deletes finidat from user_nl_clm (and the
deletion works on a real file); a matrix that sets finidat with a hybrid fails
validation; startup and branch keep their finidat handling.

Run:  python3 tests/test_hybrid_finidat.py
"""
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build

with open(os.path.join(REPO, 'config_registry.yaml')) as f:
    REGISTRY = yaml.safe_load(f)
PATHS = {'exocam_root': '/m/ExoCAM'}

USER_NL_CLM = """! comment
finidat = "/x/landplanet_dry.clm2.r.nc"
fsurdat = "/x/surfdata.nc"
hist_nhtfrq = 0
"""


def spec(run_type, **extra):
    s = {'config_type': 'cam_land_fv', 'run_type': run_type}
    if run_type in ('hybrid', 'branch'):
        s.update(run_refcase='ref', run_refdate='0156-01-01')
    s.update(extra)
    return s


class HybridDropsFinidat(unittest.TestCase):
    def test_hybrid_emits_deletion(self):
        text = '\n'.join(build._build_clm_update_block(spec('hybrid'), PATHS, label='c'))
        self.assertIn('/^[[:space:]]*finidat[[:space:]]*=/d', text)

    def test_deletion_runs_on_a_real_file(self):
        lines = build._build_clm_update_block(spec('hybrid'), PATHS, label='c')
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'user_nl_clm'), 'w') as f:
                f.write(USER_NL_CLM)
            sed = 'sed -i -E' if sys.platform != 'darwin' else "sed -i '' -E"
            script = '\n'.join(l.replace('sed -i -E', sed) for l in lines)
            r = subprocess.run(['bash', '-c', script], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(os.path.join(d, 'user_nl_clm')) as f:
                out = f.read()
        self.assertNotIn('finidat', out)
        self.assertIn('fsurdat', out)
        self.assertIn('hist_nhtfrq', out)

    def test_mixed_hybrid_too(self):
        text = '\n'.join(build._build_clm_update_block(
            spec('hybrid', config_type='cam_mixed_fv'), PATHS))
        self.assertIn('finidat[[:space:]]*=/d', text)

    def test_hybrid_fsurdat_still_upserted(self):
        text = '\n'.join(build._build_clm_update_block(
            spec('hybrid', fsurdat='surf.nc'), PATHS))
        self.assertIn("fsurdat = '/m/ExoCAM/cesm1.2.1/initial_files/cam_land_fv/surf.nc'", text)


class OtherRunTypesUnchanged(unittest.TestCase):
    def test_startup_keeps_finidat_upsert(self):
        text = '\n'.join(build._build_clm_update_block(
            spec('startup', finidat='a.clm2.r.nc'), PATHS))
        self.assertNotIn('finidat[[:space:]]*=/d" user_nl_clm; fi\nif grep', text)
        self.assertIn("finidat = '/m/ExoCAM/cesm1.2.1/initial_files/cam_land_fv/a.clm2.r.nc'", text)

    def test_branch_leaves_finidat_alone(self):
        text = '\n'.join(build._build_clm_update_block(spec('branch'), PATHS))
        self.assertNotIn('Hybrid', text)

    def test_aqua_untouched(self):
        self.assertEqual(build._build_clm_update_block(
            spec('hybrid', config_type='cam_aqua_fv'), PATHS), [])


class Validation(unittest.TestCase):
    def errors(self, s):
        full = {'exort_pkg': 'n68equiv', 'nlev': 51, 'mach': 'discover',
                'stop_option': 'ndays', 'stop_n': 5, 'rest_option': 'ndays',
                'rest_n': 5, 'resubmit': 0, 'ntasks': 126}
        full.update(s)
        return [e for e in build.validate_case(full, REGISTRY) if 'finidat cannot be set' in e]

    def test_hybrid_with_finidat_is_an_error(self):
        self.assertEqual(len(self.errors(spec('hybrid', finidat='/x/a.nc'))), 1)

    def test_hybrid_without_finidat_is_fine(self):
        self.assertEqual(self.errors(spec('hybrid')), [])

    def test_startup_with_finidat_is_fine(self):
        self.assertEqual(self.errors(spec('startup', finidat='/x/a.nc')), [])


if __name__ == '__main__':
    unittest.main()

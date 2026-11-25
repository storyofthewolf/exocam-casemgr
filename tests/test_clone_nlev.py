#!/usr/bin/env python3
"""
Clone matrices honor `nlev` (2026-11-25).

create_clone copies CAM_CONFIG_OPTS verbatim, so a clone matrix's `nlev` was
silently ignored: every case compiled at the clone source's level count. The
exovolc_v10 suite clones L51 eruption templates onto parents with 53-55 levels,
which would compile L51 and die at startup on the L5x ncdata.

Locks down: the generated block rewrites or appends -nlev and aborts if the
write does not take; generate_clone_script emits it only when nlev is set; and
--verify fails a matrix whose nlev disagrees with its ncdata's 'lev' dimension.

Run:  python3 tests/test_clone_nlev.py
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

try:
    import netCDF4
    HAVE_NC4 = True
except ImportError:
    HAVE_NC4 = False

# Stand-ins for CESM's xmlquery/xmlchange, backed by a one-line file.
# CESM 1.2.1 xmlquery prints 'env_build.xml: CAM_CONFIG_OPTS = <value>'.
XMLQUERY = """#!/bin/bash
echo "env_build.xml: CAM_CONFIG_OPTS = $(cat opts)"
"""
XMLCHANGE_OK = """#!/bin/bash
printf '%s' "${1#CAM_CONFIG_OPTS=}" > opts
"""
XMLCHANGE_NOOP = """#!/bin/bash
exit 0
"""


def run_block(lines, opts, xmlchange=XMLCHANGE_OK):
    with tempfile.TemporaryDirectory() as d:
        for name, body in (('xmlquery', XMLQUERY), ('xmlchange', xmlchange)):
            p = os.path.join(d, name)
            with open(p, 'w') as f:
                f.write(body)
            os.chmod(p, 0o755)
        with open(os.path.join(d, 'opts'), 'w') as f:
            f.write(opts)
        proc = subprocess.run(['bash', '-c', 'set -e\n' + '\n'.join(lines)], cwd=d,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        with open(os.path.join(d, 'opts')) as f:
            return proc.returncode, proc.stdout + proc.stderr, f.read()


SRC_OPTS = "-usr_src /c/eruption/SourceMods/src.cam/exort -phys cam4 -nlev 51 -carma exovolc -nadv 6"


class CloneNlevBlock(unittest.TestCase):
    def test_rewrites_existing_nlev(self):
        rc, out, opts = run_block(build._build_clone_nlev_block(53, 'c'), SRC_OPTS)
        self.assertEqual(rc, 0, out)
        self.assertIn('-nlev 53 ', opts)
        self.assertNotIn('-nlev 51', opts)
        self.assertIn('-carma exovolc -nadv 6', opts)   # rest untouched

    def test_same_value_is_a_noop(self):
        rc, out, opts = run_block(build._build_clone_nlev_block(51, 'c'), SRC_OPTS)
        self.assertEqual(rc, 0, out)
        self.assertEqual(opts, SRC_OPTS)

    def test_appends_when_absent(self):
        rc, out, opts = run_block(build._build_clone_nlev_block(55, 'c'), "-phys cam4")
        self.assertEqual(rc, 0, out)
        self.assertEqual(opts, "-phys cam4 -nlev 55")

    def test_failed_write_aborts(self):
        rc, out, _ = run_block(build._build_clone_nlev_block(53, 'case_x'), SRC_OPTS,
                               xmlchange=XMLCHANGE_NOOP)
        self.assertNotEqual(rc, 0)
        self.assertIn('case_x: CAM_CONFIG_OPTS does not carry -nlev 53', out)

    def test_prefix_match_is_not_accepted(self):
        # -nlev 5 must not satisfy a check for -nlev 53, nor 530 for 53
        rc, out, _ = run_block(build._build_clone_nlev_block(5, 'c'), SRC_OPTS)
        self.assertEqual(rc, 0, out)
        rc, out, _ = run_block(build._build_clone_nlev_block(53, 'c'), "-nlev 530",
                               xmlchange=XMLCHANGE_NOOP)
        self.assertNotEqual(rc, 0)


class CloneScriptWiring(unittest.TestCase):
    def script(self, **extra):
        spec = {'clone': 'cam_land_fv_eruption', 'config_type': 'cam_land_fv',
                'exort_pkg': 'n68equiv*', 'stop_option': 'nyears', 'stop_n': 1,
                'rest_option': 'nyears', 'rest_n': 1, 'resubmit': 0, 'ntasks': 126}
        spec.update(extra)
        with tempfile.TemporaryDirectory() as d:
            path = build.generate_clone_script('c', spec, REGISTRY, None, d, None)
            with open(path) as f:
                return f.read()

    def test_emitted_when_nlev_set(self):
        text = self.script(nlev=53)
        self.assertIn('-nlev 53', text)
        # after the -usr_src fix (both edit CAM_CONFIG_OPTS) and before cesm_setup
        self.assertLess(text.index('Fix stale -usr_src'), text.index('Set -nlev 53'))
        self.assertLess(text.index('Set -nlev 53'), text.index('./cesm_setup'))

    def test_absent_without_nlev(self):
        self.assertNotIn('Set -nlev', self.script())


@unittest.skipUnless(HAVE_NC4, 'netCDF4 not installed')
class VerifyNcdataLevels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ic = os.path.join(self.tmp.name, 'parent.cam.i.0081-01-01-00000.nc')
        with netCDF4.Dataset(self.ic, 'w') as ds:
            ds.createDimension('lev', 51)
            ds.createDimension('ilev', 52)

    def tearDown(self):
        self.tmp.cleanup()

    def nlev_errors(self, nlev):
        spec = {'clone': 'cam_land_fv_eruption', 'config_type': 'cam_land_fv',
                'ncdata': self.ic, 'nlev': nlev}
        errs, _, _ = build.verify_case(spec, REGISTRY, {'exocam_root': self.tmp.name})
        return [e for e in errs if e.startswith('nlev:')]

    def test_match_passes(self):
        self.assertEqual(self.nlev_errors(51), [])

    def test_mismatch_fails(self):
        errs = self.nlev_errors(53)
        self.assertEqual(len(errs), 1)
        self.assertIn('nlev=53 but ncdata has 51 levels', errs[0])


if __name__ == '__main__':
    unittest.main()

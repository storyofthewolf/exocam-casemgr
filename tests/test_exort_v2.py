#!/usr/bin/env python3
"""
Tests for ExoRT 2.0 (`exort_pkg: exort`) cases (ExoCAM EXOCAM2_PLAN.md, Phase 3.2).

Before the 2.0 release an `exort` case builds from the refactor checkouts of
both repos, named by the registry's `package_roots.exort`, while every 1.x
package keeps main's checkouts. What these lock down:
- the per-package roots, and that a 1.x case resolves exactly as before;
- the sandbox rule treats a package's registry roots as production;
- an exort newcase script: n84 sun from data/stellar/, NTHRDS_ATM = 1, the
  src.cam.exort -usr_src, and a sys_rootdir guard that really refuses a
  checkout compiling in another data root;
- the ExoCAM 2.0 exoplanet_mod check (do_exo_clouds / do_exo_haze);
- diff.py's package roots and its shared-tree lookup.

Run:  python3 tests/test_exort_v2.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'tools'))
import build
import diff
import utils.inputs_utils as inputs_utils

REGISTRY = {
    'paths': {
        'cesm_scripts': '/hpc/cesm/scripts', 'caseroot': '/hpc/cases',
        'rundir': '/hpc/rundir', 'archive': '/hpc/archive',
        'exocam_root': '/hpc/ExoCAM', 'exort_root': '/hpc/ExoRT',
    },
    'package_roots': {
        'exort': {'exocam_root': '/hpc/ExoCAM-refactor',
                  'exort_root': '/hpc/ExoRT-refactor'},
    },
}
with open(os.path.join(REPO, 'config_registry.yaml')) as f:
    REAL_REGISTRY = yaml.safe_load(f)


def spec_for(pkg, name='exocam_x', **extra):
    spec = {'name': name, 'config_type': 'cam_aqua_fv', 'exort_pkg': pkg, 'nlev': 40,
            'mach': 'discover', 'stop_option': 'ndays', 'stop_n': 5,
            'rest_option': 'ndays', 'rest_n': 5, 'resubmit': 0, 'ntasks': 126}
    spec.update(extra)
    return spec


def render(pkg, matrix_paths=None):
    registry = dict(REAL_REGISTRY, package_roots=REGISTRY['package_roots'])
    spec = spec_for(pkg)
    spec['_paths_override'] = build.case_paths_override(registry, spec, matrix_paths or {})
    ic_file, _ = build.find_ic_file(spec, registry)
    with tempfile.TemporaryDirectory() as d:
        path = build.generate_shell_script('case_x', spec, registry, ic_file, d,
                                           'module x\nend module x\n')
        with open(path) as f:
            return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def sys_rootdir(root):
    return ("module sys_rootdir\n"
            "  !character(len=256), parameter :: exort_rootdir = '/somewhere/else/'\n"
            f"  character(len=256), parameter :: exort_rootdir = '{root}'\n"
            "end module sys_rootdir\n")


class PackageRoots(unittest.TestCase):
    def test_exort_gets_refactor_roots(self):
        self.assertEqual(build.package_roots(REGISTRY, 'exort'),
                         {'exocam_root': '/hpc/ExoCAM-refactor', 'exort_root': '/hpc/ExoRT-refactor'})

    def test_v1_package_gets_main_roots(self):
        for pkg in ('n68equiv', 'n68equiv*', '', None):
            self.assertEqual(build.package_roots(REGISTRY, pkg),
                             {'exocam_root': '/hpc/ExoCAM', 'exort_root': '/hpc/ExoRT'})

    def test_no_package_roots_in_registry(self):
        reg = {'paths': REGISTRY['paths']}
        self.assertEqual(build.package_roots(reg, 'exort')['exort_root'], '/hpc/ExoRT')

    def test_v1_case_override_unchanged(self):
        self.assertEqual(build.case_paths_override(REGISTRY, spec_for('n68equiv'), {}), {})
        m = {'rundir': '/x'}
        self.assertEqual(build.case_paths_override(REGISTRY, spec_for('n68equiv'), m), m)

    def test_exort_case_override(self):
        o = build.case_paths_override(REGISTRY, spec_for('exort'), {'rundir': '/x'})
        self.assertEqual(o, {'exocam_root': '/hpc/ExoCAM-refactor',
                             'exort_root': '/hpc/ExoRT-refactor', 'rundir': '/x'})

    def test_matrix_paths_win(self):
        o = build.case_paths_override(REGISTRY, spec_for('exort'), {'exort_root': '/dev/ExoRT'})
        self.assertEqual(o['exort_root'], '/dev/ExoRT')
        self.assertEqual(o['exocam_root'], '/hpc/ExoCAM-refactor')


class SandboxRule(unittest.TestCase):
    def errors(self, pkg, matrix_paths, name='exocam_x'):
        spec = spec_for(pkg, name=name)
        spec['_paths_override'] = build.case_paths_override(REGISTRY, spec, matrix_paths)
        return build._sandbox_errors(spec, REGISTRY)

    def test_exort_on_package_roots_is_production(self):
        self.assertEqual(self.errors('exort', {}), [])

    def test_v1_case_on_refactor_roots_needs_sbx(self):
        errs = self.errors('n68equiv', {'exort_root': '/hpc/ExoRT-refactor'})
        self.assertEqual(len(errs), 1)
        self.assertIn('sbx_', errs[0])

    def test_exort_on_dev_checkout_needs_sbx(self):
        self.assertTrue(self.errors('exort', {'exort_root': '/dev/ExoRT'}))
        self.assertEqual(self.errors('exort', {'exort_root': '/dev/ExoRT'}, name='sbx_r_a'), [])


class ExortScript(unittest.TestCase):
    def test_exort_newcase(self):
        s = render('exort')
        self.assertIn('EXOCAM=/hpc/ExoCAM-refactor', s)
        self.assertIn('EXORT=/hpc/ExoRT-refactor', s)
        self.assertIn("exo_solar_file = '/hpc/ExoRT-refactor/data/stellar/G2V_SUN_n84.nc'", s)
        self.assertIn('-usr_src ${EXORT}/3dmodels/src.cam.exort"', s)
        self.assertIn('./xmlchange -file env_mach_pes.xml -id NTHRDS_ATM -val 1', s)
        self.assertLess(s.index('SYS_ROOTDIR='), s.index('./create_newcase'))

    def test_v1_newcase_untouched(self):
        s = render('n68equiv')
        self.assertIn(f"EXORT={REAL_REGISTRY['paths']['exort_root']}", s)
        self.assertIn('/data/solar/G2V_SUN_n68.nc', s)
        self.assertNotIn('NTHRDS_ATM', s)
        self.assertNotIn('SYS_ROOTDIR', s)

    def test_sun_solar_file(self):
        self.assertEqual(inputs_utils.sun_solar_file('exort', {'exort_root': '/r'}),
                         '/r/data/stellar/G2V_SUN_n84.nc')
        self.assertEqual(inputs_utils.sun_solar_file('n68equiv', {'exort_root': '/r'}),
                         '/r/data/solar/G2V_SUN_n68.nc')

    def test_solar_stem_checked(self):
        errs = build.validate_case(
            spec_for('exort', exo_solar_file='/r/data/solar/G2V_SUN_n68.nc'), REAL_REGISTRY)
        self.assertTrue(any("expected stem 'n84'" in e for e in errs))


class RootdirGuard(unittest.TestCase):
    """The emitted shell guard, run for real, and its generate-time twin."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.exort = os.path.join(self.tmp, 'ExoRT-refactor')
        self.f = os.path.join(self.exort, '3dmodels', 'src.cam.exort', 'sys_rootdir.F90')
        os.makedirs(os.path.join(self.tmp, 'other'))
        # alias/ExoRT-refactor is the same checkout by another path, as
        # /discover/nobackup/etwolf is /gpfsm/dnb33/etwolf on Discover
        os.symlink(self.tmp, os.path.join(self.tmp, 'alias'))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_guard(self, exort):
        lines = build._exort_rootdir_guard(spec_for('exort'))
        script = 'set -e\nEXORT=' + exort + '\n' + '\n'.join(lines) + '\necho PASSED\n'
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True)

    def test_names_itself(self):
        write(self.f, sys_rootdir(self.exort + '/'))
        r = self.run_guard(self.exort)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('PASSED', r.stdout)
        self.assertEqual(build._exort_rootdir_errors(spec_for('exort'), self.exort), [])

    def test_names_itself_through_an_alias(self):
        write(self.f, sys_rootdir(os.path.join(self.tmp, 'alias', 'ExoRT-refactor') + '/'))
        self.assertEqual(self.run_guard(self.exort).returncode, 0)
        self.assertEqual(build._exort_rootdir_errors(spec_for('exort'), self.exort), [])

    def test_names_another_checkout(self):
        write(self.f, sys_rootdir(os.path.join(self.tmp, 'other') + '/'))
        r = self.run_guard(self.exort)
        self.assertEqual(r.returncode, 1)
        self.assertIn('ERROR', r.stdout)
        self.assertNotIn('PASSED', r.stdout)
        self.assertTrue(build._exort_rootdir_errors(spec_for('exort'), self.exort))

    def test_names_a_missing_directory(self):
        write(self.f, sys_rootdir('/no/such/ExoRT/'))
        self.assertEqual(self.run_guard(self.exort).returncode, 1)
        self.assertTrue(build._exort_rootdir_errors(spec_for('exort'), self.exort))

    def test_v1_and_unreadable_are_skipped(self):
        self.assertEqual(build._exort_rootdir_guard(spec_for('n68equiv')), [])
        self.assertEqual(build._exort_rootdir_errors(spec_for('n68equiv'), self.exort), [])
        # checkout not readable here (generate on the laptop): the script checks it
        self.assertEqual(build._exort_rootdir_errors(spec_for('exort'), '/hpc/nowhere'), [])


class ExoCAM2Template(unittest.TestCase):
    V2 = ("  logical, public, parameter :: do_carma_exort = .false.\n"
          "  logical, public, parameter :: do_exo_clouds = .true.   !! v2\n"
          "  logical, public, parameter :: do_exo_haze = do_carma_exort\n")
    V1 = "  logical, public, parameter :: do_carma_exort = .false.\n"

    def check(self, text, pkg='exort'):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'exoplanet_mod.F90')
            write(p, text)
            return build._exort_v2_errors(spec_for(pkg), p)

    def test_v2_template_ok(self):
        self.assertEqual(self.check(self.V2), [])

    def test_v1_template_refused(self):
        errs = self.check(self.V1)
        self.assertEqual(len(errs), 1)
        self.assertIn('do_exo_clouds, do_exo_haze', errs[0])

    def test_commented_flag_does_not_count(self):
        self.assertTrue(self.check(self.V1 + "  !logical, parameter :: do_exo_clouds = .true.\n"
                                   "  !logical, parameter :: do_exo_haze = .true.\n"))

    def test_v1_package_not_checked(self):
        self.assertEqual(self.check(self.V1, pkg='n68equiv'), [])

    def test_flags_are_matrix_settable(self):
        self.assertIn('do_exo_clouds', build.EXO_PARAMS)
        self.assertEqual(build.PARAM_TYPES['do_exo_haze'], 'bool')


class DiffTool(unittest.TestCase):
    def test_roots_for_package(self):
        paths = dict(REGISTRY['paths'], _package_roots=REGISTRY['package_roots'])
        self.assertEqual(diff.roots_for_package(paths, 'exort')['exocam_root'], '/hpc/ExoCAM-refactor')
        self.assertEqual(diff.roots_for_package(paths, 'n68equiv')['exocam_root'], '/hpc/ExoCAM')

    def test_counterpart_in_shared_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'cesm1.2.1')
            write(os.path.join(root, 'SourceMods', 'src.cam', 'shared.F90'), 'x')
            write(os.path.join(root, 'configs', 'cam_aqua_fv', 'SourceMods', 'src.share',
                               'exoplanet_mod.F90'), 'y')
            sm = os.path.join(root, 'configs', 'cam_aqua_fv', 'SourceMods')
            self.assertEqual(diff.find_exocam_counterpart('shared.F90', 'src.cam', sm),
                             os.path.join(root, 'SourceMods', 'src.cam', 'shared.F90'))
            self.assertEqual(diff.find_exocam_counterpart('exoplanet_mod.F90', 'src.share', sm),
                             os.path.join(sm, 'src.share', 'exoplanet_mod.F90'))
            self.assertIsNone(diff.find_exocam_counterpart('nope.F90', 'src.cam', sm))


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Regression tests for per-config newcase setup (found 2026-11-20 while setting
up land and mixed cases by hand):

1. cam_land_fv newcases never set LND_DOMAIN_PATH/LND_DOMAIN_FILE, so they kept
   the stock Earth land map (1466 land cells) while the landplanet restart
   covers 3312 and the run died at startup. Now `cesm_config.<type>.xmlchange`
   in config_registry.yaml carries the config README's settings.
2. The cam_mixed_fv IC table entry (control_L51.cam.i.nc) resolved under
   initial_files/cam_mixed_fv/, where it does not exist; it lives in
   initial_files/cam_aqua_fv/. A '<config>/<file>' entry now resolves under
   initial_files/.
3. A newcase matrix silent on gases builds N2 + H2O only; --verify now warns.

Run:  python3 tests/test_config_setup.py
"""
import os
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
IC = '/m/ExoCAM/cesm1.2.1/initial_files'


def newcase_spec(config_type, **extra):
    spec = {'config_type': config_type, 'exort_pkg': 'n68equiv', 'nlev': 51,
            'mach': 'discover', 'stop_option': 'nyears', 'stop_n': 1,
            'rest_option': 'nyears', 'rest_n': 1, 'resubmit': 0, 'ntasks': 126}
    spec.update(extra)
    return spec


def render(config_type):
    spec = newcase_spec(config_type)
    ic_file, _ = build.find_ic_file(spec, REGISTRY)
    with tempfile.TemporaryDirectory() as d:
        path = build.generate_shell_script('case_x', spec, REGISTRY, ic_file, d,
                                           'module x\nend module x\n')
        with open(path) as f:
            return f.read()


class ResolveIcPath(unittest.TestCase):
    def test_bare_name_under_own_config(self):
        self.assertEqual(build.resolve_ic_path('a.nc', 'cam_land_fv', PATHS),
                         f'{IC}/cam_land_fv/a.nc')

    def test_config_relative_name_under_initial_files(self):
        self.assertEqual(build.resolve_ic_path('cam_aqua_fv/a.nc', 'cam_mixed_fv', PATHS),
                         f'{IC}/cam_aqua_fv/a.nc')

    def test_absolute_and_var_paths_verbatim(self):
        for p in ('/abs/a.nc', '$EXOCAM/x/a.nc', '${EXOCAM}/x/a.nc', '~/a.nc'):
            self.assertEqual(build.resolve_ic_path(p, 'cam_aqua_fv', PATHS), p)

    def test_mixed_table_entry_resolves_into_aqua_folder(self):
        ic_file, _ = build.find_ic_file(newcase_spec('cam_mixed_fv'), REGISTRY)
        self.assertEqual(build.resolve_ic_path(ic_file, 'cam_mixed_fv', PATHS),
                         f'{IC}/cam_aqua_fv/control_L51.cam.i.nc')


class ConfigXmlchange(unittest.TestCase):
    def test_land_newcase_sets_landplanet_domain(self):
        script = render('cam_land_fv')
        self.assertIn('./xmlchange LND_DOMAIN_PATH="${EXOCAM}/cesm1.2.1/initial_files/cam_land_fv"',
                      script)
        self.assertIn('./xmlchange LND_DOMAIN_FILE="domain.lnd.fv4x5_landplanet.nc"', script)
        # before cesm_setup
        self.assertLess(script.index('LND_DOMAIN_FILE'), script.index('./cesm_setup'))

    def test_aqua_newcase_sets_no_land_domain(self):
        self.assertNotIn('LND_DOMAIN', render('cam_aqua_fv'))

    def test_values_are_strings_not_yaml_scalars(self):
        # A bare NULL/off/on in YAML is not a string; every value must be one.
        for ctype, cfg in REGISTRY['cesm_config'].items():
            for key, val in (cfg.get('xmlchange') or {}).items():
                self.assertIsInstance(val, str, f'{ctype}.{key}')


class VerifyDerivedInputs(unittest.TestCase):
    """--verify checks the table IC and the domain file, which the matrix never names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.paths = {'exocam_root': root}
        os.makedirs(f'{root}/cesm1.2.1/initial_files/cam_aqua_fv')
        os.makedirs(f'{root}/cesm1.2.1/initial_files/cam_land_fv')
        open(f'{root}/cesm1.2.1/initial_files/cam_aqua_fv/control_L51.cam.i.nc', 'w').close()

    def tearDown(self):
        self.tmp.cleanup()

    def errors(self, config_type, registry):
        errs, _, _ = build.verify_case(newcase_spec(config_type), registry, self.paths)
        return [e for e in errs if e.startswith('nc:')]

    def test_borrowed_ic_found(self):
        self.assertEqual(self.errors('cam_mixed_fv', REGISTRY), [])

    def test_old_bare_mixed_entry_fails(self):
        reg = dict(REGISTRY, ic_files={'cam_mixed_fv': {'1bar': {51: 'control_L51.cam.i.nc'}}})
        errs = self.errors('cam_mixed_fv', reg)
        self.assertEqual(len(errs), 1)
        self.assertIn('initial_files/cam_mixed_fv/control_L51.cam.i.nc', errs[0])

    def test_missing_land_domain_fails(self):
        errs = self.errors('cam_land_fv', REGISTRY)
        self.assertTrue(any('LND_DOMAIN_FILE' in e and 'domain.lnd.fv4x5_landplanet.nc' in e
                            for e in errs), errs)


class NoAbsorberWarning(unittest.TestCase):
    def test_silent_newcase_warns(self):
        w = build._verify_no_absorber(newcase_spec('cam_land_fv'))
        self.assertEqual(len(w), 1)
        self.assertIn('composition', w[0])

    def test_o2_alone_still_warns(self):
        self.assertTrue(build._verify_no_absorber(newcase_spec('cam_mixed_fv', exo_o2bar=0.21)))

    def test_any_absorber_silences(self):
        for key in build.ABSORBER_GAS_PARAMS:
            self.assertEqual(build._verify_no_absorber(newcase_spec('cam_aqua_fv', **{key: 1e-6})),
                             [], key)

    def test_explicit_zero_still_warns(self):
        self.assertTrue(build._verify_no_absorber(newcase_spec('cam_aqua_fv', exo_co2bar=0.0)))

    def test_clone_skipped(self):
        self.assertEqual(build._verify_no_absorber({'clone': 'src'}), [])


if __name__ == '__main__':
    unittest.main()

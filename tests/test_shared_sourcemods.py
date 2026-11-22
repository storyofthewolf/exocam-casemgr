#!/usr/bin/env python3
"""
Tests for the ExoCAM 2.0 shared SourceMods layout (EXOCAM2_PLAN.md, Phase 1).

On ExoCAM's refactor branch the FV configs keep only their own files under
configs/<cfg>/SourceMods/; the rest live once in cesm1.2.1/SourceMods/, and
configs/<cfg>/COMPONENTS lists which src.* directories a case receives
(cesm1.2.1/install_sourcemods.sh assembles them). A 1.x checkout, and the
SE/circumbinary configs, have no COMPONENTS file and are copied whole.

What these lock down: a newcase script installs through install_sourcemods.sh
when COMPONENTS exists and falls back to the old cp -r otherwise, before the
exoplanet_mod.F90 heredoc; the generate-time overlay stale-base check finds a
stock file in the shared tree, but only for a component the config installs.

Run:  python3 tests/test_shared_sourcemods.py
"""
import hashlib
import os
import shutil
import sys
import tempfile
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build

with open(os.path.join(REPO, 'config_registry.yaml')) as f:
    REGISTRY = yaml.safe_load(f)

STOCK = "module m\nend module m\n"


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def render(config_type):
    spec = {'config_type': config_type, 'exort_pkg': 'n68equiv', 'nlev': 40,
            'mach': 'discover', 'stop_option': 'ndays', 'stop_n': 5,
            'rest_option': 'ndays', 'rest_n': 5, 'resubmit': 0, 'ntasks': 126}
    ic_file, _ = build.find_ic_file(spec, REGISTRY)
    with tempfile.TemporaryDirectory() as d:
        path = build.generate_shell_script('case_x', spec, REGISTRY, ic_file, d,
                                           'module x\nend module x\n')
        with open(path) as f:
            return f.read()


class NewcaseInstall(unittest.TestCase):
    def test_installs_through_script_when_components_exists(self):
        s = render('cam_aqua_fv')
        self.assertIn('if [ -f ${EXOCAM}/cesm1.2.1/configs/${CONFIG_TYPE}/COMPONENTS ]; then', s)
        self.assertIn('sh ${EXOCAM}/cesm1.2.1/install_sourcemods.sh ${CONFIG_TYPE} '
                      '${CASEROOT}/${CASE}/SourceMods', s)

    def test_falls_back_to_whole_copy(self):
        s = render('cam_aqua_fv')
        self.assertIn('  cp -r ${EXOCAM}/cesm1.2.1/configs/${CONFIG_TYPE}/SourceMods .', s)

    def test_install_precedes_exoplanet_mod(self):
        s = render('cam_land_fv')
        self.assertLess(s.index('install_sourcemods.sh'), s.index('EXOPLANET_MOD_EOF'))


class StockFileInSharedTree(unittest.TestCase):
    """_overlay_stale_errors against configs/<cfg>/SourceMods of a 2.0 tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, 'cesm1.2.1')
        write(os.path.join(root, 'SourceMods', 'src.cam', 'shared.F90'), STOCK)
        write(os.path.join(root, 'SourceMods', 'src.clm', 'landonly.F90'), STOCK)
        cfg = os.path.join(root, 'configs', 'cam_aqua_fv')
        write(os.path.join(cfg, 'COMPONENTS'), '# comment line\ncam share\n')
        write(os.path.join(cfg, 'SourceMods', 'src.share', 'exoplanet_mod.F90'), STOCK + '!own\n')
        self.sm = os.path.join(cfg, 'SourceMods')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def check(self, rel, stock_md5):
        return build._overlay_stale_errors({'files': [{'rel': rel, 'stock_md5': stock_md5}]}, self.sm)

    def test_shared_file_matches(self):
        self.assertEqual(self.check('src.cam/shared.F90', md5(STOCK)), ([], True))

    def test_shared_file_stale(self):
        errs, checked = self.check('src.cam/shared.F90', '0' * 32)
        self.assertTrue(checked)
        self.assertIn('stale base', errs[0])

    def test_shared_file_is_not_new(self):
        errs, _ = self.check('src.cam/shared.F90', None)
        self.assertIn('new to SourceMods', errs[0])

    def test_config_own_file(self):
        self.assertEqual(self.check('src.share/exoplanet_mod.F90', md5(STOCK + '!own\n')), ([], True))

    def test_component_not_installed_is_missing(self):
        errs, _ = self.check('src.clm/landonly.F90', md5(STOCK))
        self.assertIn('stock file missing', errs[0])

    def test_old_layout_unchanged(self):
        os.remove(os.path.join(os.path.dirname(self.sm), 'COMPONENTS'))
        errs, _ = self.check('src.cam/shared.F90', md5(STOCK))
        self.assertIn('stock file missing', errs[0])


class GeneratedTemplate(unittest.TestCase):
    """ExoCAM 2.0 Phase 2a: a config with user_nl_exo has no exoplanet_mod.F90
    of its own; casemgr renders on top of what gen_exoplanet_mod.py builds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.configs = os.path.join(self.tmp, 'cesm1.2.1', 'configs')
        gen = os.path.join(self.tmp, 'cesm1.2.1', 'exoplanet', 'gen_exoplanet_mod.py')
        # A stand-in generator: writes the config name, so the test sees which ran.
        write(gen, "import sys\n"
                   "a = sys.argv[1:]\n"
                   "open(a[a.index('--out') + 1], 'w').write('! generated for ' + a[0] + '\\n')\n")
        write(os.path.join(self.configs, 'cam_aqua_fv', 'user_nl_exo'), '! nothing\n')
        write(os.path.join(self.configs, 'cam_aqua_se', 'SourceMods', 'src.share',
                           'exoplanet_mod.F90'), STOCK)
        build._GENERATED_TEMPLATES.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp)
        build._GENERATED_TEMPLATES.clear()

    def test_config_with_user_nl_exo_is_generated(self):
        path = build._config_exoplanet_template(self.configs, 'cam_aqua_fv')
        with open(path) as f:
            self.assertEqual(f.read(), '! generated for cam_aqua_fv\n')

    def test_generated_once_per_config(self):
        a = build._config_exoplanet_template(self.configs, 'cam_aqua_fv')
        b = build._config_exoplanet_template(self.configs, 'cam_aqua_fv')
        self.assertEqual(a, b)

    def test_config_without_user_nl_exo_uses_its_own_file(self):
        self.assertEqual(build._config_exoplanet_template(self.configs, 'cam_aqua_se'),
                         os.path.join(self.configs, 'cam_aqua_se', 'SourceMods',
                                      'src.share', 'exoplanet_mod.F90'))


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Tests for sandbox trial cases (sbx_*) and source overlays (build.py).

Owner decisions, 2026-11-01 (exocam-operations DECISIONS.md): trial cases are
marked by name, not directory — sbx_ first, so no production --prefix matches
one — and variant code touching one or two files is an overlay beside the
matrix, embedded in the build script, with all variants behind one switch in
the overlay file. Nothing in ExoCAM changes before integration.

What these lock down: an overlay or a non-production model root needs a sbx_
name; overlay_set must hit exactly one active parameter line, typed by its
declaration; a stale base fails at generate time where the stock file is
readable and at build time always; the rendered file is written byte-for-byte
(md5 checked); scan.py never registers a sbx_ case; datamgr never moves one to
long-term storage.

Run:  python3 tests/test_sandbox_overlay.py
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build
import datamgr
import scan
from utils.manage_utils import is_sandbox_case


STOCK_F90 = """\
module cloud_fraction
  implicit none
  real, parameter :: rhmin = 0.80   ! stock
end module cloud_fraction
"""

OVERLAY_F90 = """\
module cloud_fraction
  implicit none
  integer, parameter :: cldfrc_pmode = 0    ! 0 stock, 1 ref-sigma, 2 flat
  logical, parameter :: cldfrc_debug = .false.
  real(r8), parameter :: cldfrc_flat_rh = 0.85_r8
! integer, parameter :: cldfrc_pmode = 9   commented copy, never matched
end module cloud_fraction
"""

EXO_MOD = """\
module exoplanet_mod
  real(r8), public, parameter :: exo_co2bar = 0.0004_r8
end module exoplanet_mod
"""


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def make_overlay(root, stock_md5, text=OVERLAY_F90, rel='src.cam/cloud_fraction.F90'):
    odir = os.path.join(root, 'src')
    write(os.path.join(odir, rel), text)
    write(os.path.join(odir, 'cloud_fraction.F90.orig'), 'stray file, never copied\n')
    stock = 'null' if stock_md5 is None else stock_md5
    write(os.path.join(odir, 'BASE.yaml'),
          f"exocam_commit: abc1234\nfiles:\n  {rel}:\n    stock_md5: {stock}\n")
    return odir


class Names(unittest.TestCase):
    def test_prefix_marks_trials(self):
        self.assertTrue(is_sandbox_case('sbx_cldfrc_p05_sigma'))
        self.assertTrue(is_sandbox_case('/gpfsm/cases/sbx_pt05_dbg03/'))
        self.assertFalse(is_sandbox_case('exocam_ML_grp4_pt05'))
        self.assertFalse(is_sandbox_case('exocam_ML_grp4_pt05__sbx'))


class SandboxGuard(unittest.TestCase):
    registry = {'paths': {'exocam_root': '/m/ExoCAM', 'exort_root': '/m/ExoRT'}}

    def test_overlay_needs_sbx_name(self):
        errs = build._sandbox_errors({'name': 'exocam_x', 'source_overlay': 'src'}, self.registry)
        self.assertEqual(len(errs), 1)
        self.assertIn('sbx_', errs[0])
        self.assertEqual(build._sandbox_errors(
            {'name': 'sbx_x', 'source_overlay': 'src'}, self.registry), [])

    def test_non_production_root_needs_sbx_name(self):
        spec = {'name': 'exocam_x', '_paths_override': {'exocam_root': '/m/dev/ExoCAM-x'}}
        self.assertTrue(build._sandbox_errors(spec, self.registry))
        spec['name'] = 'sbx_x'
        self.assertEqual(build._sandbox_errors(spec, self.registry), [])

    def test_production_root_restated_is_fine(self):
        spec = {'name': 'exocam_x', '_paths_override': {'exocam_root': '/m/ExoCAM/'}}
        self.assertEqual(build._sandbox_errors(spec, self.registry), [])

    def test_plain_case_untouched(self):
        self.assertEqual(build._sandbox_errors({'name': 'exocam_x'}, self.registry), [])


class OverlaySet(unittest.TestCase):
    def apply(self, settings):
        return build._apply_overlay_set({'src.cam/cloud_fraction.F90': OVERLAY_F90}, settings)

    def test_typed_by_declaration_and_comment_kept(self):
        files, errs = self.apply({'cldfrc_pmode': 2, 'cldfrc_debug': 'true',
                                  'cldfrc_flat_rh': 0.9})
        self.assertEqual(errs, [])
        text = files['src.cam/cloud_fraction.F90']
        self.assertIn('integer, parameter :: cldfrc_pmode = 2    ! 0 stock, 1 ref-sigma, 2 flat\n', text)
        self.assertIn('logical, parameter :: cldfrc_debug = .true.\n', text)
        self.assertIn('cldfrc_flat_rh = 0.9_r8\n', text)
        self.assertIn('! integer, parameter :: cldfrc_pmode = 9', text)   # comment untouched

    def test_unmatched_key_is_an_error(self):
        _, errs = self.apply({'cldfrc_nope': 1})
        self.assertEqual(len(errs), 1)
        self.assertIn('found 0', errs[0])

    def test_ambiguous_key_is_an_error(self):
        files = {'src.cam/a.F90': OVERLAY_F90, 'src.cam/b.F90': OVERLAY_F90}
        _, errs = build._apply_overlay_set(files, {'cldfrc_pmode': 1})
        self.assertIn('found 2', errs[0])

    def test_type_mismatch_is_an_error(self):
        _, errs = self.apply({'cldfrc_pmode': 1.5})
        self.assertIn('non-integral', errs[0])
        _, errs = self.apply({'cldfrc_debug': 1})
        self.assertIn('boolean', errs[0])

    def test_case_merges_over_base(self):
        spec = build.resolve_case({'overlay_set': {'cldfrc_pmode': 0, 'cldfrc_debug': False}},
                                  {'overlay_set': {'cldfrc_pmode': 1}})
        self.assertEqual(spec['overlay_set'], {'cldfrc_pmode': 1, 'cldfrc_debug': False})


class PlanOverlay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_no_overlay(self):
        self.assertEqual(build.plan_overlay({}), (None, []))

    def test_set_without_overlay_is_an_error(self):
        plan, errs = build.plan_overlay({'overlay_set': {'x': 1}})
        self.assertIsNone(plan)
        self.assertTrue(errs)

    def test_renders_relative_to_matrix_and_lists_only_base_files(self):
        make_overlay(self.tmp, md5(STOCK_F90))
        plan, errs = build.plan_overlay({'source_overlay': 'src', '_matrix_dir': self.tmp,
                                         'overlay_set': {'cldfrc_pmode': 1}})
        self.assertEqual(errs, [])
        self.assertEqual([f['rel'] for f in plan['files']], ['src.cam/cloud_fraction.F90'])
        f = plan['files'][0]
        self.assertIn('cldfrc_pmode = 1', f['content'])
        self.assertEqual(f['md5'], md5(f['content']))
        self.assertEqual(f['stock_md5'], md5(STOCK_F90))
        self.assertEqual(plan['exocam_commit'], 'abc1234')

    def test_missing_base_file(self):
        os.makedirs(os.path.join(self.tmp, 'src'))
        _, errs = build.plan_overlay({'source_overlay': 'src', '_matrix_dir': self.tmp})
        self.assertIn('BASE.yaml', errs[0])

    def test_bad_relpath(self):
        make_overlay(self.tmp, md5(STOCK_F90), rel='src.cam/cloud_fraction.F90')
        write(os.path.join(self.tmp, 'src', 'BASE.yaml'),
              "files:\n  ../etc/passwd:\n    stock_md5: null\n")
        _, errs = build.plan_overlay({'source_overlay': 'src', '_matrix_dir': self.tmp})
        self.assertIn('src.<component>/<file>', errs[0])

    def test_delimiter_in_content_is_an_error(self):
        make_overlay(self.tmp, md5(STOCK_F90), text=OVERLAY_F90 + build.OVERLAY_EOF + '\n')
        _, errs = build.plan_overlay({'source_overlay': 'src', '_matrix_dir': self.tmp})
        self.assertIn('delimiter', errs[0])


class StaleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sm = os.path.join(self.tmp, 'SourceMods')
        write(os.path.join(self.sm, 'src.cam', 'cloud_fraction.F90'), STOCK_F90)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def plan(self, stock_md5, rel='src.cam/cloud_fraction.F90'):
        return {'files': [{'rel': rel, 'stock_md5': stock_md5}]}

    def test_matching_base(self):
        self.assertEqual(build._overlay_stale_errors(self.plan(md5(STOCK_F90)), self.sm), ([], True))

    def test_stale_base(self):
        errs, checked = build._overlay_stale_errors(self.plan('0' * 32), self.sm)
        self.assertTrue(checked)
        self.assertIn('stale base', errs[0])

    def test_new_file_that_exists(self):
        errs, _ = build._overlay_stale_errors(self.plan(None), self.sm)
        self.assertIn('new to SourceMods', errs[0])

    def test_unreadable_is_deferred_not_failed(self):
        self.assertEqual(build._overlay_stale_errors(self.plan('0' * 32), '/nope'), ([], False))


class GenerateClone(unittest.TestCase):
    """End to end: a clone matrix with an overlay, generated and then the
    overlay block of the script executed against a fake case directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.caseroot = os.path.join(self.tmp, 'cases')
        src_case = os.path.join(self.caseroot, 'base_p05')
        write(os.path.join(src_case, 'SourceMods', 'src.share', 'exoplanet_mod.F90'), EXO_MOD)
        write(os.path.join(src_case, 'SourceMods', 'src.cam', 'cloud_fraction.F90'), STOCK_F90)
        self.suite = os.path.join(self.tmp, 'record', 'suites')
        make_overlay(os.path.join(self.tmp, 'record'), md5(STOCK_F90))
        self.registry = os.path.join(self.tmp, 'config_registry.yaml')
        write(self.registry, f"paths:\n  caseroot: {self.caseroot}\n"
                             f"  exocam_root: {self.tmp}/ExoCAM\n  exort_root: {self.tmp}/ExoRT\n")
        self.scripts = os.path.join(self.tmp, 'scripts')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def matrix(self, names):
        cases = ''.join(f"  - name: {n}\n    overlay_set: {{cldfrc_pmode: {i}}}\n"
                        for i, n in enumerate(names))
        path = os.path.join(self.suite, 'sweep.yaml')
        write(path, f"config_registry: {self.registry}\n"
                    f"base:\n  clone: base_p05\n  source_overlay: ../src\n"
                    f"  stop_option: nyears\n  stop_n: 2\n  rest_option: nyears\n"
                    f"  rest_n: 1\n  resubmit: 0\n  ntasks: 4\ncases:\n{cases}")
        return path

    def generate(self, names, verify=False):
        args = argparse.Namespace(scripts_dir=self.scripts)
        return build._generate_one_matrix(self.matrix(names), args, self.suite, verify)

    def test_non_sbx_name_is_refused(self):
        generated, errors, *_ = self.generate(['exocam_cldfrc_stock'])
        self.assertEqual((generated, errors), (0, 1))

    def test_verify_passes(self):
        _, _, named, failed, _ = self.generate(['sbx_cldfrc_p05_stock'], verify=True)
        self.assertEqual((named, failed), (1, 0))

    def test_stale_clone_source_is_refused(self):
        write(os.path.join(self.caseroot, 'base_p05', 'SourceMods', 'src.cam',
                           'cloud_fraction.F90'), STOCK_F90 + '! edited\n')
        generated, errors, *_ = self.generate(['sbx_cldfrc_p05_stock'])
        self.assertEqual((generated, errors), (0, 1))

    def test_script_installs_overlay_and_stamps_provenance(self):
        generated, errors, *_ = self.generate(['sbx_cldfrc_p05_stock', 'sbx_cldfrc_p05_sigma'])
        self.assertEqual((generated, errors), (2, 0))
        with open(os.path.join(self.scripts, 'sbx_cldfrc_p05_sigma_build.sh')) as f:
            script = f.read()
        self.assertIn('cldfrc_pmode = 1', script)
        self.assertNotIn('stray file', script)
        self.assertIn(build.PROVENANCE_FILE, script)

        # Run just the overlay + provenance lines in a fake case dir: the
        # stock file is replaced and the written md5 check passes.
        lines = script.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith('# Source overlay'))
        end = next(i for i, l in enumerate(lines) if l.startswith('# Fix stale -usr_src')
                   or l.startswith('# -----------') and i > start)
        case = os.path.join(self.tmp, 'fakecase')
        write(os.path.join(case, 'SourceMods', 'src.cam', 'cloud_fraction.F90'), STOCK_F90)
        snippet = ('set -e\nEXOCAM=/nonexistent\nEXORT=/nonexistent\n'
                   + '\n'.join(lines[start:end]) + '\n')
        r = subprocess.run(['bash', '-c', snippet], cwd=case, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(case, 'SourceMods', 'src.cam', 'cloud_fraction.F90')) as f:
            self.assertIn('cldfrc_pmode = 1', f.read())
        with open(os.path.join(case, build.PROVENANCE_FILE)) as f:
            prov = f.read()
        self.assertIn('case: sbx_cldfrc_p05_sigma', prov)
        self.assertIn('cldfrc_pmode: 1', prov)
        self.assertIn('exocam_sha: unknown', prov)

        # Same snippet against an edited (stale) case file: refuses.
        write(os.path.join(case, 'SourceMods', 'src.cam', 'cloud_fraction.F90'), 'edited\n')
        r = subprocess.run(['bash', '-c', snippet], cwd=case, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('stale overlay base', r.stdout)


class ScanAndDatamgr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ('exocam_prod', 'sbx_cldfrc_p05_stock'):
            write(os.path.join(self.tmp, 'cases', name, 'SourceMods', 'src.share',
                               'exoplanet_mod.F90'), EXO_MOD)
            write(os.path.join(self.tmp, 'archive', name, 'atm', 'hist',
                               f'{name}.cam.h0.0001-01.nc'), 'x')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_scan_never_returns_trials(self):
        found = [os.path.basename(p) for p in scan.find_case_dirs(os.path.join(self.tmp, 'cases'))]
        self.assertEqual(found, ['exocam_prod'])
        self.assertEqual(scan.find_case_dirs(
            os.path.join(self.tmp, 'cases', 'sbx_cldfrc_p05_stock')), [])

    def test_move_hist_blocks_trials(self):
        paths = {'caseroot': os.path.join(self.tmp, 'cases'),
                 'archive': os.path.join(self.tmp, 'archive'),
                 'long_term': os.path.join(self.tmp, 'lt')}
        args = argparse.Namespace(cases=['sbx_cldfrc_p05_stock'], prefix=None,
                                  models=None, execute=True)
        datamgr.cmd_move_hist(args, paths)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'lt', 'sbx_cldfrc_p05_stock')))

    def test_retire_blocks_trials_unless_purge(self):
        paths = {'caseroot': os.path.join(self.tmp, 'cases'),
                 'archive': os.path.join(self.tmp, 'archive'),
                 'long_term': os.path.join(self.tmp, 'lt')}
        args = argparse.Namespace(cases=['sbx_cldfrc_p05_stock'], prefix=None,
                                  keep_config=True, keep_years=None, keep_restarts=False,
                                  purge=False, execute=True, registry=None)
        datamgr.cmd_retire_case(args, paths)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'lt', 'sbx_cldfrc_p05_stock')))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, 'cases', 'sbx_cldfrc_p05_stock')))


if __name__ == '__main__':
    unittest.main()

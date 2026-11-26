#!/usr/bin/env python3
"""
Tests for the sandbox tree (owner decision, 2026-11-22).

A matrix naming `sandbox: <record>` builds its sbx_ cases in
<paths.sandbox>/<record>/{cases,rundir,archive}, one tree per dev record,
with the run, build and archive directories set inside it (CESM's machine
defaults would put them under production scratch). Every verb finds a case
by name in either place (manage_utils.case_paths).

Run:  python3 tests/test_sandbox_tree.py
"""
import argparse
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build
from utils import manage_utils as mu

EXO_MOD = """\
module exoplanet_mod
  real(r8), public, parameter :: exo_co2bar = 0.0004_r8
end module exoplanet_mod
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


class Tree(unittest.TestCase):
    """A production tree with one case, and a sandbox with two records."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        self.paths = {'caseroot': f'{t}/cases', 'rundir': f'{t}/rundir',
                      'archive': f'{t}/archive', 'sandbox': f'{t}/sandbox'}
        os.makedirs(f'{t}/cases/prod_a')
        os.makedirs(f'{t}/cases/sbx_old_x')            # an older sbx_ case
        os.makedirs(f'{t}/sandbox/rec1/cases/sbx_rec1_a')
        os.makedirs(f'{t}/sandbox/rec1/archive/sbx_rec1_a/rest')
        os.makedirs(f'{t}/sandbox/rec2/cases/sbx_rec2_b')

    def tearDown(self):
        shutil.rmtree(self.tmp)


class CasePaths(Tree):
    def test_sandbox_case_gets_its_record_tree(self):
        cp = mu.case_paths(self.paths, 'sbx_rec1_a')
        self.assertEqual(cp['caseroot'], f'{self.tmp}/sandbox/rec1/cases')
        self.assertEqual(cp['rundir'], f'{self.tmp}/sandbox/rec1/rundir')
        self.assertEqual(cp['archive'], f'{self.tmp}/sandbox/rec1/archive')

    def test_others_keep_production(self):
        self.assertIs(mu.case_paths(self.paths, 'prod_a'), self.paths)
        self.assertIs(mu.case_paths(self.paths, 'sbx_old_x'), self.paths)

    def test_discover_sees_both(self):
        self.assertEqual(mu.discover_cases(self.paths),
                         ['prod_a', 'sbx_old_x', 'sbx_rec1_a', 'sbx_rec2_b'])

    def test_restart_sets_look_in_the_tree(self):
        os.makedirs(f'{self.tmp}/sandbox/rec1/archive/sbx_rec1_a/rest/0002-01-01-00000')
        sets = mu.restart_sets('sbx_rec1_a', self.paths)
        self.assertEqual(len(sets), 1)

    def test_no_sandbox_configured(self):
        plain = {k: v for k, v in self.paths.items() if k != 'sandbox'}
        self.assertNotIn('sbx_rec1_a', mu.discover_cases(plain))
        self.assertIs(mu.case_paths(plain, 'sbx_rec1_a'), plain)


class GenerateSandbox(unittest.TestCase):
    """End to end: a sandbox matrix cloned from a production case."""

    def setUp(self):
        self.tmp = t = tempfile.mkdtemp()
        write(f'{t}/cases/base_p05/SourceMods/src.share/exoplanet_mod.F90', EXO_MOD)
        self.registry = f'{t}/config_registry.yaml'
        write(self.registry,
              f"paths:\n  caseroot: {t}/cases\n  rundir: {t}/rundir\n"
              f"  archive: {t}/archive\n  long_term: {t}/lt\n  sandbox: {t}/sandbox\n"
              f"  exocam_root: {t}/ExoCAM\n  exort_root: {t}/ExoRT\n")
        self.suite = f'{t}/suites'

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def matrix(self, names, record='rec1', extra=''):
        cases = ''.join(f"  - name: {n}\n" for n in names)
        path = f'{self.suite}/m.yaml'
        sandbox = f"sandbox: {record}\n" if record else ""
        write(path, f"config_registry: {self.registry}\n{sandbox}"
                    f"base:\n  clone: base_p05\n  stop_option: nyears\n  stop_n: 2\n"
                    f"  rest_option: nyears\n  rest_n: 1\n  resubmit: 0\n  ntasks: 4\n"
                    f"{extra}cases:\n{cases}")
        return path

    def generate(self, names, scripts_dir=build.DEFAULT_SCRIPTS_DIR, **kw):
        args = argparse.Namespace(scripts_dir=scripts_dir)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            return build._generate_one_matrix(self.matrix(names, **kw), args, self.suite, False)
        finally:
            os.chdir(cwd)

    def script(self, case, record='rec1'):
        with open(f'{self.tmp}/sandbox/{record}/build_scripts/{case}_build.sh') as f:
            return f.read()

    def test_case_run_and_archive_in_the_tree(self):
        generated, errors, *_ = self.generate(['sbx_rec1_a'])
        self.assertEqual((generated, errors), (1, 0))
        text = self.script('sbx_rec1_a')
        t = self.tmp
        self.assertIn(f'CASEROOT={t}/sandbox/rec1/cases\n', text)
        self.assertIn(f'RUNDIR={t}/sandbox/rec1/rundir\n', text)
        self.assertIn(f'ARCHIVE={t}/sandbox/rec1/archive\n', text)
        for line in ('./xmlchange RUNDIR=${RUNDIR}/${CASE}/run',
                     './xmlchange EXEROOT=${RUNDIR}/${CASE}/bld',
                     './xmlchange DOUT_S_ROOT=${ARCHIVE}/${CASE}'):
            self.assertIn(line, text)

    def test_clones_from_production(self):
        self.generate(['sbx_rec1_a'])
        text = self.script('sbx_rec1_a')
        self.assertIn(f'CLONE_ROOT={self.tmp}/cases\n', text)
        self.assertIn('./create_clone -clone ${CLONE_ROOT}/${CLONE_OF} -case ${CASEROOT}/${CASE}',
                      text)

    def test_only_sbx_cases(self):
        generated, errors, *_ = self.generate(['exocam_plain'])
        self.assertEqual((generated, errors), (0, 1))

    def test_bad_record(self):
        with self.assertRaises(SystemExit):
            self.generate(['sbx_rec1_a'], record='../up')

    def test_explicit_scripts_dir_wins(self):
        self.generate(['sbx_rec1_a'], scripts_dir=f'{self.tmp}/mine')
        self.assertTrue(os.path.exists(f'{self.tmp}/mine/sbx_rec1_a_build.sh'))

    def test_plain_matrix_unchanged(self):
        generated, _, *_ = self.generate(['exocam_plain'], record=None,
                                         scripts_dir=f'{self.tmp}/plain')
        self.assertEqual(generated, 1)
        with open(f'{self.tmp}/plain/exocam_plain_build.sh') as f:
            text = f.read()
        self.assertNotIn('CLONE_ROOT', text)
        self.assertNotIn('DOUT_S_ROOT', text)
        self.assertIn('./create_clone -clone ${CASEROOT}/${CLONE_OF}', text)

    def test_paths_override_moves_run_and_archive(self):
        # A plain matrix whose `paths:` points rundir/archive elsewhere (e.g.
        # another user's scratch) must move CESM's RUNDIR/EXEROOT/DOUT_S_ROOT
        # there too, or the restarts are staged where the run never looks.
        t = self.tmp
        extra = ("  run_type: hybrid\n  run_refcase: base_p05\n"
                 "  run_refdate: 0002-01-01\n  brnch_retain_casename: 'false'\n")
        path = self.matrix(['exocam_plain'], record=None, extra=extra)
        with open(path) as f:
            text = f.read()
        write(path, text.replace(
            "base:\n",
            f"paths:\n  caseroot: {t}/other/cases\n  rundir: {t}/other/rundir\n"
            f"  archive: {t}/other/archive\nbase:\n", 1))
        args = argparse.Namespace(scripts_dir=f'{t}/plain')
        cwd = os.getcwd()
        os.chdir(t)
        try:
            generated, errors, *_ = build._generate_one_matrix(path, args, self.suite, False)
        finally:
            os.chdir(cwd)
        self.assertEqual((generated, errors), (1, 0))
        with open(f'{t}/plain/exocam_plain_build.sh') as f:
            text = f.read()
        self.assertIn(f'CASEROOT={t}/other/cases\n', text)
        self.assertIn(f'RUNDIR={t}/other/rundir\n', text)
        self.assertIn(f'ARCHIVE={t}/other/archive\n', text)
        for line in ('./xmlchange RUNDIR=${RUNDIR}/${CASE}/run',
                     './xmlchange EXEROOT=${RUNDIR}/${CASE}/bld',
                     './xmlchange DOUT_S_ROOT=${ARCHIVE}/${CASE}'):
            self.assertIn(line, text)
        self.assertIn(f'{t}/other/rundir/${{CASE}}/run', text)   # restarts staged there

    def test_branch_reference_searched_everywhere(self):
        extra = ("  run_type: branch\n  run_refcase: base_p05\n"
                 "  run_refdate: 0002-01-01\n  brnch_retain_casename: 'false'\n")
        generated, errors, *_ = self.generate(['sbx_rec1_a'], extra=extra)
        self.assertEqual((generated, errors), (1, 0))
        text = self.script('sbx_rec1_a')
        self.assertIn(f'PROD_ARCHIVE={self.tmp}/archive\n', text)
        self.assertIn('for d in "${ARCHIVE}" "${PROD_ARCHIVE}" ${SANDBOX}/*/archive "${LONG_TERM}"; do',
                      text)


if __name__ == '__main__':
    unittest.main()

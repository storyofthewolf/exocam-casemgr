#!/usr/bin/env python3
"""
Regression tests for the exit-code convention (manage_utils.Outcome and the
verbs that return it).

The gap these lock down (review finding 3, 2026-09-13): most verbs printed
per-case ERROR / SKIP lines and exited 0, so a wrapping script or agent could
not tell success from partial or total failure. Convention (manage_utils):
0 ok, 1 failed, 2 usage (argparse), 3 partial (policy skips), 4 aborted
(reserved: no verb prompts since 2026-11, so none produces it);
precedence FAILED > ABORTED > PARTIAL > OK.

The subprocess tests drive the real CLIs against a temp caseroot/archive and
a fake `squeue` on PATH, so they validate the wiring in main(), not just the
tally class.

Run:  python3 tests/test_exit_codes.py
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from utils.manage_utils import (Outcome, EXIT_OK, EXIT_FAILED, EXIT_USAGE,
                          EXIT_PARTIAL, EXIT_ABORTED)


class OutcomeCase(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(Outcome().exit_code(), EXIT_OK)

    def test_skip_is_partial(self):
        o = Outcome(); o.skip()
        self.assertEqual(o.exit_code(), EXIT_PARTIAL)

    def test_abort_beats_skip(self):
        o = Outcome(); o.skip(); o.abort()
        self.assertEqual(o.exit_code(), EXIT_ABORTED)

    def test_fail_beats_everything(self):
        o = Outcome(); o.skip(); o.abort(); o.fail()
        self.assertEqual(o.exit_code(), EXIT_FAILED)

    def test_fail_count(self):
        o = Outcome(); o.fail(0)
        self.assertEqual(o.exit_code(), EXIT_OK)
        o.fail(3)
        self.assertEqual(o.failed, 3)

    def test_codes_are_distinct(self):
        self.assertEqual(sorted({EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PARTIAL, EXIT_ABORTED}),
                         [0, 1, 2, 3, 4])


class CliCase(unittest.TestCase):
    """Drive the real CLIs against a temp fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.caseroot = os.path.join(d, 'caseroot')
        self.archive = os.path.join(d, 'archive')
        self.rundir = os.path.join(d, 'rundir')
        self.bin = os.path.join(d, 'bin')
        case = os.path.join(self.caseroot, 'mycase')
        os.makedirs(case)
        os.makedirs(os.path.join(self.archive, 'mycase', 'atm', 'hist'))
        os.makedirs(self.rundir)
        os.makedirs(self.bin)
        with open(os.path.join(case, 'CaseStatus'), 'w') as f:
            f.write('build complete 2026-09-13 10:00:00\n')
        open(os.path.join(case, 'mycase.run'), 'w').close()
        sq = os.path.join(self.bin, 'squeue')
        with open(sq, 'w') as f:
            f.write('#!/bin/sh\necho mycase\n')
        os.chmod(sq, os.stat(sq).st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, tool, *argv, queued=False, stdin=''):
        env = dict(os.environ)
        if queued:
            env['PATH'] = self.bin + os.pathsep + env.get('PATH', '')
        else:
            # make sure no real squeue is reachable either way
            env['PATH'] = os.path.join(self.tmp.name, 'empty')
            os.makedirs(env['PATH'], exist_ok=True)
            env['PATH'] += os.pathsep + os.path.dirname(sys.executable)
        cmd = [sys.executable, os.path.join(REPO, tool),
               '--caseroot', self.caseroot, '--archive', self.archive,
               '--rundir', self.rundir] + list(argv)
        proc = subprocess.run(cmd, cwd=REPO, env=env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)
        return proc.returncode, proc.stdout

    def test_submit_preview_ok(self):
        code, out = self.run_cli('runmgr.py', 'submit', 'mycase')
        self.assertEqual(code, EXIT_OK, out)

    def test_submit_queued_case_is_partial(self):
        code, out = self.run_cli('runmgr.py', 'submit', 'mycase', queued=True)
        self.assertEqual(code, EXIT_PARTIAL, out)
        self.assertIn('[QUEUED]', out)

    def test_submit_execute_asks_nothing(self):
        # --execute is the confirmation: with stdin empty (as under hpc-run,
        # where it is /dev/null) the verb still acts. No sbatch on PATH, so
        # the act is a per-case ERROR -> 1, which proves it got that far.
        code, out = self.run_cli('runmgr.py', 'submit', 'mycase', '--execute')
        self.assertEqual(code, EXIT_FAILED, out)
        self.assertIn('sbatch not found', out)
        self.assertNotIn('[yes/no]', out)

    def test_unknown_case_is_failed(self):
        code, out = self.run_cli('runmgr.py', 'submit', 'nope')
        self.assertEqual(code, EXIT_FAILED, out)

    def test_bad_usage_is_2(self):
        code, _ = self.run_cli('runmgr.py', 'submit', '--bogus')
        self.assertEqual(code, EXIT_USAGE)

    def test_check_is_read_only_ok(self):
        code, out = self.run_cli('runmgr.py', 'check', 'mycase', queued=True)
        self.assertEqual(code, EXIT_OK, out)

    def test_clean_nothing_to_do_is_partial(self):
        code, out = self.run_cli('datamgr.py', 'clean', 'purge-bld', 'mycase')
        self.assertEqual(code, EXIT_PARTIAL, out)

    def test_avg_preview_ok(self):
        open(os.path.join(self.archive, 'mycase', 'atm', 'hist', 'mycase.cam.h0.0001-01.nc'), 'w').close()
        code, out = self.run_cli('datamgr.py', 'avg', 'mycase', '--last', '1',
                                 '--models', 'atm')
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn('--execute', out)

    def _long_term_record(self):
        lt = os.path.join(self.tmp.name, 'long_term')
        os.makedirs(os.path.join(lt, 'mycase'))
        marker = os.path.join(lt, 'mycase', 'case.yaml')
        open(marker, 'w').close()
        return lt, marker

    def test_retire_duplicate_is_skipped_without_flag(self):
        # Was a per-case [yes/no]; now a duplicate long-term record is skipped
        # unless --replace-existing is in the command line.
        lt, marker = self._long_term_record()
        code, out = self.run_cli('datamgr.py', '--long-term', lt, 'retire', 'mycase', '--execute')
        self.assertEqual(code, EXIT_PARTIAL, out)
        self.assertIn('--replace-existing', out)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(os.path.isdir(os.path.join(self.caseroot, 'mycase')))

    def test_retire_replace_existing_previews(self):
        lt, marker = self._long_term_record()
        code, out = self.run_cli('datamgr.py', '--long-term', lt, 'retire', 'mycase',
                                 '--replace-existing')
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn('will be deleted', out)
        self.assertTrue(os.path.exists(marker))     # preview touches nothing


    def test_avg_no_files_is_partial(self):
        code, out = self.run_cli('datamgr.py', 'avg', 'mycase', '--last', '1', '--models', 'atm')
        self.assertEqual(code, EXIT_PARTIAL, out)


class MakeCase(unittest.TestCase):
    """build.py make previews unless --execute, and never prompts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scripts = os.path.join(self.tmp.name, 'scripts')
        os.makedirs(self.scripts)
        self.marker = os.path.join(self.tmp.name, 'ran')
        with open(os.path.join(self.scripts, 'mycase_build.sh'), 'w') as f:
            f.write(f'#!/bin/sh\ntouch {self.marker}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *argv):
        cmd = [sys.executable, os.path.join(REPO, 'build.py'),
               '--scripts-dir', self.scripts, 'make', 'mycase'] + list(argv)
        proc = subprocess.run(cmd, cwd=REPO, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)
        return proc.returncode, proc.stdout

    def test_preview_runs_nothing(self):
        code, out = self.make()
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn('preview only', out)
        self.assertFalse(os.path.exists(self.marker))

    def test_send_it_preview_says_it_would_submit(self):
        code, out = self.make('--send-it')
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn('and submit each case', out)
        self.assertFalse(os.path.exists(self.marker))

    def test_execute_runs_without_a_prompt(self):
        code, out = self.make('--execute')
        self.assertTrue(os.path.exists(self.marker), out)
        self.assertNotIn('[yes/no]', out)


if __name__ == '__main__':
    unittest.main()

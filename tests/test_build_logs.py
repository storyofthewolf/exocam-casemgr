#!/usr/bin/env python3
"""
Tests for streamed per-attempt build logs (build._attempt_log_path,
_run_streaming, _point_latest, rebuild_case).

Why (2026-09-13): `make` buffered a build's output in memory and wrote the
log only after the script exited; rebuild_case returned one line. A build
in progress could not be tailed, a killed build lost its output, and every
attempt overwrote the last.

Run:  python3 tests/test_build_logs.py
"""
import os
import stat
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build


class BuildLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_attempt_paths_are_unique_and_named(self):
        logs = os.path.join(self.d, 'logs')
        p1, latest = build._attempt_log_path(logs, 'c.build')
        open(p1, 'w').close()
        p2, _ = build._attempt_log_path(logs, 'c.build')
        self.assertNotEqual(p1, p2)
        self.assertTrue(os.path.basename(p1).startswith('c.build.'))
        self.assertEqual(latest, os.path.join(logs, 'c.build.log'))

    def test_streaming_log_exists_while_running_and_records_exit(self):
        log = os.path.join(self.d, 'x.log')
        # The script proves the log is live by reading its own log file
        # mid-run: the line it echoed first must already be on disk.
        script = os.path.join(self.d, 's.sh')
        with open(script, 'w') as f:
            f.write('echo hello\nsleep 0.2\ngrep -c hello "$1" > "$2"\nexit 3\n')
        probe = os.path.join(self.d, 'probe')
        rc = build._run_streaming(['bash', script, log, probe], self.d, log)
        self.assertEqual(rc, 3)
        with open(probe) as f:
            self.assertEqual(f.read().strip(), '1')
        text = open(log).read()
        self.assertIn('hello', text)
        self.assertIn('exit 3', text)
        self.assertEqual(build._log_tail(log), 'hello')

    def test_latest_symlink_tracks_newest(self):
        logs = os.path.join(self.d, 'logs')
        p1, latest = build._attempt_log_path(logs, 'c')
        open(p1, 'w').close()
        build._point_latest(latest, p1)
        p2, _ = build._attempt_log_path(logs, 'c')
        open(p2, 'w').close()
        build._point_latest(latest, p2)
        self.assertEqual(os.readlink(latest), os.path.basename(p2))

    def test_rebuild_case_failure_names_log_and_tail(self):
        case = os.path.join(self.d, 'mycase')
        os.makedirs(case)
        script = os.path.join(case, 'mycase.build')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\necho compiling\necho "ERROR: undefined reference" >&2\nexit 2\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        ok, detail = build.rebuild_case(case, 'mycase')
        self.assertFalse(ok)
        self.assertIn('exit 2', detail)
        self.assertIn('undefined reference', detail)
        self.assertIn('logs/casemgr_rebuild.', detail)
        self.assertTrue(os.path.islink(os.path.join(case, 'logs', 'casemgr_rebuild.log')))

    def test_rebuild_case_success_names_log(self):
        case = os.path.join(self.d, 'mycase')
        os.makedirs(case)
        script = os.path.join(case, 'mycase.build')
        with open(script, 'w') as f:
            f.write('#!/bin/sh\necho ok\n')
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        ok, detail = build.rebuild_case(case, 'mycase')
        self.assertTrue(ok)
        self.assertIn('[log:', detail)

    def test_rebuild_case_missing_script(self):
        case = os.path.join(self.d, 'mycase')
        os.makedirs(case)
        ok, detail = build.rebuild_case(case, 'mycase')
        self.assertFalse(ok)
        self.assertIn('not found', detail)


if __name__ == '__main__':
    unittest.main()

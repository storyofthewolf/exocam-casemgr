#!/usr/bin/env python3
"""
Regression tests for the SLURM status probe (runmgr._resolve_status).

The bug these lock down (found in review, 2026-09-13): the queue was only
consulted when the last CaseStatus event was 'run started' / 'run SUCCESSFUL'.
A case that had just been submitted still ended in 'build complete', so it
read BUILT — and `runmgr.py submit` would sbatch it a second time.

Run:  python3 tests/test_status_probe.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runmgr
from manage_utils import ACTIVE_STATUSES


def _cs(event):
    status = 'UNKNOWN'
    for prefix, label in runmgr._STATUS_MAP.items():
        if event.startswith(prefix):
            status = label
            break
    return {'status': status, 'last_event': event, 'last_ts': '2026-09-13 12:00:00'}


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self._saved = runmgr._active_jobs_cache
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        runmgr._active_jobs_cache = self._saved
        self.tmp.cleanup()

    def resolve(self, event, queued):
        runmgr._active_jobs_cache = frozenset({'mycase'}) if queued else frozenset()
        return runmgr._resolve_status(_cs(event), self.tmp.name, 'mycase')

    def test_just_submitted_reads_queued_not_built(self):
        self.assertEqual(self.resolve('build complete', queued=True), 'QUEUED')

    def test_resubmit_after_failure_reads_queued(self):
        self.assertEqual(self.resolve('run FAILED', queued=True), 'QUEUED')

    def test_queued_is_active_so_submit_blocks_it(self):
        self.assertIn('QUEUED', ACTIVE_STATUSES)

    def test_not_queued_built_stays_built(self):
        self.assertEqual(self.resolve('build complete', queued=False), 'BUILT')

    def test_successful_plus_queued_is_resubmitted(self):
        self.assertEqual(self.resolve('run SUCCESSFUL', queued=True), 'RESUBMITTED')

    def test_started_plus_queued_is_running(self):
        self.assertEqual(self.resolve('run started', queued=True), 'RUNNING')

    def test_started_not_queued_is_running_question(self):
        self.assertEqual(self.resolve('run started', queued=False), 'RUNNING?')

    def test_started_not_queued_with_timeout_is_wallclock(self):
        with open(os.path.join(self.tmp.name, 'run.out'), 'w') as f:
            f.write('CSM EXECUTION BEGINS HERE\n'
                    'slurmstepd: JOB 1 CANCELLED AT ... DUE TO TIME LIMIT\n')
        self.assertEqual(self.resolve('run started', queued=False), 'WALLCLOCK')

    def test_squeue_unavailable_keeps_raw_label(self):
        runmgr._active_jobs_cache = None
        self.assertEqual(runmgr._resolve_status(_cs('build complete'), self.tmp.name, 'mycase'), 'BUILT')
        self.assertEqual(runmgr._resolve_status(_cs('run started'), self.tmp.name, 'mycase'), 'RUNNING')

    def test_probe_status_and_check_share_one_resolver(self):
        # cmd_check must not carry its own copy of the conditional logic.
        import inspect
        src = inspect.getsource(runmgr.cmd_check)
        self.assertIn('_resolve_status(', src)
        self.assertNotIn("startswith('run SUCCESSFUL')", src)


if __name__ == '__main__':
    unittest.main()

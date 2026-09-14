#!/usr/bin/env python3
"""
Tests for job-ID persistence (manage_utils.record_job / last_job /
submit_case) and the sacct verdict it enables (runmgr._sacct_verdict,
_resolve_status).

Why (2026-09-13): submit_case parsed the SLURM job ID out of sbatch's output
and every caller dropped it. The queue forgets a job the moment it leaves,
so a 'run started' case that was no longer queued could only be *inferred*
(RUNNING? / a run.out string search). With the ID recorded per case, runmgr
asks sacct for SLURM's own verdict: WALLCLOCK / CANCELLED / OOM / CRASHED.

Run:  python3 tests/test_job_log.py
"""
import os
import stat
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import utils.manage_utils as manage_utils
import runmgr
from utils.manage_utils import record_job, last_job, submit_case, JOB_LOG_NAME


def _fake_bin(d, name, body):
    path = os.path.join(d, name)
    with open(path, 'w') as f:
        f.write('#!/bin/sh\n' + body + '\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class JobLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.case_dir = os.path.join(self.tmp.name, 'mycase')
        os.makedirs(self.case_dir)
        self.bin = os.path.join(self.tmp.name, 'bin')
        os.makedirs(self.bin)
        self._path = os.environ['PATH']
        self._cache = runmgr._active_jobs_cache

    def tearDown(self):
        os.environ['PATH'] = self._path
        runmgr._active_jobs_cache = self._cache
        self.tmp.cleanup()

    def use_fake_bin(self):
        os.environ['PATH'] = self.bin + os.pathsep + self._path

    def test_record_and_last(self):
        self.assertIsNone(last_job(self.case_dir))
        self.assertTrue(record_job(self.case_dir, '111', 'submit'))
        self.assertTrue(record_job(self.case_dir, '222', 'continue'))
        job = last_job(self.case_dir)
        self.assertEqual((job['job_id'], job['verb']), ('222', 'continue'))
        with open(os.path.join(self.case_dir, JOB_LOG_NAME)) as f:
            self.assertEqual(len(f.read().splitlines()), 2)

    def test_record_unwritable_dir_is_best_effort(self):
        self.assertFalse(record_job(os.path.join(self.tmp.name, 'nope'), '1', 'submit'))

    def test_submit_case_records_job_id(self):
        _fake_bin(self.bin, 'sbatch', 'echo "Submitted batch job 4242"')
        self.use_fake_bin()
        open(os.path.join(self.case_dir, 'mycase.run'), 'w').close()
        ok, detail = submit_case(self.case_dir, 'mycase', verb='restart')
        self.assertTrue(ok)
        self.assertEqual(detail, '4242')
        job = last_job(self.case_dir)
        self.assertEqual((job['job_id'], job['verb']), ('4242', 'restart'))

    def test_submit_failure_records_nothing(self):
        _fake_bin(self.bin, 'sbatch', 'echo "sbatch: error: nope" >&2; exit 1')
        self.use_fake_bin()
        ok, _ = submit_case(self.case_dir, 'mycase')
        self.assertFalse(ok)
        self.assertIsNone(last_job(self.case_dir))

    # --- sacct verdict ---

    def _resolve_started_not_queued(self):
        runmgr._active_jobs_cache = frozenset()   # squeue: nothing queued
        cs = {'status': 'RUNNING', 'last_event': 'run started', 'last_ts': 'x'}
        return runmgr._resolve_status(cs, self.case_dir, 'mycase')

    def test_no_record_falls_back_to_run_out(self):
        self.assertEqual(self._resolve_started_not_queued(), 'RUNNING?')

    def test_timeout_is_wallclock(self):
        record_job(self.case_dir, '9', 'submit')
        _fake_bin(self.bin, 'sacct', 'echo TIMEOUT')
        self.use_fake_bin()
        self.assertEqual(self._resolve_started_not_queued(), 'WALLCLOCK')

    def test_cancelled_by_user(self):
        record_job(self.case_dir, '9', 'submit')
        _fake_bin(self.bin, 'sacct', 'echo "CANCELLED by 1001"')
        self.use_fake_bin()
        self.assertEqual(self._resolve_started_not_queued(), 'CANCELLED')

    def test_oom_and_failed(self):
        record_job(self.case_dir, '9', 'submit')
        _fake_bin(self.bin, 'sacct', 'echo OUT_OF_MEMORY')
        self.use_fake_bin()
        self.assertEqual(self._resolve_started_not_queued(), 'OOM')
        _fake_bin(self.bin, 'sacct', 'echo FAILED')
        self.assertEqual(self._resolve_started_not_queued(), 'CRASHED')

    def test_completed_is_contradictory_stays_running_question(self):
        record_job(self.case_dir, '9', 'submit')
        _fake_bin(self.bin, 'sacct', 'echo COMPLETED')
        self.use_fake_bin()
        self.assertEqual(self._resolve_started_not_queued(), 'RUNNING?')

    def test_sacct_error_falls_back(self):
        record_job(self.case_dir, '9', 'submit')
        _fake_bin(self.bin, 'sacct', 'exit 1')
        self.use_fake_bin()
        with open(os.path.join(self.case_dir, 'run.out'), 'w') as f:
            f.write('CSM EXECUTION BEGINS HERE\nCANCELLED AT x DUE TO TIME LIMIT\n')
        self.assertEqual(self._resolve_started_not_queued(), 'WALLCLOCK')

    def test_sacct_not_consulted_when_queued(self):
        # A queued job never needs sacct — and a bogus sacct must not be reached.
        record_job(self.case_dir, '9', 'submit')
        _fake_bin(self.bin, 'sacct', 'echo FAILED')
        self.use_fake_bin()
        runmgr._active_jobs_cache = frozenset({'mycase'})
        cs = {'status': 'RUNNING', 'last_event': 'run started', 'last_ts': 'x'}
        self.assertEqual(runmgr._resolve_status(cs, self.case_dir, 'mycase'), 'RUNNING')


if __name__ == '__main__':
    unittest.main()

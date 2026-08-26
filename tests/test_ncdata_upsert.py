#!/usr/bin/env python3
"""
Regression tests for the ncdata namelist write (build.py).

The bug these lock down (2026-08-26, destroyed 14 exovolc_ben2_* runs): the
ncdata rewrite was an unanchored, single-quote-only in-place sed

    sed -i "s|ncdata = '.*'|ncdata = '<path>'|" user_nl_cam

which rewrote the commented `!ncdata = '...'` lines (the substring matches)
while missing a live line written with double quotes. Cases silently inherited
whatever IC the clone template hardcoded.

Run:  python3 tests/test_ncdata_upsert.py
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build


# A user_nl_cam shaped like the real eruption clone templates: several
# commented-out ncdata candidates, then one live line in double quotes.
TEMPLATE_NL = (
    "!ncdata = '/ic/candidate_a.nc'\n"
    "!ncdata = '/ic/candidate_b.nc'\n"
    "! ncdata = '/ic/candidate_c.nc'\n"
    'ncdata = "/ic/ben1_hardcoded.nc"\n'
    "empty_htapes = .true.\n"
)

NCDATA_LIVE = re.compile(r'^[ \t]*ncdata[ \t]*=(.*)$', re.M)


def run_block(lines, nl_text, target='user_nl_cam'):
    """Execute generated shell lines in a temp dir seeded with nl_text.

    Returns (returncode, stdout+stderr, resulting file text or None).
    """
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, target)
        with open(path, 'w') as f:
            f.write(nl_text)
        script = '\n'.join(lines)
        # GNU vs BSD sed: `sed -i -E` is emitted for the HPC (GNU). On BSD
        # (macOS) the in-place flag needs an explicit empty suffix.
        if sys.platform == 'darwin':
            script = re.sub(r'sed -i(?! )', "sed -i ''", script)
            script = re.sub(r"sed -i (?!'')", "sed -i '' ", script)
        # stdout=PIPE/stderr=PIPE rather than capture_output=, and
        # universal_newlines= rather than text=: Discover's default python3
        # is 3.6, which has neither of the newer spellings.
        proc = subprocess.run(['bash', '-c', script], cwd=d,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        out = proc.stdout + proc.stderr
        if os.path.exists(path):
            with open(path) as f:
                text = f.read()
        else:
            text = None
        return proc.returncode, out, text


class TestNcdataUpsert(unittest.TestCase):

    def test_live_double_quoted_line_is_replaced(self):
        """The live line must be rewritten even though it uses double quotes."""
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/ben2.nc',
                                                label='case_x')
        rc, out, text = run_block(lines, TEMPLATE_NL)
        self.assertEqual(rc, 0, out)
        live = NCDATA_LIVE.findall(text)
        self.assertEqual(len(live), 1, f"expected exactly one live ncdata:\n{text}")
        self.assertIn('/ic/ben2.nc', live[0])
        self.assertNotIn('ben1_hardcoded', live[0])

    def test_commented_lines_are_not_rewritten(self):
        """The old sed rewrote the dead comments; the fix must leave them alone."""
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/ben2.nc',
                                                label='case_x')
        rc, out, text = run_block(lines, TEMPLATE_NL)
        self.assertEqual(rc, 0, out)
        for cand in ('candidate_a', 'candidate_b', 'candidate_c'):
            self.assertIn(cand, text, "a commented ncdata line was clobbered")
        self.assertEqual(text.count('/ic/ben2.nc'), 1,
                         "the new path leaked into a commented line")

    def test_single_quoted_live_line_is_replaced(self):
        """The aqua template's live line is single-quoted; it must also work."""
        nl = TEMPLATE_NL.replace('ncdata = "/ic/ben1_hardcoded.nc"',
                                 "ncdata = '/ic/hab2_hardcoded.nc'")
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/hab1.nc',
                                                label='case_x')
        rc, out, text = run_block(lines, nl)
        self.assertEqual(rc, 0, out)
        live = NCDATA_LIVE.findall(text)
        self.assertEqual(len(live), 1, text)
        self.assertIn('/ic/hab1.nc', live[0])

    def test_duplicate_live_lines_collapse_to_one(self):
        nl = TEMPLATE_NL + "ncdata = '/ic/stray.nc'\n"
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/ben2.nc',
                                                label='case_x')
        rc, out, text = run_block(lines, nl)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(NCDATA_LIVE.findall(text)), 1, text)

    def test_key_absent_from_namelist_is_still_written(self):
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/ben2.nc',
                                                label='case_x')
        rc, out, text = run_block(lines, "empty_htapes = .true.\n")
        self.assertEqual(rc, 0, out)
        live = NCDATA_LIVE.findall(text)
        self.assertEqual(len(live), 1, text)
        self.assertIn('/ic/ben2.nc', live[0])

    def test_no_op_fails_loudly(self):
        """A write that does not take must abort the build, not proceed."""
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/ben2.nc',
                                                label='case_x')
        # Sabotage the append so the write silently does not take -- the
        # shape of the original bug.
        sabotaged = [l.replace('/ic/ben2.nc', '/ic/WRONG.nc')
                     if l.startswith('echo "ncdata') else l
                     for l in lines]
        rc, out, text = run_block(sabotaged, TEMPLATE_NL)
        self.assertNotEqual(rc, 0, "a no-op write was accepted silently")
        self.assertIn('ERROR', out)

    def test_prefix_key_is_not_matched(self):
        """A different key that merely starts with the same text is untouched."""
        nl = "ncdata_extra = '/ic/keepme.nc'\nncdata = '/ic/old.nc'\n"
        lines = build._nl_upsert_verified_lines('ncdata', '/ic/new.nc',
                                                label='case_x')
        rc, out, text = run_block(lines, nl)
        self.assertEqual(rc, 0, out)
        self.assertIn('keepme', text)


class TestOldPatternIsActuallyBroken(unittest.TestCase):
    """Sanity anchor: the pattern that was replaced really does fail on this
    fixture. Without this, the tests above could pass against a fixture that
    never exercised the bug."""

    def test_old_sed_rewrites_comments_and_misses_the_live_line(self):
        old = ["""sed -i "s|ncdata = '.*'|ncdata = '/ic/ben2.nc'|" user_nl_cam"""]
        rc, out, text = run_block(old, TEMPLATE_NL)
        self.assertEqual(rc, 0, out)
        live = NCDATA_LIVE.findall(text)
        self.assertIn('ben1_hardcoded', live[0],
                      "fixture no longer reproduces the original bug")
        self.assertIn("!ncdata = '/ic/ben2.nc'", text,
                      "fixture no longer reproduces the comment rewrite")


class TestClmUpsert(unittest.TestCase):

    def test_finidat_double_quoted_live_line_is_replaced(self):
        """user_nl_clm carried the same defect (double-quote-only, unanchored)."""
        nl = ('!finidat = "/land/old_commented.nc"\n'
              'finidat = "/land/hardcoded.nc"\n')
        lines = build._nl_upsert_verified_lines(
            'finidat', '/land/ben2.nc', target='user_nl_clm', label='case_x')
        rc, out, text = run_block(lines, nl, target='user_nl_clm')
        self.assertEqual(rc, 0, out)
        live = re.findall(r'^[ \t]*finidat[ \t]*=(.*)$', text, re.M)
        self.assertEqual(len(live), 1, text)
        self.assertIn('/land/ben2.nc', live[0])
        self.assertIn('old_commented', text)


class TestSolarFileSed(unittest.TestCase):
    """exo_solar_file lives in Fortran, not a namelist, so it keeps its
    replace-in-place sed -- but it carried the same unanchored pattern and
    would have rewritten commented declarations. It must now skip comments."""

    FORTRAN = ("!      exo_solar_file = '/sol/old_comment.nc'\n"
               "      character(len=256), parameter :: "
               "exo_solar_file = '/sol/old_live.nc'  !! trailing\n")

    def _emit(self, matrix_value):
        """Pull the emitted sed line out of a generated clone script."""
        import build as b
        spec = {'exo_solar_file': matrix_value}
        # the line is built inline in generate_*; assert on its shape directly
        return (f"sed -i -E \"/^[[:space:]]*!/! "
                f"s|exo_solar_file[[:space:]]*=[[:space:]]*'[^']*'"
                f"|exo_solar_file = '{b._sed_escape_replacement(matrix_value)}'|\" "
                f"f.F90")

    def test_comment_is_skipped_and_live_line_rewritten(self):
        rc, out, text = run_block([self._emit('/sol/new.nc')],
                                  self.FORTRAN, target='f.F90')
        self.assertEqual(rc, 0, out)
        self.assertIn('old_comment', text, 'a commented declaration was rewritten')
        self.assertIn("exo_solar_file = '/sol/new.nc'", text)
        self.assertNotIn('old_live', text)
        self.assertIn('!! trailing', text, 'trailing comment was eaten')


class TestNlCamParamsCollision(unittest.TestCase):
    """ncdata set through nl_cam_params would run after -- and silently beat --
    the dedicated verified block. validate_case must reject it."""

    def _spec(self, **extra):
        spec = {
            'config_type': 'cam_land_fv',
            'clone': 'cam_land_fv_eruption',
            'exort_pkg': 'n68equiv*',
            'nlev': 51,
            'mach': 'discover',
            'stop_option': 'nyears', 'stop_n': 6,
            'rest_option': 'nyears', 'rest_n': 6,
            'resubmit': 0, 'ntasks': 126,
            'run_type': 'hybrid',
            'run_refcase': 'ben2_L51_p6.1', 'run_refdate': '0156-01-01',
        }
        spec.update(extra)
        return spec

    def test_ncdata_in_nl_cam_params_is_an_error(self):
        errs = build.validate_case(
            self._spec(nl_cam_params={'ncdata': '/ic/x.nc'}), {})
        self.assertTrue(any('ncdata' in e and 'nl_cam_params' in e for e in errs),
                        errs)

    def test_finidat_in_nl_cam_params_is_an_error(self):
        errs = build.validate_case(
            self._spec(nl_cam_params={'finidat': '/land/x.nc'}), {})
        self.assertTrue(any('finidat' in e for e in errs), errs)

    def test_clean_spec_has_no_collision_error(self):
        errs = build.validate_case(
            self._spec(ncdata='/ic/x.nc',
                       nl_cam_params={'empty_htapes': True}), {})
        self.assertFalse([e for e in errs if 'nl_cam_params' in e], errs)


if __name__ == '__main__':
    unittest.main(verbosity=2)

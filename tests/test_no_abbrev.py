#!/usr/bin/env python3
"""
Every argparse parser refuses abbreviated long options.

argparse's default accepts any unambiguous prefix, so `--exec` reaches
`--execute`. The hpc-connect guard runs these scripts token-free at its
`operate` profile and reads their arguments as written; a prefix it does
not recognise could then carry a destructive flag past it (hpc-connect
live test, 2026-11-12). Subparsers do not inherit allow_abbrev, so every
add_parser call needs it too.

Run:  python3 tests/test_no_abbrev.py
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ["build.py", "datamgr.py", "query.py", "runmgr.py", "scan.py", "tools/diff.py"]


def _parser_calls(path):
    tree = ast.parse(open(os.path.join(REPO, path)).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("ArgumentParser", "add_parser"):
            yield node


class TestNoAbbrev(unittest.TestCase):
    def test_every_parser_sets_allow_abbrev_false(self):
        seen = 0
        for path in SCRIPTS:
            for call in _parser_calls(path):
                seen += 1
                with self.subTest(file=path, line=call.lineno):
                    kw = {k.arg: k.value for k in call.keywords}
                    self.assertIn("allow_abbrev", kw)
                    self.assertIs(kw["allow_abbrev"].value, False)
        self.assertGreaterEqual(seen, 28)

    def test_runmgr_refuses_a_prefix_of_execute(self):
        sys.path.insert(0, REPO)
        try:
            import runmgr
        except ImportError as exc:  # a dependency missing locally
            self.skipTest(str(exc))
        parser = runmgr.build_parser()
        for verb in ("xml", "continue", "restart"):
            for prefix in ("--exec", "--execut", "--ex"):
                with self.subTest(verb=verb, flag=prefix):
                    with self.assertRaises(SystemExit) as info, \
                            open(os.devnull, "w") as null, \
                            _redirect_stderr(null):
                        parser.parse_args([verb, "case1", prefix])
                    self.assertEqual(info.exception.code, 2)
            # the full spelling still parses
            self.assertTrue(parser.parse_args([verb, "case1", "--execute"]).execute)


def _redirect_stderr(stream):
    import contextlib
    return contextlib.redirect_stderr(stream)


if __name__ == "__main__":
    unittest.main()

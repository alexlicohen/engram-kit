#!/usr/bin/env python3
"""Teeth test for regen.regen_status_tallies (v3.4) — STATUS.md's note tallies are GENERATED, so
the STATUS-count WARN (and the recurring hand-drift) becomes structurally impossible.

Proves: counts come from index.jsonl (not the file's prior text); the test count is real; and a
deliberate hand-edit INSIDE the markers is overwritten. Hermetic: builds a temp repo + chdir.
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
import regen  # noqa: E402


class TestRegenStatusTallies(unittest.TestCase):
    def _repo(self, tmp, status_body):
        os.makedirs(os.path.join(tmp, "_manifest"))
        os.makedirs(os.path.join(tmp, "scripts", "tests"))
        with open(os.path.join(tmp, "_manifest", "index.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"source": "gmail", "domain": "research"}) + "\n")
            f.write(json.dumps({"source": "gmail", "domain": "personal"}) + "\n")
            f.write(json.dumps({"source": "imessage", "domain": "research"}) + "\n")
        with open(os.path.join(tmp, "scripts", "tests", "test_x.py"), "w", encoding="utf-8") as f:
            f.write("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
        with open(os.path.join(tmp, "_manifest", "STATUS.md"), "w", encoding="utf-8") as f:
            f.write(status_body)

    def _run(self, status_body):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp, status_body)
            os.chdir(tmp)
            try:
                regen.regen_status_tallies()
                return open(os.path.join(tmp, "_manifest", "STATUS.md"), encoding="utf-8").read()
            finally:
                os.chdir(cwd)

    def test_generates_correct_counts_and_overwrites_hand_edit(self):
        # a LYING hand-edit inside the markers (wrong count) must be overwritten with the real one
        body = ("## tallies\n\n" + regen.TALLY_START
                + "\n**Total notes: 99,999** — by source: bogus.\n" + regen.TALLY_END + "\n")
        out = self._run(body)
        self.assertIn("**Total notes: 3**", out)          # from index.jsonl, not the lie
        self.assertNotIn("99,999", out)
        self.assertIn("gmail 2", out)                      # by-source generated
        self.assertIn("imessage 1", out)
        self.assertIn("2 tests", out)                      # def test_ count
        self.assertIn("| research | 2 |", out)             # by-domain generated (counts + subpath col)

    def test_missing_markers_raises(self):
        with self.assertRaises(SystemExit):
            self._run("## tallies\n\n(no markers here)\n")


if __name__ == "__main__":
    unittest.main()

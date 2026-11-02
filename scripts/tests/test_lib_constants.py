#!/usr/bin/env python3
"""Guard test for the single-owner constants (_lib/constants.py), including the finding-4 addition
BACKFILL_WAVES.

The constants live in _manifest/constants/*.json (the single source of truth); DOMAINS, TAG_VOCAB,
and BACKFILL_WAVES must load, have the right types, and match the expected canonical sets. The Node
twin (_lib/constants.mjs) was RETIRED with the Workflow-runtime migration — the pure-JS runtime has
no fs, so distillers no longer load constants via Node (only Python reads them now). There is thus
no Python/Node parity check to run anymore, only the Python loader under test.

Reads the REAL _manifest/constants/*.json (that's the single source under test) but touches
nothing else. Run: python3 -m pytest this_file.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_REPO = os.path.dirname(_SCRIPTS)
sys.path.insert(0, _SCRIPTS)  # scripts/ on path for _lib.*

from _lib.constants import DOMAINS, TAG_VOCAB, BACKFILL_WAVES  # noqa: E402

EXPECTED_BACKFILL = {
    "gmail-run.jsonl", "imessage-run.jsonl", "granola-redistill.jsonl",
    "gmail-secondary-run.jsonl",
}


class TestConstantsLoad(unittest.TestCase):
    def test_python_types_and_content(self):
        self.assertIsInstance(DOMAINS, list)
        self.assertTrue(DOMAINS, "DOMAINS must be non-empty")
        self.assertIn("personal", DOMAINS)
        self.assertIsInstance(TAG_VOCAB, set)
        self.assertTrue(TAG_VOCAB)
        self.assertIsInstance(BACKFILL_WAVES, set)

    def test_backfill_waves_exact_set(self):
        self.assertEqual(BACKFILL_WAVES, EXPECTED_BACKFILL)


if __name__ == "__main__":
    unittest.main(verbosity=2)

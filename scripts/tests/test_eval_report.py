#!/usr/bin/env python3
"""Guard test for the retrieval-eval report helper (scripts/eval_report.py) — the DETERMINISTIC half
of the eval harness (the agent fan-out is exercised by the /engram-eval skill, not here).

Covers: report files written in stable question order, the run-over-run previous-report selection,
and input validation (unknown question id / bad grade are hard errors). Hermetic — temp dirs only.
"""
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)

import eval_report  # noqa: E402


class TestEvalReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.q = os.path.join(self.tmp, "questions.jsonl")
        with open(self.q, "w", encoding="utf-8") as f:
            for qid, dom in [("a", "finance"), ("b", "research"), ("c", "ventures")]:
                f.write(json.dumps({"id": qid, "domain": dom, "question": f"q {qid}",
                                    "expect_hint": [], "check": ""}) + "\n")
        self.reports = os.path.join(self.tmp, "reports")
        eval_report.QFILE = self.q          # redirect module globals at the source (single owner)
        eval_report.REPORT_DIR = self.reports

    def _write(self, rows, date):
        rf = os.path.join(self.tmp, f"res-{date}.jsonl")
        with open(rf, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        eval_report.cmd_write(Namespace(results=rf, date=date))

    def test_writes_report_files(self):
        self._write([{"id": "a", "grade": "WELL", "hops": 3, "notes": ["x"], "stale": []},
                     {"id": "b", "grade": "PARTIAL", "hops": 5, "notes": [], "stale": ["f -> n"]}],
                    "2026-01-01")
        self.assertTrue(os.path.exists(os.path.join(self.reports, "2026-01-01.md")))
        raw = [json.loads(l) for l in open(os.path.join(self.reports, "2026-01-01.jsonl"))]
        self.assertEqual([r["id"] for r in raw], ["a", "b"])   # stable question order

    def test_prev_report_selection(self):
        self._write([{"id": "a", "grade": "WELL"}], "2026-01-01")
        self.assertEqual(eval_report._prev_report("2026-01-02"),
                         os.path.join(self.reports, "2026-01-01.jsonl"))
        self.assertIsNone(eval_report._prev_report("2026-01-01"))   # nothing strictly before

    def test_diff_detects_regression_and_improvement(self):
        self._write([{"id": "a", "grade": "WELL"}, {"id": "b", "grade": "PARTIAL"}], "2026-01-01")
        self._write([{"id": "a", "grade": "THIN"}, {"id": "b", "grade": "WELL"}], "2026-01-02")
        grades = {json.loads(l)["id"]: json.loads(l)["grade"]
                  for l in open(os.path.join(self.reports, "2026-01-02.jsonl"))}
        self.assertEqual(grades, {"a": "THIN", "b": "WELL"})   # a regressed, b improved

    def test_unknown_id_rejected(self):
        rf = os.path.join(self.tmp, "bad.jsonl")
        open(rf, "w").write(json.dumps({"id": "ZZZ", "grade": "WELL"}) + "\n")
        with self.assertRaises(SystemExit):
            eval_report.cmd_write(Namespace(results=rf, date="2026-01-03"))

    def test_bad_grade_rejected(self):
        rf = os.path.join(self.tmp, "bad2.jsonl")
        open(rf, "w").write(json.dumps({"id": "a", "grade": "GREAT"}) + "\n")
        with self.assertRaises(SystemExit):
            eval_report.cmd_write(Namespace(results=rf, date="2026-01-04"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

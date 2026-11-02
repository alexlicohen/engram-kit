#!/usr/bin/env python3
"""Teeth test for scripts/gen_digests.py — the on-demand digest generator that replaced the
committed regen.regen_digests (plan generic-bubbling-eagle.md, Feature D).

Proves: rows are collected reverse-chron; action-items groups by domain and lists only
action-bearing notes; decisions lists only decision-bearing notes with a [domain] tag; write()
emits both files matching the render functions. Hermetic: builds a temp repo + chdir (notes live
under REAL domain names so regen.notes_in/TOPS find them)."""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
import gen_digests  # noqa: E402


def _note(dirp, name, date, title, actions=None, decisions=None):
    os.makedirs(dirp, exist_ok=True)
    body = f'---\ntitle: "{title}"\ndate: {date}\ndomain: {os.path.basename(dirp)}\n---\n\nIntro.\n'
    if actions:
        body += "\n## Action items\n" + "".join(f"- {a}\n" for a in actions)
    if decisions:
        body += "\n## Decisions\n" + "".join(f"- {d}\n" for d in decisions)
    with open(os.path.join(dirp, name), "w", encoding="utf-8") as f:
        f.write(body)


class TestGenDigests(unittest.TestCase):
    def _build(self, tmp):
        # research: one note with both sections (later date), one with actions only (earlier)
        _note(os.path.join(tmp, "research"), "2026-03-01-a.md", "2026-03-01", "Note A",
              actions=["do X"], decisions=["chose Y"])
        _note(os.path.join(tmp, "research"), "2026-01-01-b.md", "2026-01-01", "Note B",
              actions=["do Z"])
        # personal: decisions only
        _note(os.path.join(tmp, "personal"), "2026-02-01-c.md", "2026-02-01", "Note C",
              decisions=["picked P"])
        # a note with NEITHER section — must appear in neither digest
        _note(os.path.join(tmp, "personal"), "2026-02-15-d.md", "2026-02-15", "Note D")

    def _run(self, fn):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            self._build(tmp)
            os.chdir(tmp)
            try:
                return fn(tmp)
            finally:
                os.chdir(cwd)

    def test_collect_rows_reverse_chron(self):
        rows = self._run(lambda tmp: gen_digests.collect_rows())
        dates = [r["date"] for r in rows]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(len(rows), 4)

    def test_action_items_grouped_by_domain(self):
        rows = self._run(lambda tmp: gen_digests.collect_rows())
        ai = gen_digests.render_action_items(rows)
        self.assertIn("# Action-items rollup", ai)
        self.assertIn("gen_digests.py", ai)               # provenance names the new owner
        self.assertIn("2 notes.", ai)                     # A + B have actions; C, D do not
        self.assertIn("## research (2)", ai)
        self.assertNotIn("## personal", ai)               # personal has no action-bearing note
        # reverse-chron within the domain: A (03-01) before B (01-01)
        self.assertLess(ai.index("Note A"), ai.index("Note B"))
        self.assertNotIn("Note D", ai)                    # neither-section note excluded

    def test_decisions_log(self):
        rows = self._run(lambda tmp: gen_digests.collect_rows())
        dec = gen_digests.render_decisions(rows)
        self.assertIn("# Decisions log", dec)
        self.assertIn("2 notes.", dec)                    # A + C have decisions
        self.assertIn("### 2026-03-01 · [research] ·", dec)
        self.assertIn("### 2026-02-01 · [personal] ·", dec)
        self.assertLess(dec.index("Note A"), dec.index("Note C"))  # reverse-chron overall
        self.assertNotIn("Note B", dec)                   # actions-only note excluded
        self.assertNotIn("Note D", dec)

    def test_write_matches_render(self):
        def go(tmp):
            rows = gen_digests.collect_rows()
            paths = gen_digests.write(out_dir=os.path.join(tmp, "digests"), rows=rows)
            self.assertEqual(len(paths), 2)
            ai = open(os.path.join(tmp, "digests", "action-items.md"), encoding="utf-8").read()
            dec = open(os.path.join(tmp, "digests", "decisions.md"), encoding="utf-8").read()
            self.assertEqual(ai, gen_digests.render_action_items(rows))
            self.assertEqual(dec, gen_digests.render_decisions(rows))
        self._run(go)


if __name__ == "__main__":
    unittest.main()

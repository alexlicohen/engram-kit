#!/usr/bin/env python3
"""Regression test for regen_mocs()'s scaffold-coupling guard.

Proves the bug a code review found: write_top_moc() links every non-root subpath value present in
the data into the domain's Areas map, but the sub-cluster MOC loop only emits a listing for subpaths
that already have a committed {dom}/{sub}/_index.md scaffold (glob-driven). Before the fix, a note
classified into a subpath with no scaffold yet (e.g. a freshly added/renamed assign_subpaths.py rule)
would silently vanish from every MOC while check.sh stayed green. regen_mocs() must now fail loud
instead. Hermetic: builds a temp repo + chdir, monkeypatches TOPS.
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
import regen  # noqa: E402

NOTE = """---
title: "{title}"
date: {date}
domain: {dom}
subpath: {sub}
status: done
---

body.
"""


class TestRegenMocsScaffoldGuard(unittest.TestCase):
    def _repo(self, tmp, notes):
        os.makedirs(os.path.join(tmp, "research"), exist_ok=True)
        for i, (title, date, sub) in enumerate(notes):
            with open(os.path.join(tmp, "research", f"{date}-note-{i}.md"), "w", encoding="utf-8") as f:
                f.write(NOTE.format(title=title, date=date, dom="research", sub=sub or "null"))

    def _run(self, notes, with_scaffold=None, read_back=None):
        cwd = os.getcwd()
        orig_tops = regen.TOPS
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp, notes)
            if with_scaffold:
                sdir = os.path.join(tmp, "research", with_scaffold)
                os.makedirs(sdir, exist_ok=True)
                with open(os.path.join(sdir, "_index.md"), "w", encoding="utf-8") as f:
                    f.write(regen._gen_head("research", with_scaffold, f"research/{with_scaffold}"))
            os.chdir(tmp)
            regen.TOPS = ["research"]
            try:
                regen.regen_mocs()
                if read_back:
                    return open(read_back, encoding="utf-8").read()
            finally:
                os.chdir(cwd)
                regen.TOPS = orig_tops

    def test_subpath_without_scaffold_raises(self):
        # "grants" has notes but no committed {dom}/grants/_index.md scaffold anywhere in the temp repo.
        with self.assertRaises(SystemExit) as ctx:
            self._run([("A grants note", "2026-01-01", "grants")])
        msg = str(ctx.exception)
        self.assertIn("grants", msg)
        self.assertIn("research", msg)
        self.assertIn("no committed scaffold", msg)

    def test_subpath_with_scaffold_does_not_raise(self):
        # same note, but the scaffold now exists — must regenerate cleanly, and the sub-cluster MOC
        # must actually pick up the note (read back from inside the temp repo, before cwd restores).
        content = self._run([("A grants note", "2026-01-01", "grants")], with_scaffold="grants",
                             read_back="research/grants/_index.md")
        self.assertIn("A grants note", content)

    def test_root_only_notes_never_trigger_the_guard(self):
        # subpath-less (root/unfiled) notes are exempt — only non-empty subpath values are checked.
        self._run([("Unfiled note", "2026-01-01", "")])  # must not raise


if __name__ == "__main__":
    unittest.main()

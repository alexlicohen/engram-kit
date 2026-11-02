#!/usr/bin/env python3
"""Teeth test for scripts/bump_watermark.py — covers two real bugs a 2026-07-10 code review found
before the first automated Outlook sync (range=aab8a33b..HEAD full-repo review):

  * SOURCE_TO_WATERMARK_KEY had no entry for "gmail-update" (a live, valid --source value in
    sync_verify.SOURCES) -> an unhandled KeyError crash instead of the clean "no tracked
    watermark" refusal every other watermark-less source gets. Now asserted at import time to
    stay in sync with sync_verify.SOURCES, and gmail-update maps to None like drive/gmail-remediate2.
  * The backwards-watermark guard would deterministically hard-fail Outlook's sync given the real
    staged data: 48 pending threads dated Feb-June 2026, watermark already at 2026-07-06 from an
    earlier run. Unlike gmail/granola (date-bounded connector query), Outlook's worklist has no
    date lower-bound, so an older backlog batch landing behind the watermark is EXPECTED, not a
    bug — it's now a silent no-op (exit 0, watermark unchanged) instead of a hard refusal, scoped
    to BACKLOG_SOURCES so gmail/granola/imessage keep the original bug-catching behavior.

Self-contained temp root (its own git repo + _manifest/sync-state.json + note files); mirrors
test_resolve_wave.py's fixture style. Run: python3 -m pytest this_file.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
BUMP_WATERMARK = _REPO / "scripts" / "bump_watermark.py"
PY = sys.executable

sys.path.insert(0, str(_REPO / "scripts"))
import bump_watermark as bw  # noqa: E402
import sync_verify as sv  # noqa: E402


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def note_text(source, ref, date):
    return (
        "---\n"
        f'title: "Thread"\n'
        f"date: {date}\n"
        "tags: [correspondence]\n"
        "domain: personal\n"
        f"source: {source}\n"
        "provenance:\n"
        f'  ref: "{ref}"\n'
        "  ingested: 2026-07-10\n"
        "  method: distilled\n"
        "participants: []\n"
        "status: done\n"
        "related: []\n"
        "supersedes: []\n"
        "superseded_by: []\n"
        "---\n\nBody.\n"
    )


def make_root(tmp, source, watermark_key, watermark_date):
    root = pathlib.Path(tmp)
    (root / "_manifest").mkdir()
    (root / "outlook").mkdir()
    sh(["git", "init", "-q"], cwd=root)
    sh(["git", "config", "user.email", "test@example.com"], cwd=root)
    sh(["git", "config", "user.name", "Test"], cwd=root)
    state = {watermark_key: {"watermark_date": watermark_date}} if watermark_date else {}
    (root / "_manifest" / "sync-state.json").write_text(json.dumps(state))
    return root


def add_note(root, domain, fname, source, ref, date):
    p = root / domain / fname
    p.write_text(note_text(source, ref, date))
    sh(["git", "add", str(p)], cwd=root)


def run_bump(root, source, extra=None):
    cmd = [PY, str(BUMP_WATERMARK), "--source", source, "--root", str(root)]
    if extra:
        cmd += extra
    return sh(cmd)


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestSourceToWatermarkKeyCompleteness(unittest.TestCase):
    def test_covers_every_sync_verify_source(self):
        self.assertEqual(set(bw.SOURCE_TO_WATERMARK_KEY), set(sv.SOURCES))

    def test_gmail_update_maps_to_none_not_missing(self):
        self.assertIn("gmail-update", bw.SOURCE_TO_WATERMARK_KEY)
        self.assertIsNone(bw.SOURCE_TO_WATERMARK_KEY["gmail-update"])

    def test_gmail_update_refuses_cleanly_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, "gmail-update", "gmail_personal", None)
            r = run_bump(root, "gmail-update")
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("no tracked watermark", r.stdout, out(r))
            self.assertNotIn("Traceback", r.stdout + r.stderr, out(r))


class TestBacklogSourceBackwardsWatermark(unittest.TestCase):
    def test_outlook_older_batch_is_silent_noop_not_a_hard_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            # watermark already at 2026-07-06 (an earlier, smaller run); today's wave's only new
            # note is dated 2026-06-30 -- older, exactly the real staged-data scenario
            root = make_root(tmp, "outlook", "outlook", "2026-07-06")
            add_note(root, "outlook", "old-thread.md", "outlook",
                     "outlook:thread:abc123def456", "2026-06-30")
            r = run_bump(root, "outlook")
            self.assertEqual(r.returncode, 0, out(r))
            self.assertNotIn("refusing", r.stdout.lower(), out(r))
            state = json.loads((root / "_manifest" / "sync-state.json").read_text())
            self.assertEqual(state["outlook"]["watermark_date"], "2026-07-06",
                              "watermark must stay at the higher existing value, not move backwards")

    def test_gmail_backwards_watermark_still_hard_fails(self):
        # non-backlog source: the ORIGINAL bug-catching behavior must be unchanged
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "_manifest").mkdir()
            (root / "research").mkdir()
            sh(["git", "init", "-q"], cwd=root)
            sh(["git", "config", "user.email", "test@example.com"], cwd=root)
            sh(["git", "config", "user.name", "Test"], cwd=root)
            (root / "_manifest" / "sync-state.json").write_text(
                json.dumps({"gmail_personal": {"watermark_date": "2026-07-06"}}))
            add_note(root, "research", "old-thread.md", "gmail",
                     "gmail:thread:abc123def456", "2026-06-30")
            r = run_bump(root, "gmail")
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("refusing", r.stdout.lower(), out(r))
            state = json.loads((root / "_manifest" / "sync-state.json").read_text())
            self.assertEqual(state["gmail_personal"]["watermark_date"], "2026-07-06")

    def test_outlook_forward_watermark_still_advances_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, "outlook", "outlook", "2026-07-01")
            add_note(root, "outlook", "new-thread.md", "outlook",
                     "outlook:thread:abc123def456", "2026-07-08")
            r = run_bump(root, "outlook")
            self.assertEqual(r.returncode, 0, out(r))
            state = json.loads((root / "_manifest" / "sync-state.json").read_text())
            self.assertEqual(state["outlook"]["watermark_date"], "2026-07-08")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Regression test for guard-audit finding 2: iMessage re-distill dup-ingest hole,
INCLUDING the data-loss follow-up (reconcile must never touch a committed note).

THE HOLE (pre-fix): the done-marker `_sources/imsg-done.d/<chunk_id>` was written by the LLM
sub-agent AFTER it wrote the chunk's notes. A crash between note-write and marker-write left
notes-on-disk-with-no-marker; on resume `imsg_worklist.py` re-emitted the chunk and it was
re-distilled. The re-distilled note (a) carries the same `imessage:` ref, EXEMPT from the dup-ref
lint, and (b) usually lands on a DIFFERENT filename (run-to-run slug variance), so the
slug-collision WARN never fires either. Result: a silent SECOND note sharing one chunk ref.

THE FIX (this test proves teeth):
  A. The marker is stamped FROM CODE (scripts/_lib/imsg_markers.py, driven post-Workflow by
     scripts/stamp_markers.py — the retired _lib/imsg_markers.mjs did this inside the workflow
     before the pure-JS runtime lost fs access) only AFTER the returned note paths are verified to
     exist -> a marker means "notes verifiably exist". A chunk that under-reports its paths
     (durable_found > returned paths) is WITHHELD (finding 3).
  B. Before a re-emitted (crash-orphaned) chunk is re-distilled, imsg_worklist.compute_worklist
     reconciles crash debris -- but ONLY a git-UNTRACKED new note may be archived. A TRACKED /
     COMMITTED note that shares a pending chunk's ref (which happens on a fresh clone / rebuilt
     `_sources`, since markers are gitignored & re-derivable) is SKIPPED and surfaced, NEVER
     archived -- otherwise reconcile would DROP committed notes (data loss).

Self-contained: a synthetic git repo in a temp dir. Touches neither the real store nor real
_sources. Run: python3 scripts/tests/test_finding2_imsg_dedup.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_REPO = os.path.dirname(_SCRIPTS)
sys.path.insert(0, _SCRIPTS)  # import _lib.* and imsg_worklist

from _lib.imsg_markers import (  # noqa: E402
    chunk_ref, build_ref_index, stamp_verified, marker_path,
)
import imsg_worklist  # noqa: E402


def write_note(root, relpath, ref):
    """Write a minimal valid store note carrying the given provenance ref."""
    fp = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            'title: "synthetic"\n'
            "date: 2024-01-01\n"
            "domain: personal\n"
            "source: imessage\n"
            "provenance:\n"
            '  ref: "%s"\n'
            "  ingested: 2026-06-30\n"
            "  method: distilled\n"
            "status: done\n"
            "---\n"
            "## Summary\nsynthetic body\n" % ref
        )
    return fp


class TestFinding2(unittest.TestCase):
    def _git(self, *args):
        return subprocess.run(["git", "-C", self.root, *args],
                              check=True, capture_output=True, text=True)

    def _commit(self, *relpaths, msg="commit"):
        self._git("add", "--", *relpaths)
        self._git("commit", "-q", "-m", msg)

    def _untracked(self, relpath):
        out = self._git("ls-files", "--others", "--exclude-standard").stdout.split("\n")
        return relpath in out

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="imsg-dedup-test-")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")
        os.makedirs(os.path.join(self.root, "personal"))
        self.done = os.path.join(self.root, "_sources", "imsg-done.d")
        os.makedirs(self.done)
        # Mirror reality: _sources/ (hence the done-markers) is GITIGNORED and re-derivable.
        with open(os.path.join(self.root, ".gitignore"), "w") as f:
            f.write("_sources/\n")
        self._commit(".gitignore", msg="init")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _wave(self, *chunks):
        wave = os.path.join(self.root, "wave.jsonl")
        with open(wave, "w") as f:
            for c in chunks:
                f.write(json.dumps(c) + "\n")
        return wave, os.path.join(self.root, "pending.jsonl")

    # ---- A. happy path: fresh chunk -> notes verified -> marker written (code, not LLM) --------
    def test_A_happy_path_marker_written(self):
        ref = chunk_ref("GUIDA", "A_c000")
        write_note(self.root, "personal/2024-01-01-foo-imA-aaa111.md", ref)
        chunk = {"chunk_id": "A_c000", "ref": ref, "durable_found": 1,
                 "notes": ["personal/2024-01-01-foo-imA-aaa111.md"], "sensitive": []}

        ok, missing = stamp_verified(self.done, self.root, chunk)   # Python twin
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        mk = marker_path(self.done, "A_c000")
        self.assertTrue(os.path.exists(mk), "marker must be stamped after verification")
        with open(mk) as f:
            rec = json.load(f)
        self.assertTrue(rec["verified"])
        self.assertEqual(rec["ref"], ref)
        self.assertEqual(rec["notes"], ["personal/2024-01-01-foo-imA-aaa111.md"])

    # ---- B. missing declared note -> NO marker -> chunk stays pending ---------------------------
    def test_B_missing_declared_note_no_marker(self):
        chunk = {"chunk_id": "B_c000", "ref": chunk_ref("GUIDB", "B_c000"),
                 "notes": ["personal/does-not-exist.md"], "sensitive": []}

        ok, missing = stamp_verified(self.done, self.root, chunk)
        self.assertFalse(ok)
        self.assertEqual(missing, ["personal/does-not-exist.md"])
        self.assertFalse(os.path.exists(marker_path(self.done, "B_c000")))

        wave, out = self._wave({"chunk_id": "B_c000", "guid": "GUIDB", "chat_id": "B", "fp": "x"})
        res = imsg_worklist.compute_worklist(wave, out, root=self.root, done_dir=self.done)
        self.assertEqual(res["pending"], 1)
        self.assertEqual(res["done"], 0)

    # ---- C. crash window: UNTRACKED notes on disk, NO marker -> re-run must NOT create a dup ----
    def test_C_crash_window_untracked_no_dup(self):
        ref = chunk_ref("GUIDC", "C_c000")
        # The crash: chunk C_c000's note exists on disk (freshly Written, UNTRACKED) with no marker.
        orphan = write_note(self.root, "personal/2024-01-01-plan-imC-ccc111.md", ref)
        self.assertTrue(self._untracked("personal/2024-01-01-plan-imC-ccc111.md"),
                        "precondition: crash debris is git-untracked")
        self.assertEqual(len(build_ref_index(self.root, wanted={ref}).get(ref, [])), 1)
        self.assertFalse(os.path.exists(marker_path(self.done, "C_c000")))

        wave, out = self._wave({"chunk_id": "C_c000", "guid": "GUIDC", "chat_id": "C", "fp": "x"})
        res = imsg_worklist.compute_worklist(wave, out, root=self.root, done_dir=self.done)

        self.assertEqual(res["pending"], 1, "chunk is re-emitted for re-distillation")
        self.assertEqual(len(res["reconciled"]), 1, "the UNTRACKED crash-orphan is archived")
        self.assertEqual(res["reconcile_skipped"], [], "nothing tracked to skip")
        self.assertFalse(os.path.exists(orphan), "orphan is moved out of the live store")
        arch = os.path.join(self.root, "_archive", "imsg-crash-orphans",
                            "2024-01-01-plan-imC-ccc111.md")
        self.assertTrue(os.path.exists(arch), "orphan is archived (never deleted)")
        self.assertEqual(build_ref_index(self.root, wanted={ref}).get(ref, []), [])

        # Re-distill writes a note for the SAME chunk with a DIFFERENT filename (the run-to-run slug
        # variance that used to dup). With the orphan gone, the store ends with EXACTLY ONE note.
        write_note(self.root, "personal/2024-01-01-the-plan-imC-ddd222.md", ref)
        live = build_ref_index(self.root, wanted={ref}).get(ref, [])
        self.assertEqual(len(live), 1, "re-distill REPLACES; no second note shares the chunk ref")

    # ---- C (negative control): without reconcile, the same sequence DOES dup -------------------
    def test_C_negative_control_dup_without_reconcile(self):
        ref = chunk_ref("GUIDC", "C_c000")
        write_note(self.root, "personal/2024-01-01-plan-imC-ccc111.md", ref)      # crash-orphan
        write_note(self.root, "personal/2024-01-01-the-plan-imC-ddd222.md", ref)  # re-distill (no reconcile)
        live = build_ref_index(self.root, wanted={ref}).get(ref, [])
        self.assertEqual(len(live), 2,
                         "control: skipping reconcile leaves two notes sharing one exempt ref (the bug)")

    # ---- E. DATA-LOSS GUARD: a COMMITTED note bearing a PENDING chunk's ref is NEVER archived ---
    def test_E_committed_note_not_archived(self):
        # The reviewer's exact scenario: fresh clone / rebuilt _sources -> markers gone (gitignored),
        # but the iMessage note is COMMITTED. compute_worklist reads the chunk as 'pending'. Reconcile
        # must NOT archive the committed original (that would drop curated edits + break wikilinks).
        ref = chunk_ref("GUIDF", "F_c000")
        note = write_note(self.root, "personal/2024-01-01-committed-imF-fff111.md", ref)
        self._commit("personal/2024-01-01-committed-imF-fff111.md", msg="curated committed note")
        self.assertFalse(self._untracked("personal/2024-01-01-committed-imF-fff111.md"))
        self.assertFalse(os.path.exists(marker_path(self.done, "F_c000")), "no marker (gitignored/lost)")

        wave, out = self._wave({"chunk_id": "F_c000", "guid": "GUIDF", "chat_id": "F", "fp": "x"})
        res = imsg_worklist.compute_worklist(wave, out, root=self.root, done_dir=self.done)

        self.assertEqual(res["pending"], 1, "chunk reads as pending (marker absent)")
        self.assertEqual(res["reconciled"], [], "NOTHING archived — the note is tracked")
        self.assertTrue(os.path.exists(note), "the committed note is UNTOUCHED (no data loss)")
        self.assertEqual([(r[0], r[2]) for r in res["reconcile_skipped"]], [(ref, "tracked")],
                         "the tracked match is skipped + surfaced as anomalous")
        self.assertEqual(len(build_ref_index(self.root, wanted={ref}).get(ref, [])), 1)

    # ---- D. safety: reconcile touches ONLY pending-chunk (untracked) orphans, not completed ----
    def test_D_reconcile_spares_completed_chunk(self):
        done_ref = chunk_ref("GUIDD", "D_c000")     # completed chunk (has a marker) -> spared
        pend_ref = chunk_ref("GUIDE", "E_c000")     # pending chunk with an untracked crash-orphan
        done_note = write_note(self.root, "personal/2024-01-01-done-imD-eee111.md", done_ref)
        pend_note = write_note(self.root, "personal/2024-01-01-pend-imE-fff222.md", pend_ref)
        with open(marker_path(self.done, "D_c000"), "w") as f:
            f.write('{"chunk_id":"D_c000","verified":true}\n')

        wave, out = self._wave(
            {"chunk_id": "D_c000", "guid": "GUIDD", "chat_id": "D", "fp": "x"},
            {"chunk_id": "E_c000", "guid": "GUIDE", "chat_id": "E", "fp": "x"},
        )
        res = imsg_worklist.compute_worklist(wave, out, root=self.root, done_dir=self.done)

        self.assertEqual(res["done"], 1)
        self.assertEqual(res["pending"], 1)
        self.assertTrue(os.path.exists(done_note), "completed chunk's note must NOT be reconciled")
        self.assertFalse(os.path.exists(pend_note), "pending chunk's UNTRACKED orphan IS reconciled")
        self.assertEqual([r[0] for r in res["reconciled"]], [pend_ref])

    # ---- G. completeness (finding 3): under-reported chunk -> NO marker -> stays pending --------
    def test_G_undercount_withholds_marker(self):
        # Chunk claims 2 durable items but returns only 1 path (the note exists, so it would pass a
        # naive existence check). It must be WITHHELD so the second item is not lost forever.
        write_note(self.root, "personal/2024-01-01-only-one-imG-ggg111.md",
                   chunk_ref("GUIDG", "G_c000"))
        chunk = {"chunk_id": "G_c000", "ref": chunk_ref("GUIDG", "G_c000"), "durable_found": 2,
                 "notes": ["personal/2024-01-01-only-one-imG-ggg111.md"], "sensitive": []}

        ok, missing = stamp_verified(self.done, self.root, chunk)
        self.assertFalse(ok, "count mismatch must withhold the marker")
        self.assertTrue(missing and "incomplete" in missing[0])
        self.assertFalse(os.path.exists(marker_path(self.done, "G_c000")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

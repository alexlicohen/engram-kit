#!/usr/bin/env python3
"""Teeth test for scripts/stamp_markers.py — the post-Workflow iMessage marker stamper that
RELOCATES the finding-2 guard out of the retired _lib/imsg_markers.mjs (the pure-JS Workflow runtime
can no longer stamp from code). It reads each per-chunk RESULT file
`_sources/imsg-results.d/<chunk_id>.json` and, only if every declared note+sensitive path EXISTS and
len(notes)+len(sensitive)==durable_found, stamps `_sources/imsg-done.d/<chunk_id>`.

Proves:
  * all declared paths exist + count matches    -> marker stamped
  * a missing declared path                     -> NO marker (chunk stays pending)
  * durable_found > returned paths (undercount) -> NO marker (finding 3; stays pending)
  * the CLI wires end-to-end (subprocess).

Verification itself is the single-owner _lib/imsg_markers.py::stamp_verified (already teeth-tested by
test_finding2); this test covers the result-file iteration + wiring. Self-contained temp root.
Run: python3 -m pytest this_file.
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
sys.path.insert(0, _SCRIPTS)
import stamp_markers  # noqa: E402

STAMP = os.path.join(_SCRIPTS, "stamp_markers.py")
PY = sys.executable


def write_note(root, relpath):
    fp = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write("---\ntitle: x\nsource: imessage\n---\nbody\n")
    return fp


def write_result(results_dir, chunk):
    os.makedirs(results_dir, exist_ok=True)
    with open(os.path.join(results_dir, chunk["chunk_id"] + ".json"), "w", encoding="utf-8") as f:
        json.dump(chunk, f)


class TestStampMarkers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="stamp-test-")
        self.results = os.path.join(self.root, "_sources", "imsg-results.d")
        self.done = os.path.join(self.root, "_sources", "imsg-done.d")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _marker(self, cid):
        return os.path.exists(os.path.join(self.done, cid))

    def test_all_exist_count_ok_stamps(self):
        write_note(self.root, "personal/a.md")
        write_result(self.results, {"chunk_id": "A_c000", "ref": "imessage:G:A_c000",
                                    "notes": [os.path.join(self.root, "personal/a.md")],
                                    "sensitive": [], "durable_found": 1})
        stamped, withheld = stamp_markers.stamp_all(self.results, self.done, self.root)
        self.assertEqual(stamped, ["A_c000"])
        self.assertEqual(withheld, [])
        self.assertTrue(self._marker("A_c000"))

    def test_missing_path_no_marker(self):
        write_result(self.results, {"chunk_id": "B_c000", "ref": "imessage:G:B_c000",
                                    "notes": [os.path.join(self.root, "personal/missing.md")],
                                    "sensitive": [], "durable_found": 1})
        stamped, withheld = stamp_markers.stamp_all(self.results, self.done, self.root)
        self.assertEqual(stamped, [])
        self.assertEqual(len(withheld), 1)
        self.assertEqual(withheld[0][0], "B_c000")
        self.assertFalse(self._marker("B_c000"))

    def test_undercount_no_marker(self):
        write_note(self.root, "personal/c.md")
        write_result(self.results, {"chunk_id": "C_c000", "ref": "imessage:G:C_c000",
                                    "notes": [os.path.join(self.root, "personal/c.md")],
                                    "sensitive": [], "durable_found": 2})   # claims 2, returns 1
        stamped, withheld = stamp_markers.stamp_all(self.results, self.done, self.root)
        self.assertEqual(stamped, [])
        self.assertIn("incomplete", withheld[0][1][0])
        self.assertFalse(self._marker("C_c000"))

    def test_cli_end_to_end(self):
        write_note(self.root, "personal/d.md")
        write_result(self.results, {"chunk_id": "D_c000", "ref": "imessage:G:D_c000",
                                    "notes": [os.path.join(self.root, "personal/d.md")],
                                    "sensitive": [], "durable_found": 1})
        r = subprocess.run([PY, STAMP, "--root", self.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stamped 1", r.stdout)
        self.assertTrue(self._marker("D_c000"))


def write_store_note(root, relpath, ref):
    fp = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write('---\ntitle: x\nsource: imessage\nprovenance:\n  ref: "%s"\n---\nbody\n' % ref)
    return fp


class TestHeldSensitiveResolution(unittest.TestCase):
    """A chunk whose RESULT declares a held sensitive draft `_sources/<pfx>-sensitive/<name>.md` must
    not be WITHHELD forever once that draft has been RESOLVED: released into the store (moved to
    `<domain>/<name>.md`, same provenance ref — or retitled but carrying the same `<im|wa><n>-<hash>`
    token AND ref), or excluded (moved to `excluded/`, or its ref in `excluded-refs.jsonl`). It must
    STILL be withheld when the draft is genuinely pending review (`confirmed/`) or lost everywhere —
    and the two withheld kinds are reported distinctly. Both sources (imsg default + whatsapp)."""

    SOURCES = (("imsg", None, "imessage:any;+;chatG:%s", "im57"),
               ("whatsapp", "whatsapp", "whatsapp:123@g.us:%s", "wa174"))

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="stamp-held-test-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _dirs(self, pfx):
        return (os.path.join(self.root, "_sources", "%s-results.d" % pfx),
                os.path.join(self.root, "_sources", "%s-done.d" % pfx),
                os.path.join(self.root, "_sources", "%s-sensitive" % pfx))

    def _chunk(self, pfx, reftpl, tok, cid, name):
        results, done, sens = self._dirs(pfx)
        chunk = {"chunk_id": cid, "ref": reftpl % cid, "notes": [],
                 "sensitive": [os.path.join(sens, "%s-%s-abc123.md" % (name, tok))],
                 "durable_found": 1}
        write_result(results, chunk)
        return chunk, os.path.basename(chunk["sensitive"][0])

    def _stamp(self, pfx):
        results, done, _ = self._dirs(pfx)
        return stamp_markers.stamp_all(results, done, self.root)

    def _marked(self, pfx, cid):
        return os.path.exists(os.path.join(self._dirs(pfx)[1], cid))

    def test_released_same_basename_stamps(self):
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                ch, base = self._chunk(pfx, reftpl, tok, "R_c000", "released")
                write_store_note(self.root, "personal/" + base, ch["ref"])
                stamped, withheld = self._stamp(pfx)
                self.assertIn("R_c000", stamped, withheld)
                self.assertTrue(self._marked(pfx, "R_c000"))

    def test_released_retitled_same_token_and_ref_stamps(self):
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                ch, base = self._chunk(pfx, reftpl, tok, "T_c000", "old-title")
                write_store_note(self.root, "health/new-title-%s-abc123.md" % tok, ch["ref"])
                stamped, withheld = self._stamp(pfx)
                self.assertIn("T_c000", stamped, withheld)

    def test_same_basename_but_different_ref_is_not_a_release(self):
        # a colliding DIFFERENT note (imsg_reclassify writes `<base>-2.md` on a name clash) must not
        # satisfy the draft just because the basename matches
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                ch, base = self._chunk(pfx, reftpl, tok, "X_c000", "clash")
                write_store_note(self.root, "personal/" + base, reftpl % "OTHER_c000")
                stamped, withheld = self._stamp(pfx)
                self.assertNotIn("X_c000", stamped)
                self.assertFalse(self._marked(pfx, "X_c000"))

    def test_excluded_dir_stamps(self):
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                ch, base = self._chunk(pfx, reftpl, tok, "E_c000", "excluded")
                write_note(self.root, os.path.join("_sources", "%s-sensitive" % pfx, "excluded", base))
                stamped, withheld = self._stamp(pfx)
                self.assertIn("E_c000", stamped, withheld)

    def test_excluded_refs_ledger_stamps(self):
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                ch, base = self._chunk(pfx, reftpl, tok, "L_c000", "ledgered")
                sens = self._dirs(pfx)[2]
                os.makedirs(sens, exist_ok=True)
                with open(os.path.join(sens, "excluded-refs.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ref": ch["ref"], "reason": "test"}) + "\n")
                stamped, withheld = self._stamp(pfx)
                self.assertIn("L_c000", stamped, withheld)

    def test_pending_review_still_withheld(self):
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                ch, base = self._chunk(pfx, reftpl, tok, "P_c000", "pending")
                write_note(self.root, os.path.join("_sources", "%s-sensitive" % pfx, "confirmed", base))
                stamped, withheld = self._stamp(pfx)
                self.assertNotIn("P_c000", stamped)
                self.assertFalse(self._marked(pfx, "P_c000"))
                reasons = dict(withheld)["P_c000"]
                self.assertTrue(any(r.startswith("pending-review") for r in reasons), reasons)

    def test_lost_everywhere_withheld_distinctly(self):
        for pfx, _, reftpl, tok in self.SOURCES:
            with self.subTest(pfx):
                self._chunk(pfx, reftpl, tok, "M_c000", "lost")
                stamped, withheld = self._stamp(pfx)
                self.assertNotIn("M_c000", stamped)
                reasons = dict(withheld)["M_c000"]
                self.assertTrue(any(r.startswith("LOST") for r in reasons), reasons)

    def test_cli_whatsapp_resolves_and_reports_lost_distinctly(self):
        ch, base = self._chunk("whatsapp", "whatsapp:123@g.us:%s", "wa174", "C1_c000", "rel")
        write_store_note(self.root, "personal/" + base, ch["ref"])
        self._chunk("whatsapp", "whatsapp:123@g.us:%s", "wa175", "C2_c000", "gone")
        r = subprocess.run([PY, STAMP, "--root", self.root, "--source", "whatsapp"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stamped 1", r.stdout)
        self.assertTrue(self._marked("whatsapp", "C1_c000"))
        self.assertIn("LOST", r.stderr)
        self.assertIn("C2_c000", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

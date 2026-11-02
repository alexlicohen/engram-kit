#!/usr/bin/env python3
"""Tests for the hand-curated `_manifest/clarify-manual.jsonl` sidecar that build_clarify_queue.py
merges into the regenerated queue (`load_manual_items()` + the "5. hand-curated sidecar" step in
`build()`). The generated queue is fully rebuilt every run, which would otherwise wipe a
hand-added question (e.g. a `dossier-conflict` no harvester can produce) on the next `check.sh`;
the sidecar is committed and re-merged every build so such a question survives.

Hermetic: every test builds a throwaway store in a TemporaryDirectory and chdirs into it, so the
real _manifest/clarify-manual.jsonl, _manifest/clarify-queue.jsonl and _manifest/people*.md are
never read or written.

Covers: an `open` sidecar entry is merged into the built queue in the generated-item shape; a
`resolved` entry is skipped; regenerating twice produces byte-identical output with no duplicate
ids; and a malformed line (bad JSON, missing field, bad status, duplicate id) fails loudly rather
than being silently dropped.
Run: python3 -m pytest scripts/tests/test_clarify_manual_sidecar.py -v
"""
import json
import os
import sys
import tempfile
import shutil
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)

_ORIG_CWD = os.getcwd()
import build_clarify_queue as B   # noqa: E402  (chdirs to the repo root at import time)
os.chdir(_ORIG_CWD)


def manual_item(iid="manual:test-item", status="open", **overrides):
    rec = dict(id=iid, category="dossier-conflict", priority=0.9, entity="Some Entity",
               question="What is true here?", candidates=[],
               evidence=dict(dossier="dossiers/example.md", notes=["research/x.md"]),
               status=status, added="2026-09-15")
    rec.update(overrides)
    return rec


class ManualSidecarTestCase(unittest.TestCase):
    """Base: build a minimal (empty-ish) synthetic store, chdir into it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clarify-manual-")
        os.makedirs(os.path.join(self.tmp, "_manifest"))
        self.write("_manifest/people.md", "# engram — people / entity registry\n")
        self.write("_manifest/people-discovered.md", "# discovered\n\n| Name | Meetings | Domain |\n")
        self.write("_manifest/index.jsonl", "")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.chdir, _ORIG_CWD)
        os.chdir(self.tmp)

    def write(self, rel, text):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return rel

    def write_manual(self, records):
        self.write(B.MANUAL, "\n".join(json.dumps(r) for r in records) + "\n")


class TestOpenEntryMerged(ManualSidecarTestCase):
    def test_open_entry_appears_in_built_queue(self):
        self.write_manual([manual_item()])
        items = B.build()
        by_id = {it["id"]: it for it in items}
        self.assertIn("manual:test-item", by_id)
        it = by_id["manual:test-item"]
        self.assertEqual(it["category"], "dossier-conflict")
        self.assertEqual(it["status"], "open")
        # generated-item shape: writeback/blocked_on are defaulted when the sidecar omits them
        self.assertIn("writeback", it)
        self.assertIn("blocked_on", it)
        self.assertEqual(it["evidence"]["dossier"], "dossiers/example.md")

    def test_no_sidecar_file_is_a_noop(self):
        # no clarify-manual.jsonl written at all
        items = B.build()
        self.assertFalse(any(it["category"] == "dossier-conflict" for it in items))

    def test_load_manual_items_directly(self):
        self.write_manual([manual_item("manual:a"), manual_item("manual:b")])
        ids = {it["id"] for it in B.load_manual_items()}
        self.assertEqual(ids, {"manual:a", "manual:b"})


class TestResolvedSkipped(ManualSidecarTestCase):
    def test_resolved_entry_is_not_merged(self):
        self.write_manual([manual_item("manual:open-one", status="open"),
                            manual_item("manual:resolved-one", status="resolved",
                                        resolved="2026-09-16", answer="it was X")])
        items = B.build()
        ids = {it["id"] for it in items}
        self.assertIn("manual:open-one", ids)
        self.assertNotIn("manual:resolved-one", ids)


class TestRegenIdempotent(ManualSidecarTestCase):
    def test_regen_twice_yields_identical_queue_and_no_duplicate_ids(self):
        self.write_manual([manual_item("manual:one"), manual_item("manual:two")])
        B.build()
        with open(B.QUEUE, encoding="utf-8") as f:
            first = f.read()
        B.build()
        with open(B.QUEUE, encoding="utf-8") as f:
            second = f.read()
        self.assertEqual(first, second)
        ids = [json.loads(l)["id"] for l in second.splitlines() if l.strip()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("manual:one", ids)
        self.assertIn("manual:two", ids)


class TestMalformedLineFailsLoudly(ManualSidecarTestCase):
    def test_bad_json_raises(self):
        self.write(B.MANUAL, '{"id": "manual:broken", not json\n')
        with self.assertRaises(ValueError):
            B.load_manual_items()

    def test_missing_required_field_raises(self):
        rec = manual_item()
        del rec["question"]
        self.write_manual([rec])
        with self.assertRaises(ValueError):
            B.load_manual_items()

    def test_bad_status_raises(self):
        self.write_manual([manual_item(status="maybe")])
        with self.assertRaises(ValueError):
            B.load_manual_items()

    def test_duplicate_id_within_sidecar_raises(self):
        self.write_manual([manual_item("manual:dup"), manual_item("manual:dup")])
        with self.assertRaises(ValueError):
            B.load_manual_items()

    def test_non_object_line_raises(self):
        self.write(B.MANUAL, "[1, 2, 3]\n")
        with self.assertRaises(ValueError):
            B.load_manual_items()

    def test_dossier_as_list_raises(self):
        # evidence.dossier must be a single string path (documented shape in SKILL.md) -- a list
        # (e.g. from citing two dossiers) must fail loudly, not silently pass a list downstream
        # to writeback.file.
        rec = manual_item()
        rec["evidence"] = dict(dossier=["dossiers/a.md", "dossiers/b.md"], notes=["research/x.md"])
        self.write_manual([rec])
        with self.assertRaises(ValueError):
            B.load_manual_items()

    def test_notes_not_a_list_raises(self):
        rec = manual_item()
        rec["evidence"] = dict(dossier="dossiers/a.md", notes="research/x.md")
        self.write_manual([rec])
        with self.assertRaises(ValueError):
            B.load_manual_items()


if __name__ == "__main__":
    unittest.main()

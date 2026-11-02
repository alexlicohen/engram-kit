#!/usr/bin/env python3
"""Teeth test for scripts/outlook_select.py's dedupe_by_msgid() — the fix for a real,
verified-live 2026-07-10 data-corruption bug (code review at `range=aab8a33b..HEAD`): the
same email landing in `_sources/outlook/` twice (once manually-exported, once via the newer
`outlook_local_export.py` local-cache path) produced TWO records in one thread's member list,
so the rendered `.txt` the distiller reads repeated that message's body — at the time of the
finding, 99% of live KEEP threads carried at least one duplicated message.

Proves:
  * dedupe_by_msgid collapses records sharing an identical msgid to one, keeping the first
  * records with no msgid at all are never dropped (can't safely dedupe without an id)
  * build_threads applies the dedupe: two files with the same Message-ID (the manual-export +
    local-cache-export scenario) still correctly union into ONE thread (via the shared
    Thread-Index GUID, unaffected) but contribute only ONE member, not two
  * a genuinely different message sharing a thread (different msgid) is preserved, not dropped

Hermetic: synthetic record dicts shaped exactly like scripts/outlook_extract.py's index rows
(msgid/thridx_guid/refs/ts/file only — the fields build_threads/dedupe_by_msgid touch).
Run: python3 -m pytest this_file.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
import outlook_select as osel  # noqa: E402


def rec(file, msgid=None, thridx_guid=None, refs=None, ts="2026-03-01T00:00:00Z"):
    return {"file": file, "msgid": msgid, "thridx_guid": thridx_guid, "refs": refs or [], "ts": ts}


class TestDedupeByMsgid(unittest.TestCase):
    def test_collapses_identical_msgid_keeping_first(self):
        members = [
            rec("manual_export.eml", msgid="m1@example.com"),
            rec("local_abc123_999.eml", msgid="m1@example.com"),
        ]
        out = osel.dedupe_by_msgid(members)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["file"], "manual_export.eml")

    def test_keeps_distinct_msgids(self):
        members = [rec("a.eml", msgid="m1@x"), rec("b.eml", msgid="m2@x")]
        out = osel.dedupe_by_msgid(members)
        self.assertEqual(len(out), 2)

    def test_never_drops_records_with_no_msgid(self):
        # malformed/headerless messages have no Message-ID at all — can't safely tell if two
        # such records are duplicates, so all of them survive rather than risk losing content
        members = [rec("a.eml", msgid=None), rec("b.eml", msgid=None)]
        out = osel.dedupe_by_msgid(members)
        self.assertEqual(len(out), 2)

    def test_mixed_msgid_and_no_msgid(self):
        members = [
            rec("a.eml", msgid="m1@x"),
            rec("a_dup.eml", msgid="m1@x"),
            rec("b.eml", msgid=None),
        ]
        out = osel.dedupe_by_msgid(members)
        self.assertEqual(len(out), 2)
        self.assertEqual({m["file"] for m in out}, {"a.eml", "b.eml"})


class TestBuildThreadsAppliesDedupe(unittest.TestCase):
    def test_manual_and_local_cache_duplicate_collapse_to_one_member(self):
        # the exact live-verified scenario: identical email present as two files sharing a
        # Message-ID AND the same Exchange conversation GUID (it's literally the same message)
        recs = [
            rec("manual_export.eml", msgid="dup@x", thridx_guid="GUID-A", ts="2026-03-01T00:00:00Z"),
            rec("local_abc_999.eml", msgid="dup@x", thridx_guid="GUID-A", ts="2026-03-01T00:00:00Z"),
        ]
        threads = osel.build_threads(recs)
        self.assertEqual(len(threads), 1)
        members = list(threads.values())[0]
        self.assertEqual(len(members), 1, "duplicate copy of the same message must not double-count")

    def test_real_reply_in_same_thread_is_preserved(self):
        recs = [
            rec("msg1.eml", msgid="root@x", thridx_guid="GUID-B", ts="2026-03-01T00:00:00Z"),
            rec("msg2.eml", msgid="reply@x", thridx_guid="GUID-B", ts="2026-03-02T00:00:00Z"),
        ]
        threads = osel.build_threads(recs)
        self.assertEqual(len(threads), 1)
        members = list(threads.values())[0]
        self.assertEqual(len(members), 2, "a real second message in the thread must survive dedupe")


if __name__ == "__main__":
    unittest.main()

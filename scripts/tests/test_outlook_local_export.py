#!/usr/bin/env python3
"""Teeth test for scripts/outlook_local_export.py — the local-cache Outlook export path (works
where the Graph device-code path is blocked by an employer's Conditional Access policy; see
`_manifest/SYNC.md` "Outlook" step 0 history).

Covers the pure logic against a synthetic fake profile tree (never touches the real
~/Library/Group Containers Outlook profile):
  * find_account_mime_dirs samples up to SAMPLE_SIZE messages' From/To/Cc/Delivered-To/
    X-Original-To headers and matches only the MimeFiles dir(s) addressing the target account
  * export() copies verbatim bytes, skips a basename already in state (idempotent re-run)
  * export() de-dupes a basename appearing under >1 folder-UUID subdir WITHIN one matched
    account dir, in BOTH dry-run and real mode (a real bug caught live 2026-07-10: dry-run
    initially over-counted duplicates because it skipped the in-memory de-dupe that only the
    real-copy branch did)
  * export() does NOT de-dupe a colliding basename ACROSS two different matched account dirs —
    a second code-review-caught bug: the original global de-dupe set silently and permanently
    dropped a distinct message whenever two matched accounts happened to share a numeric id
  * a corrupt/truncated state file degrades to a full rescan instead of crashing Step 0 outright
  * a message caught mid-write (unparseable / no load-bearing headers) is skipped and NOT marked
    exported, so it's retried on the next run instead of being silently truncated forever
  * a message with no MimeFiles dir at all matching the account raises SystemExit, not a silent
    no-op

Hermetic: builds a temp fake PROFILE_ROOT with real RFC822 bytes (minimal but valid) as .mime
fixtures. Run: python3 -m pytest this_file.
"""
import email.message
import glob
import os
import shutil
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
import outlook_local_export as ole  # noqa: E402

TARGET = "avery.quillfeather@work.example"
OTHER = "averyq@example.com"


def _mime_bytes(to_addr, subject="hi"):
    m = email.message.EmailMessage()
    m["From"] = "someone@example.com"
    m["To"] = to_addr
    m["Subject"] = subject
    m["Date"] = "Thu, 19 Mar 2026 10:29:14 -0400"
    m.set_content("body text")
    return m.as_bytes()


class TestOutlookLocalExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_root = os.path.join(self.tmp, "Outlook 15 Profiles")
        self.dest = os.path.join(self.tmp, "dest")
        self.state_path = os.path.join(self.tmp, "state.json")
        self._orig_state = ole.STATE_PATH
        ole.STATE_PATH = self.state_path

        # account "2" = target (work), 2 messages in 2 different folder-UUID subdirs, one
        # basename DUPLICATED across both (simulating a message present under >1 folder)
        target_dir_a = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                                     "MimeFiles", "UUIDA")
        target_dir_b = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                                     "MimeFiles", "UUIDB")
        os.makedirs(target_dir_a)
        os.makedirs(target_dir_b)
        with open(os.path.join(target_dir_a, "100.mime"), "wb") as f:
            f.write(_mime_bytes(TARGET, "msg 100"))
        with open(os.path.join(target_dir_a, "101.mime"), "wb") as f:
            f.write(_mime_bytes(TARGET, "msg 101"))
        with open(os.path.join(target_dir_b, "100.mime"), "wb") as f:  # duplicate basename
            f.write(_mime_bytes(TARGET, "msg 100 (also in folder B)"))

        # account "3" = other (personal gmail), should never be selected/touched
        other_dir = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "3",
                                  "MimeFiles", "UUIDC")
        os.makedirs(other_dir)
        with open(os.path.join(other_dir, "200.mime"), "wb") as f:
            f.write(_mime_bytes(OTHER, "unrelated"))

    def tearDown(self):
        ole.STATE_PATH = self._orig_state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_account_mime_dirs_matches_only_target(self):
        dirs = ole.find_account_mime_dirs(TARGET, profile_root=self.profile_root)
        self.assertEqual(len(dirs), 1)
        self.assertIn(os.path.join("S0", "2", "MimeFiles"), dirs[0])

    def test_find_account_mime_dirs_no_match_returns_empty(self):
        dirs = ole.find_account_mime_dirs("nobody@nowhere.example", profile_root=self.profile_root)
        self.assertEqual(dirs, [])

    def test_export_copies_verbatim_bytes_and_dedupes_duplicate_basename(self):
        count = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        # 2 unique basenames (100, 101) despite 100 appearing in two folder-UUID dirs
        self.assertEqual(count, 2)
        copied = os.listdir(self.dest)
        self.assertEqual(len(copied), 2)

    def test_export_is_idempotent_on_rerun(self):
        ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        second = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        self.assertEqual(second, 0)

    def test_dry_run_matches_real_run_count(self):
        # the real bug: dry-run over-counted the duplicate basename because it skipped the
        # in-memory de-dupe that only the copy branch used to perform
        dry_count = ole.export(TARGET, self.dest, dry_run=True, profile_root=self.profile_root, settle_seconds=0)
        self.assertEqual(dry_count, 2)
        self.assertEqual(os.listdir(self.dest), [])  # dry-run writes nothing

    def test_no_matching_account_exits(self):
        with self.assertRaises(SystemExit):
            ole.export("nobody@nowhere.example", self.dest, dry_run=True,
                       profile_root=self.profile_root, settle_seconds=0)

    def test_state_file_written_atomically_no_tmp_leftover(self):
        ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        leftovers = glob.glob(self.state_path + "*.tmp")
        self.assertEqual(leftovers, [])
        self.assertTrue(os.path.exists(self.state_path))

    def test_corrupt_state_file_recovers_to_full_rescan(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            f.write('{"exported_by_dir": {"trunc')  # truncated JSON, simulates a killed write
        count = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        self.assertEqual(count, 2)  # degrades to empty state -> full rescan, not a crash


class TestAccountSniffingRobustness(unittest.TestCase):
    """A single unlucky first sample (e.g. Bcc'd, so From/To/Cc don't literally contain the
    target) must not misdiagnose a real, matching account as unrelated."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_root = os.path.join(self.tmp, "Outlook 15 Profiles")
        mime_dir = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                                 "MimeFiles", "UUIDA")
        os.makedirs(mime_dir)
        # first-glob-order file (alphabetically/numerically first) does NOT mention TARGET at all
        with open(os.path.join(mime_dir, "001.mime"), "wb") as f:
            f.write(_mime_bytes("bccd-recipient@example.com", "no visible target address"))
        # a later sample in the same account genuinely addresses TARGET
        with open(os.path.join(mime_dir, "002.mime"), "wb") as f:
            f.write(_mime_bytes(TARGET, "this one has the target"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matches_when_any_sampled_message_addresses_target(self):
        dirs = ole.find_account_mime_dirs(TARGET, profile_root=self.profile_root)
        self.assertEqual(len(dirs), 1, "a real match must not be missed just because the FIRST "
                                        "sampled message didn't happen to mention the address")


class TestCrossAccountBasenameCollision(unittest.TestCase):
    """A second, independent account dir that ALSO addresses TARGET (e.g. a delegate mailbox),
    sharing a numeric basename with the first account by coincidence — must NOT be treated as an
    intra-account duplicate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_root = os.path.join(self.tmp, "Outlook 15 Profiles")
        self.dest = os.path.join(self.tmp, "dest")
        self.state_path = os.path.join(self.tmp, "state.json")
        self._orig_state = ole.STATE_PATH
        ole.STATE_PATH = self.state_path

        dir_a = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                              "MimeFiles", "UUIDA")
        dir_b = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "9",
                              "MimeFiles", "UUIDZ")
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        with open(os.path.join(dir_a, "999.mime"), "wb") as f:
            f.write(_mime_bytes(TARGET, "account 2's message 999"))
        with open(os.path.join(dir_b, "999.mime"), "wb") as f:  # same basename, DIFFERENT account
            f.write(_mime_bytes(TARGET, "account 9's UNRELATED message 999"))

    def tearDown(self):
        ole.STATE_PATH = self._orig_state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_both_accounts_matched_and_neither_copy_dropped(self):
        dirs = ole.find_account_mime_dirs(TARGET, profile_root=self.profile_root)
        self.assertEqual(len(dirs), 2)
        count = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        self.assertEqual(count, 2, "a same-basename message from a DIFFERENT account must not "
                                    "be silently dropped as a false duplicate")
        self.assertEqual(len(os.listdir(self.dest)), 2)


class TestMidWriteTruncation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_root = os.path.join(self.tmp, "Outlook 15 Profiles")
        self.dest = os.path.join(self.tmp, "dest")
        self.state_path = os.path.join(self.tmp, "state.json")
        self._orig_state = ole.STATE_PATH
        ole.STATE_PATH = self.state_path

        mime_dir = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                                 "MimeFiles", "UUIDA")
        os.makedirs(mime_dir)
        with open(os.path.join(mime_dir, "100.mime"), "wb") as f:
            f.write(_mime_bytes(TARGET, "complete message"))
        with open(os.path.join(mime_dir, "101.mime"), "wb") as f:
            f.write(b"not a real email, just partial bytes Outlook hadn't finished writing")

    def tearDown(self):
        ole.STATE_PATH = self._orig_state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_truncated_message_skipped_not_marked_exported(self):
        count = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        self.assertEqual(count, 1, "only the complete message should be counted as exported")
        self.assertEqual(len(os.listdir(self.dest)), 1)
        # simulate Outlook finishing the write between runs
        mime_dir = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                                 "MimeFiles", "UUIDA")
        with open(os.path.join(mime_dir, "101.mime"), "wb") as f:
            f.write(_mime_bytes(TARGET, "now complete"))
        second = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root, settle_seconds=0)
        self.assertEqual(second, 1, "the now-complete message must be picked up, not skipped "
                                     "forever because an earlier truncated read was seen")


class TestSettleTimeGate(unittest.TestCase):
    """A real bug (2026-07-10 code review): _looks_like_complete_message() only checks header
    PRESENCE, not body completeness. Outlook writes headers-first, body-streamed-after, so a file
    caught with all headers flushed but the body still mid-write passed every header check, got
    copied truncated, and was marked exported forever (never retried). The fix is a settle-time
    age gate in export() itself, checked BEFORE the file is even read -- these tests prove the gate
    actually gates, and that a file which has aged past it is exported normally."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_root = os.path.join(self.tmp, "Outlook 15 Profiles")
        self.dest = os.path.join(self.tmp, "dest")
        self.state_path = os.path.join(self.tmp, "state.json")
        self._orig_state = ole.STATE_PATH
        ole.STATE_PATH = self.state_path
        self.mime_dir = os.path.join(self.profile_root, "Main Profile", "Files", "S0", "2",
                                      "MimeFiles", "UUIDA")
        os.makedirs(self.mime_dir)
        self.mime_path = os.path.join(self.mime_dir, "200.mime")
        with open(self.mime_path, "wb") as f:
            f.write(_mime_bytes(TARGET, "header-complete, freshly written"))

    def tearDown(self):
        ole.STATE_PATH = self._orig_state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_file_skipped_by_default_gate(self):
        # freshly written (mtime ~= now) -- default gate (settle_seconds not overridden to 0)
        # must skip it even though it parses as header-complete
        count = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root)
        self.assertEqual(count, 0, "a freshly-written file must not be exported yet")
        self.assertEqual(os.listdir(self.dest) if os.path.isdir(self.dest) else [], [])

    def test_fresh_file_not_marked_seen_picked_up_once_settled(self):
        # first pass with the real gate: skipped, and NOT marked seen
        ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root)
        # simulate time passing without rewriting the file (settle_seconds=0 bypasses the gate,
        # standing in for "the file is now old enough")
        second = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root,
                             settle_seconds=0)
        self.assertEqual(second, 1, "a file skipped for freshness must still be picked up later, "
                                     "not permanently marked seen by the skip itself")

    def test_aged_file_exported_immediately(self):
        old = time.time() - (ole.SETTLE_SECONDS + 5)
        os.utime(self.mime_path, (old, old))
        count = ole.export(TARGET, self.dest, dry_run=False, profile_root=self.profile_root)
        self.assertEqual(count, 1, "a file older than SETTLE_SECONDS should export on the first pass")


if __name__ == "__main__":
    unittest.main()

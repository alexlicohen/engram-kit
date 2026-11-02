#!/usr/bin/env python3
"""Teeth test for the iMessage TIMESTAMP watermark (single-owned in _lib/imsg_time.py, consumed by
imsg_select / imsg_extract / bump_watermark).

Proves:
  * --since accepts a bare DATE and an ISO-8601 TIMESTAMP (a date == midnight UTC; back-compat);
  * the watermark is CEIL-to-second, so it is >= the exact max instant and the strict-`>` query
    re-fetches nothing already seen;
  * TWO same-day --since values fetch DISJOINT message sets (the core requirement) — demonstrated
    against a synthetic sqlite whose WHERE mirrors imsg_extract's `m.date > ?`;
  * bump_watermark.py --source imessage reads the max-processed-ts sidecar and (dry-run) sets the
    watermark to that timestamp, refuses backwards, and fails closed when the sidecar is missing.

Self-contained (temp dirs + in-memory sqlite); touches nothing real. Run: python3 -m pytest this_file.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_REPO = os.path.dirname(_SCRIPTS)
sys.path.insert(0, _SCRIPTS)
from _lib.imsg_time import since_to_apple_ns, apple_ns_to_iso_second, NS  # noqa: E402

BUMP = os.path.join(_SCRIPTS, "bump_watermark.py")
PY = sys.executable


class TestSinceParser(unittest.TestCase):
    def test_date_and_timestamp_accepted(self):
        self.assertEqual(since_to_apple_ns("2026-06-30"),
                         since_to_apple_ns("2026-06-30T00:00:00Z"))     # date == midnight UTC
        self.assertEqual(since_to_apple_ns(""), 0)
        self.assertEqual(since_to_apple_ns(None), 0)
        self.assertGreater(since_to_apple_ns("2026-06-30T14:23:01Z"),
                           since_to_apple_ns("2026-06-30"))             # ts later than its midnight

    def test_ceil_ge_exact_and_excludes_boundary(self):
        ns = since_to_apple_ns("2026-06-30T14:23:01Z") + 500_000_000    # +0.5s (sub-second)
        self.assertEqual(apple_ns_to_iso_second(ns, ceil=False), "2026-06-30T14:23:01Z")
        ceil_iso = apple_ns_to_iso_second(ns, ceil=True)
        self.assertEqual(ceil_iso, "2026-06-30T14:23:02Z")
        # ceil >= exact, so a strict-`>` --since re-fetches nothing already seen (the boundary msg)
        self.assertGreaterEqual(since_to_apple_ns(ceil_iso), ns)
        self.assertFalse(ns > since_to_apple_ns(ceil_iso))


class TestDisjointSameDaySync(unittest.TestCase):
    """Two same-day --since values fetch disjoint message sets (mirrors imsg_extract's WHERE)."""

    def _db(self):
        con = sqlite3.connect(":memory:")
        con.executescript(
            "CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INTEGER);"
            "CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);")
        return con

    def _fetch(self, con, chat_id, since_ns):
        # EXACTLY imsg_extract's query WHERE (strict `>`), ordered ascending
        return [r[0] for r in con.execute(
            "SELECT m.date FROM message m JOIN chat_message_join j ON m.ROWID=j.message_id "
            "WHERE j.chat_id=? AND m.date>? ORDER BY m.date", (chat_id, since_ns)).fetchall()]

    def test_two_same_day_since_are_disjoint(self):
        con = self._db()
        day = "2026-06-30"
        ts = [f"{day}T09:00:00Z", f"{day}T12:30:15Z", f"{day}T18:45:59Z", f"{day}T23:59:59Z"]
        nss = [since_to_apple_ns(t) + off for t, off in zip(ts, (111, 222, 333, 444))]  # +sub-second
        for i, n in enumerate(nss, 1):
            con.execute("INSERT INTO message(ROWID, date) VALUES(?,?)", (i, n))
            con.execute("INSERT INTO chat_message_join VALUES(1, ?)", (i,))
        con.commit()

        # run 1: --since a prior date -> every same-day message; watermark = ceil(max processed)
        run1 = self._fetch(con, 1, since_to_apple_ns("2026-06-29"))
        self.assertEqual(run1, nss, "run 1 sees every same-day message")
        watermark = apple_ns_to_iso_second(max(nss), ceil=True)

        # a NEW same-day message arrives AFTER run 1 (its date > the previous max)
        n5 = max(nss) + 10 * NS
        con.execute("INSERT INTO message(ROWID, date) VALUES(5, ?)", (n5,))
        con.execute("INSERT INTO chat_message_join VALUES(1, 5)")
        con.commit()

        # run 2: --since <run1 watermark> -> ONLY the strictly-newer message
        run2 = self._fetch(con, 1, since_to_apple_ns(watermark))
        self.assertEqual(run2, [n5], "run 2 fetches only strictly-newer messages")
        self.assertEqual(set(run1) & set(run2), set(), "the two same-day fetches are DISJOINT")
        self.assertEqual(set(run1) | set(run2), set(nss) | {n5}, "union misses nothing")


class TestBumpWatermarkImessage(unittest.TestCase):
    def _root(self, tmp, maxts=None, watermark="2026-06-30"):
        os.makedirs(os.path.join(tmp, "_sources"))
        os.makedirs(os.path.join(tmp, "_manifest"))
        if maxts is not None:
            with open(os.path.join(tmp, "_sources", "imessage.maxts"), "w") as f:
                f.write(maxts + "\n")
        sp = os.path.join(tmp, "_manifest", "sync-state.json")
        with open(sp, "w") as f:
            json.dump({"imessage": {"watermark_date": watermark}}, f)
        return sp

    def _bump(self, tmp, *extra):
        return subprocess.run([PY, BUMP, "--source", "imessage", "--root", tmp, *extra],
                              capture_output=True, text=True)

    def test_dry_run_sets_timestamp_from_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = self._root(tmp, maxts="2026-06-30T14:23:02Z", watermark="2026-06-30")
            r = self._bump(tmp, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("2026-06-30T14:23:02Z", r.stdout)          # -> the exact timestamp, not a day
            self.assertIn("--dry-run", r.stdout)
            self.assertEqual(json.load(open(sp))["imessage"]["watermark_date"], "2026-06-30",
                             "dry-run must not write")

    def test_write_sets_timestamp_and_keeps_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = self._root(tmp, maxts="2026-06-30T14:23:02Z", watermark="2026-06-30")
            r = self._bump(tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            entry = json.load(open(sp))["imessage"]
            self.assertEqual(entry["watermark_date"], "2026-06-30T14:23:02Z")
            self.assertEqual(entry["previous_watermark_date"], "2026-06-30")

    def test_refuses_backwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._root(tmp, maxts="2026-06-29T00:00:00Z", watermark="2026-06-30T12:00:00Z")
            r = self._bump(tmp)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("backwards", r.stdout)

    def test_missing_sidecar_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._root(tmp, maxts=None, watermark="2026-06-30")
            r = self._bump(tmp)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("no max-processed-timestamp sidecar", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

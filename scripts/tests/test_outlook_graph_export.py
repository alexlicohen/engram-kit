#!/usr/bin/env python3
"""Teeth test for scripts/outlook_graph_export.py + outlook_graph_auth.py — the Graph-based
auto-export that replaces the manual "Save as .eml" step (`_manifest/SYNC.md` "Outlook" section).

Covers only the pure logic (no network, no real Microsoft endpoints, no real token cache):
  * _load_since's fallback chain: --since override > local export-state file > sync-state.json
    watermark_date (as an ISO8601 midnight) > SystemExit if none exist
  * _safe_name sanitizes a Graph message id into a filesystem-safe filename fragment
  * outlook_graph_auth's token cache round-trips (save -> load) and is written 0600, and
    get_access_token() returns a cached token without hitting the network when it's still fresh

Hermetic: monkeypatches module-level path constants to temp files; never touches
~/.engram_outlook_token_cache.json or _sources/. Run: python3 -m pytest this_file.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
import outlook_graph_export as oge  # noqa: E402
import outlook_graph_auth as oga  # noqa: E402


class TestSafeName(unittest.TestCase):
    def test_sanitizes_graph_id_chars(self):
        # real Graph message ids contain base64url-ish chars including - _ and sometimes ==
        self.assertEqual(oge._safe_name("AAMkAG=I2/TG+uq"), "AAMkAG_I2_TG_uq")

    def test_preserves_safe_chars(self):
        self.assertEqual(oge._safe_name("abc-123_XYZ.eml"), "abc-123_XYZ.eml")


class TestODataFilterEncoding(unittest.TestCase):
    """A real bug caught live 2026-07-10: the $filter query was built with an un-encoded literal
    space (`receivedDateTime ge {since}`), which urllib.request.Request rejects outright with
    InvalidURL before any request reaches Microsoft. export() now builds the querystring via
    urllib.parse.urlencode, which percent-encodes it. Reproduce the encoding directly (no network)
    rather than exercising export() end-to-end, since this file otherwise avoids mocking."""

    def test_filter_value_has_no_raw_space_once_encoded(self):
        import urllib.parse
        since = "2026-07-06T00:00:00Z"
        query = urllib.parse.urlencode({
            "$filter": f"receivedDateTime ge {since}",
            "$select": "id,receivedDateTime,subject",
            "$orderby": "receivedDateTime asc",
            "$top": "50",
        })
        self.assertNotIn(" ", query)  # urlencode uses '+' or %20, never a raw space
        # and the resulting URL must be constructible without urllib raising InvalidURL —
        # this is the exact call that crashed pre-fix (control characters incl. raw spaces
        # are rejected by urllib.request.Request itself, before any network call is made)
        import urllib.request
        req = urllib.request.Request(f"https://graph.microsoft.com/v1.0/me/messages?{query}")
        self.assertIn("filter=receivedDateTime", req.full_url)


class TestLoadSince(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmp, "outlook-graph-export-state.json")
        self.sync_state_path = os.path.join(self.tmp, "sync-state.json")
        self._orig_state = oge.STATE_PATH
        self._orig_sync = oge.SYNC_STATE_PATH
        oge.STATE_PATH = self.state_path
        oge.SYNC_STATE_PATH = self.sync_state_path

    def tearDown(self):
        oge.STATE_PATH = self._orig_state
        oge.SYNC_STATE_PATH = self._orig_sync
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_override_wins(self):
        with open(self.sync_state_path, "w") as f:
            json.dump({"outlook": {"watermark_date": "2026-01-01"}}, f)
        self.assertEqual(oge._load_since("2026-06-01T00:00:00Z"), "2026-06-01T00:00:00Z")

    def test_export_state_wins_over_watermark(self):
        with open(self.state_path, "w") as f:
            json.dump({"last_exported": "2026-07-08T12:00:00Z"}, f)
        with open(self.sync_state_path, "w") as f:
            json.dump({"outlook": {"watermark_date": "2026-01-01"}}, f)
        self.assertEqual(oge._load_since(None), "2026-07-08T12:00:00Z")

    def test_falls_back_to_sync_state_watermark_minus_2day_margin(self):
        with open(self.sync_state_path, "w") as f:
            json.dump({"outlook": {"watermark_date": "2026-07-06"}}, f)
        self.assertEqual(oge._load_since(None), "2026-07-04T00:00:00Z")

    def test_no_state_no_watermark_raises(self):
        with self.assertRaises(SystemExit):
            oge._load_since(None)

    def test_save_since_roundtrips(self):
        oge._save_since("2026-07-09T08:00:00Z")
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)["last_exported"], "2026-07-09T08:00:00Z")


class TestTokenCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmp, "token_cache.json")
        self._orig = oga.CACHE_PATH
        oga.CACHE_PATH = self.cache_path

    def tearDown(self):
        oga.CACHE_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_roundtrip_and_permissions(self):
        oga._save_cache({"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
                          "scope": "offline_access Mail.Read"})
        cache = oga._load_cache()
        self.assertEqual(cache["access_token"], "AT1")
        self.assertEqual(cache["refresh_token"], "RT1")
        mode = stat.S_IMODE(os.stat(self.cache_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_get_access_token_uses_fresh_cache_without_network(self):
        oga._save_cache({"access_token": "STILL_FRESH", "refresh_token": "RT1",
                          "expires_in": 3600, "scope": "offline_access Mail.Read"})
        # No monkeypatched network call exists — if this reaches urlopen it'll raise/hang, not
        # silently succeed, so a passing assertion here proves the fresh-cache short-circuit fired.
        self.assertEqual(oga.get_access_token(interactive_fallback=False), "STILL_FRESH")

    def test_get_access_token_raises_when_expired_and_no_refresh_noninteractive(self):
        oga._save_cache({"access_token": "EXPIRED", "expires_in": -10,
                          "scope": "offline_access Mail.Read"})
        with self.assertRaises(RuntimeError):
            oga.get_access_token(interactive_fallback=False)

    def test_status_reports_no_cache_cleanly(self):
        self.assertIsNone(oga._load_cache())


if __name__ == "__main__":
    unittest.main()

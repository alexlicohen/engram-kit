#!/usr/bin/env python3
"""Hermetic tests for the Apple Reminders bridge wrapper (scripts/_lib/reminders.py).

osascript is NEVER invoked here — every test injects a fake `runner(op, request_json, timeout)`
so the suite stays deterministic and CI-safe (no Reminders app, no TCC, no macOS dependency).
The JXA itself (scripts/reminders_bridge.js) is exercised live by doctor(live=True); this file
covers the Python contract: request serialization, response parsing per op, the BridgeError
taxonomy (tcc / timeout / garbage / fail), and doctor(live=False) report shape.

Run: python3 -m pytest scripts/tests/test_reminders_bridge.py -q
"""
import json
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)  # scripts/ on path for _lib.*

from _lib import reminders  # noqa: E402
from _lib.reminders import bridge, doctor, BridgeError  # noqa: E402


def ok_env(result):
    """A well-formed success envelope as the JS bridge would print it."""
    return (0, json.dumps({"ok": True, "result": result}), "")


def err_env(code, msg="boom"):
    """A well-formed {"ok":false} app-level error envelope."""
    return (0, json.dumps({"ok": False, "error": msg, "code": code}), "")


class Recorder:
    """A fake runner that records calls and returns a canned response (or raises)."""

    def __init__(self, response=None, raises=None, by_op=None):
        self.response = response
        self.raises = raises
        self.by_op = by_op or {}
        self.calls = []

    def __call__(self, op, request_json, timeout):
        self.calls.append({"op": op, "request": request_json, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        if op in self.by_op:
            return self.by_op[op]
        return self.response


class TestBridgeHappyPath(unittest.TestCase):
    def test_returns_result_and_serializes_request(self):
        rec = Recorder(response=ok_env({"lists": {"Engram Test": "id-1"}}))
        result = bridge("ensure_lists", {"names": ["Engram Test"]}, runner=rec)
        self.assertEqual(result, {"lists": {"Engram Test": "id-1"}})
        # exactly one call; op + serialized payload + default timeout forwarded verbatim
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["op"], "ensure_lists")
        self.assertEqual(json.loads(rec.calls[0]["request"]), {"names": ["Engram Test"]})
        self.assertEqual(rec.calls[0]["timeout"], reminders.DEFAULT_TIMEOUT)

    def test_custom_timeout_forwarded(self):
        rec = Recorder(response=ok_env({"items": []}))
        bridge("dump_incomplete", {}, runner=rec, timeout=200)
        self.assertEqual(rec.calls[0]["timeout"], 200)
        self.assertEqual(json.loads(rec.calls[0]["request"]), {})

    def test_none_payload_serializes_to_empty_object(self):
        rec = Recorder(response=ok_env({"items": []}))
        bridge("dump_incomplete", None, runner=rec)
        self.assertEqual(json.loads(rec.calls[0]["request"]), {})

    def test_response_shape_per_op(self):
        # Each op's canonical result flows back through bridge() unchanged (response parsing).
        cases = {
            "ensure_lists": {"lists": {"Work": "id-a"}},
            "create": {"created": [{"task_id": "t1", "reminder_id": "x-apple-reminder://A", "ok": True}],
                       "errors": [{"task_id": "t2", "error": "list-not-found:Nope"}]},
            "read": {"found": [{"id": "x-apple-reminder://A", "completed": False, "list": "Work",
                                "title": "do thing", "due": "2026-07-15"}], "missing": ["x-apple-reminder://Z"]},
            "dump_incomplete": {"items": [{"id": "x-apple-reminder://A", "title": "t", "list": "Work",
                                           "due": None, "notes_head": "engram-task: t1", "flagged": True,
                                           "priority": 1}]},
            "find_by_token": {"matches": [{"task_id": "t1", "reminder_id": "x-apple-reminder://A",
                                           "completed": False, "list": "Work"}]},
            "complete": {"done": ["x-apple-reminder://A"], "missing": []},
            "delete": {"done": ["x-apple-reminder://A"], "missing": ["x-apple-reminder://Z"]},
        }
        for op, result in cases.items():
            rec = Recorder(response=ok_env(result))
            self.assertEqual(bridge(op, {}, runner=rec), result, "op={}".format(op))


class TestBridgeErrors(unittest.TestCase):
    def test_tcc_from_stderr_1743(self):
        rec = Recorder(response=(1, "", "execution error: Not authorized to send Apple events to Reminders. (-1743)"))
        with self.assertRaises(BridgeError) as cm:
            bridge("dump_incomplete", {}, runner=rec)
        self.assertEqual(cm.exception.code, "tcc")

    def test_tcc_from_envelope(self):
        rec = Recorder(response=err_env("tcc", "not authorized"))
        with self.assertRaises(BridgeError) as cm:
            bridge("read", {"ids": ["x"]}, runner=rec)
        self.assertEqual(cm.exception.code, "tcc")

    def test_timeout(self):
        rec = Recorder(raises=subprocess.TimeoutExpired(cmd=["osascript"], timeout=120))
        with self.assertRaises(BridgeError) as cm:
            bridge("dump_incomplete", {}, runner=rec)
        self.assertEqual(cm.exception.code, "timeout")

    def test_garbage_non_json(self):
        rec = Recorder(response=(0, "this is not json", ""))
        with self.assertRaises(BridgeError) as cm:
            bridge("read", {"ids": []}, runner=rec)
        self.assertEqual(cm.exception.code, "garbage")

    def test_garbage_empty_stdout(self):
        rec = Recorder(response=(0, "   ", ""))
        with self.assertRaises(BridgeError) as cm:
            bridge("read", {"ids": []}, runner=rec)
        self.assertEqual(cm.exception.code, "garbage")

    def test_garbage_wrong_shape(self):
        # valid JSON but missing the "ok" envelope key
        rec = Recorder(response=(0, json.dumps({"result": {"x": 1}}), ""))
        with self.assertRaises(BridgeError) as cm:
            bridge("read", {"ids": []}, runner=rec)
        self.assertEqual(cm.exception.code, "garbage")

    def test_nonzero_exit_is_fail(self):
        rec = Recorder(response=(2, "", "some osascript crash"))
        with self.assertRaises(BridgeError) as cm:
            bridge("create", {"list": "X", "items": []}, runner=rec)
        self.assertEqual(cm.exception.code, "fail")

    def test_app_level_error_is_fail(self):
        rec = Recorder(response=err_env("internal", "unknown-op:frobnicate"))
        with self.assertRaises(BridgeError) as cm:
            bridge("frobnicate", {}, runner=rec)
        self.assertEqual(cm.exception.code, "fail")
        self.assertIn("unknown-op", str(cm.exception))


class TestDoctorOffline(unittest.TestCase):
    def setUp(self):
        # Force osascript "present" so doctor proceeds to the injected runner regardless of host.
        self._orig_which = reminders.shutil.which
        reminders.shutil.which = lambda name: "/usr/bin/osascript"

    def tearDown(self):
        reminders.shutil.which = self._orig_which

    def test_report_shape_ok(self):
        rec = Recorder(response=ok_env({"lists": {}}))
        rep = doctor(live=False, runner=rec)
        self.assertTrue(rep["osascript_present"])
        self.assertEqual(rep["osascript_path"], "/usr/bin/osascript")
        self.assertTrue(rep["tcc_ok"])
        self.assertFalse(rep["live"])
        self.assertIsNone(rep["smoke"])
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["features_source"], "pinned")
        self.assertEqual(set(rep["features"].keys()),
                         {"byid_appwide", "allday_due", "flagged_settable", "recently_deleted_behavior"})
        # the trivial TCC-surfacing call was ensure_lists on an empty name list (creates nothing)
        self.assertEqual(rec.calls[0]["op"], "ensure_lists")
        self.assertEqual(json.loads(rec.calls[0]["request"]), {"names": []})

    def test_tcc_denied(self):
        rec = Recorder(response=(1, "", "error -1743 not authorized"))
        rep = doctor(live=False, runner=rec)
        self.assertFalse(rep["tcc_ok"])
        self.assertTrue(any("failed" in e for e in rep["errors"]))
        # features still fall back to pinned values (never invented)
        self.assertEqual(rep["features_source"], "pinned")
        self.assertIn("byid_appwide", rep["features"])

    def test_no_osascript(self):
        reminders.shutil.which = lambda name: None
        rep = doctor(live=False, runner=Recorder(response=ok_env({"lists": {}})))
        self.assertFalse(rep["osascript_present"])
        self.assertFalse(rep["tcc_ok"])
        self.assertTrue(any("osascript" in e for e in rep["errors"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

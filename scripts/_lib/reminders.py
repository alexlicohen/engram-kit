"""Python side of the Apple Reminders JXA bridge (scripts/reminders_bridge.js).

    bridge(op, payload, runner=None, timeout=120) -> result dict
        Runs one osascript batch and returns the JS bridge's `result` on success.
        Raises BridgeError(code) on any failure, distinguishing:
            "tcc"     — Automation permission denied (-1743 in stderr, or JS code:"tcc")
            "timeout" — osascript exceeded `timeout` seconds
            "garbage" — stdout was empty / not the expected JSON envelope
            "fail"    — nonzero exit, or a well-formed {"ok":false} app-level error
        `runner` is injectable for tests (osascript is NEVER spawned by the test suite):
            runner(op, request_json, timeout) -> (returncode, stdout, stderr)
            (raise subprocess.TimeoutExpired to simulate a timeout).

    doctor(live=False, runner=None) -> report dict
        Read-only health/feature report. live=True runs the full create→read→complete→
        delete smoke inside a list named exactly "Engram Test" (read-only everywhere else).

Empirically pinned on macOS 26.4.1 (build 25E253) — see reminders_bridge.js EMPIRICAL FINDINGS
and the KNOWN_* constants below; doctor(live=True) re-pins them on the running machine.
"""
import json
import os
import shutil
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
BRIDGE_JS = os.path.join(_SCRIPTS, "reminders_bridge.js")

TEST_LIST = "Engram Test"          # the ONLY list doctor(live=True) may write to.
DEFAULT_TIMEOUT = 120

# ── Pinned platform behaviour (macOS 26.4.1). doctor(live=True) overwrites `features` with the
#    freshly OBSERVED values; these are the last-known-good defaults for doctor(live=False). ──
KNOWN_MACOS_TESTED = "26.4.1 (build 25E253)"
KNOWN_BYID_APPWIDE = True          # App.reminders.byId resolves a moved reminder (but ~21s/call).
KNOWN_ALLDAY_DUE = True            # alldayDueDate exists and is settable (all-day due dates).
KNOWN_FLAGGED_SETTABLE = True      # `flagged` is settable via JXA.
KNOWN_RECENTLY_DELETED = "missing"  # a deleted reminder is absent app-wide (no scriptable RD container).
# MEASURED on the reference corpus (733 reminders / 11 lists, 61 incomplete), macOS 26.4.1. Each bulk
# property read is ~32 ms/item AND every access through a whose()-filter re-runs the whole-list scan,
# so cost is ~O(props × corpus). Lists retain full COMPLETED history, which is what makes it heavy.
KNOWN_TIMING_NOTE = (
    "dump_incomplete ~165s (7 property scans of the full completed-history corpus — pass timeout>=240); "
    "app-wide byId (complete / delete / unscoped read) ~40-100s PER id (iCloud variance); a SCOPED read "
    "(with `lists` hint) ~1s. So: pass `lists` to read, keep complete/delete batches small, and reserve "
    "dump_incomplete for background sync/nightly — never an interactive path."
)


class BridgeError(RuntimeError):
    """A Reminders bridge failure. `.code` in {tcc, timeout, garbage, fail}."""

    def __init__(self, code, message=""):
        self.code = code
        self.detail = message
        super().__init__("{}: {}".format(code, message) if message else code)


def _default_runner(op, request_json, timeout):
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", BRIDGE_JS, op],
        input=request_json, capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _is_tcc(stderr):
    s = (stderr or "").lower()
    return "-1743" in s or "not authoriz" in s or "not allowed" in s or "not permitted" in s


def bridge(op, payload, runner=None, timeout=DEFAULT_TIMEOUT):
    """Run one bridge op. Returns the `result` dict, or raises BridgeError."""
    runner = runner or _default_runner
    request_json = json.dumps(payload or {})
    try:
        rc, stdout, stderr = runner(op, request_json, timeout)
    except subprocess.TimeoutExpired:
        raise BridgeError("timeout", "osascript exceeded {}s for op={}".format(timeout, op))
    # TCC can surface at the process level (nonzero exit, -1743 in stderr) before the JS runs.
    if _is_tcc(stderr):
        raise BridgeError("tcc", (stderr or "").strip())
    if rc != 0:
        raise BridgeError("fail", "exit {}: {}".format(rc, (stderr or "").strip() or (stdout or "").strip()))
    stdout = (stdout or "").strip()
    if not stdout:
        raise BridgeError("garbage", "empty stdout (stderr: {})".format((stderr or "").strip()[:200]))
    try:
        resp = json.loads(stdout)
    except (ValueError, TypeError):
        raise BridgeError("garbage", stdout[:300])
    if not isinstance(resp, dict) or "ok" not in resp:
        raise BridgeError("garbage", "unexpected response shape: {}".format(stdout[:300]))
    if not resp.get("ok"):
        js_code = resp.get("code") or "fail"
        mapped = "tcc" if js_code == "tcc" else "fail"
        raise BridgeError(mapped, resp.get("error", ""))
    return resp.get("result")


# ────────────────────────────────────────── doctor ──────────────────────────────────────────

def doctor(live=False, runner=None):
    """Structured health/feature report for the Reminders bridge.

    live=False: checks osascript is present + a trivial TCC-surfacing call; `features` are the
    last-known pinned values (source="pinned").
    live=True: additionally runs the full smoke in the "Engram Test" list and OVERWRITES `features`
    with freshly observed values (source="observed"); `smoke` holds the step-by-step transcript.
    """
    report = {
        "osascript_present": False,
        "osascript_path": None,
        "tcc_ok": None,
        "features": {
            "byid_appwide": KNOWN_BYID_APPWIDE,
            "allday_due": KNOWN_ALLDAY_DUE,
            "flagged_settable": KNOWN_FLAGGED_SETTABLE,
            "recently_deleted_behavior": KNOWN_RECENTLY_DELETED,
        },
        "features_source": "pinned",
        "macos_tested": KNOWN_MACOS_TESTED,
        "timing_note": KNOWN_TIMING_NOTE,
        "live": bool(live),
        "errors": [],
        "smoke": None,
    }
    path = shutil.which("osascript")
    report["osascript_path"] = path
    report["osascript_present"] = bool(path)
    if not path:
        report["tcc_ok"] = False
        report["errors"].append("osascript not found on PATH")
        return report

    # Trivial JXA call that surfaces the TCC gate but creates nothing (ensure_lists on []).
    try:
        bridge("ensure_lists", {"names": []}, runner=runner)
        report["tcc_ok"] = True
    except BridgeError as e:
        report["tcc_ok"] = e.code != "tcc"
        report["errors"].append("trivial call failed: {}".format(e))
        if e.code == "tcc":
            return report  # nothing further is probeable without Automation permission

    if not live:
        return report

    smoke, observed = _live_smoke(runner=runner)
    report["smoke"] = smoke
    report["features"].update(observed)
    report["features_source"] = "observed"
    return report


def _tomorrow_iso():
    import datetime
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()


# dump_incomplete / unscoped read must scan every list's full completed history (~32 ms/item, ~733
# items on the reference corpus) => allow well beyond the 120 s default for those ops.
T_DUMP = 300


def _live_smoke(runner=None):
    """Full create→read→complete→delete smoke in the "Engram Test" list. Read-only elsewhere.

    Returns (transcript, observed) where observed pins:
        byid_appwide, allday_due, flagged_settable, recently_deleted_behavior.
    Each step is timed (ms). Cleanup deletes the created reminder; the (empty) test list is left.
    """
    import time
    steps = []
    observed = {
        "byid_appwide": None, "allday_due": None,
        "flagged_settable": None, "recently_deleted_behavior": None,
    }

    def step(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            ok = True
        except BridgeError as e:
            detail, ok = "{}: {}".format(e.code, e.detail), False
        except Exception as e:  # noqa: BLE001 — smoke must never crash doctor
            detail, ok = "{}: {}".format(type(e).__name__, e), False
        steps.append({"step": name, "ok": ok, "ms": int((time.time() - t0) * 1000), "detail": detail})
        return detail if ok else None

    due = _tomorrow_iso()
    task_id = "doctor-smoke"
    notes = "engram-task: {}\nnote: doctor(live=True) smoke — safe to delete".format(task_id)
    state = {"rid": None, "deleted": False}

    step("ensure_lists", lambda: bridge("ensure_lists", {"names": [TEST_LIST]}, runner=runner))

    def _create():
        res = bridge("create", {"list": TEST_LIST, "items": [{
            "task_id": task_id, "title": "engram doctor smoke",
            "notes": notes, "due": due, "priority": 1, "flagged": True,
        }]}, runner=runner)
        if res["created"]:
            state["rid"] = res["created"][0]["reminder_id"]
        return res
    step("create", _create)

    def _dump():
        # Real all-lists dump over the live corpus — also finding #5 (timing) and the flagged/all-day observation.
        res = bridge("dump_incomplete", {}, runner=runner, timeout=T_DUMP)
        mine = next((it for it in res["items"] if it["id"] == state["rid"]), None)
        if mine is not None:
            observed["flagged_settable"] = bool(mine.get("flagged"))
            observed["allday_due"] = (mine.get("due") == due)
            return {"corpus_incomplete": len(res["items"]), "found_mine": True,
                    "flagged": mine.get("flagged"), "due": mine.get("due"), "priority": mine.get("priority")}
        return {"corpus_incomplete": len(res["items"]), "found_mine": False}
    if state["rid"]:
        step("dump_incomplete", _dump)

    def _byid():
        # Force the app-wide byId fallback by scoping to a list that does NOT contain the reminder.
        res = bridge("read", {"ids": [state["rid"]], "lists": ["__engram_no_such_list__"]}, runner=runner)
        ok = any(f["id"] == state["rid"] for f in res["found"])
        observed["byid_appwide"] = ok
        return {"resolved_appwide": ok, "found": res["found"], "missing": res["missing"]}
    if state["rid"]:
        step("read(app-wide byId probe)", _byid)

    if state["rid"]:
        step("complete", lambda: bridge("complete", {"ids": [state["rid"]]}, runner=runner))

    def _read_completed():
        res = bridge("read", {"ids": [state["rid"]], "lists": [TEST_LIST]}, runner=runner)
        f = next((x for x in res["found"] if x["id"] == state["rid"]), None)
        return {"completed": (f or {}).get("completed"), "found": bool(f)}
    if state["rid"]:
        step("read(after complete)", _read_completed)

    def _delete():
        res = bridge("delete", {"ids": [state["rid"]]}, runner=runner)
        state["deleted"] = state["rid"] in res["done"]
        return res
    if state["rid"]:
        step("delete", _delete)

    def _read_deleted():
        # Scoped read hits the byId fallback for the (now-deleted) id → pins recently-deleted behaviour.
        res = bridge("read", {"ids": [state["rid"]], "lists": [TEST_LIST]}, runner=runner)
        if state["rid"] in res["missing"]:
            observed["recently_deleted_behavior"] = "missing"
            return {"missing": True}
        f = next((x for x in res["found"] if x["id"] == state["rid"]), None)
        observed["recently_deleted_behavior"] = "container:{}".format((f or {}).get("list"))
        return {"missing": False, "still_found_in": (f or {}).get("list")}
    if state["rid"]:
        step("read(after delete)", _read_deleted)

    # Cleanup safety net: only if the primary delete did not confirm removal.
    if state["rid"] and not state["deleted"]:
        step("cleanup(ensure deleted)",
             lambda: bridge("delete", {"ids": [state["rid"]]}, runner=runner))

    steps.append({"step": "note", "ok": True, "ms": 0,
                  "detail": "left the (empty) '{}' list in place by design".format(TEST_LIST)})
    return steps, observed


if __name__ == "__main__":
    import sys
    _live = "--live" in sys.argv
    print(json.dumps(doctor(live=_live), indent=2))

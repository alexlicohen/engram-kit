#!/usr/bin/env python3
"""Teeth test for scripts/resolve_wave.py — the Python port of a retired JS helper (resolveWave)
that the ORCHESTRATOR runs (pre-launch) to validate + atomically CONSUME a distiller's wave contract.
The guard moved to Python because the Workflow runtime is pure JS (no fs / no dynamic loader), so a
distiller cannot run the old JS guard; every check must therefore still bite from Python.

Proves each validation FAILS CLOSED and the happy path consumes + prints the wave path:
  * valid contract                      -> exit 0, prints wave_rel/wave_abs, renames .json -> .consumed.json
  * wrong expected_count                 -> exit 1 (LINE-COUNT mismatch)
  * wrong sha256                         -> exit 1 (SHA256 mismatch)
  * mode not incremental|backfill        -> exit 1
  * mode=backfill without allow_history  -> exit 1 (allow_history)
  * mode=incremental at a BACKFILL_WAVES basename -> exit 1 (FULL-BACKFILL)
  * re-invoke after consume              -> exit 1 (MISSING; CONSUMED contract exists)

Self-contained synthetic root (its own _sources/) in a temp dir; resolve_wave reads the REAL
BACKFILL_WAVES from _manifest/constants via the module's own location, so the fixture needs no
_manifest/. Touches nothing real. Run: python3 -m pytest this_file.
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
RESOLVE_WAVE = _REPO / "scripts" / "resolve_wave.py"
WAVE_PREPARE = _REPO / "scripts" / "wave_prepare.py"
PY = sys.executable
SOURCE = "granola-new"                 # not a backfill basename; passes the incremental guard


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def make_root(tmp):
    root = pathlib.Path(tmp)
    (root / "_sources").mkdir()
    return root


def write_wave(root, name="granola-new-delta.jsonl", n=3):
    wave = root / "_sources" / name
    wave.write_text("".join(json.dumps({"id": f"g-{i}"}) + "\n" for i in range(n)))
    return wave


def live(root, source=SOURCE):
    return root / "_sources" / f"{source}.contract.json"


def consumed(root, source=SOURCE):
    return root / "_sources" / f"{source}.contract.consumed.json"


def hand_contract(root, wave, *, source=SOURCE, mode="incremental", count=None, sha256=None, **extra):
    """Write a contract by hand (to simulate the stale/malformed states wave_prepare refuses)."""
    data = wave.read_bytes()
    c = {"source": source, "mode": mode,
         "wave": os.path.relpath(wave, root),
         "expected_count": (sum(1 for ln in data.splitlines() if ln.strip()) if count is None else count),
         "sha256": (hashlib.sha256(data).hexdigest() if sha256 is None else sha256),
         "created": "2026-07-01T00:00:00Z"}
    c.update(extra)
    live(root, source).write_text(json.dumps(c, indent=2) + "\n")


def prepare(root, wave, mode="incremental", allow=False, source=SOURCE):
    cmd = [PY, str(WAVE_PREPARE), "--source", source, "--mode", mode, "--wave", str(wave),
           "--root", str(root)]
    if allow:
        cmd.append("--allow-history")
    return sh(cmd)


def resolve(root, source=SOURCE):
    return sh([PY, str(RESOLVE_WAVE), "--source", source, "--root", str(root)])


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestResolveWave(unittest.TestCase):

    def test_valid_consumes_and_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            self.assertEqual(prepare(root, wave).returncode, 0)
            r = resolve(root)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertIn("RESOLVED mode=incremental expected=3", r.stdout, out(r))
            self.assertIn("wave_rel=", r.stdout, out(r))
            self.assertIn("wave_abs=", r.stdout, out(r))
            self.assertIn(str(wave), r.stdout, out(r))              # absolute path printed
            self.assertFalse(live(root).exists(), "live contract must be consumed")
            self.assertTrue(consumed(root).exists(), "consumed contract must exist")

    def test_wrong_count_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            hand_contract(root, wave, count=4)                     # sha correct, count wrong
            r = resolve(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("LINE-COUNT mismatch", r.stderr, out(r))
            self.assertTrue(live(root).exists(), "a refused contract must NOT be consumed")

    def test_wrong_sha_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            hand_contract(root, wave, sha256="0" * 64)             # count correct, sha wrong
            r = resolve(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("SHA256 mismatch", r.stderr, out(r))
            self.assertTrue(live(root).exists())

    def test_bad_mode_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            hand_contract(root, wave, mode="sideways")
            r = resolve(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("mode must be", r.stderr, out(r))
            self.assertTrue(live(root).exists())

    def test_backfill_without_allow_history_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            hand_contract(root, wave, mode="backfill")             # no allow_history
            r = resolve(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("allow_history", r.stderr, out(r))
            self.assertTrue(live(root).exists())

    def test_incremental_at_backfill_basename_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            # basename is a KNOWN full-backfill default (gmail-run.jsonl ∈ BACKFILL_WAVES); an
            # incremental contract pointing at it is the Bug-1 footgun -> must be refused.
            wave = write_wave(root, name="gmail-run.jsonl", n=3)
            hand_contract(root, wave, source="gmail", mode="incremental")
            r = resolve(root, source="gmail")
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("FULL-BACKFILL", r.stderr, out(r))
            self.assertTrue(live(root, "gmail").exists())

    def test_reinvoke_after_consume_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            self.assertEqual(prepare(root, wave).returncode, 0)
            r1 = resolve(root)
            self.assertEqual(r1.returncode, 0, out(r1))
            r2 = resolve(root)                                     # the re-invoke that used to re-distill
            self.assertEqual(r2.returncode, 1, out(r2))
            self.assertIn("MISSING", r2.stderr, out(r2))
            self.assertIn("CONSUMED contract exists", r2.stderr, out(r2))

    def test_reinvoke_message_warns_when_source_has_no_done_markers(self):
        # granola-new (SOURCE) is IN HAS_DONE_MARKERS -- safe-to-resume wording expected
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            self.assertEqual(prepare(root, wave).returncode, 0)
            self.assertEqual(resolve(root).returncode, 0)
            r = resolve(root)
            self.assertIn("safe to resume", r.stderr, out(r))
            self.assertNotIn("REPROCESSES THE WHOLE WAVE", r.stderr, out(r))

    def test_reinvoke_message_warns_when_source_has_no_done_markers_outlook(self):
        # outlook is NOT in HAS_DONE_MARKERS -- must get the stronger reprocess-from-scratch warning
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, name="outlook-run.jsonl", n=3)
            self.assertEqual(prepare(root, wave, source="outlook").returncode, 0)
            self.assertEqual(resolve(root, source="outlook").returncode, 0)
            r = resolve(root, source="outlook")
            self.assertIn("REPROCESSES THE WHOLE WAVE", r.stderr, out(r))
            self.assertNotIn("safe to resume", r.stderr, out(r))

    def test_reinvoke_message_warns_for_imsg_reclass(self):
        # Regression (2026-07-10 code review): imsg-reclass was wrongly IN HAS_DONE_MARKERS, so a
        # re-pinned/relaunched interrupted run printed a false "safe to resume" message even though
        # imsg_reclassify.mjs has no stamp_markers.py call or done.d directory of its own -- it must
        # get the same REPROCESSES warning as any other no-done-marker source.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, name="imsg-reclass-run.jsonl", n=3)
            self.assertEqual(prepare(root, wave, source="imsg-reclass").returncode, 0)
            self.assertEqual(resolve(root, source="imsg-reclass").returncode, 0)
            r = resolve(root, source="imsg-reclass")
            self.assertIn("REPROCESSES THE WHOLE WAVE", r.stderr, out(r))
            self.assertNotIn("safe to resume", r.stderr, out(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)

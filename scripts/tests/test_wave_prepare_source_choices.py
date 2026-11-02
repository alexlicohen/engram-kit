#!/usr/bin/env python3
"""Teeth test for scripts/wave_prepare.py's --source validation — a 2026-07-10 code review found
--source had no choices= restriction (unlike sync_verify.py/bump_watermark.py, which both validate
against the shared SOURCES dict) and a stale help string missing outlook/whatsapp/gmail-update. A
typo like --source outlok silently wrote a dead contract file with no indication of the mistake
until resolve_wave.py failed one step later with a confusing MISSING-contract error pointing
nowhere near the real cause.

Run: python3 -m pytest this_file.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
WAVE_PREPARE = _REPO / "scripts" / "wave_prepare.py"
PY = sys.executable

sys.path.insert(0, str(_REPO / "scripts"))
import wave_prepare as wp  # noqa: E402
import sync_verify as sv  # noqa: E402


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestKnownSourcesCoverage(unittest.TestCase):
    def test_known_sources_covers_every_sync_verify_source(self):
        self.assertEqual(set(wp.KNOWN_SOURCES), set(sv.SOURCES))


class TestSourceTypoRejected(unittest.TestCase):
    def test_typo_source_rejected_at_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "_sources").mkdir()
            wave = root / "_sources" / "outlook-run.jsonl"
            wave.write_text(json.dumps({"id": "1"}) + "\n")
            r = sh([PY, str(WAVE_PREPARE), "--source", "outlok", "--mode", "incremental",
                    "--wave", str(wave), "--root", str(root)])
            self.assertNotEqual(r.returncode, 0, out(r))
            self.assertIn("invalid choice", r.stderr, out(r))
            self.assertFalse((root / "_sources" / "outlok.contract.json").exists(),
                              "a rejected --source must not write a dead contract file")

    def test_valid_source_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "_sources").mkdir()
            wave = root / "_sources" / "outlook-run.jsonl"
            wave.write_text(json.dumps({"id": "1"}) + "\n")
            r = sh([PY, str(WAVE_PREPARE), "--source", "outlook", "--mode", "incremental",
                    "--wave", str(wave), "--root", str(root)])
            self.assertEqual(r.returncode, 0, out(r))
            self.assertTrue((root / "_sources" / "outlook.contract.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Guard test for the wave-contract INTEGRITY checks in scripts/resolve_wave.py — the line-count
and sha256 assertions that pin the wave a distiller runs to the exact content the contract
promised. (test_finding1 covers the mode/consume/backfill machinery; this isolates the two
content-integrity checks so each can be mutation-tested independently by scripts/mutate_check.py.)

The guard moved out of a retired JS helper (resolveWave) into resolve_wave.py because the
Workflow runtime is pure JS (no fs / no dynamic loader), so the distiller cannot run the JS guard;
the orchestrator runs the Python one pre-launch.

Each check is isolated so a mutation to ONE of them is provable:
  * count check  — a contract whose sha256 MATCHES the file but whose expected_count is wrong
                   must throw LINE-COUNT (sha would pass; only the count check catches it);
  * sha256 check — a contract whose expected_count is correct but whose sha256 is wrong must
                   throw SHA256 (count passes; only the sha check catches it).
Plus the happy path: a correct contract resolves.

Self-contained synthetic root in a temp dir (its own _sources/); resolveWave reads BACKFILL_WAVES
from the real repo via the module's own location, so the fixture needs no _manifest/. Touches
nothing real. Run: python3 -m pytest this_file.
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
PY = sys.executable
SOURCE = "granola-new"                       # not a full-backfill basename -> passes the incr guard
WAVE_BASENAME = "granola-new-delta.jsonl"


def make_root(tmp):
    root = pathlib.Path(tmp)
    (root / "_sources").mkdir()
    return root


def write_wave(root, n=3):
    wave = root / "_sources" / WAVE_BASENAME
    wave.write_text("".join(json.dumps({"id": f"g-{i}"}) + "\n" for i in range(n)))
    return wave


def write_contract(root, wave, *, count=None, sha256=None):
    """Hand-write an incremental contract, defaulting count+sha to the wave's true values so a
    caller can corrupt exactly ONE of them."""
    data = wave.read_bytes()
    true_count = sum(1 for ln in data.splitlines() if ln.strip())
    true_sha = hashlib.sha256(data).hexdigest()
    c = {
        "source": SOURCE,
        "mode": "incremental",
        "wave": os.path.relpath(wave, root),
        "expected_count": true_count if count is None else count,
        "sha256": true_sha if sha256 is None else sha256,
        "created": "2026-07-01T00:00:00Z",
    }
    p = root / "_sources" / f"{SOURCE}.contract.json"
    p.write_text(json.dumps(c, indent=2) + "\n")
    return p


def resolve(root):
    """Invoke resolve_wave.py exactly as the orchestrator does (pre-launch validate + consume)."""
    return subprocess.run([PY, str(RESOLVE_WAVE), "--source", SOURCE, "--root", str(root)],
                          capture_output=True, text=True)


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestWaveIntegrity(unittest.TestCase):
    def test_count_mismatch_throws(self):
        """sha correct, count wrong -> only the count check can catch it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            write_contract(root, wave, count=4)          # true count is 3
            r = resolve(root)
            self.assertNotEqual(r.returncode, 0, out(r))
            self.assertIn("LINE-COUNT mismatch", r.stderr, out(r))

    def test_sha256_mismatch_throws(self):
        """count correct, sha wrong -> only the sha256 check can catch it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            write_contract(root, wave, sha256="0" * 64)  # true sha differs
            r = resolve(root)
            self.assertNotEqual(r.returncode, 0, out(r))
            self.assertIn("SHA256 mismatch", r.stderr, out(r))

    def test_valid_contract_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=3)
            write_contract(root, wave)                   # both correct
            r = resolve(root)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertIn("RESOLVED mode=incremental expected=3", r.stdout, out(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)

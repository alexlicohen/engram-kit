#!/usr/bin/env python3
"""Teeth test for scripts/sync_verify.py — the post-wave verifier that gates every sync commit.
Covers two real bugs a 2026-07-10 code review found live on disk before the first automated
Outlook sync (range=aab8a33b..HEAD full-repo review):

  * STALE CONTRACT: a *.contract.consumed.json from a PRIOR run is never deleted, so it can sit
    next to a wave file a LATER worklist run has since regenerated with different content. Trusting
    its expected_count blindly would silently under-verify (or over-verify) whatever a distiller
    actually processed against the CURRENT wave. sync_verify.py now re-hashes the wave file the
    contract names and FAILS if it no longer matches — verified against the exact stale state found
    on disk: outlook.contract.consumed.json (expected_count=33, a 2026-07-06 sha256) next to a
    since-regenerated 48-line outlook-run.jsonl.
  * matches() used to short-circuit the source-field check on a blank/missing `source:` — a note
    with the right ref_prefix but NO source field would pass as a match, letting a miscounted note
    plug a gap left by a genuinely missing note and print a false SYNC OK.

Self-contained temp root (its own git repo + _sources/); mirrors test_resolve_wave.py's fixture
style. Run: python3 -m pytest this_file.
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
SYNC_VERIFY = _REPO / "scripts" / "sync_verify.py"
PY = sys.executable

sys.path.insert(0, str(_REPO / "scripts"))
import sync_verify as sv  # noqa: E402


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def make_git_root(tmp):
    root = pathlib.Path(tmp)
    (root / "_sources").mkdir()
    sh(["git", "init", "-q"], cwd=root)
    sh(["git", "config", "user.email", "test@example.com"], cwd=root)
    sh(["git", "config", "user.name", "Test"], cwd=root)
    return root


def write_wave(root, name, n):
    wave = root / "_sources" / name
    wave.write_text("".join(json.dumps({"id": f"o-{i}"}) + "\n" for i in range(n)))
    return wave


def write_consumed_contract(root, source, wave, count, sha256_hex):
    p = root / "_sources" / f"{source}.contract.consumed.json"
    p.write_text(json.dumps({
        "source": source, "mode": "incremental",
        "wave": os.path.relpath(wave, root),
        "expected_count": count, "sha256": sha256_hex,
        "created": "2026-07-06T00:00:00Z",
    }, indent=2) + "\n")
    return p


def sha_of(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def verify(root, source="outlook", dropped=0):
    return sh([PY, str(SYNC_VERIFY), "--source", source, "--dropped", str(dropped),
               "--root", str(root)])


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestStaleContractDetection(unittest.TestCase):
    def test_stale_contract_sha_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_git_root(tmp)
            wave = write_wave(root, "outlook-run.jsonl", n=48)  # CURRENT wave: 48 lines
            # consumed contract still describes the OLD 33-line wave (stale, exactly the real bug)
            write_consumed_contract(root, "outlook", wave, count=33, sha256_hex="0" * 64)
            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("STALE", r.stdout, out(r))

    def test_fresh_contract_matching_wave_proceeds_past_staleness_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_git_root(tmp)
            wave = write_wave(root, "outlook-run.jsonl", n=2)
            write_consumed_contract(root, "outlook", wave, count=2, sha256_hex=sha_of(wave))
            r = verify(root)
            # Passes the staleness gate (no STALE in output); fails later for an unrelated reason
            # (0 matched notes vs expected 2) since this fixture writes no actual note files —
            # that's fine, this test only proves the fresh-contract path isn't blocked at the gate.
            self.assertNotIn("STALE", r.stdout, out(r))

    def test_missing_wave_file_referenced_by_contract_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_git_root(tmp)
            wave = write_wave(root, "outlook-run.jsonl", n=2)
            write_consumed_contract(root, "outlook", wave, count=2, sha256_hex=sha_of(wave))
            wave.unlink()  # the wave the contract names no longer exists
            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("no longer exists", r.stdout, out(r))


class TestMatchesRejectsBlankSource(unittest.TestCase):
    def test_blank_source_is_not_a_match(self):
        cfg = sv.SOURCES["outlook"]
        self.assertFalse(sv.matches(cfg, "", "outlook:thread:abc123def456", "outlook/x.md"))

    def test_missing_source_is_not_a_match(self):
        cfg = sv.SOURCES["outlook"]
        self.assertFalse(sv.matches(cfg, None, "outlook:thread:abc123def456", "outlook/x.md"))

    def test_correct_source_still_matches(self):
        cfg = sv.SOURCES["outlook"]
        self.assertTrue(sv.matches(cfg, "outlook", "outlook:thread:abc123def456", "outlook/x.md"))

    def test_wrong_source_still_rejected(self):
        cfg = sv.SOURCES["outlook"]
        self.assertFalse(sv.matches(cfg, "gmail", "outlook:thread:abc123def456", "outlook/x.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

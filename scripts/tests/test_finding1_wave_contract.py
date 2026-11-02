#!/usr/bin/env python3
"""Regression test for guard-audit FINDING 1 (2026-07-01): a stale wave contract could drive a
silent in-place re-distill of history that overwrote just-written notes, and sync_verify
greenlit it ("SYNC OK") because it counted MODIFIED notes as valid delta and ignored the
contract's mode.

Proves the three fixes have TEETH:
  (i)   mode=incremental + matched MODIFIED notes  -> sync_verify exits 1, names the paths;
  (ii)  the contract is ONE-SHOT — resolve_wave.py consumes it (atomic rename to
        *.contract.consumed.json); a second resolve_wave.py call throws MISSING;
  (iii) mode=backfill without allow_history -> resolve_wave.py throws; and wave_prepare refuses
        `--mode backfill` without `--allow-history`;
  (iv)  happy paths still pass: incremental added-only -> SYNC OK (exit 0); backfill with
        --allow-history -> resolve_wave.py returns; sync_verify reads the CONSUMED contract.

Plus the two adversarial evasions of the incremental guard (review round 2):
  (2a)  delete-then-recreate: a matched COMMITTED note DELETED while a new-named note is added
        used to pass as pure "added" (git `D` states fell through classification) -> now FAILS,
        naming the deleted path;
  (2b)  staged-add overwrite: a matched staged-added note overwritten in place (`AM`) used to
        classify as "added" -> now classifies as MODIFIED and FAILS.

Self-contained: every test builds a synthetic store (its own git repo) in a temp dir; the real
store and real `_sources/*.contract*.json` are never touched. Run directly:

    python3 scripts/tests/test_finding1_wave_contract.py
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
ENGRAM = TESTS_DIR.parent.parent                      # scripts/tests/ -> repo root
RESOLVE_WAVE = ENGRAM / "scripts" / "resolve_wave.py"
WAVE_PREPARE = ENGRAM / "scripts" / "wave_prepare.py"
SYNC_VERIFY = ENGRAM / "scripts" / "sync_verify.py"
PY = sys.executable

NOTE_TMPL = """---
title: "{title}"
date: 2026-07-01
tags: [test]
domain: personal
source: granola
provenance:
  method: distilled
  ref: "granola:{ref}"
participants: []
status: active
---

{body}
"""


def sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def git(root, *args):
    r = sh(["git", "-c", "user.name=test", "-c", "user.email=test@test"] + list(args), cwd=root)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


def make_root(tmp):
    """Synthetic store: its own git repo (so `git status --porcelain` sees only fixture state),
    one domain dir, an empty _sources/."""
    root = pathlib.Path(tmp)
    (root / "_sources").mkdir()
    (root / "personal").mkdir()
    git(root, "init", "-q")
    (root / "personal" / ".gitkeep").write_text("")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def write_wave(root, n=2):
    wave = root / "_sources" / "granola-new-delta.jsonl"
    wave.write_text("".join(
        json.dumps({"id": f"aaa-{i}", "title": f"t{i}", "date": "2026-07-01"}) + "\n"
        for i in range(n)))
    return wave


def write_note(root, i, body="Body.", name=None):
    p = root / "personal" / (name or f"test-note-{i}.md")
    p.write_text(NOTE_TMPL.format(title=f"Test note {i}", ref=f"aaa-{i}", body=body))
    return p


def live_contract(root, source="granola-new"):
    return root / "_sources" / f"{source}.contract.json"


def consumed_contract(root, source="granola-new"):
    return root / "_sources" / f"{source}.contract.consumed.json"


def hand_contract(root, wave, mode, consumed=False, source="granola-new", **extra):
    """Write a contract by hand (to simulate legacy/stale states wave_prepare now refuses)."""
    data = wave.read_bytes()
    c = {"source": source, "mode": mode,
         "wave": os.path.relpath(wave, root),
         "expected_count": sum(1 for ln in data.splitlines() if ln.strip()),
         "sha256": hashlib.sha256(data).hexdigest(),
         "created": "2026-07-01T00:00:00Z"}
    c.update(extra)
    p = consumed_contract(root, source) if consumed else live_contract(root, source)
    p.write_text(json.dumps(c, indent=2) + "\n")
    return p


def prepare(root, wave, mode, allow=False, source="granola-new"):
    cmd = [PY, str(WAVE_PREPARE), "--source", source, "--mode", mode,
           "--wave", str(wave), "--root", str(root)]
    if allow:
        cmd.append("--allow-history")
    return sh(cmd)


def resolve(root, source="granola-new"):
    """Run resolve_wave.py exactly as the orchestrator does before launching a distiller
    (validate + atomically consume the contract). Replaces a retired JavaScript guard — the
    Workflow runtime is pure JS (no fs/import), so the guard runs in Python pre-launch."""
    return sh([PY, str(RESOLVE_WAVE), "--source", source, "--root", str(root)])


def verify(root, dropped=0, source="granola-new"):
    cmd = [PY, str(SYNC_VERIFY), "--source", source, "--root", str(root)]
    if dropped:
        cmd += ["--dropped", str(dropped)]
    return sh(cmd)


def out(r):
    return f"\n--- rc={r.returncode}\n--- stdout:\n{r.stdout}\n--- stderr:\n{r.stderr}"


class TestFinding1WaveContract(unittest.TestCase):

    # ---- (i) incremental wave that MODIFIES notes -> sync_verify FAILS -------------------

    def test_i_incremental_modified_notes_fail_verification(self):
        """Full chain: prepare -> resolve_wave.py (consumes) -> distiller OVERWRITES existing notes
        in place (the finding-1 incident) -> sync_verify must exit 1 and name the paths."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_note(root, 0)
            write_note(root, 1)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "existing notes")
            wave = write_wave(root, n=2)

            self.assertEqual(prepare(root, wave, "incremental").returncode, 0)
            r = resolve(root)
            self.assertEqual(r.returncode, 0, out(r))

            # simulate the re-distill: same deterministic paths, Write overwrites in place
            write_note(root, 0, body="Re-distilled body — curated frontmatter wiped.")
            write_note(root, 1, body="Re-distilled body — curated frontmatter wiped.")

            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("mode=incremental", r.stdout, out(r))
            self.assertIn("MODIFIED", r.stdout, out(r))
            self.assertIn("personal/test-note-0.md", r.stdout, out(r))
            self.assertIn("personal/test-note-1.md", r.stdout, out(r))
            self.assertNotIn("SYNC OK", r.stdout, out(r))

    # ---- (ii) one-shot contract: second resolve_wave.py throws -------------------------------

    def test_ii_contract_is_consumed_second_resolve_throws(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            self.assertEqual(prepare(root, wave, "incremental").returncode, 0)
            self.assertTrue(live_contract(root).exists())

            r1 = resolve(root)
            self.assertEqual(r1.returncode, 0, out(r1))
            self.assertIn("RESOLVED mode=incremental expected=2", r1.stdout, out(r1))
            self.assertFalse(live_contract(root).exists(), "live contract must be consumed")
            self.assertTrue(consumed_contract(root).exists(), "consumed contract must exist")

            r2 = resolve(root)   # the re-invoke that used to re-distill silently
            self.assertNotEqual(r2.returncode, 0, out(r2))
            self.assertIn("MISSING", r2.stderr, out(r2))
            self.assertIn("CONSUMED contract exists", r2.stderr, out(r2))

    # ---- (2a) delete-then-recreate must not pass as pure "added" -------------------------

    def test_2a_incremental_delete_then_recreate_fails(self):
        """Evasion 2a: the re-distill DELETES committed notes and writes new-named ones. The
        added count equals expected, and deletes used to be invisible to git_changed_md — this
        passed as a clean added-only sync. Must now FAIL, naming the deleted paths."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            n0 = write_note(root, 0)
            n1 = write_note(root, 1)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "existing notes")
            wave = write_wave(root, n=2)
            hand_contract(root, wave, "incremental", consumed=True)   # post-distill state

            n0.unlink()                                               # git ` D`
            n1.unlink()
            write_note(root, 0, name="test-note-0-v2.md")             # new-named recreations
            write_note(root, 1, name="test-note-1-v2.md")             # added == expected == 2

            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("DELETED", r.stdout, out(r))
            self.assertIn("personal/test-note-0.md", r.stdout, out(r))
            self.assertIn("personal/test-note-1.md", r.stdout, out(r))
            self.assertNotIn("SYNC OK", r.stdout, out(r))

    # ---- (2b) staged-add overwritten in place (AM) must count as MODIFIED ----------------

    def test_2b_incremental_staged_add_overwrite_fails(self):
        """Evasion 2b: a note staged-added earlier in the session, then overwritten in place,
        shows as `AM` — which used to classify as "added" and pass. Must now FAIL as MODIFIED."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            hand_contract(root, wave, "incremental", consumed=True)   # post-distill state

            write_note(root, 0)
            write_note(root, 1)
            git(root, "add", "personal")                              # both now `A `
            write_note(root, 0, body="Overwritten after staging.")    # note 0 -> `AM`

            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("MODIFIED", r.stdout, out(r))
            self.assertIn("personal/test-note-0.md", r.stdout, out(r))
            self.assertNotIn("personal/test-note-1.md", r.stdout.split("MODIFIED", 1)[1],
                             "the untouched staged-add must not be blamed" + out(r))
            self.assertNotIn("SYNC OK", r.stdout, out(r))

    # ---- (iii) backfill is gated behind --allow-history ----------------------------------

    def test_iii_resolve_refuses_backfill_without_allow_history(self):
        """A backfill contract lacking allow_history (stale/hand-made) must be refused —
        and must NOT be consumed (validation failed)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            hand_contract(root, wave, "backfill")            # no allow_history
            r = resolve(root)
            self.assertNotEqual(r.returncode, 0, out(r))
            self.assertIn("allow_history", r.stderr, out(r))
            self.assertTrue(live_contract(root).exists(),
                            "a refused contract must stay live (not consumed)")
            self.assertFalse(consumed_contract(root).exists())

    def test_iii_wave_prepare_refuses_backfill_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            r = prepare(root, wave, "backfill")              # no --allow-history
            self.assertNotEqual(r.returncode, 0, out(r))
            self.assertIn("--allow-history", r.stderr, out(r))
            self.assertFalse(live_contract(root).exists(), "no contract may be written")
            # and the flag is meaningless on an incremental wave
            r = prepare(root, wave, "incremental", allow=True)
            self.assertNotEqual(r.returncode, 0, out(r))
            self.assertFalse(live_contract(root).exists())

    # ---- (iv) happy paths still pass ------------------------------------------------------

    def test_iv_happy_incremental_added_only(self):
        """prepare -> resolve_wave.py (consumes) -> distiller CREATES notes -> SYNC OK, exit 0.
        sync_verify reads the consumed contract (the live one is gone)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            self.assertEqual(prepare(root, wave, "incremental").returncode, 0)
            r = resolve(root)
            self.assertEqual(r.returncode, 0, out(r))
            write_note(root, 0)                              # new files -> git ADDED (??)
            write_note(root, 1)
            r = verify(root)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertIn("SYNC OK: +2 notes (expected 2)", r.stdout, out(r))
            self.assertIn("(2 added, 0 modified)", r.stdout, out(r))

    def test_iv_happy_backfill_with_allow_history(self):
        """Authorized backfill (granola-redistill class): modified-in-place notes verify clean."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_note(root, 0)
            write_note(root, 1)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "existing notes")
            wave = write_wave(root, n=2)

            r = prepare(root, wave, "backfill", allow=True)
            self.assertEqual(r.returncode, 0, out(r))
            contract = json.loads(live_contract(root).read_text())
            self.assertIs(contract.get("allow_history"), True,
                          "authorization must be recorded ON DISK in the contract")

            r = resolve(root)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertIn("RESOLVED mode=backfill expected=2", r.stdout, out(r))
            self.assertTrue(consumed_contract(root).exists())

            write_note(root, 0, body="Legitimate in-place re-distill.")
            write_note(root, 1, body="Legitimate in-place re-distill.")
            r = verify(root)
            self.assertEqual(r.returncode, 0, out(r))
            self.assertIn("SYNC OK: +2 notes (expected 2)", r.stdout, out(r))
            self.assertIn("(0 added, 2 modified)", r.stdout, out(r))

    def test_count_assertion_not_weakened(self):
        """The pre-existing count assertion must still fire in BOTH modes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            # incremental: only 1 of 2 expected notes created
            hand_contract(root, wave, "incremental", consumed=True)
            write_note(root, 0)
            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("SYNC FAIL: on-disk delta 1 != expected 2", r.stdout, out(r))
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            write_note(root, 0)
            write_note(root, 1)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "existing notes")
            wave = write_wave(root, n=2)
            # backfill: only 1 of 2 expected notes re-distilled
            hand_contract(root, wave, "backfill", consumed=True, allow_history=True)
            write_note(root, 0, body="Re-distilled.")
            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("SYNC FAIL: on-disk delta 1 != expected 2", r.stdout, out(r))

    def test_iv_verify_fails_when_live_contract_never_consumed(self):
        """Seam guard: a still-live contract at verify time means the distiller never ran —
        sync_verify must refuse rather than verify against thin air (or a stale consumed copy)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            wave = write_wave(root, n=2)
            self.assertEqual(prepare(root, wave, "incremental").returncode, 0)
            write_note(root, 0)
            write_note(root, 1)
            r = verify(root)
            self.assertEqual(r.returncode, 1, out(r))
            self.assertIn("never consumed", r.stdout, out(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)

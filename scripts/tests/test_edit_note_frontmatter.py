"""Tests for scripts/edit_note_frontmatter.py — the guarded writer for a deliberate frozen-
frontmatter-field change on a note, backed by the sanctioned-override ledger
(`_manifest/frontmatter-overrides.jsonl`).

Hermetic: builds a throwaway git repo with a single minimal note under a domain dir, `git init`s +
commits it so HEAD exists, then runs the real `edit_note_frontmatter.main()` in-process against it
via `--root <repo>`. `run_lint` (which normally shells out to lint_store.py over the WHOLE store) is
monkeypatched per-test to avoid needing a full lintable store — this still exercises the real
gate/ledger/rollback logic, just not lint_store.py itself. Run: python3 -m pytest this_file.
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)

import edit_note_frontmatter as W  # noqa: E402

REL = "personal/foo.md"
TITLE = "Original Title"

# NOTE has no pre-existing `updated:` line — the realistic common shape (this store's own CLAUDE.md
# notes `updated:` is "documented in schema.md but was never extracted" on most notes). The writer
# stamps `updated:` only when it already exists; see TestPreexistingUpdated for that path.
NOTE = f"""---
title: "{TITLE}"
date: 2026-07-01
tags: [personal]
domain: personal
source: imessage
provenance:
  ref: "imessage:some-guid:1_c000"
  ingested: 2026-07-01
  method: distilled
status: done
---

## Summary
Original body.
"""

NOTE_WITH_UPDATED = NOTE.replace("status: done\n", "status: done\nupdated: 2026-07-01\n")


def _git(args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path
    fp = root / REL
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(NOTE, encoding="utf-8")

    manifest = root / "_manifest"
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "frontmatter-overrides.jsonl").write_text("", encoding="utf-8")

    assert _git(["init", "-q"], root).returncode == 0
    assert _git(["add", "-A"], root).returncode == 0
    assert _git(["commit", "-qm", "seed"], root).returncode == 0
    return root


def _ledger_path(root):
    return os.path.join(root, "_manifest", "frontmatter-overrides.jsonl")


def _ledger_entries(root):
    p = _ledger_path(root)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _run(root, args, monkeypatch, lint_ok=True, lint_msg=""):
    monkeypatch.setattr(W, "run_lint", lambda r: (lint_ok, lint_msg))
    return W.main(["--root", str(root), *args])


class TestHappyPath:
    def test_sets_title_only_and_records_ledger_entry(self, repo, monkeypatch):
        note_before = (repo / REL).read_text(encoding="utf-8")
        rc = _run(repo, ["--path", REL, "--set", 'title=New Title',
                         "--reason", "test correction"], monkeypatch)
        assert rc == 0

        note_after = (repo / REL).read_text(encoding="utf-8")
        assert 'title: "New Title"' in note_after
        assert TITLE not in note_after

        # nothing else in the frontmatter or body changed besides title (this note has no
        # pre-existing `updated:` line, so the writer's stamp-if-present logic is a no-op here)
        before_lines = set(note_before.splitlines())
        after_lines = set(note_after.splitlines())
        added = after_lines - before_lines
        removed = before_lines - after_lines
        assert added == {'title: "New Title"'}
        assert removed == {f'title: "{TITLE}"'}

        entries = _ledger_entries(repo)
        assert len(entries) == 1
        e = entries[0]
        assert e["path"] == REL
        assert e["field"] == "title"
        assert e["old"] == TITLE
        assert e["new"] == "New Title"

    def test_nested_provenance_ref_field(self, repo, monkeypatch):
        rc = _run(repo, ["--path", REL, "--set", 'provenance.ref=imessage:x:c12',
                         "--reason", "fix misref"], monkeypatch)
        assert rc == 0
        note_after = (repo / REL).read_text(encoding="utf-8")
        assert 'ref: "imessage:x:c12"' in note_after
        assert "imessage:some-guid:1_c000" not in note_after

        entries = _ledger_entries(repo)
        assert len(entries) == 1
        assert entries[0]["field"] == "provenance.ref"
        assert entries[0]["old"] == "imessage:some-guid:1_c000"
        assert entries[0]["new"] == "imessage:x:c12"


class TestRefusals:
    def test_refuses_verified_field(self, repo, monkeypatch, capsys):
        note_before = (repo / REL).read_text(encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            _run(repo, ["--path", REL, "--set", 'verified=2026-01-01',
                       "--reason", "nope"], monkeypatch)
        assert ei.value.code != 0

        assert (repo / REL).read_text(encoding="utf-8") == note_before
        assert _ledger_entries(repo) == []

    def test_refuses_dirty_note(self, repo, monkeypatch):
        # uncommitted working-tree change -> HEAD != working copy
        dirty = NOTE.replace("Original body.", "Hand-edited, not yet committed.")
        (repo / REL).write_text(dirty, encoding="utf-8")

        with pytest.raises(SystemExit) as ei:
            _run(repo, ["--path", REL, "--set", 'title=New Title',
                       "--reason", "test"], monkeypatch)
        assert ei.value.code != 0

        assert (repo / REL).read_text(encoding="utf-8") == dirty
        assert _ledger_entries(repo) == []

    def test_noop_when_value_unchanged(self, repo, monkeypatch):
        note_before = (repo / REL).read_text(encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            _run(repo, ["--path", REL, "--set", f'title={TITLE}',
                       "--reason", "test"], monkeypatch)
        assert ei.value.code != 0

        assert (repo / REL).read_text(encoding="utf-8") == note_before
        assert _ledger_entries(repo) == []


class TestPreexistingUpdated:
    """Regression for a real bug found while writing these tests (fixed 2026-07-20): a note that
    ALREADY carries an `updated:` line dated before today was wrongly REFUSED — main() stamps
    `updated:` to today (it is mutable in the redistill profile), but the internal preserve-gate had
    `updated` in its frozen-check set, so the writer's own stamp tripped its own gate. The fix drops
    `updated` from that set; here we assert the corrected behavior: the edit SUCCEEDS and `updated:`
    is restamped to today."""

    def test_preexisting_updated_field_is_restamped_not_refused(self, tmp_path, monkeypatch):
        root = tmp_path
        fp = root / REL
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(NOTE_WITH_UPDATED, encoding="utf-8")
        (root / "_manifest").mkdir(parents=True, exist_ok=True)
        (root / "_manifest" / "frontmatter-overrides.jsonl").write_text("", encoding="utf-8")
        assert _git(["init", "-q"], root).returncode == 0
        assert _git(["add", "-A"], root).returncode == 0
        assert _git(["commit", "-qm", "seed"], root).returncode == 0

        rc = _run(root, ["--path", REL, "--set", 'title=New Title', "--reason", "test"], monkeypatch)
        assert rc == 0
        text = fp.read_text(encoding="utf-8")
        assert 'title: "New Title"' in text
        # `updated:` was restamped away from the pre-existing 2026-07-01 (to today), not left frozen
        assert "updated: 2026-07-01" not in text
        assert __import__("datetime").date.today().isoformat() in text
        # exactly one override entry, for the title (NOT for updated — updated is mutable, unrecorded)
        entries = _ledger_entries(root)
        assert len(entries) == 1 and entries[0]["field"] == "title"


class TestRollback:
    def test_lint_failure_restores_note_and_truncates_ledger(self, repo, monkeypatch):
        note_before = (repo / REL).read_text(encoding="utf-8")
        ledger_before_size = os.path.getsize(_ledger_path(repo))

        with pytest.raises(SystemExit) as ei:
            _run(repo, ["--path", REL, "--set", 'title=New Title',
                       "--reason", "test"], monkeypatch, lint_ok=False, lint_msg="lint error")
        assert ei.value.code != 0

        assert (repo / REL).read_text(encoding="utf-8") == note_before
        assert os.path.getsize(_ledger_path(repo)) == ledger_before_size
        assert _ledger_entries(repo) == []


class TestGateSeam:
    """F14: the writer -> verify_frontmatter_preserved gate END-TO-END seam. A ledgered, still-
    UNCOMMITTED edit must be excused by check_path when the real ledger (load_overrides) is passed,
    and must still hard-fail when it is not (overrides=frozenset()) — proving it is the ledger match,
    not some blanket exemption, that lets the edit through. Covers both a scalar field (title) and a
    nested field (provenance.ref)."""

    def test_title_change_excused_only_with_ledger(self, repo, monkeypatch):
        import verify_frontmatter_preserved as V
        from _lib.frontmatter_diff import load_overrides

        rc = _run(repo, ["--path", REL, "--set", 'title=New Title',
                         "--reason", "test"], monkeypatch)
        assert rc == 0

        overrides = load_overrides(str(repo))
        excused = V.check_path(str(repo), REL, "redistill", overrides=overrides)
        assert excused == []

        strict = V.check_path(str(repo), REL, "redistill", overrides=frozenset())
        assert strict != []
        assert any(v["field"] == "title" for v in strict)

    def test_provenance_ref_change_excused_only_with_ledger(self, repo, monkeypatch):
        import verify_frontmatter_preserved as V
        from _lib.frontmatter_diff import load_overrides

        rc = _run(repo, ["--path", REL, "--set", 'provenance.ref=imessage:x:c12',
                         "--reason", "fix misref"], monkeypatch)
        assert rc == 0

        overrides = load_overrides(str(repo))
        excused = V.check_path(str(repo), REL, "redistill", overrides=overrides)
        assert excused == []

        strict = V.check_path(str(repo), REL, "redistill", overrides=frozenset())
        assert strict != []
        assert any(v["field"] == "provenance.ref" for v in strict)

    def test_wrong_head_sha_in_ledger_does_not_excuse(self, repo, monkeypatch):
        """The sha binding is load-bearing: a ledger entry whose head_sha no longer matches the
        note's current HEAD blob must not excuse the violation, even though (path, field, old, new)
        still match."""
        import verify_frontmatter_preserved as V
        from _lib.frontmatter_diff import load_overrides

        rc = _run(repo, ["--path", REL, "--set", 'title=New Title',
                         "--reason", "test"], monkeypatch)
        assert rc == 0

        entries = _ledger_entries(repo)
        assert len(entries) == 1
        entries[0]["head_sha"] = "0" * 40
        with open(_ledger_path(repo), "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

        overrides = load_overrides(str(repo))
        viols = V.check_path(str(repo), REL, "redistill", overrides=overrides)
        assert viols != []
        assert any(v["field"] == "title" for v in viols)


class TestLedgerAppendAndRollback:
    """F15: appending a new override entry must PRESERVE a pre-existing ledger entry, in order; a
    rollback must truncate only the entries this run itself appended, leaving prior history
    byte-identical."""

    PRIOR_ENTRY = {"path": REL, "field": "status", "old": "todo", "new": "done",
                   "head_sha": "deadbeef" * 5, "date": "2026-01-01", "reason": "prior edit"}

    def _seed_ledger(self, repo):
        with open(_ledger_path(repo), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.PRIOR_ENTRY) + "\n")

    def test_happy_path_preserves_prior_entry_first(self, repo, monkeypatch):
        self._seed_ledger(repo)
        rc = _run(repo, ["--path", REL, "--set", 'title=New Title',
                         "--reason", "test"], monkeypatch)
        assert rc == 0

        entries = _ledger_entries(repo)
        assert len(entries) == 2
        assert entries[0] == self.PRIOR_ENTRY
        assert entries[1]["field"] == "title"

    def test_rollback_leaves_prior_entry_untouched_and_byte_identical(self, repo, monkeypatch):
        self._seed_ledger(repo)
        pre_run_bytes = open(_ledger_path(repo), "rb").read()

        with pytest.raises(SystemExit):
            _run(repo, ["--path", REL, "--set", 'title=New Title', "--reason", "test"],
                 monkeypatch, lint_ok=False, lint_msg="lint error")

        post_bytes = open(_ledger_path(repo), "rb").read()
        assert post_bytes == pre_run_bytes
        assert _ledger_entries(repo) == [self.PRIOR_ENTRY]


class TestMultiSet:
    """F18: multiple --set in one invocation must write ALL named fields, append one ledger entry
    PER field (all sharing the same head_sha), and roll back BOTH the note and BOTH new ledger
    entries together on a lint failure."""

    def test_two_fields_write_both_and_append_two_entries(self, repo, monkeypatch):
        rc = _run(repo, ["--path", REL,
                         "--set", 'title=New Title',
                         "--set", 'provenance.ref=imessage:x:c12',
                         "--reason", "multi-field fix"], monkeypatch)
        assert rc == 0

        note_after = (repo / REL).read_text(encoding="utf-8")
        assert 'title: "New Title"' in note_after
        assert 'ref: "imessage:x:c12"' in note_after

        entries = _ledger_entries(repo)
        assert len(entries) == 2
        assert {e["field"] for e in entries} == {"title", "provenance.ref"}
        assert len({e["head_sha"] for e in entries}) == 1     # one note, one HEAD blob, shared sha

    def test_lint_failure_rolls_back_both_fields(self, repo, monkeypatch):
        note_before = (repo / REL).read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            _run(repo, ["--path", REL,
                       "--set", 'title=New Title',
                       "--set", 'provenance.ref=imessage:x:c12',
                       "--reason", "multi-field fix"], monkeypatch, lint_ok=False, lint_msg="bad")

        assert (repo / REL).read_text(encoding="utf-8") == note_before
        assert _ledger_entries(repo) == []


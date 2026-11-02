"""Regressions for the three confirmed defects in the SANCTIONED-OVERRIDE GATE (2026-08-02 review).

The gate is the store's trust anchor: "check.sh green" is supposed to prove that the frozen
frontmatter fields of every in-place-rewritten note are intact. Each class below encodes one
reproduction that made that claim false, so a regression fails HERE rather than silently in the
store.

  M3  a COMMITTED ledger entry re-armed whenever the note's content returned to the recorded blob
      (git blob shas are content hashes, so a revert re-authorized the change). Liveness is now
      "the ledger line is uncommitted".
  M4  no field allowlist and no `kind` in the matched tuple: a hand-appended line could excuse a
      machine-written `verified:` stamp (which no sanctioned writer may set), and a "removed" excuse
      could cover a "changed" violation. Nothing audited the ledger's OWN diff either, so a
      committed line could be rewritten in place unseen.
  M5  the gated-source list was a third hardcoded copy (check.sh's `case`, clarify_writeback's
      frozenset, prose in edit_note_frontmatter).

Hermetic: each test builds a throwaway git repo (no fixture from the real store) and drives the real
gate — either `verify_frontmatter_preserved.check_path` as a library or the CLI as a subprocess.
Run: python3 -m pytest scripts/tests/test_override_ledger_gate.py
"""
import json
import os
import re
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SCRIPTS)
sys.path.insert(0, _SCRIPTS)

import verify_frontmatter_preserved as V                              # noqa: E402
from _lib.frontmatter_diff import (load_overrides, ledger_audit, override_key,   # noqa: E402
                                   override_entry, REDISTILL_SOURCES,
                                   UNEXCUSABLE_FIELDS, PROFILES)

PY = sys.executable
GATE = os.path.join(_SCRIPTS, "verify_frontmatter_preserved.py")
LEDGER_REL = "_manifest/frontmatter-overrides.jsonl"
REL = "research/n1.md"


def _note(title="Original Title", source="gmail", body="Original body.", verified=None):
    ver = f"verified: {verified}\n" if verified else ""
    return (f"---\n"
            f'title: "{title}"\n'
            f"date: 2026-01-05\n"
            f"tags: [research]\n"
            f"domain: research\n"
            f"source: {source}\n"
            f"participants: [Avery Quillfeather]\n"
            f"provenance:\n"
            f'  ref: "gmail:thread:abc"\n'
            f"  ingested: 2026-01-05\n"
            f"  method: distilled\n"
            f"status: done\n"
            f"{ver}"
            f"related: []\n"
            f"---\n\n"
            f"## Summary\n{body}\n")


def _git(args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, env=env)
    return r


def _write(root, rel, text):
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(root, rel):
    with open(os.path.join(str(root), rel), encoding="utf-8") as fh:
        return fh.read()


def _commit(root, msg="c"):
    assert _git(["add", "-A"], root).returncode == 0
    assert _git(["commit", "-qm", msg], root).returncode == 0
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def _blob(root, rel):
    return _git(["rev-parse", f"HEAD:{rel}"], root).stdout.strip()


def _append_ledger(root, entry):
    p = os.path.join(str(root), LEDGER_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


@pytest.fixture
def repo(tmp_path):
    """A git repo holding one distilled-source note and an empty (committed) override ledger."""
    _write(tmp_path, REL, _note())
    _write(tmp_path, LEDGER_REL, "")
    assert _git(["init", "-q"], tmp_path).returncode == 0
    _commit(tmp_path, "seed")
    return tmp_path


def _gate_cli(root, *args):
    return subprocess.run([PY, GATE, "--root", str(root), *args], capture_output=True, text=True)


# =====================================================================================
# M3 — a COMMITTED ledger entry must be inert forever, even if the note's content comes back
# =====================================================================================
class TestCommittedEntryIsPermanentlyInert:
    """Reproduction (confirmed 3/0): commit a guarded title fix together with its ledger line; the
    entry is supposed to be inert. Later, deliberately revert the note (`git checkout <pre-fix> --
    <note>`) and commit that revert — HEAD's blob is byte-identical to the one the ledger entry
    recorded. Any subsequent automated in-place rewrite that re-derives the corrected title then
    matched the committed entry and passed the gate at exit 0, with no guarded writer and no new
    ledger line: the deliberate revert was silently undone. Every committed line was one latent
    re-authorization, ~112 of them and growing."""

    def _committed_fix_then_reverted(self, repo):
        """Return (root, entry) with: c1 = original note, c2 = title fix + its ledger line,
        c3 = a deliberate revert of the note back to c1's blob."""
        blob_a = _blob(repo, REL)                                     # pre-fix blob
        entry = override_entry(REL, "title", "Original Title", "Corrected Title",
                               blob_a, "2026-08-02", "audit fix", kind="changed")
        _write(repo, REL, _note(title="Corrected Title"))
        _append_ledger(repo, entry)
        _commit(repo, "guarded title fix + ledger")                   # entry is now committed
        # ... and a deliberate revert of the NOTE only (the ledger stays — it is append-only)
        assert _git(["checkout", "HEAD~1", "--", REL], repo).returncode == 0
        _commit(repo, "revert the title fix")
        assert _blob(repo, REL) == blob_a, "HEAD's blob is back to the pre-fix content"
        return entry

    def test_committed_entry_no_longer_excuses_a_re_derived_change(self, repo):
        self._committed_fix_then_reverted(repo)
        # an automated in-place rewrite re-derives the corrected title (body changes too, as a
        # redistill's would) — no guarded writer, no new ledger line
        _write(repo, REL, _note(title="Corrected Title", body="Re-distilled body."))

        viols = V.check_path(str(repo), REL, "redistill", overrides=load_overrides(str(repo)))
        assert [v["field"] for v in viols] == ["title"], viols
        r = _gate_cli(repo, REL)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "title" in r.stdout

    def test_load_overrides_drops_every_committed_entry(self, repo):
        entry = self._committed_fix_then_reverted(repo)
        assert load_overrides(str(repo)) == set()
        # the key is exactly the one that WOULD have matched — proving it is committedness, not a
        # near-miss tuple, that makes it inert
        _write(repo, REL, _note(title="Corrected Title", body="Re-distilled body."))
        key = override_key(entry["path"], entry["field"], entry["old"], entry["new"],
                           entry["kind"], _blob(repo, REL))
        assert V.check_path(str(repo), REL, "redistill", overrides={key}) == []

    def test_an_uncommitted_entry_still_excuses_its_own_change(self, repo):
        """The window the design always claimed must keep working: guarded edit -> check.sh green ->
        commit. Only committedness ends it."""
        blob = _blob(repo, REL)
        _write(repo, REL, _note(title="Corrected Title"))
        _append_ledger(repo, override_entry(REL, "title", "Original Title", "Corrected Title",
                                            blob, "2026-08-02", "audit fix", kind="changed"))
        assert len(load_overrides(str(repo))) == 1
        assert V.check_path(str(repo), REL, "redistill",
                            overrides=load_overrides(str(repo))) == []
        assert _gate_cli(repo, REL).returncode == 0

    def test_the_excuse_expires_the_moment_the_pair_is_committed(self, repo):
        blob = _blob(repo, REL)
        _write(repo, REL, _note(title="Corrected Title"))
        _append_ledger(repo, override_entry(REL, "title", "Original Title", "Corrected Title",
                                            blob, "2026-08-02", "audit fix", kind="changed"))
        _commit(repo, "fix + ledger")
        assert load_overrides(str(repo)) == set()
        # nothing to excuse either — the note now MATCHES head, which is the point
        assert V.check_path(str(repo), REL, "redistill",
                            overrides=load_overrides(str(repo))) == []

    def test_end_to_end_through_the_real_guarded_writer(self, repo, monkeypatch):
        """Same lifecycle driven by edit_note_frontmatter.py itself, so the writer's entry shape and
        the gate's match stay pinned together."""
        import edit_note_frontmatter as W
        monkeypatch.setattr(W, "run_lint", lambda r: (True, ""))
        assert W.main(["--root", str(repo), "--path", REL,
                       "--set", "title=Corrected Title", "--reason", "audit fix"]) == 0

        # before commit: excused
        assert V.check_path(str(repo), REL, "redistill",
                            overrides=load_overrides(str(repo))) == []
        blob_a = _blob(repo, REL)
        _commit(repo, "guarded fix + ledger")
        assert _git(["checkout", "HEAD~1", "--", REL], repo).returncode == 0
        _commit(repo, "revert")
        assert _blob(repo, REL) == blob_a

        _write(repo, REL, _note(title="Corrected Title", body="Re-distilled body."))
        viols = V.check_path(str(repo), REL, "redistill", overrides=load_overrides(str(repo)))
        assert any(v["field"] == "title" for v in viols), viols


# =====================================================================================
# M4 — field allowlist, kind in the matched tuple, and an audit of the ledger's own diff
# =====================================================================================
class TestVerifiedIsNeverExcusable:
    """Reproduction (confirmed 3/0): `head_sha` is a one-command public lookup
    (`git rev-parse HEAD:<path>`), so hand-appending
    `{field: "verified", old, new, head_sha: <that>}` made the gate exit 0 over a MACHINE-altered
    `verified:` stamp — while edit_note_frontmatter.py refuses `--set verified=` outright. The
    excuse was a bare membership test with nothing constraining the field."""

    def _repo_with_verified(self, tmp_path):
        _write(tmp_path, REL, _note(verified="2026-01-01"))
        _write(tmp_path, LEDGER_REL, "")
        assert _git(["init", "-q"], tmp_path).returncode == 0
        _commit(tmp_path, "seed")
        return tmp_path

    def test_a_forged_ledger_line_cannot_excuse_a_machine_written_verified_stamp(self, tmp_path):
        root = self._repo_with_verified(tmp_path)
        _write(root, REL, _note(verified="2026-08-02", body="Re-distilled body."))
        _append_ledger(root, override_entry(REL, "verified", "2026-01-01", "2026-08-02",
                                            _blob(root, REL), "2026-08-02", "forged",
                                            kind="changed"))
        # the line IS live (uncommitted) and names the exact current HEAD blob — and is still refused
        assert len(load_overrides(str(root))) == 1
        viols = V.check_path(str(root), REL, "redistill", overrides=load_overrides(str(root)))
        assert [v["field"] for v in viols] == ["verified"], viols
        r = _gate_cli(root, REL)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "verified" in r.stdout

    def test_the_hard_deny_set_is_the_same_one_the_writer_refuses_to_set(self):
        import edit_note_frontmatter as W
        assert "verified" in UNEXCUSABLE_FIELDS
        assert not (W._SETTABLE & UNEXCUSABLE_FIELDS)
        assert W._SETTABLE == PROFILES["redistill"]["preserve"] - UNEXCUSABLE_FIELDS

    def test_the_writer_still_refuses_verified_outright(self, tmp_path, monkeypatch):
        root = self._repo_with_verified(tmp_path)
        import edit_note_frontmatter as W
        monkeypatch.setattr(W, "run_lint", lambda r: (True, ""))
        with pytest.raises(SystemExit) as ei:
            W.main(["--root", str(root), "--path", REL, "--set", "verified=2026-08-02",
                    "--reason", "nope"])
        assert ei.value.code != 0


class TestKindIsPartOfTheMatch:
    """Reproduction (confirmed 3/0): `kind` was not in the matched tuple, so an entry recording a
    "removed" transition (the only kind clarify_writeback mints) also excused a "changed" violation
    carrying the same (field, old, new)."""

    def test_a_removed_excuse_does_not_cover_a_changed_violation(self, repo):
        _write(repo, REL, _note(title="Corrected Title"))
        _append_ledger(repo, override_entry(REL, "title", "Original Title", "Corrected Title",
                                            _blob(repo, REL), "2026-08-02", "wrong kind",
                                            kind="removed"))
        viols = V.check_path(str(repo), REL, "redistill", overrides=load_overrides(str(repo)))
        assert [(v["field"], v["kind"]) for v in viols] == [("title", "changed")], viols

    def test_the_matching_kind_does_excuse_it(self, repo):
        _write(repo, REL, _note(title="Corrected Title"))
        _append_ledger(repo, override_entry(REL, "title", "Original Title", "Corrected Title",
                                            _blob(repo, REL), "2026-08-02", "right kind",
                                            kind="changed"))
        assert V.check_path(str(repo), REL, "redistill",
                            overrides=load_overrides(str(repo))) == []

    def test_a_legacy_line_without_kind_never_matches(self, repo):
        _write(repo, REL, _note(title="Corrected Title"))
        _append_ledger(repo, {"path": REL, "field": "title", "old": "Original Title",
                              "new": "Corrected Title", "head_sha": _blob(repo, REL),
                              "date": "2026-07-20", "reason": "pre-kind entry"})
        assert V.check_path(str(repo), REL, "redistill",
                            overrides=load_overrides(str(repo))) != []

    def test_override_entry_requires_an_explicit_kind(self):
        with pytest.raises(TypeError):
            override_entry(REL, "title", "a", "b", "sha", "2026-08-02", "r")


class TestLedgerOwnDiffIsAudited:
    """Reproduction (confirmed 3/0): nothing looked at the ledger's own diff — check.sh's
    `pre_mod_notes` / `notes_changed` greps both exclude `_manifest/` — so a COMMITTED ledger line
    could be rewritten in place with no signal anywhere."""

    PRIOR = {"path": REL, "field": "title", "kind": "changed", "old": "a", "new": "b",
             "head_sha": "f" * 40, "date": "2026-01-01", "reason": "prior"}

    def _repo_with_committed_ledger(self, repo, n=2):
        _write(repo, LEDGER_REL, "".join(
            json.dumps({**self.PRIOR, "reason": f"prior {i}"}) + "\n" for i in range(n)))
        _commit(repo, "seed ledger")
        return repo

    def test_modifying_a_committed_line_fails_the_audit(self, repo):
        root = self._repo_with_committed_ledger(repo)
        lines = _read(root, LEDGER_REL).splitlines()
        lines[0] = json.dumps({**self.PRIOR, "new": "SOMETHING ELSE", "reason": "prior 0"})
        _write(root, LEDGER_REL, "\n".join(lines) + "\n")

        ok, new_lines, problems = ledger_audit(str(root))
        assert not ok and new_lines == []
        assert "MODIFIED in place" in problems[0]
        r = _gate_cli(root, "--audit-ledger")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "OVERRIDE-LEDGER ERROR" in r.stderr

    def test_removing_a_committed_line_fails_the_audit(self, repo):
        root = self._repo_with_committed_ledger(repo)
        lines = _read(root, LEDGER_REL).splitlines()
        _write(root, LEDGER_REL, lines[0] + "\n")
        ok, _, problems = ledger_audit(str(root))
        assert not ok
        assert "REMOVED" in problems[0]
        assert _gate_cli(root, "--audit-ledger").returncode == 1

    def test_appending_passes_and_every_new_line_is_printed_for_review(self, repo):
        root = self._repo_with_committed_ledger(repo)
        _append_ledger(root, override_entry(REL, "title", "Original Title", "Corrected Title",
                                            _blob(root, REL), "2026-08-02", "new fix",
                                            kind="changed"))
        ok, new_lines, problems = ledger_audit(str(root))
        assert ok and problems == [] and len(new_lines) == 1
        r = _gate_cli(root, "--audit-ledger")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "REVIEW 1 uncommitted" in r.stdout
        assert "Corrected Title" in r.stdout          # the operator sees the actual excuse

    def test_a_clean_ledger_says_so(self, repo):
        root = self._repo_with_committed_ledger(repo)
        r = _gate_cli(root, "--audit-ledger")
        assert r.returncode == 0
        assert "no uncommitted entries" in r.stdout

    def test_check_sh_runs_the_audit_and_fails_on_it(self):
        sh = open(os.path.join(_SCRIPTS, "check.sh"), encoding="utf-8").read()
        assert "--audit-ledger" in sh
        block = sh.split("--audit-ledger", 1)[1].split("\nfi\n", 1)[0]
        assert "rc=1" in block, "the ledger audit must be able to fail check.sh"


# =====================================================================================
# M5 — one owner for the gated-source list
# =====================================================================================
class TestRedistillSourcesHaveOneOwner:
    """The set used to be hardcoded in three places (check.sh's `case`, clarify_writeback's own
    frozenset, prose in edit_note_frontmatter.py that conceded it was hand-synced). Adding a sixth
    distilled source and missing one copy silently reopens the seam the gate closed."""

    def test_clarify_writeback_imports_the_constant_rather_than_redefining_it(self):
        import clarify_writeback as cw
        import _lib.frontmatter_diff as fd
        assert cw.REDISTILL_SOURCES is fd.REDISTILL_SOURCES
        src = open(os.path.join(_SCRIPTS, "clarify_writeback.py"), encoding="utf-8").read()
        assert not re.search(r'^REDISTILL_SOURCES\s*=', src, re.M), \
            "clarify_writeback must import the set, not define its own copy"

    def test_check_sh_carries_no_hardcoded_copy_and_routes_through_the_gate(self):
        """check.sh called `--classify` with a shell-built path list until 2026-08-04; it now calls
        `--modified notes`, which enumerates AND classifies (through the same `classify`, the one
        owner of the routing) without a path ever crossing the shell boundary."""
        sh = open(os.path.join(_SCRIPTS, "check.sh"), encoding="utf-8").read()
        assert "--modified notes" in sh
        code = "\n".join(ln for ln in sh.splitlines() if not ln.lstrip().startswith("#"))
        for s in sorted(REDISTILL_SOURCES):
            assert not re.search(rf'\b{re.escape(s)}\s*[|)]', code), \
                f"check.sh still hardcodes the gated source {s!r} — one owner is REDISTILL_SOURCES"

    def test_the_classify_cli_routes_exactly_the_constants_members(self, tmp_path):
        """The list check.sh ACTUALLY uses, executed: one note per candidate source through the
        real CLI, asserting the redistill/other split equals REDISTILL_SOURCES."""
        candidates = sorted(REDISTILL_SOURCES) + ["obsidian", "gdrive", "manual", "claude-chat"]
        rels = []
        for s in candidates:
            rel = f"research/{s}.md"
            _write(tmp_path, rel, _note(source=s))
            rels.append(rel)
        _write(tmp_path, LEDGER_REL, "")
        assert _git(["init", "-q"], tmp_path).returncode == 0
        _commit(tmp_path, "seed")

        r = _gate_cli(tmp_path, "--classify", *rels)
        assert r.returncode == 0, r.stdout + r.stderr
        got = dict(reversed(ln.split("\t")) for ln in r.stdout.splitlines() if ln.strip())
        assert {os.path.basename(p)[:-3] for p, k in got.items() if k == "redistill"} \
            == set(REDISTILL_SOURCES)

    def test_classify_reads_source_from_the_frontmatter_block_only(self, tmp_path):
        """check.sh's old `git show HEAD:<f> | grep -m1 '^source:'` scanned the WHOLE file, so a
        note with no frontmatter `source:` but a body line `source: gmail` classified as redistill
        and got the precise field gate instead of the stop-and-investigate halt (fail-open)."""
        rel = "research/body-source.md"
        _write(tmp_path, rel,
               "---\ntitle: \"x\"\ndate: 2026-01-05\ndomain: research\nstatus: done\n---\n\n"
               "## Summary\nsource: gmail\n")
        assert _git(["init", "-q"], tmp_path).returncode == 0
        _commit(tmp_path, "seed")
        assert V.classify(str(tmp_path), rel) == "other"

    def test_classify_is_fail_closed_for_an_untracked_note(self, repo):
        _write(repo, "research/new.md", _note())
        assert V.classify(str(repo), "research/new.md") == "other"

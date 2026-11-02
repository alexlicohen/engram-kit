"""Tests for the engram MCP server — focused on the security-critical read sandbox.

The MCP server is read-only, but the local working tree can hold plaintext secrets (`_vault/`) and
raw connector pulls (`_sources/`). These tests prove the sandbox refuses those and path-traversal,
and serves genuine tracked notes. Runs under the repo's Python 3.9 suite (no `mcp` SDK needed —
the module imports the SDK lazily in main()).

HERMETIC: every test that touches the store runs against a throwaway git repo built by the
`store` fixture (tracked notes + a tracked `_vault/` file + a gitignored `_sources/` file +
an index.jsonl), with `engram_mcp.ROOT` repointed at it. Nothing here reads the real store, so
the sandbox assertions cannot pass or fail by accident of what happens to be on disk.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
import engram_mcp as em  # noqa: E402

NOTES = [
    ("research/2026-01-01-a.md", "papers", "A", ["papers"], ["Sam Riverton"]),
    ("research/2026-06-01-b.md", "grants", "B", ["grants"], ["Sam Riverton", "Lee Novak"]),
    ("work/2026-03-01-c.md", "", "C", ["meeting"], ["Lee Novak"]),
]


def _note_text(title, domain, subpath, tags, participants, date):
    return (
        "---\n"
        f'title: "{title}"\n'
        f"date: {date}\n"
        f"tags: [{', '.join(tags)}]\n"
        f"domain: {domain}\n"
        f"subpath: {subpath or '~'}\n"
        "source: manual\n"
        f"participants: [{', '.join(participants)}]\n"
        "status: done\n"
        "---\n\nBody.\n"
    )


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("engram-store").resolve()
    (root / ".gitignore").write_text("_sources/\n", encoding="utf-8")
    (root / "INDEX.md").write_text("# engram — index\n\n- research\n- work\n", encoding="utf-8")
    (root / "dossiers").mkdir()
    (root / "dossiers" / "example-project.md").write_text(
        "---\ntitle: \"Example Project — dossier\"\ndomain: work\n---\n\n# Example Project\n",
        encoding="utf-8")
    (root / "_vault").mkdir()
    (root / "_vault" / "secret.md").write_text("password: fixture-only-not-a-real-secret\n",
                                               encoding="utf-8")
    (root / "_sources").mkdir()
    (root / "_sources" / "raw.jsonl").write_text('{"raw": "never served"}\n', encoding="utf-8")
    (root / "_manifest").mkdir()
    records = []
    for rel, subpath, title, tags, participants in NOTES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        date = os.path.basename(rel)[:10]
        p.write_text(_note_text(title, rel.split("/")[0], subpath, tags, participants, date),
                     encoding="utf-8")
        records.append({"path": rel, "title": title, "date": date, "domain": rel.split("/")[0],
                        "subpath": subpath or None, "tags": tags, "participants": participants})
    (root / "_manifest" / "index.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    # the brief tool shells out to <ROOT>/scripts/engram_brief.py with cwd=ROOT
    os.symlink(_REPO / "scripts", root / "scripts")

    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-qm", "seed"]):
        subprocess.run([em.GIT, "-C", str(root), *cmd], check=True, capture_output=True, env=env)

    old = em.ROOT
    em.ROOT = root
    yield root
    em.ROOT = old


@pytest.fixture
def tracked(store):
    return em.load_tracked()


# ─────────────────────────── the sandbox must REFUSE ───────────────────────────

def test_refuses_vault_secrets(tracked):
    """_vault/ is git-tracked (encrypted in git, PLAINTEXT on disk) — must never be served."""
    vault = [p for p in tracked if p.startswith("_vault/")]
    assert vault, "expected _vault/ to be tracked (else this test proves nothing)"
    with pytest.raises(ValueError, match="_vault"):
        em.safe_resolve(vault[0], tracked)


def test_refuses_sources_raw_pulls(tracked):
    """_sources/ is gitignored (raw email/iMessage/PHI-holding) → absent from tracked → refused."""
    assert not any(p.startswith("_sources/") for p in tracked)
    with pytest.raises(ValueError):
        em.safe_resolve("_sources/raw.jsonl", tracked)
    with pytest.raises(ValueError):
        em.safe_resolve("_sources/anything.jsonl", tracked)


def test_refuses_path_traversal(tracked):
    for bad in ("../../../etc/passwd", "dossiers/../../etc/hosts", "/etc/passwd",
                "../.git/config", "dossiers/../_vault/secret.md"):
        with pytest.raises(ValueError):
            em.safe_resolve(bad, tracked)


def test_refuses_untracked_and_root(tracked):
    with pytest.raises(ValueError):
        em.safe_resolve("does-not-exist-xyz.md", tracked)
    with pytest.raises(ValueError):
        em.safe_resolve(".", tracked)
    with pytest.raises(ValueError):
        em.safe_resolve("", tracked)


# ─────────────────────────── the sandbox must ALLOW ───────────────────────────

def test_allows_a_real_dossier(tracked):
    p = em.safe_resolve("dossiers/example-project.md", tracked)
    assert p.is_file() and p.name == "example-project.md"


def test_allows_e_prefix_and_absolute(store, tracked):
    """Tolerate the skill's $E/ prefix and an absolute path inside the root."""
    a = em.safe_resolve("$E/dossiers/example-project.md", tracked)
    b = em.safe_resolve(str(store / "dossiers" / "example-project.md"), tracked)
    assert a == b == (store / "dossiers" / "example-project.md").resolve()


def test_read_tool_refuses_vault_gracefully(store):
    """The tool wrapper returns a 'refused' string (not a traceback) on a denied path."""
    out = em.engram_read("_vault/secret.md")
    assert out.startswith("refused:") and "_vault" in out


def test_read_tool_serves_a_tracked_note(store):
    out = em.engram_read("research/2026-01-01-a.md")
    assert out.startswith("---") and "Body." in out


# ─────────────────────────── grep excludes _vault ───────────────────────────

def _hit_paths(grep_out):
    """The file path of each 'path:line:content' grep row (the security-relevant field —
    the string '_vault/' may legitimately appear inside a note's *content*)."""
    paths = []
    for line in grep_out.splitlines():
        if ":" in line and not line.startswith(("…", "no matches", "no ", "search error")):
            paths.append(line.split(":", 1)[0])
    return paths


def test_grep_never_returns_vault(store):
    """No result may come FROM a _vault/ file, even for a term that occurs there."""
    out = em.engram_grep("password", include_archive=True, max_results=200)
    assert not any(p.startswith("_vault/") for p in _hit_paths(out))


def test_grep_finds_tracked_content(store):
    out = em.engram_grep("Body.")
    assert "research/2026-01-01-a.md" in _hit_paths(out)
    assert not any(p.startswith("_vault/") for p in _hit_paths(out))


# ─────────────────────────── frontmatter filtering ───────────────────────────

def test_filter_records_domain_and_date_and_sort():
    recs = [
        {"date": "2026-01-01", "domain": "research", "title": "A", "path": "research/a.md", "tags": ["grants"]},
        {"date": "2026-06-01", "domain": "research", "title": "B", "path": "research/b.md", "tags": ["papers"]},
        {"date": "2026-03-01", "domain": "work", "title": "C", "path": "work/c.md", "tags": []},
    ]
    out = em.filter_records(recs, domain="research", limit=10)
    assert [r["title"] for r in out] == ["B", "A"]  # newest-first
    assert em.filter_records(recs, tag="papers") == [recs[1]]
    assert em.filter_records(recs, since="2026-02-01", until="2026-05-01") == [recs[2]]


def test_filter_records_participant_substring():
    recs = [{"date": "2026-01-01", "domain": "x", "title": "t", "path": "p", "participants": ["Sam Riverton"]}]
    assert em.filter_records(recs, participant="riverton")
    assert not em.filter_records(recs, participant="nobody")


def test_filter_records_subpath():
    """Subpath is the primary within-domain narrowing axis — it must filter, incl. null subpath."""
    recs = [
        {"date": "2026-01-01", "domain": "research", "subpath": "papers", "title": "A", "path": "a"},
        {"date": "2026-02-01", "domain": "research", "subpath": "grants", "title": "B", "path": "b"},
        {"date": "2026-03-01", "domain": "research", "subpath": None, "title": "C", "path": "c"},
    ]
    assert [r["title"] for r in em.filter_records(recs, subpath="papers")] == ["A"]
    assert [r["title"] for r in em.filter_records(recs, domain="research", subpath="grants")] == ["B"]
    assert em.filter_records(recs, subpath="nope") == []


def test_frontmatter_surfaces_subpath_live(store):
    """A real subpathed row renders as [domain/subpath] and the subpath filter narrows."""
    out = em.engram_frontmatter(domain="research", subpath="papers", limit=5)
    assert "[research/papers]" in out
    assert "[research/grants]" not in out  # the filter actually excludes other subpaths


# ─────────────────────────── live tools smoke ───────────────────────────

def test_index_returns_map_and_howto(store):
    out = em.engram_index()
    assert "# " in out and "RECALL RECIPES" in out and "engram_read" in out


def test_frontmatter_tool_live(store):
    out = em.engram_frontmatter(domain="research", limit=3)
    assert " · " in out and "research" in out


def test_brief_tool_runs(store):
    out = em.engram_brief()
    assert "engram brief" in out.lower(), out

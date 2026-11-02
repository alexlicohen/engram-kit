"""Tests for the fidelity-audit sampler (scripts/audit_sample.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import audit_sample as A  # noqa: E402


# F16/F11/F19: audit_sample.resolve_chunk_fp() memoizes chat-DB run-ledger lookups in
# A._chunk_runs.cache (a module-level function attribute) and _chunk_runs() globs "_sources/*run*"
# relative to the process CWD. Without a reset, a test that exercises an imessage/whatsapp ref
# (directly or via fetch_id) either reuses a stale cache from another test module or — on a cold
# cache — globs the REAL, gitignored _sources/ of this checkout, so a `None`/absence assertion is
# only true by accident of what happens to be on disk. Reset before AND after every test in this
# file.
@pytest.fixture(autouse=True)
def reset_chunk_cache():
    A._chunk_runs.cache = None
    yield
    A._chunk_runs.cache = None


# The shipped SOURCE_RULES skip no gmail domain; these tests exercise skip_domains with a store
# whose `ventures` mail lives in a second, connector-unreachable mailbox.
@pytest.fixture(autouse=True)
def second_mailbox_rule(monkeypatch):
    monkeypatch.setitem(A.SOURCE_RULES, "gmail", A.SourceRule(
        True, None, frozenset({"ventures"}), "gmail connector re-fetch (ventures mail is unreachable)"))


def test_high_stakes_filter():
    assert A.is_high_stakes({"domain": "finance", "tags": []})
    assert A.is_high_stakes({"domain": "personal", "tags": ["tax"]})       # tag-driven
    assert not A.is_high_stakes({"domain": "personal", "tags": ["social"]})


def test_eligibility():
    assert A.eligible({"source": "gmail", "domain": "finance"}, False)
    assert not A.eligible({"source": "gmail", "domain": "ventures"}, False)  # separate mailbox
    assert A.eligible({"source": "granola", "domain": "research"}, False)
    assert not A.eligible({"source": "obsidian"}, False)                    # verbatim, no fidelity risk
    assert A.eligible({"source": "outlook", "domain": "research"}, False)   # local thread file
    # chat-DB sources share ONE gate; whatsapp used to be excluded outright even with the flag.
    assert not A.eligible({"source": "imessage", "guid": "x"}, False)       # gated
    assert A.eligible({"source": "imessage", "guid": "x"}, True)            # opt-in
    assert not A.eligible({"source": "whatsapp", "guid": "x"}, False)
    assert A.eligible({"source": "whatsapp", "guid": "x"}, True)
    assert not A.eligible({"source": "brand-new-connector"}, True)          # no rule => ineligible


def test_source_rules_is_the_only_owner():
    """Eligibility must be derivable from SOURCE_RULES alone — no second hardcoded list."""
    for src, rule in A.SOURCE_RULES.items():
        assert rule.why, "%s needs a documented reason" % src
        assert rule.gate in (None, "chatdb")
        assert A.eligible({"source": src, "domain": None}, True) is rule.auditable
    # every excluded/gated source explains itself to the operator
    assert "chatdb" in A.exclusion_reason("whatsapp", False)
    assert "verbatim" in A.exclusion_reason("obsidian", False)
    assert "SOURCE_RULES" in A.exclusion_reason("nope", True)


def test_fetch_id_outlook_resolves_to_local_thread_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "_sources" / "outlook-threads").mkdir(parents=True)
    (tmp_path / "_sources" / "outlook-threads" / "0a0a0a0a0a01.txt").write_text("thread")
    r = {"source": "outlook", "ref": "outlook:thread:0a0a0a0a0a01"}
    assert A.fetch_id(r) == "_sources/outlook-threads/0a0a0a0a0a01.txt"
    # a missing thread file is None (reported), never a guessed path
    assert A.fetch_id({"source": "outlook", "ref": "outlook:thread:deadbeef"}) is None
    assert A.fetch_id({"source": "outlook", "ref": "outlook:thread:../etc/passwd"}) is None
    assert A.fetch_id({"source": "outlook", "ref": ""}) is None


def test_fetch_id_gmail_canonicalizes_to_hex(tmp_path, monkeypatch):
    # a decimal backfill ref must resolve to the connector's hex thread id
    dec = {"source": "gmail", "ref": "gmail:thread:1000000000000000002"}
    assert A.fetch_id(dec) == "de0b6b3a7640002"
    hexref = {"source": "gmail", "ref": "gmail:thread:de0b6b3a7640002"}
    assert A.fetch_id(hexref) == "de0b6b3a7640002"                        # idempotent
    assert A.fetch_id({"source": "granola", "ref": "granola:abc-123"}) == "abc-123"
    # imessage/whatsapp refs resolve to a LOCAL chunk path (audit_sample.resolve_chunk_fp); an
    # unknown chunk yields None, never the raw ref — an auditor must not be handed a non-path.
    # Hermetic: chdir into a tmp sandbox with an EMPTY _sources/ so the None comes from a known,
    # controlled absence of any matching run-ledger chunk, not from whatever happens (or doesn't
    # happen) to be sitting in the real gitignored _sources/ of this checkout.
    (tmp_path / "_sources").mkdir()
    monkeypatch.chdir(tmp_path)
    assert A.fetch_id({"source": "imessage", "ref": "imessage:g:c1"}) is None


def _recs():
    return [
        {"path": "finance/a.md", "source": "gmail", "domain": "finance", "tags": [], "ref": "gmail:thread:1"},
        {"path": "finance/b.md", "source": "gmail", "domain": "finance", "tags": [], "ref": "gmail:thread:2"},
        {"path": "personal/c.md", "source": "gmail", "domain": "personal", "tags": ["social"], "ref": "gmail:thread:3"},
        {"path": "ventures/d.md", "source": "gmail", "domain": "ventures", "tags": [], "ref": "gmail:thread:4"},
    ]


def test_scope_and_determinism():
    recs = _recs()
    _, all1, _ = A.sample(recs, 10, "all", "s", set(), False)
    paths = {r["path"] for r in all1}
    assert "ventures/d.md" not in paths                                     # ineligible
    assert paths == {"finance/a.md", "finance/b.md", "personal/c.md"}
    # high-stakes drops the personal/social note
    _, hs, _ = A.sample(recs, 10, "high-stakes", "s", set(), False)
    assert {r["path"] for r in hs} == {"finance/a.md", "finance/b.md"}
    # same seed => identical order
    _, again, _ = A.sample(recs, 10, "all", "s", set(), False)
    assert [r["path"] for r in all1] == [r["path"] for r in again]


def test_exclude_removes_audited():
    recs = _recs()
    _, chosen, _ = A.sample(recs, 10, "all", "s", {"finance/a.md"}, False)
    assert "finance/a.md" not in {r["path"] for r in chosen}


def test_domains_filter():
    recs = _recs()  # a,b = finance; c = personal
    _, ef, _ = A.sample(recs, 10, "all", "s", set(), False, None, {"finance"})
    assert {r["path"] for r in ef} == {"finance/a.md", "finance/b.md"}
    _, none, _ = A.sample(recs, 10, "all", "s", set(), False, None, {"health"})
    assert none == []


def test_only_restricts_to_synced_notes():
    """The post-sync spot-check restricts the pool to the notes the sync just wrote."""
    recs = _recs()
    only = {"finance/b.md", "personal/c.md"}
    _, chosen, _ = A.sample(recs, 10, "all", "s", set(), False, only)
    assert {r["path"] for r in chosen} == {"finance/b.md", "personal/c.md"}
    # --only still respects eligibility (an ineligible synced note is dropped, not audited blind)
    _, chosen2, _ = A.sample(recs, 10, "all", "s", set(), False, {"ventures/d.md", "finance/a.md"})
    assert {r["path"] for r in chosen2} == {"finance/a.md"}


def test_sample_reports_what_eligibility_dropped():
    """An empty pool must be explainable: emitted=0 is not the same as a clean audit."""
    recs = _recs() + [{"path": "work/ACME.md", "source": "obsidian", "domain": "work", "tags": []},
                      {"path": "personal/w.md", "source": "whatsapp", "domain": "personal",
                       "tags": [], "ref": "whatsapp:j:1_c000"}]
    # --only naming ONLY ineligible notes: pool empty, but `filtered` names the culprits
    pool, chosen, filtered = A.sample(recs, 10, "all", "s", set(), False,
                                      {"work/ACME.md", "personal/w.md"})
    assert pool == [] and chosen == []
    assert filtered == {"obsidian": 1, "whatsapp": 1}
    # the same notes with the chat-DB gate open: whatsapp is no longer filtered
    pool, _, filtered = A.sample(recs, 10, "all", "s", set(), True,
                                 {"work/ACME.md", "personal/w.md"})
    assert [r["path"] for r in pool] == ["personal/w.md"]
    assert filtered == {"obsidian": 1}
    # nothing dropped by eligibility => empty counter (the caller must not WARN)
    _, _, filtered = A.sample(recs, 10, "all", "s", set(), False, {"finance/a.md"})
    assert filtered == {}

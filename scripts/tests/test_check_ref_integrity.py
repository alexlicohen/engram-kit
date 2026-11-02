"""Tests for scripts/check_ref_integrity.py — flags a chat-DB note whose provenance.ref resolves to
a chunk whose date range excludes the note's own date, and (when possible) suggests the correct
chunk from the same guid.

Hermetic: builds a tmp_path sandbox with `_sources/*run*.jsonl` chat-DB run ledgers + the chunk .txt
files they reference (resolve_chunk_fp requires the file to actually exist), and a fake
`_manifest/index.jsonl`. chdir's into the sandbox (audit_sample.load_index/resolve_chunk_fp read
relative paths off CWD) and resets audit_sample._chunk_runs' memo cache before/after every test. Run: python3 -m pytest this_file.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)

import audit_sample as A                      # noqa: E402
import check_ref_integrity as C                # noqa: E402


@pytest.fixture(autouse=True)
def reset_chunk_cache():
    A._chunk_runs.cache = None
    yield
    A._chunk_runs.cache = None


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    (tmp_path / "_sources").mkdir()
    (tmp_path / "_manifest").mkdir()
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _touch(root, rel):
    fp = root / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("chunk text\n", encoding="utf-8")
    return str(rel)


def _write_ledger(root, name, records):
    fn = root / "_sources" / name
    with open(fn, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _write_index(root, records):
    fn = root / "_manifest" / "index.jsonl"
    with open(fn, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _note_rec(path, source, ref, date):
    return {"path": path, "title": path, "date": date, "domain": "personal", "subpath": "",
            "tags": [], "status": "done", "source": source, "ref": ref, "participants": [],
            "related": []}


class TestInRange:
    def test_in_range_note_is_not_flagged(self, sandbox):
        _touch(sandbox, "_sources/imessage/1__c000.txt")
        _write_ledger(sandbox, "imessage-run.jsonl", [
            {"chunk_id": "1_c000", "chat_id": 1, "guid": "guid-a",
             "fp": "_sources/imessage/1__c000.txt", "date_start": "2020-01-01", "date_end": "2020-12-31"},
        ])
        _write_index(sandbox, [
            _note_rec("personal/a.md", "imessage", "imessage:guid-a:1_c000", "2020-06-15"),
        ])
        findings = C.scan(str(sandbox))
        assert findings == []


class TestOutOfRange:
    def test_out_of_range_with_better_chunk_is_flagged_and_suggested(self, sandbox):
        _touch(sandbox, "_sources/imessage/1__c000.txt")
        _touch(sandbox, "_sources/imessage/1__c001.txt")
        _write_ledger(sandbox, "imessage-run.jsonl", [
            {"chunk_id": "1_c000", "chat_id": 1, "guid": "guid-a",
             "fp": "_sources/imessage/1__c000.txt", "date_start": "2018-01-01", "date_end": "2018-12-31"},
            {"chunk_id": "1_c001", "chat_id": 1, "guid": "guid-a",
             "fp": "_sources/imessage/1__c001.txt", "date_start": "2020-01-01", "date_end": "2020-12-31"},
        ])
        # legacy short ref 'c0' resolves to the FIRST chunk (2018), but the note is dated 2020 —
        # the 2020 chunk (c001, same guid) is the correct home.
        _write_index(sandbox, [
            _note_rec("personal/b.md", "imessage", "imessage:guid-a:c0", "2020-06-15"),
        ])
        findings = C.scan(str(sandbox))
        assert len(findings) == 1
        f = findings[0]
        assert f["path"] == "personal/b.md"
        assert f["note_date"] == "2020-06-15"
        assert f["suggested_ref"] == "imessage:guid-a:1_c001"
        # a better chunk exists (a real mis-resolve, with a fix) -> high-confidence BUG
        assert f["kind"] == "bug"
        assert f["suggested_ref"] is not None

    def test_out_of_range_with_no_better_chunk_flagged_with_null_suggestion(self, sandbox):
        _touch(sandbox, "_sources/imessage/2__c000.txt")
        _write_ledger(sandbox, "imessage-run.jsonl", [
            {"chunk_id": "2_c000", "chat_id": 2, "guid": "guid-b",
             "fp": "_sources/imessage/2__c000.txt", "date_start": "2018-01-01", "date_end": "2018-12-31"},
        ])
        # only chunk of guid-b is 2018, but the note is dated 2021 — no chunk of this guid covers it.
        _write_index(sandbox, [
            _note_rec("personal/c.md", "imessage", "imessage:guid-b:2_c000", "2021-01-01"),
        ])
        findings = C.scan(str(sandbox))
        assert len(findings) == 1
        f = findings[0]
        assert f["path"] == "personal/c.md"
        assert f["suggested_ref"] is None
        assert f["suggested_chunk"] is None
        # note dated AFTER the chunk's last message, no better chunk of this guid covers it ->
        # (F7 fix) advisory, not a bug — the only bug signal is a better chunk actually existing.
        assert f["kind"] == "advisory"

    def test_before_range_with_no_better_chunk_is_advisory_not_bug(self, sandbox):
        _touch(sandbox, "_sources/imessage/3__c000.txt")
        _write_ledger(sandbox, "imessage-run.jsonl", [
            {"chunk_id": "3_c000", "chat_id": 3, "guid": "guid-c",
             "fp": "_sources/imessage/3__c000.txt", "date_start": "2019-06-01", "date_end": "2019-12-31"},
        ])
        # note dated BEFORE the chunk's first message (a retrospective note — texting now about a
        # years-earlier event), and no other chunk of this guid covers it either. Per the F7 fix this
        # is advisory in BOTH directions (before-start and after-end) when no better chunk exists —
        # only a better chunk actually containing the date is a bug signal.
        _write_index(sandbox, [
            _note_rec("personal/e.md", "imessage", "imessage:guid-c:3_c000", "2015-01-01"),
        ])
        findings = C.scan(str(sandbox))
        assert len(findings) == 1
        f = findings[0]
        assert f["path"] == "personal/e.md"
        assert f["kind"] == "advisory"
        assert f["suggested_ref"] is None


class TestCrossSourceScoping:
    def test_whatsapp_ref_does_not_resolve_to_colliding_imessage_chunk_id(self, sandbox):
        # imessage chunk_id "5_c000" is IN range for the note's date; a numerically-identical
        # whatsapp chunk_id "5_c000" is OUT of range. A source-scoping bug would let the whatsapp
        # ref resolve to the imessage file (glob sorts imessage-run.jsonl before whatsapp-run.jsonl)
        # and wrongly conclude "in range, don't flag."
        _touch(sandbox, "_sources/imessage/5__c000.txt")
        _touch(sandbox, "_sources/whatsapp/5__c000.txt")
        _write_ledger(sandbox, "imessage-run.jsonl", [
            {"chunk_id": "5_c000", "chat_id": 5, "guid": "imsg-guid",
             "fp": "_sources/imessage/5__c000.txt", "date_start": "2020-01-01", "date_end": "2020-12-31"},
        ])
        _write_ledger(sandbox, "whatsapp-run.jsonl", [
            {"chunk_id": "5_c000", "chat_id": 5, "guid": "1234567890@s.whatsapp.net",
             "fp": "_sources/whatsapp/5__c000.txt", "date_start": "2022-01-01", "date_end": "2022-12-31"},
        ])
        _write_index(sandbox, [
            _note_rec("personal/d.md", "whatsapp", "whatsapp:1234567890@s.whatsapp.net:5_c000",
                     "2020-06-15"),
        ])
        findings = C.scan(str(sandbox))
        # the whatsapp chunk's real range is 2022 — 2020-06-15 is out of range, so it MUST be
        # flagged (a source-scoping bug would instead resolve it to the imessage chunk, which DOES
        # cover 2020-06-15, and wrongly skip it).
        assert len(findings) == 1
        assert findings[0]["path"] == "personal/d.md"
        assert findings[0]["resolved_chunk"] == "_sources/whatsapp/5__c000.txt"

    def test_resolve_chunk_fp_scopes_by_source_directly(self, sandbox):
        _touch(sandbox, "_sources/imessage/5__c000.txt")
        _touch(sandbox, "_sources/whatsapp/5__c000.txt")
        _write_ledger(sandbox, "imessage-run.jsonl", [
            {"chunk_id": "5_c000", "chat_id": 5, "guid": "imsg-guid",
             "fp": "_sources/imessage/5__c000.txt", "date_start": "2020-01-01", "date_end": "2020-12-31"},
        ])
        _write_ledger(sandbox, "whatsapp-run.jsonl", [
            {"chunk_id": "5_c000", "chat_id": 5, "guid": "1234567890@s.whatsapp.net",
             "fp": "_sources/whatsapp/5__c000.txt", "date_start": "2022-01-01", "date_end": "2022-12-31"},
        ])
        imsg_fp = A.resolve_chunk_fp("imessage:imsg-guid:5_c000")
        wa_fp = A.resolve_chunk_fp("whatsapp:1234567890@s.whatsapp.net:5_c000")
        assert imsg_fp == "_sources/imessage/5__c000.txt"
        assert wa_fp == "_sources/whatsapp/5__c000.txt"
        assert imsg_fp != wa_fp

#!/usr/bin/env python3
"""Deterministic ref-integrity checker for chat-DB notes (iMessage / WhatsApp).

Catches a note whose `provenance.ref` points at a chunk that CANNOT contain its content. The
measured failure mode (2026-07-20 audit): an incremental-era write emitted the LEGACY SHORT ref form
`imessage:<guid>:c<n>`, which by construction can only resolve to backfill chunk 0 — so a 2026 note
ends up pointing at a 2017-2019 chunk. It is invisible to the lint's dup-ref check because chat-DB
refs are dup-ref-EXEMPT (one chunk legitimately yields many notes). The signal here is orthogonal and
deterministic: a note's `date` (drawn from the source message timestamps by the distiller) must fall
INSIDE the date range of the chunk its ref resolves to.

For each flagged note it also searches the SAME guid's other chunks for the one whose date range DOES
contain the note date — that both confirms the misref (the content lives in a real, different chunk)
and yields the fix (`--set provenance.ref=...` for edit_note_frontmatter.py).

    python3 scripts/check_ref_integrity.py [--json] [--root <path>]

INSTRUMENT, advisory — like dossier_drift.py it reports and never mutates. It is wired into check.sh
as an INFORMATIONAL line (never gates): the heuristic can in principle flag a legitimately
retrospective note (dated to an event earlier than the conversation that recorded it), so a human
confirms each before fixing via the guarded writer. Reuses audit_sample.resolve_chunk_fp (the single
owner of ref->chunk resolution) and the run-ledger date ranges.
"""
import argparse
import glob
import json
import os
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)

import audit_sample as A                                          # noqa: E402
from _lib.imsg_markers import chunk_ref                           # noqa: E402

CHAT_DB_SOURCES = {"imessage", "whatsapp"}


def chunk_ranges(root):
    """Every chat-DB chunk's date range + guid, from the run ledgers. Returns
    {fp: {"guid","start","end","chunk_id"}} and, per guid, the list of its chunks."""
    by_fp, by_guid = {}, {}
    for fn in sorted(glob.glob(os.path.join(root, "_sources", "*run*.jsonl"))):
        if A._ledger_source(os.path.basename(fn)) is None:   # one owner of the chat-DB-ledger decision
            continue
        with open(fn, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fp, guid = r.get("fp"), r.get("guid")
                s, e, cid = r.get("date_start"), r.get("date_end"), r.get("chunk_id")
                if not (fp and guid and s and e):
                    continue
                rec = {"guid": guid, "start": s, "end": e, "chunk_id": cid, "fp": fp}
                by_fp.setdefault(fp, rec)
                by_guid.setdefault(guid, []).append(rec)
    return by_fp, by_guid


def find_containing_chunk(by_guid, guid, date):
    """A chunk of `guid` whose [start,end] contains `date` (prefer a BACKFILL chunk `<chat>_c<nnn>`
    over an incremental one, matching how a legacy ref is meant to resolve). None if no match."""
    hits = [c for c in by_guid.get(guid, []) if c["start"] <= date <= c["end"]]
    if not hits:
        return None
    hits.sort(key=lambda c: ("incr" in (c["chunk_id"] or ""), c["chunk_id"] or ""))
    return hits[0]


def scan(root):
    # audit_sample os.chdir()s to ITS repo root at import, and load_index/resolve_chunk_fp read
    # CWD-relative — so without this, a `--root <other>` would read other's chunk ledgers here while
    # resolving refs against THIS repo, skipping every note and printing a false "0 bugs". Align CWD
    # to root (and drop the possibly-other-repo resolver cache) for the duration, then restore both so
    # scan() has no lasting side effect on the caller's CWD or audit_sample's memo.
    prev_cwd = os.getcwd()
    os.chdir(os.path.abspath(root))
    A._chunk_runs.cache = None
    try:
        return _scan_here(root)
    finally:
        os.chdir(prev_cwd)
        A._chunk_runs.cache = None


def _scan_here(root):
    """The scan body, run with CWD == root (see scan)."""
    by_fp, by_guid = chunk_ranges(root)
    findings = []
    for r in A.load_index():
        if r.get("source") not in CHAT_DB_SOURCES:
            continue
        ref, date = r.get("ref"), r.get("date")
        if not (ref and date):
            continue
        fp = A.resolve_chunk_fp(ref)
        rng = by_fp.get(fp) if fp else None
        if not rng:
            continue                                    # unresolvable chunk is a separate concern (audit)
        if rng["start"] <= date <= rng["end"]:
            continue                                    # in range — fine
        head = ref.rsplit(":", 1)[0]                     # '<source>:<guid>' (guids carry no ':')
        guid = head.split(":", 1)[1] if ":" in head else ""   # guard a malformed single-colon ref
        better = find_containing_chunk(by_guid, guid, date)
        suggested = None
        if better and better["fp"] != fp:
            suggested = chunk_ref(guid, better["chunk_id"], source=r["source"])
        # Classify. The ONLY high-confidence bug signal is a better chunk of the same guid actually
        # containing the date (a real mis-resolve, with a fix). A note dated OUTSIDE its chunk's range
        # with no better chunk is advisory, both directions: dated AFTER the last message = a
        # prospective event discussed in-chunk (a wedding held later); dated BEFORE the first message =
        # a retrospective note (texting now about a years-past event, dated to the event). The distiller
        # dates by the event, not the message timestamp, so neither is a defensible "the ref is wrong"
        # — flagging a correct retrospective note as a BUG would invite a human to corrupt a good ref.
        is_bug = bool(suggested)
        findings.append({
            "path": r["path"], "note_date": date, "ref": ref,
            "resolved_chunk": os.path.relpath(fp, root),
            "chunk_range": f'{rng["start"]}..{rng["end"]}',
            "suggested_ref": suggested,
            "suggested_chunk": os.path.relpath(better["fp"], root) if better else None,
            "kind": "bug" if is_bug else "advisory",
        })
    findings.sort(key=lambda f: (f["kind"] != "bug", f["suggested_ref"] is None, f["path"]))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flag chat-DB notes whose provenance ref points at a "
                                             "chunk that cannot contain the note's date.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=os.path.dirname(_SCRIPTS))
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)

    findings = scan(root)
    if a.json:
        print(json.dumps(findings, indent=2, ensure_ascii=False))
        return 0

    bugs = [f for f in findings if f["kind"] == "bug"]
    advisory = [f for f in findings if f["kind"] == "advisory"]
    # Headline foregrounds the ACTIONABLE count (real ref bugs); the advisory tail is expected noise
    # (notes dated to a future event discussed in-chunk) and is summarized, not itemized alarmingly.
    print(f"ref-integrity: {len(bugs)} likely ref bug(s)"
          + (f" + {len(advisory)} advisory (note dated outside chunk range — a prospective or "
             f"retrospective event referenced in-chunk)" if advisory else "") + ".")
    for f in bugs:
        print(f"  BUG {f['path']}")
        print(f"      date {f['note_date']} vs chunk {f['resolved_chunk']} ({f['chunk_range']})")
        print(f"      -> content is in {f['suggested_chunk']}; fix: provenance.ref={f['suggested_ref']}")
    for f in advisory:
        print(f"  advisory {f['path']} (date {f['note_date']} vs {f['resolved_chunk']} {f['chunk_range']})")
    if bugs:
        print("\nConfirm each BUG by reading the note against the suggested chunk, then fix the ref with "
              "scripts/edit_note_frontmatter.py (a provenance.ref change is frozen — the guarded writer "
              "records the sanctioned override). Advisory items are usually correct as-is.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

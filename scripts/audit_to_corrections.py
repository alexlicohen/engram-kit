#!/usr/bin/env python3
"""audit_to_corrections.py — extract FABRICATION verdicts from an /engram-audit fan-out's JSON into
the corrections ledgers (Feature C, plan generic-bubbling-eagle.md).

Reads the auditor JSON (a list of records, each shaped
`{path, source, verdict, issues:[{severity, note_claim, source_says, kind}], one_line}` — see
`.claude/skills/engram-audit/SKILL.md`, "Auditor instruction template"). For EVERY record with
`verdict == "FABRICATION"` it appends one correction via `log_correction.py`, auto-populating
id/date/source/kind/note_path/audit_report/finding. `rule_text` stays null and status/applied_to
start open/[] — the prompt-hardening half is a deliberate human/engineer step, never automated here.

    python3 scripts/audit_to_corrections.py --in verdicts.json \
        --audit-report _manifest/audit-reports/<date>.md
    cat verdicts.json | python3 scripts/audit_to_corrections.py \
        --audit-report _manifest/audit-reports/<date>.md

Idempotent: log_correction dedupes on the deterministic id, so re-running on the same JSON adds
nothing new. Only the sources in `log_correction.VALID_SOURCES` (gmail/granola/imessage/outlook/
whatsapp) have ledgers; a FABRICATION on any other source is skipped with a note (no ledger to
write to). The source is matched case-insensitively after stripping, so an auditor that returns
"Gmail" / " outlook" lands in the ledger instead of being skipped as unknown.

Exit codes: 0 = ok, 2 = unreadable input, 3 = EVERY record was skipped and nothing was logged
(an all-skipped run is a wiring failure — a source typo, or a fan-out that reported no `path` —
and must never read as a clean run that simply had nothing to do).
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for log_correction
from log_correction import (build_entry, log_correction, VALID_KINDS, VALID_SOURCES,  # noqa: E402
                            ROOT)

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def map_kind(raw):
    """Map an auditor-reported issue `kind` string into the closed ledger taxonomy (VALID_KINDS)."""
    k = (raw or "").strip().lower()
    if k in VALID_KINDS:
        return k
    # Order matters: the more specific classes are tested BEFORE the broad ones they would fall into.
    if "owner" in k or "reversal" in k:
        return "owner-reversal"
    # BEFORE the false-resolved branch (2026-09-05): every "unresolved"/"false-unresolved" string
    # CONTAINS "resolv", so the broad branch below swallowed the mirror class whole and the ledger
    # recorded the exact opposite defect. Keep this test first, and keep it narrower than the one
    # below (match the "unresolv"/"unanswered"/"still open" stems only).
    if "unresolv" in k or "false-unres" in k or "unanswered" in k or "still open" in k:
        return "false-unresolved"
    if "resolv" in k or "false-res" in k:            # false-resolved / falsely resolved / written up as done
        return "false-resolved"
    if "figure" in k or "amount" in k or "number" in k:
        return "fabricated-figure"
    if "date" in k:
        return "inferred-date"
    if "anchor" in k:
        return "over-anchoring"
    if any(t in k for t in ("attribut", "speaker", "categ", "label", "misattrib", "who")):
        return "misattribution"
    if "gap" in k or "fill" in k or "invent" in k:   # broad: only after the specific classes above
        return "gap-filling"
    return "other"


def _choose_issue(record):
    """Pick the issue that best represents the fabrication: the first one flagged severity
    FABRICATION, else the first issue (or {} if none)."""
    issues = record.get("issues") or []
    for iss in issues:
        if str(iss.get("severity", "")).strip().upper() == "FABRICATION":
            return iss
    return issues[0] if issues else {}


def _finding_text(record, issue):
    """Human-readable finding: prefer the auditor's one_line, else compose from the issue."""
    one = (record.get("one_line") or "").strip()
    if one:
        return one
    nc = (issue.get("note_claim") or "").strip()
    ss = (issue.get("source_says") or "").strip()
    if nc and ss:
        return "%s — source says: %s" % (nc, ss)
    return nc or ss or "FABRICATION flagged by fidelity audit (no detail provided)"


def date_from_report(audit_report):
    """Derive the audit date from an `audit-reports/<YYYY-MM-DD>.md` basename, else None."""
    if not audit_report:
        return None
    stem = os.path.splitext(os.path.basename(audit_report))[0]
    return stem if _ISO_RE.match(stem) else None


def convert(records, audit_report=None, date=None, root=ROOT):
    """Log every FABRICATION record; return (logged_ids, dup_ids, skipped[(path, reason)])."""
    logged, dups, skipped = [], [], []
    for rec in records:
        if str(rec.get("verdict", "")).strip().upper() != "FABRICATION":
            continue
        # Case-folded (2026-09-05): VALID_SOURCES is lowercase and the auditor's `source` is free
        # text an LLM fills in from the note's frontmatter — "Gmail"/"Outlook"/" imessage" were all
        # silently skipped as "no corrections ledger", i.e. a real FABRICATION was dropped on a
        # capital letter. The normalized value is what gets WRITTEN, so the ledger stays lowercase.
        source = (rec.get("source") or "").strip().lower()
        note_path = (rec.get("path") or "").strip()
        if not note_path:
            skipped.append((note_path, "record has no `path`"))
            continue
        if source not in VALID_SOURCES:
            skipped.append((note_path, "source %r has no corrections ledger" % source))
            continue
        issue = _choose_issue(rec)
        entry = build_entry(source, map_kind(issue.get("kind")), note_path,
                            _finding_text(rec, issue), date=date, audit_report=audit_report)
        if log_correction(entry, root=root):
            logged.append(entry["id"])
        else:
            dups.append(entry["id"])
    return logged, dups, skipped


def main():
    ap = argparse.ArgumentParser(
        description="Extract FABRICATION verdicts from an /engram-audit JSON into the corrections ledgers.")
    ap.add_argument("--in", dest="infile", help="audit JSON file (omit to read stdin)")
    ap.add_argument("--audit-report", help="the audit-reports/<date>.md this JSON came from "
                    "(sets the entries' audit_report field + default date)")
    ap.add_argument("--date", help="override the audit date YYYY-MM-DD "
                    "(default: derived from --audit-report basename, else today)")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    raw = open(args.infile, encoding="utf-8").read() if args.infile else sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError as e:
        print("ERROR: input is not valid JSON: %s" % e, file=sys.stderr)
        sys.exit(2)
    # accept a bare list, or a wrapper object exposing the list under a common key.
    if isinstance(data, dict):
        data = data.get("results") or data.get("verdicts") or data.get("records") or []
    if not isinstance(data, list):
        print("ERROR: expected a JSON list of audit records", file=sys.stderr)
        sys.exit(2)

    date = args.date or date_from_report(args.audit_report)
    logged, dups, skipped = convert(data, audit_report=args.audit_report, date=date, root=root)

    for eid in logged:
        print("LOGGED %s" % eid)
    for eid in dups:
        print("DUP    %s" % eid)
    for path, why in skipped:
        print("SKIP   %s (%s)" % (path, why), file=sys.stderr)
    print("summary: %d logged, %d dup, %d skipped (of %d FABRICATION verdicts)"
          % (len(logged), len(dups), len(skipped), len(logged) + len(dups) + len(skipped)))
    # An ALL-SKIPPED run is a failure, not a quiet success (2026-09-05). Every FABRICATION the audit
    # found was dropped — a `source` this loop doesn't know, or records with no `path` — so the audit
    # cost was paid and the ledger learned nothing. Exiting 0 made that indistinguishable from a
    # clean audit with no findings, and the SKIP lines go to stderr where a wrapper often drops them.
    if not logged and not dups and skipped:
        print("ERROR: ALL %d FABRICATION record(s) were SKIPPED and nothing was logged — the "
              "corrections ledger learned NOTHING from this audit. Check the `source` values in the "
              "input against log_correction.VALID_SOURCES %r and that every record carries a `path` "
              "(reasons listed above)." % (len(skipped), tuple(VALID_SOURCES)), file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()

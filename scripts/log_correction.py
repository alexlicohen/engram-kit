#!/usr/bin/env python3
"""log_correction.py — the ONLY writer to the fidelity-audit corrections ledgers
`_manifest/corrections/{gmail,granola,imessage,outlook,whatsapp}.jsonl` (Feature C, plan
generic-bubbling-eagle.md).

Append-only JSONL, one correction per line, modeled on `_manifest/clarify-queue.jsonl`. Each entry
records a fidelity-audit FABRICATION so the NEXT sync's distiller prompt can carry it as an extra
adversarial reminder (`scripts/corrections_brief.py` renders the OPEN ones; each distiller's Setup
agent folds them into `distillPrompt()`). `rule_text` (the permanent prompt-hardening sentence) and
flipping `status` -> `applied` stay a human/engineer step — this writer never sets them.

Usable two ways:
  * importable:  from log_correction import log_correction, build_entry, VALID_KINDS, ledger_path
  * CLI:         python3 scripts/log_correction.py --source granola --kind owner-reversal \
                     --note-path research/....md --finding "..." \
                     --audit-report _manifest/audit-reports/<date>.md

Validates shape (required fields, `kind` in the closed taxonomy, `source` in VALID_SOURCES,
`date` ISO, `status` in VALID_STATUS), REFUSES an entry whose text scores PHI level=high
(scripts/phi_scan.py — the store's single PHI heuristic), and DEDUPES on `id` (idempotent: a second
call with an already-present id is a no-op). Returns True iff a new line was appended.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for phi_scan
from phi_scan import scan as phi_scan   # noqa: E402  (single owner of the PHI heuristic — never re-implement)

# The sources that HAVE a corrections ledger. ONE owner — audit_to_corrections.py,
# corrections_brief.py and corrections_lint.py import this; never re-hardcode a list.
# outlook added 2026-09-03; whatsapp added 2026-09-05 (it is a DISTILLED source with its own
# FIDELITY RULES block in _whatsapp-pending/, so its audit findings had nowhere to land).
VALID_SOURCES = ("gmail", "granola", "imessage", "outlook", "whatsapp")

# Closed taxonomy — the distiller FIDELITY RULES failure classes (see the *_distill.mjs prompts
# and .claude/skills/engram-audit/SKILL.md "What fidelity defects look like"). audit_to_corrections.py
# maps each auditor-reported issue kind into exactly one of these.
VALID_KINDS = (
    "over-anchoring",   # imported an external specific, or anchored the note to a wrong subject (rules 1 & 4)
    "inferred-date",    # computed/guessed a date the source never states (rule 2)
    "misattribution",   # speaker/category misattribution: who said/decided it, the exact label/category (rule 3)
    "owner-reversal",   # action-item OWNER flipped (granola lab-meeting sub-class of rule 3)
    # The two classes below were previously swept into "other". Both are now NAMED rules in the
    # distiller prompts (gmail rules 1 & 8, imessage rules 1 & 4), measured across the 2026-07-19
    # first large audit pass (25 false-resolved defects) and the chat-DB pass that followed, so the
    # ledger needs to distinguish them — "other" hid the store's second-largest defect class.
    "false-resolved",   # a plan/proposal/question written up as decided, done, or answered
    # The MIRROR of false-resolved (added 2026-09-05, seeded by the outlook back-catalog audit): the
    # source DID deliver the result / answer the question / complete the step, and the note writes it
    # up as still open, pending, or unanswered. It is a distinct defect — false-resolved invents
    # closure, this one erases it (a delivered number becomes an open action item) — and collapsing
    # the two hid it, since every "unresolved" string fell into the false-resolved branch.
    "false-unresolved",  # a delivered result / answered question / completed step written up as still open
    "gap-filling",      # the source is silent and the note supplies a plausible specific
    "fabricated-figure",  # an invented/derived number the source never states (gmail rule 7; 71 in the EF pass)
    "other",            # a real fabrication that doesn't fit the classes above
)

# `stale` (2026-09-05) = an OPEN entry aged out by `corrections_brief.py --older-than <date>`: it
# stops injecting into distiller prompts without ever being deleted (the ledger is append-only in
# spirit — a line is rewritten in place, never dropped) and without claiming the prompt was hardened,
# which is what `applied` asserts.
VALID_STATUS = ("open", "applied", "stale")

REQUIRED_FIELDS = ("id", "date", "source", "kind", "note_path", "finding")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def ledger_path(source, root=ROOT):
    return os.path.join(root, "_manifest", "corrections", "%s.jsonl" % source)


def _note_token(note_path):
    """Stable per-note token for the id: the last hyphen-delimited segment of the filename stem.
    Every distiller bakes a collision-proof unique suffix there (gmail `<thrid_last6>`, granola
    `g<id8>`, imsg `im<chat>-<h>`), so this is note-unique by construction. Falls back to a short
    sha1 of the whole path when the stem has no hyphen (e.g. a hand-authored note)."""
    stem = os.path.splitext(os.path.basename(note_path or ""))[0]
    token = stem.rsplit("-", 1)[-1] if "-" in stem else stem
    token = _TOKEN_RE.sub("", token.lower())
    if not token:
        token = hashlib.sha1((note_path or "").encode("utf-8")).hexdigest()[:8]
    return token


def make_id(date, note_path, kind):
    """Deterministic, idempotent correction id: `audit-<date>-<note-token>-<kind>`. Same inputs
    always produce the same id, so re-running the extractor on the same audit never duplicates.

    `source` was a parameter until 2026-09-05 and was never used in the id (each source has its own
    ledger file, so the id only has to be unique WITHIN one). Dropped rather than folded in, so every
    existing id stays byte-identical and dedup keeps holding across the change."""
    return "audit-%s-%s-%s" % (date, _note_token(note_path), kind)


def build_entry(source, kind, note_path, finding, date=None, audit_report=None,
                entry_id=None, status="open", applied_to=None, rule_text=None):
    """Assemble a correction entry with the canonical key order/shape (does NOT validate — call
    log_correction() or validate_entry() for that)."""
    date = date or datetime.date.today().isoformat()
    eid = entry_id or make_id(date, note_path, kind)
    return {
        "id": eid,
        "date": date,
        "source": source,
        "kind": kind,
        "note_path": note_path,
        "audit_report": audit_report,
        "finding": finding,
        "rule_text": rule_text,
        "status": status,
        "applied_to": list(applied_to) if applied_to is not None else [],
    }


# Free-text fields an auditor (an LLM quoting a source thread) fills in — the only place clinical
# patient PHI could reach a COMMITTED file through this loop. `note_path` is included because an
# over-anchoring finding is frequently about a name/date that also ended up in the slug.
PHI_SCANNED_FIELDS = ("finding", "rule_text", "note_path")


def phi_check(entry):
    """Run the store's PHI heuristic (scripts/phi_scan.scan) over the entry's free text. Returns the
    scan result dict. Callers refuse on level == "high" — see validate_entry."""
    return phi_scan("\n".join(str(entry.get(f) or "") for f in PHI_SCANNED_FIELDS))


def validate_entry(entry):
    """Raise ValueError unless `entry` is a well-formed, PHI-free correction. Returns the entry."""
    for f in REQUIRED_FIELDS:
        if not entry.get(f):
            raise ValueError("correction entry missing required field %r: %r" % (f, entry))
    if entry["source"] not in VALID_SOURCES:
        raise ValueError("source %r not in %r" % (entry["source"], VALID_SOURCES))
    if entry["kind"] not in VALID_KINDS:
        raise ValueError("kind %r not in the closed taxonomy %r" % (entry["kind"], VALID_KINDS))
    if not _ISO_RE.match(str(entry["date"])):
        raise ValueError("date %r must be YYYY-MM-DD" % (entry["date"],))
    if entry.get("status", "open") not in VALID_STATUS:
        raise ValueError("status %r must be one of %r" % (entry.get("status"), VALID_STATUS))
    if entry.get("applied_to") is not None and not isinstance(entry["applied_to"], list):
        raise ValueError("applied_to must be a list, got %r" % (entry["applied_to"],))
    # PHI gate (2026-09-05). A correction quotes the SOURCE ("source says: ...") — i.e. raw email /
    # transcript / message text — into a file that is COMMITTED, and applies to every source, not
    # just the clinical-adjacent ones (a school-letter PHI class arrived from a non-MED sender).
    # Biased to recall exactly like the ingest-side screen: a refused ledger line costs one re-word,
    # a committed one is in git history forever. The message names only WHICH signals fired —
    # phi_scan never returns the matched substring, and this must never echo the finding text.
    ph = phi_check(entry)
    if ph["level"] == "high":
        raise ValueError(
            "REFUSED: correction %r scores PHI level=high (signals %r, ctx=%d acad=%d) — engram's one "
            "hard rule is no clinical-patient PHI in the store, and a corrections ledger is committed. "
            "Re-word the finding to describe the DEFECT without the identifying detail (the note path "
            "+ kind are enough for the distiller to learn from), or drop the entry."
            % (entry.get("id"), ph["strong"], ph["ctx"], ph["acad"]))
    return entry


def iter_ledger(source, root=ROOT):
    """Yield (lineno, entry) for every non-empty line of the source's ledger (nothing if the file
    doesn't exist). RAISES ValueError on a malformed line.

    A malformed line used to be `continue`d here and in corrections_brief.render_open — which is the
    worst possible handling for a DEDUP key: a half-written line silently drops its id from
    existing_ids, so the next run appends a duplicate of an entry that is already there, and the
    brief silently under-injects. The ledger is small and machine-written; a line that does not parse
    is a bug or a bad merge, and must stop the caller. `scripts/corrections_lint.py` (wired into
    check.sh) is the gate that catches it before a distiller launch does."""
    p = ledger_path(source, root)
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError as e:
                raise ValueError("MALFORMED corrections ledger %s line %d: not valid JSON (%s)"
                                 % (p, lineno, e))
            if not isinstance(entry, dict):
                raise ValueError("MALFORMED corrections ledger %s line %d: expected a JSON object, "
                                 "got %s" % (p, lineno, type(entry).__name__))
            if not entry.get("id"):
                raise ValueError("MALFORMED corrections ledger %s line %d: missing `id` (the dedup "
                                 "key)" % (p, lineno))
            yield lineno, entry


def existing_ids(source, root=ROOT):
    """Set of `id`s already present in the source's ledger (empty if the file doesn't exist).
    Raises ValueError on a malformed line — see iter_ledger."""
    return {e["id"] for _, e in iter_ledger(source, root)}


def log_correction(entry, root=ROOT):
    """Validate -> dedupe-on-id -> append one correction line. Returns True iff a new line was
    appended (False = the id already existed, a no-op). The ONLY sanctioned ledger writer."""
    validate_entry(entry)
    source = entry["source"]
    if entry["id"] in existing_ids(source, root):
        return False
    p = ledger_path(source, root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def mark_stale_before(source, cutoff, root=ROOT):
    """Flip every `open` entry DATED STRICTLY BEFORE `cutoff` (YYYY-MM-DD) to status `stale`.
    Returns the list of ids flipped. Rewrites the ledger in place, atomically — every line is kept
    (a correction is NEVER deleted, exactly like a note); only the `status` value changes.

    Lives here, not in corrections_brief.py, because this file is the ONE owner of ledger writes;
    `corrections_brief.py --older-than` is only the CLI surface for it."""
    if not _ISO_RE.match(str(cutoff)):
        raise ValueError("cutoff %r must be YYYY-MM-DD" % (cutoff,))
    p = ledger_path(source, root)
    if not os.path.exists(p):
        return []
    flipped, out = [], []
    for _, entry in iter_ledger(source, root):
        if entry.get("status") == "open" and str(entry.get("date", "")) < cutoff:
            entry["status"] = "stale"
            flipped.append(entry["id"])
        out.append(json.dumps(entry, ensure_ascii=False) + "\n")
    if flipped:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(out)
        os.replace(tmp, p)
    return flipped


def main():
    ap = argparse.ArgumentParser(
        description="Append one fidelity-audit correction (the only corrections-ledger writer).")
    ap.add_argument("--source", required=True, choices=VALID_SOURCES)
    ap.add_argument("--kind", required=True, choices=VALID_KINDS)
    ap.add_argument("--note-path", required=True)
    ap.add_argument("--finding", required=True)
    ap.add_argument("--date", help="audit date YYYY-MM-DD (default: today)")
    ap.add_argument("--audit-report", help="path to the audit-reports/<date>.md this came from")
    ap.add_argument("--id", help="override the deterministic id (rarely needed)")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    entry = build_entry(args.source, args.kind, args.note_path, args.finding,
                        date=args.date, audit_report=args.audit_report, entry_id=args.id)
    appended = log_correction(entry, root=root)
    verb = "LOGGED" if appended else "DUP (no-op)"
    print("%s %s -> %s" % (verb, entry["id"], ledger_path(args.source, root)))


if __name__ == "__main__":
    main()

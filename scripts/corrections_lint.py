#!/usr/bin/env python3
"""corrections_lint.py — the gate over `_manifest/corrections/*.jsonl` (the fidelity-audit
corrections ledgers). Wired into `scripts/check.sh` next to lint_store.py, fail-fast.

    python3 scripts/corrections_lint.py [--root R] [--source S]

These files are COMMITTED, hand-editable, merge-able, and read at distiller-launch time by
`corrections_brief.py` / `log_correction.existing_ids`. Until 2026-09-05 both readers `continue`d
past a line they could not parse, so a bad merge or a truncated append was invisible: the brief
silently under-injected and the dedup key silently vanished (making the NEXT audit append a
duplicate). The readers now raise; this is the gate that makes them raise BEFORE a sync run does.

Checks, per ledger file:
  * every non-empty line parses as a JSON object and has an `id`   (log_correction.iter_ledger)
  * the entry is well-formed AND PHI-clean                          (log_correction.validate_entry)
  * `source` == the filename stem (a gmail finding filed in outlook.jsonl never reaches the gmail
    distiller's brief — it is invisible, not merely misplaced)
  * `kind` ∈ VALID_KINDS, `status` ∈ VALID_STATUS                   (validate_entry; closed sets)
  * ids are unique WITHIN the file (a duplicate id defeats dedup: log_correction would refuse the
    second write, so a duplicate means something wrote around the sanctioned writer)
  * an UNKNOWN ledger file in _manifest/corrections/ (a source not in VALID_SOURCES) is an error —
    it is a ledger nothing renders, i.e. findings written to a file no distiller will ever read.

Exit 0 = clean (prints one summary line), 1 = at least one error (each printed as ERROR: <file>[:<line>]).
Single owner: every rule above is VALID_SOURCES / VALID_KINDS / VALID_STATUS / validate_entry from
log_correction.py — this file re-implements none of them.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path
from log_correction import (iter_ledger, ledger_path, validate_entry,   # noqa: E402
                            VALID_SOURCES, ROOT)


def lint_source(source, root=ROOT):
    """Return a list of error strings for one source's ledger ([] = clean)."""
    p = ledger_path(source, root)
    rel = os.path.relpath(p, root)
    errors, seen = [], {}
    try:
        entries = list(iter_ledger(source, root))
    except ValueError as e:
        return ["ERROR: %s" % e]            # already carries file + line number
    for lineno, entry in entries:
        where = "%s:%d" % (rel, lineno)
        if entry.get("source") != source:
            errors.append("ERROR: %s: source %r != ledger file stem %r — this correction will never "
                          "reach a distiller brief (corrections_brief renders per-file)."
                          % (where, entry.get("source"), source))
        try:
            validate_entry(entry)
        except ValueError as e:
            errors.append("ERROR: %s: %s" % (where, e))
        eid = entry.get("id")
        if eid in seen:
            errors.append("ERROR: %s: duplicate id %r (first seen at line %d) — dedup is keyed on "
                          "`id`, so a duplicate means a writer bypassed log_correction.py."
                          % (where, eid, seen[eid]))
        else:
            seen[eid] = lineno
    return errors


def lint_all(root=ROOT):
    """Lint every ledger + flag stray files in _manifest/corrections/. Returns (errors, counts)."""
    errors, counts = [], {}
    for source in VALID_SOURCES:
        errs = lint_source(source, root)
        errors.extend(errs)
        try:
            counts[source] = len(list(iter_ledger(source, root)))
        except ValueError:
            counts[source] = -1          # unreadable; `errs` already carries the error
    d = os.path.join(root, "_manifest", "corrections")
    if os.path.isdir(d):
        known = {"%s.jsonl" % s for s in VALID_SOURCES}
        for name in sorted(os.listdir(d)):
            if name.endswith(".jsonl") and name not in known:
                errors.append("ERROR: _manifest/corrections/%s: no such source in "
                              "log_correction.VALID_SOURCES %r — nothing renders this ledger, so "
                              "every finding in it is invisible to every distiller."
                              % (name, tuple(VALID_SOURCES)))
    return errors, counts


def main():
    ap = argparse.ArgumentParser(description="Lint the fidelity-audit corrections ledgers.")
    ap.add_argument("--source", choices=VALID_SOURCES, help="lint just this one ledger")
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    if args.source:
        errors = lint_source(args.source, root)
        counts = {args.source: len(list(iter_ledger(args.source, root)))} if not errors else {}
    else:
        errors, counts = lint_all(root)

    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print("corrections-lint: %d error(s)" % len(errors), file=sys.stderr)
        sys.exit(1)
    print("corrections-lint OK — %d entries across %d ledgers (%s)"
          % (sum(counts.values()), len(counts),
             ", ".join("%s=%d" % (s, n) for s, n in sorted(counts.items()))))


if __name__ == "__main__":
    main()

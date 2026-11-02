#!/usr/bin/env python3
"""Deterministic scan for cross-note contradictions in named-entity ownership/interest
percentages (e.g. "60% of Acme Holdings LLC") -- the defect class where two notes about the
same entity state different ownership shares (originally found by an /engram-eval cold agent cross-referencing
notes, not by any systematic tool -- this script is that tool). Read-only, zero LLM cost.

Finds every (entity, percentage) pair asserted across the store, groups by normalized entity
name, and flags any entity where MORE THAN ONE distinct percentage is asserted. It never edits
notes or decides which value is correct -- a flag is a LEAD for a human or an audit agent to
adjudicate (source-verify before fixing, same convention as /engram-audit).

Scope (v1) -- percentage-of-entity claims only, deliberately. An ownership/interest percentage
for a given entity is expected to be STABLE at a point in time, and legitimate changes are
almost always narrated explicitly ("the minority interest grew to full ownership") rather than silently
overwritten -- so a bare multi-value flag is a high-precision, low-noise signal here. Dollar
amounts were deliberately NOT included: they vary constantly and legitimately (different
transactions, different dates for the same entity), so a naive multi-value flag would be mostly
false positives without date-scoped transaction modeling. Extending to dollar amounts (or other
fact classes) is a real future enhancement, not attempted here -- don't bolt it on without adding
that date/transaction scoping first.

Residual known limitation (found running v1 live against the real store, 2026-07-10, after
tightening the entity regex): a note can legitimately contain TWO DIFFERENT percentage-type facts
near the same entity name (e.g. a loan interest rate AND a fee percentage near the same company name;
a growth rate AND an ownership percentage near the same entity) -- these are not
contradictions, just two distinct facts sharing a sentence. This scan cannot yet distinguish
"same fact, different value" from "different facts, both true" -- every flag needs a quick human/
audit-agent read before being treated as a real bug. A plausible v2 improvement: only pair a
percentage with an entity when an ownership-indicating word (owns/holds/interest/stake/membership/
transfers) appears in the same sentence -- not built here, left as a documented next step.

Entity matching is EXACT-STRING after normalization (lowercase, strip commas/periods, collapse
whitespace) -- it will NOT catch alias variants of the same entity (e.g. "Acme Holdings,
LLC" vs. "Acme") unless they normalize identically. This is a known, deliberate limitation:
fuzzy alias-matching (à la build_clarify_queue.py's looks_like_variant) adds false-positive risk
that isn't worth it for a v1 high-precision tool. A human/agent reviewing a flag can always widen
the search by entity name manually.

Usage:
    python3 scripts/fact_consistency_scan.py                    # scan the whole store
    python3 scripts/fact_consistency_scan.py --domains finance,ventures
    python3 scripts/fact_consistency_scan.py --json              # machine-readable
"""
import argparse
import collections
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from _lib.notes import iter_notes, split_frontmatter  # noqa: E402
from _lib.constants import DOMAINS  # noqa: E402

# A percentage, optionally decimal: "12%", "7.5%"
PCT_RE = re.compile(r'\b(\d{1,3}(?:\.\d+)?)\s?%')

# A proper-noun entity name (2-5 title-case-or-numeric words, e.g. "Acme Fund 2 LLC") ending
# in a common legal-entity suffix. Deliberately strict: EVERY word must start uppercase-or-digit
# (kills generic phrases like "the LLC" or "My LLC"), and at most one comma is allowed, only in
# the position immediately before the suffix (kills false-merging a comma-separated LIST of
# distinct entities, e.g. "Example Credit Union, Sample Federal Credit Union, ... Placeholder
# Trust", into one giant fake "entity"). Both failure modes were found by running v1 of this
# regex live against the real store -- see the two synthetic regression tests for them.
ENTITY_RE = re.compile(
    r"\b((?:[A-Z0-9][A-Za-z0-9&'\-]*\s+){1,4}[A-Z0-9][A-Za-z0-9&'\-]*),?\s+"
    r"(LLC|L\.L\.C\.|Inc\.?|Corp\.?|Corporation|Trust|LP|L\.P\.|Partners|Holdings)\b"
)

# How far (chars) a percentage and an entity mention may be apart in the same sentence to be
# considered associated. Sentences are split first, so this just guards against very long
# run-on sentences pairing a percentage with an unrelated entity mention.
MAX_SENTENCE_SPAN = 400


def normalize_entity(name):
    name = name.strip().rstrip('.').replace(',', '')
    name = re.sub(r'\s+', ' ', name)
    return name.lower()


def sentences(text):
    # Cheap sentence splitter -- good enough for this heuristic; doesn't need to be perfect.
    for chunk in re.split(r'(?<=[.!?])\s+|\n+', text):
        chunk = chunk.strip()
        if chunk:
            yield chunk


def scan_note(path):
    """Yield (entity_normalized, entity_display, percentage_str) for every percentage-near-entity
    pairing found in this note's BODY (frontmatter excluded -- participants/tags aren't prose)."""
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        return
    head, body = split_frontmatter(text)
    if body is None:
        body = text
    for sent in sentences(body):
        if len(sent) > MAX_SENTENCE_SPAN:
            continue
        pcts = list(PCT_RE.finditer(sent))
        ents = list(ENTITY_RE.finditer(sent))
        if not pcts or not ents:
            continue
        for ent_m in ents:
            entity_display = f"{ent_m.group(1).strip()} {ent_m.group(2)}"
            entity_norm = normalize_entity(entity_display)
            for pct_m in pcts:
                yield entity_norm, entity_display, pct_m.group(1)


def scan(root, domains=None):
    tops = domains if domains else DOMAINS
    # entity_norm -> {pct_value -> [(path, entity_display), ...]}
    findings = collections.defaultdict(lambda: collections.defaultdict(list))
    for path in iter_notes(tops):
        full = os.path.join(root, path) if not os.path.isabs(path) else path
        for entity_norm, entity_display, pct in scan_note(full):
            findings[entity_norm][pct].append((path, entity_display))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default=_ROOT)
    ap.add_argument('--domains', default=None, help='comma-separated domain list (default: all)')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    domains = args.domains.split(',') if args.domains else None
    os.chdir(args.root)
    findings = scan(args.root, domains)

    conflicts = {e: v for e, v in findings.items() if len(v) > 1}

    if args.json:
        out = []
        for entity_norm, by_pct in sorted(conflicts.items()):
            out.append({
                'entity': entity_norm,
                'values': {pct: [{'path': p, 'display': d} for p, d in refs]
                           for pct, refs in sorted(by_pct.items())},
            })
        print(json.dumps(out, indent=2))
        return

    if not conflicts:
        print(f"fact-consistency scan: {len(findings)} entities with a percentage claim, "
              f"0 conflicts (all consistent)")
        return

    print(f"fact-consistency scan: {len(findings)} entities checked, "
          f"{len(conflicts)} with CONFLICTING percentages\n")
    for entity_norm, by_pct in sorted(conflicts.items()):
        display = next(iter(next(iter(by_pct.values()))))[1]
        print(f"CONFLICT  {display}  ({len(by_pct)} distinct values)")
        for pct, refs in sorted(by_pct.items()):
            paths = ', '.join(p for p, _ in refs[:3])
            more = f" (+{len(refs)-3} more)" if len(refs) > 3 else ""
            print(f"    {pct}%  ->  {paths}{more}")
        print()
    print("A conflict is a LEAD, not truth -- source-verify before editing any note "
          "(per the /engram-audit convention). This scan never edits notes.")


if __name__ == '__main__':
    main()

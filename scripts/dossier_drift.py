#!/usr/bin/env python3
"""Post-sync dossier freshness check — deterministic, read-only, INFORMATIONAL (never fails).

Automates the manual "is this dossier stale?" scan (dossier drift was the #1
recurring quality-decay mode: a dossier gets synthesized once, then newer notes accumulate in its
domain/entity without the dossier ever being told about them).

For each dossier in dossiers/*.md, compares the dossier's COVERAGE DATE against its ASSOCIATED
notes (from _manifest/index.jsonl) and reports any note that is ingested ON OR AFTER the coverage
date and not already linked from the dossier body.

COVERAGE DATE = max(frontmatter generated:, frontmatter updated: [if present],
                     body "**Current as of:** YYYY-MM-DD" [if present]).

ASSOCIATION — single-owner reuse, two tiers:
  1. Specific (property/FM dossiers): reuse SLUG_DOSSIER from scripts/gen_backlinks.py verbatim
     (imported, not copied) — a note associates to dossier `sd` iff its domain == `sd_dom` and its
     slug contains one of `frags`. This is authoritative for those dossiers.
  2. Coarse (a domain-level one-pager not covered by SLUG_DOSSIER): every note whose `domain` ==
     the dossier's frontmatter `domain`. DOCUMENTED RULE: to avoid a note being double-claimed by
     both an entity dossier and the coarser domain dossier it shares a domain with, coarse
     association EXCLUDES any note that already matches a SLUG_DOSSIER entry (that note's drift
     belongs to its specific dossier, not the domain-level one).

A note counts as "already referenced" if its slug appears in the dossier body either as a
[[wikilink]] or inside a markdown link target ending in "<slug>.md".

Usage:
    python3 scripts/dossier_drift.py           # human-readable report, exit 0 always
    python3 scripts/dossier_drift.py --json     # machine-readable
"""
import glob
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from gen_backlinks import (  # single owner — do NOT paste a copy (see CLAUDE.md)
    SLUG_DOSSIER, DOSSIER_TOPICS, DOMAIN_CATCHALL)

DOSSIER_DIR = os.path.join(_ROOT, "dossiers")
INDEX_PATH = os.path.join(_ROOT, "_manifest", "index.jsonl")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURRENT_AS_OF_RE = re.compile(r"\*\*Current as of:\*\*\s*(\d{4}-\d{2}-\d{2})")


def slug_of(path):
    return os.path.splitext(os.path.basename(path))[0]


def read_frontmatter(text):
    """Return (head_dict, body) for a note/dossier with '---' frontmatter."""
    if not text.startswith("---") or "\n---" not in text[3:]:
        return {}, text
    end = text.index("\n---", 3)
    head_block = text[3:end]
    body = text[end + 4:]
    head = {}
    for m in re.finditer(r"^(\w+):\s*(.*)$", head_block, re.M):
        head[m.group(1)] = m.group(2).strip().strip('"')
    return head, body


def load_index(path):
    """Return list of note dicts from _manifest/index.jsonl (one record per note).

    An ABSENT index means the store has not been indexed yet (a fresh clone, before the first
    `bash scripts/check.sh` regenerates it) — not an error. This tool is INFORMATIONAL and never
    gates, so it reports "nothing associated" instead of crashing, and engram_brief.py, which
    reuses it, keeps working on a fresh clone too.
    """
    if not os.path.exists(path):
        return []
    notes = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            notes.append(json.loads(line))
    return notes


def coverage_date(head, body):
    dates = []
    for key in ("generated", "updated"):
        v = head.get(key, "")
        if DATE_RE.match(v):
            dates.append(v)
    m = CURRENT_AS_OF_RE.search(body)
    if m:
        dates.append(m.group(1))
    return max(dates) if dates else None


def slug_dossier_specific_slugs():
    """Set of dossier slugs that are targets of a SLUG_DOSSIER entry (the specific/entity dossiers)."""
    return {sd for _dom, _frags, sd in SLUG_DOSSIER}


def note_matches_slug_dossier(note):
    """True if `note` (index.jsonl record) matches ANY SLUG_DOSSIER entry (i.e. belongs to a
    specific entity dossier, not the coarser domain one)."""
    sl = slug_of(note["path"]).lower()
    dom = note.get("domain", "")
    for sd_dom, frags, _sd in SLUG_DOSSIER:
        if dom == sd_dom and any(fr in sl for fr in frags):
            return True
    return False


def note_topic_dossier(note):
    """The DOSSIER_TOPICS slug a note routes to by (domain, tags-or-slug-fragment), or None. A
    note routes to the ONE topic dossier its tags (or its slug) select — so it does NOT associate
    to every dossier in its domain."""
    dom = note.get("domain", "")
    tags = set(note.get("tags") or [])
    sl = slug_of(note["path"]).lower()
    for slug, (d_dom, topic_tags, frags) in DOSSIER_TOPICS.items():
        if dom == d_dom and ((tags & topic_tags) or any(fr in sl for fr in frags)):
            return slug
    return None


def associated_notes(dossier_slug, dossier_domain, all_notes):
    """Return list of index.jsonl note records associated with this dossier, most-specific tier first:
      1. SLUG_DOSSIER target  -> only its slug-fragment notes.
      2. DOSSIER_TOPICS dossier -> domain notes whose `tags:` hit its topic set.
      3. DOMAIN_CATCHALL dossier -> domain notes owned by NO SLUG_DOSSIER and NO topic dossier.
      4. plain single-dossier domain -> coarse domain association minus SLUG_DOSSIER notes.
    Tiers 2+3 stop a multi-dossier domain (research) from having every note claim every dossier."""
    # Tier 1: this dossier IS a SLUG_DOSSIER target -> use only its specific entries.
    my_entries = [(sd_dom, frags) for sd_dom, frags, sd in SLUG_DOSSIER if sd == dossier_slug]
    if my_entries:
        out = []
        for note in all_notes:
            sl = slug_of(note["path"]).lower()
            dom = note.get("domain", "")
            if any(dom == sd_dom and any(fr in sl for fr in frags) for sd_dom, frags in my_entries):
                out.append(note)
        return out
    # Tier 2: a topic dossier -> domain notes whose tags or slug-fragment route to it.
    if dossier_slug in DOSSIER_TOPICS:
        d_dom, topic_tags, frags = DOSSIER_TOPICS[dossier_slug]
        return [n for n in all_notes
                if n.get("domain") == d_dom
                and ((set(n.get("tags") or []) & topic_tags)
                     or any(fr in slug_of(n["path"]).lower() for fr in frags))]
    # Tier 3: the domain CATCH-ALL -> domain notes routed to no specific (slug or topic) dossier.
    if DOMAIN_CATCHALL.get(dossier_domain) == dossier_slug:
        return [n for n in all_notes
                if n.get("domain") == dossier_domain
                and not note_matches_slug_dossier(n)
                and note_topic_dossier(n) is None]
    # Tier 4: coarse domain association (single-dossier domains), excluding SLUG_DOSSIER-owned notes.
    return [n for n in all_notes
            if n.get("domain") == dossier_domain and not note_matches_slug_dossier(n)]


def referenced_slugs(body):
    """Slugs already linked in the dossier body: [[slug]] wikilinks and markdown links whose
    target path ends in <slug>.md."""
    refs = set(re.findall(r"\[\[([^\]|#]+)\]\]", body))
    # strip any leading path segments off wikilinks (e.g. [[dossiers/example-project]])
    refs = {r.rsplit("/", 1)[-1] for r in refs}
    for target in re.findall(r"\]\(([^)]+)\)", body):
        target = target.split("#", 1)[0]
        if target.endswith(".md"):
            refs.add(os.path.splitext(os.path.basename(target))[0])
    return refs


def analyze(dossier_path, all_notes):
    text = open(dossier_path, encoding="utf-8").read()
    head, body = read_frontmatter(text)
    dslug = slug_of(dossier_path)
    cov = coverage_date(head, body)
    dom = head.get("domain", "")
    notes = associated_notes(dslug, dom, all_notes)
    refs = referenced_slugs(text)

    def ingest_date(n):
        # STORE-entry date, not the subject/event date: a capture/backfill note about a PAST decision
        # has an old `date:` but a recent `ingested:` — comparing ingest is what makes it drift-visible.
        return n.get("ingested") or n.get("date", "")

    newer_unlinked = []
    for n in notes:
        ndate = ingest_date(n)
        if not DATE_RE.match(ndate):
            continue
        nslug = slug_of(n["path"])
        if nslug in refs:
            continue
        # `>=`, not `>`: a note ingested ON the coverage date is a PERMANENT blind spot under `>`
        # (coverage only moves forward, so a note the reconciliation pass stamped past but never
        # folded can never be flagged again). Linkage — not the date — is what excludes a note that
        # WAS folded, which the `nslug in refs` check above already does.
        if cov is None or ndate >= cov:
            newer_unlinked.append(n)
    newer_unlinked.sort(key=ingest_date, reverse=True)

    return {
        "dossier": os.path.relpath(dossier_path, _ROOT),
        "slug": dslug,
        "domain": dom,
        "coverage_date": cov,
        "associated_count": len(notes),
        "drifted_count": len(newer_unlinked),
        # show INGESTED (what drift compares to coverage) so the report isn't self-contradictory; a
        # captured/backfilled note also gets its event date in parens for context.
        "top5": [{"date": ingest_date(n),
                  "event_date": n.get("date", ""), "path": n["path"]} for n in newer_unlinked[:5]],
        # ALL newer-unlinked paths (most-recent-ingested first) — the reconciliation worklist a
        # dossier-refresh pass reads; --json only (the text report stays to top5 for readability).
        "newer_paths": [n["path"] for n in newer_unlinked],
    }


def main():
    as_json = "--json" in sys.argv

    all_notes = load_index(INDEX_PATH)
    dossier_paths = sorted(
        p for p in glob.glob(os.path.join(DOSSIER_DIR, "*.md"))
        if slug_of(p) != "README"
    )

    results = [analyze(p, all_notes) for p in dossier_paths]
    drifted = [r for r in results if r["drifted_count"] > 0]
    clean = [r for r in results if r["drifted_count"] == 0]

    if as_json:
        print(json.dumps({"drifted": drifted, "clean_count": len(clean),
                           "total": len(results)}, indent=2))
        return 0

    if not drifted:
        print(f"dossier-drift: {len(clean)} dossiers clean (0 drifted).")
        return 0

    drifted.sort(key=lambda r: r["drifted_count"], reverse=True)
    for r in drifted:
        cov = r["coverage_date"] or "(no coverage date found)"
        print(f"DRIFTED  {r['dossier']}  coverage={cov}  "
              f"{r['drifted_count']} newer-unlinked note(s) of {r['associated_count']} associated "
              f"(dates below = INGESTED, vs coverage)")
        for t in r["top5"]:
            ev = f"  (event {t['event_date']})" if t.get("event_date") and t["event_date"] != t["date"] else ""
            print(f"    {t['date']}  {t['path']}{ev}")
    print(f"\n{len(drifted)} dossiers drifted, {len(clean)} dossiers clean "
          f"({len(results)} total).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

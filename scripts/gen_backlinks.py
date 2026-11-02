#!/usr/bin/env python3
"""Populate each note's `related:` with backlinks — idempotent, additive (never overwrites
existing links). Two link types:
  1. series chain: chronological prev + next note in the same recurring series (per MOC norm()).
  2. dossier hub: a link to the project dossier the note belongs to.
Preserves any manually-added related entries.

    python3 scripts/gen_backlinks.py

**The four routing tables below are the ones you edit for your own store.** They are the SINGLE
OWNER of note -> dossier association (dossier_drift.py imports them rather than keeping a copy),
and they ship with one worked example each, pointing at the seed dossier `dossiers/example-project.md`.
Add a row per dossier you create; leave a table empty when it has nothing to say.
"""
import re, glob, os, collections, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS as TOPS
from _lib.series import norm

# series key (normalized, per _lib.series.norm) -> dossier slug. For a recurring meeting series
# whose notes all belong to one dossier, e.g. {"example project sync": "example-project"}.
SERIES_DOSSIER = {}
# ...and the SUBSTRING form, for a series whose title is not written the same way every time (an
# acronym in some notes, spelled out in others), which gives it more than one normalized key:
# (domain, [lowercased title substrings], dossier slug).
SERIES_SUBSTR_DOSSIER = []
# domain -> dossier slug, applied to EVERY note in the domain. Only sensible for a domain with a
# single hub dossier, e.g. {"infra": "store-infrastructure"}.
DOMAIN_DOSSIER = {}
# entity sub-dossier: a note whose SLUG contains any fragment (and is in the given domain) also
# links to its specific entity dossier — finer-grained than DOMAIN_DOSSIER. Fragments are domain-
# scoped and should be deliberately narrow (no bare common words) to avoid cross-entity matches.
SLUG_DOSSIER = [
    ("work", ["example-project"], "example-project"),
]

# TOPIC association for a SPECIFIC dossier in a domain that also holds OTHER dossiers, consumed by
# dossier_drift.py so a note routes to its topic dossier instead of every dossier in the domain
# claiming every note in it (the precision bug this table exists to prevent: N dossiers in one
# domain each claiming all of that domain's notes, so every drift report was noise). dossier slug ->
# (domain, {routing tags}, [slug substrings]). A note routes here iff domain matches AND (its tags
# intersect the tag set OR its slug contains a fragment). The slug fragments cover a dossier
# identified by title alone, with no distinguishing tag. Narrow by design; a note matching none of a
# domain's topics falls to the domain CATCH-ALL if one exists, else associates to no dossier
# (correct — it belongs to no synthesis). Example shape:
#   "grant-renewal": ("research", {"grants"}, ["renewal", "competing-renewal"]),
DOSSIER_TOPICS = {}
# domain -> its CATCH-ALL dossier: claims domain notes routed to NO topic dossier above and owned by
# NO SLUG_DOSSIER entry. Give a domain a catch-all only when a note with no specific home still
# belongs in some synthesis; a domain with no catch-all leaves such notes unassociated, which is the
# right answer when they belong to no dossier at all. Example: {"research": "lab-overview"}.
DOMAIN_CATCHALL = {}

def slug(fp): return os.path.splitext(os.path.basename(fp))[0]

def read(fp):
    txt = open(fp, encoding="utf-8").read()
    if not txt.startswith("---") or "\n---" not in txt[3:]:
        raise SystemExit(f"gen_backlinks: {fp} has no valid frontmatter (missing closing '---'); "
                         f"run scripts/lint_store.py first for an actionable list")
    end = txt.index("\n---", 3)
    return txt[:end], txt[end:]  # frontmatter (incl leading ---), rest (\n---...)

def field(head, k):
    m = re.search(rf'^{k}:\s*(.*)$', head, re.M); return m.group(1).strip() if m else ""

def set_related(head, links):
    body = "[" + ", ".join(f'"[[{l}]]"' for l in links) + "]"
    if re.search(r'^related:', head, re.M):
        return re.sub(r'^related:.*$', f'related: {body}', head, count=1, flags=re.M)
    return head + f"\nrelated: {body}"

def main():
    files = [f for d in TOPS for f in glob.glob(f"{d}/**/*.md", recursive=True) if not f.endswith("_index.md")]
    meta = {}
    for fp in files:
        head, _ = read(fp)
        meta[fp] = dict(title=field(head, "title").strip('"'), date=field(head, "date"),
                        dom=field(head, "domain"), head=head)
    # group by domain+series
    groups = collections.defaultdict(list)
    for fp, m in meta.items():
        groups[(m["dom"], norm(m["title"]))].append(fp)
    changed = 0
    for (dom, series), members in groups.items():
        members.sort(key=lambda fp: meta[fp]["date"])
        dossier = SERIES_DOSSIER.get(series.lower()) or DOMAIN_DOSSIER.get(dom)
        # A series whose TITLE varies across notes (an acronym in some, spelled out in others) has
        # more than one normalized key, so SERIES_DOSSIER's exact lookup cannot catch it — match on
        # a substring instead. Overrides the exact/domain lookup above; first match wins.
        for sd_dom, frags, sd in SERIES_SUBSTR_DOSSIER:
            if dom == sd_dom and any(fr in series.lower() for fr in frags):
                dossier = sd
                break
        for i, fp in enumerate(members):
            head, rest = read(fp)
            existing = re.findall(r'\[\[([^\]]+)\]\]', field(head, "related"))
            add = []
            if i > 0: add.append(slug(members[i-1]))
            if i < len(members)-1: add.append(slug(members[i+1]))
            if dossier: add.append(dossier)
            sl = slug(fp).lower()
            for sd_dom, frags, sd in SLUG_DOSSIER:
                if dom == sd_dom and any(fr in sl for fr in frags):
                    add.append(sd)
            new = list(dict.fromkeys(existing + [a for a in add if a != slug(fp)]))
            if new != existing:
                open(fp, "w", encoding="utf-8").write(set_related(head, new) + rest)
                changed += 1
    print(f"backlinks updated in {changed} notes (of {len(files)})")

if __name__ == "__main__":
    main()

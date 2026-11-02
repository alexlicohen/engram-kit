#!/usr/bin/env python3
"""Build the Tier-1 query index + a backlinks/orphan map from the note corpus.

    python3 scripts/build_index.py

Emits:
  _manifest/index.jsonl  — one JSON record per note (path, title, date, domain, subpath,
                           tags, status, source, ref, participants, related, updated, verified).
                           The grep/yq-able scan surface: read this ONE file before drilling
                           into notes (the file-based analog to Obsidian Dataview/Bases).
  _manifest/backlinks.md — generated note -> inbound [[links]] map + an Orphans section.
                           Inbound links are resolved from EVERY link source in the store —
                           note `related:` frontmatter, note bodies, and hub files
                           (dossiers/, digests/, literature/, MOCs, people.md, glossary) —
                           so a note reachable from a dossier hub is NOT counted as an orphan.
                           Read-only; supports the "never rename without fixing inbound links" rule.

Deterministic + idempotent (sorted by path). Run after each ingest (see scripts/check.sh).
"""
import re, glob, os, json, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS as TOPS
from _lib.provenance import parse_ref

WIKILINK = re.compile(r'\[\[([^\]]+)\]\]')
# [text](note.md) or [text](<note (with parens).md>); display may contain escaped \]
MDLINK = re.compile(r'(?<!\!)\[(?:[^\]]|\\.)*?\]\((?:<([^<>]+?\.md)>|([^()<>\s]+?\.md))\)')


def split_list(s):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip().strip('"').strip("'") for x in s.split(",") if x.strip()]


def target_slug(raw):
    """Normalize a [[wikilink]] target to a bare note slug: strip an |alias, any
    path prefix (dossiers/foo, research/bar.md), and a trailing .md."""
    t = raw.split("|")[0].strip()
    t = os.path.basename(t)
    if t.endswith(".md"):
        t = t[:-3]
    return t


def parse(fp):
    """Return (index-record, list-of-outbound-target-slugs). Outbound targets come
    from both the frontmatter and the body so cross-links in prose are counted."""
    txt = open(fp, encoding="utf-8").read()
    if txt.startswith("---") and "\n---" in txt[3:]:
        i = txt.index("\n---", 3)
        head, body = txt[:i], txt[i:]
    else:
        head, body = "", txt

    def g(k):
        m = re.search(rf'^{k}:\s*(.*)$', head, re.M)
        return m.group(1).strip().strip('"') if m else ""

    sub = g("subpath")
    upd = g("updated")      # documented in schema.md but was never extracted — same shape as subpath
    ver = g("verified")     # human-confirmed epistemic signal (schema.md); null/absent = unverified
    rec = dict(
        path=fp, title=g("title"), date=g("date"),
        domain=g("domain"), subpath="" if sub in ("", "null", "~") else sub,
        tags=split_list(g("tags")),
        status=g("status"), source=g("source"),
        ref=(parse_ref(head) or "").strip(),   # single owner (finding 3): was an unanchored inline regex
        participants=split_list(g("participants")),
        related=WIKILINK.findall(g("related")),
        updated="" if upd in ("", "null", "~") else upd,
        verified="" if ver in ("", "null", "~") else ver,
    )
    # provenance.ingested is nested (indented) so g()'s '^key:' anchor misses it — pull it out
    # separately. It is the note's STORE-entry date (vs `date:` = the subject/event date); dossier
    # drift compares ingest, not event, so a captured/backfilled note about a PAST decision is visible.
    im = re.search(r'^\s*ingested:\s*(\S+)', head, re.M)
    rec["ingested"] = im.group(1).strip().strip('"') if im else ""
    out = [target_slug(x) for x in WIKILINK.findall(txt)]
    return rec, out


def hub_files():
    """Link-source files that aren't notes but reference notes — natural navigation hubs."""
    hubs = sorted(glob.glob("dossiers/*.md")) + sorted(glob.glob("digests/*.md")) \
        + sorted(glob.glob("profile/**/*.md", recursive=True)) \
        + sorted(glob.glob("literature/**/*.md", recursive=True)) \
        + sorted(glob.glob("*/**/_index.md", recursive=True)) \
        + ["_manifest/people.md", "glossary.md"]
    return [h for h in hubs if os.path.exists(h)]


def main():
    files = sorted(f for d in TOPS for f in glob.glob(f"{d}/**/*.md", recursive=True)
                   if not f.endswith("_index.md"))
    parsed = [parse(fp) for fp in files]
    recs = [r for r, _ in parsed]
    out_links = {r["path"]: outs for (r, outs) in parsed}

    with open("_manifest/index.jsonl", "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    slug_of = {r["path"]: os.path.splitext(os.path.basename(r["path"]))[0] for r in recs}
    by_slug = {}
    slug_collisions = {}   # basename slug -> [paths]; two notes sharing a slug mask each other in the graph
    for r in recs:
        s = slug_of[r["path"]]
        if s in by_slug:
            slug_collisions.setdefault(s, [by_slug[s]]).append(r["path"])
        else:
            by_slug[s] = r["path"]  # first path wins, deterministic

    hubs = hub_files()
    # a note is "connected outbound" if it links to a real navigation destination —
    # another NOTE or a HUB file (dossier/digest/MOC/people). Hub slugs are basenames
    # (all MOCs share `_index`; that's fine — connectivity, not identity).
    hub_slugs = {os.path.splitext(os.path.basename(h))[0] for h in hubs}
    valid_targets = set(by_slug) | hub_slugs

    # Two inbound layers, tracked separately so the headline is honest:
    #   curated  = [[wikilinks]] in note related:/bodies + hub files (the cross-reference graph)
    #   moc      = markdown [text](note.md) links from hub files (auto-generated MOC listing —
    #              every note appears in its domain MOC, so this is the drill-reachability layer)
    curated = {r["path"]: set() for r in recs}   # display labels linking TO each note (wikilink)
    moc = {r["path"]: set() for r in recs}       # hub paths that markdown-list each note

    # 1) note -> note (frontmatter related: + body prose wikilinks); outbound counts notes AND hubs
    has_out = {}
    for r in recs:
        src = r["path"]
        resolves = False
        for tgt in out_links[src]:
            dst = by_slug.get(tgt)
            if dst and dst != src:
                curated[dst].add(slug_of[src])
            if tgt in valid_targets and tgt != slug_of[src]:
                resolves = True
        has_out[src] = resolves

    # 2) hub file -> note (wikilinks = curated; markdown links = MOC listing)
    for hub in hubs:
        label = os.path.splitext(hub)[0]  # e.g. dossiers/example-project
        text = open(hub, encoding="utf-8").read()
        for raw in WIKILINK.findall(text):
            dst = by_slug.get(target_slug(raw))
            if dst:
                curated[dst].add(label)
        for g1, g2 in MDLINK.findall(text):
            dst = by_slug.get(target_slug(g1 or g2))
            if dst:
                moc[dst].add(label)

    inbound = curated  # backlinks.md reverse index stays the curated cross-reference graph
    # true orphan = in NO hub at all (no curated inbound, no MOC listing, no outbound)
    orphans = [r["path"] for r in recs
               if not curated[r["path"]] and not moc[r["path"]] and not has_out[r["path"]]]
    no_xref = [r["path"] for r in recs if not curated[r["path"]]]  # MOC-reachable but uncurated
    with_inbound = sum(1 for v in curated.values() if v)
    reachable = sum(1 for r in recs if curated[r["path"]] or moc[r["path"]])

    out = ["# engram — backlinks map (generated)\n",
           "_Generated by `scripts/build_index.py`. Read-only reverse index: who links **to** each "
           "note via curated `[[wikilinks]]` (note `related:`, note bodies, dossier/digest/MOC hubs). "
           "Supports the never-rename-without-fixing-inbound-links rule. Regenerate after each ingest._\n",
           f"\n**{len(recs)} notes · {reachable} reachable (curated link or MOC listing) · "
           f"{len(orphans)} true orphans (in no hub at all).**\n",
           f"\n_Cross-link layer: {with_inbound} notes have a curated inbound wikilink; "
           f"{len(no_xref)} are MOC-reachable but have no curated cross-link (the enrichment surface)._\n"]
    for r in recs:
        ins = inbound[r["path"]]
        if ins:
            out.append(f"\n**{slug_of[r['path']]}** ← " +
                       ", ".join(f"`{s}`" for s in sorted(ins)))
    out.append("\n\n## True orphans (no curated link, no MOC listing, no outbound)\n")
    out.append("_In no hub at all — these need a `related:` link or a dossier/MOC reference._\n")
    for p in orphans:
        out.append(f"- `{p}`")
    open("_manifest/backlinks.md", "w", encoding="utf-8").write("\n".join(out) + "\n")
    for s, paths in sorted(slug_collisions.items()):
        print(f"  WARN slug collision '{s}': " + ", ".join(sorted(paths)))
    print(f"index: {len(recs)} notes -> _manifest/index.jsonl ; "
          f"backlinks: {reachable} reachable, {len(orphans)} true orphans, "
          f"{with_inbound} curated-inbound, {len(no_xref)} uncurated -> _manifest/backlinks.md")


if __name__ == "__main__":
    main()

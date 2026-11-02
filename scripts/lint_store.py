#!/usr/bin/env python3
"""Validate the engram store. Exits non-zero if any ERROR is found (safe for a hook/CI).

    python3 scripts/lint_store.py

Checks: every note has valid YAML frontmatter; `domain` matches its top-level directory;
provenance `ref` present; tags drawn from the controlled vocab (schema.md); `related:`
wikilink targets resolve.
"""
import re, glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS as TOPS, TAG_VOCAB as VOCAB
from _lib.provenance import parse_ref, gmail_thread_key, thread_id
from _lib.frontmatter_diff import _get as fm_get  # nested provenance.ingested reader — single owner

# gmail/outlook filename convention (distiller prompts, FILE NAMING section): slug ends in the
# last 6-12 hex chars of the thread id (the prompts themselves specify EXACTLY the last 6; this
# check tolerates up to 12 for older/longer suffixes), added to avoid parallel-worker filename
# collisions. A note whose provenance.ref thread id does NOT end with that same suffix is flagged.
# One instance was DIRECTLY confirmed in the parent store: a filename suffix sat in the MIDDLE of
# the note's ref thread id, not at the end — consistent with a worker writing a neighbouring/wrong thread's id
# into the ref while the slug (derived from the correct thread in the same turn) stayed right.
# WARN, not ERROR: a store-wide dry pass in the parent store found ~300 mismatches. That volume is NOT all independently confirmed as this same
# mechanism (only the one instance above was source-verified); treat the rest as an unexplained
# backlog worth auditing, not a confirmed diagnosis for every row. Never edit a flagged note here —
# a real mismatch needs source-side verification of which thread is correct before any fix.
GMAIL_OUTLOOK_SLUG_RE = re.compile(r'-([0-9a-fA-F]{6,12})$')

# Investigation in the parent store found such a backlog dominated by a one-time slug-CONVENTION
# drift, not mis-keys (a back-catalog run that predates the suffix convention; every ref still
# resolved to a real thread). Set the cutoff to the date your naming convention took effect. Only notes ingested ON/AFTER this
# cutoff get individual WARNs; older ones are backlog, audited separately, and rolled into one
# summary count so they don't bury the small number of real, actionable warnings.
SLUG_REF_RULE_SINCE = "2026-08-01"

# Per-note allowlist for recent slug-ref-mismatch notes that have been SOURCE-AUDITED and confirmed
# a slug-only defect (provenance.ref is correct — the ref, not the filename, is what dedup/worklist
# resumability actually depends on). Add an entry ONLY after a human/agent has verified the ref
# against the real source thread for that exact path — never to silence a fresh, unaudited WARN.
# Remove a path's entry if it is later renamed or the slug is fixed (keep this list == reality).
SLUG_REF_AUDITED_OK = {
    # Shipped example entry (fictional); replace with your own audited paths.
    "research/2026-01-01-example-audited-note-abc123.md":
        "example: ref verified against source; slug-only defect",
}

# Print grouping: a store-wide run of the new rule above emits ~300+ WARN lines, which used to bury
# the small number of PRE-EXISTING warnings (11, e.g. off-vocab tags) under one flat 50-line cap —
# a real report regression, not a new bug in those checks. Report per-category counts and cap each
# category's printed examples independently so no category silently disappears.
WARN_CAP_PER_CATEGORY = 20

# typed-field enums (enforce types/enums, not just presence)
STATUS_VOCAB = {"pending", "distilled", "flagged", "done", "verbatim"}
SOURCE_VOCAB = {"granola", "gmail", "obsidian", "claude-chat", "manual", "gdrive",
                "pubmed", "imessage", "whatsapp", "outlook", "other"}
TYPE_VOCAB = {"event", "evergreen"}   # event = dated point-in-time; evergreen = living state note

def main():
    errors, warns = [], []
    refs = {}                       # provenance ref -> [paths]; a ref in >1 note = a dup-ingest bug
    all_slugs = {}
    legacy_slug_mismatch_counts = {}  # src -> count of pre-cutoff slug-ref-mismatch notes
    off_convention_counts = {}        # src -> count of recent notes where slug is a non-suffix
                                       # substring of the (correct) thread id — cosmetic, not a mis-key
    audited_benign_count = 0          # recent notes in SLUG_REF_AUDITED_OK — source-verified, slug-only
    files = [f for d in TOPS for f in glob.glob(f"{d}/**/*.md", recursive=True) if not f.endswith("_index.md")]
    for fp in files:
        all_slugs[os.path.splitext(os.path.basename(fp))[0]] = fp
    # dossiers, profile/ (synthesized), literature, and top-level reference docs are valid wikilink
    # targets too (they are NOT linted as notes — not in TOPS — like dossiers)
    for fp in (glob.glob("dossiers/*.md") + glob.glob("profile/**/*.md", recursive=True)
               + glob.glob("literature/*.md") + glob.glob("*.md")):
        all_slugs[os.path.splitext(os.path.basename(fp))[0]] = fp
    for fp in files:
        txt = open(fp, encoding="utf-8").read()
        if not txt.startswith("---") or "\n---" not in txt[3:]:
            errors.append(f"{fp}: missing/invalid frontmatter"); continue
        head = txt[:txt.index("\n---", 3)]
        g = lambda k: (re.search(rf'^{k}:\s*"?(.*?)"?\s*$', head, re.M) or [None, ""])[1] if re.search(rf'^{k}:', head, re.M) else None
        dom = g("domain"); topdir = fp.split("/")[0]
        if dom != topdir: errors.append(f"{fp}: domain '{dom}' != dir '{topdir}'")
        # Key on parse_ref, the single owner — NOT a `"ref:" in head` substring test (finding 5):
        # a substring test passes `href:`/`xref:` with no real ref, and a note whose `ref:` is present
        # but unparseable would silently escape dup-detection. Require a parseable ref; error otherwise.
        ref = parse_ref(head)
        if not ref:
            errors.append(f"{fp}: missing/unparseable provenance ref")
        else:
            key = ref.strip()
            # Canonicalize gmail thread refs to the INT form so the SAME thread stored decimal
            # (mbox backfill) vs hex (connector) collapses to one key — otherwise a real cross-base
            # dup-ingest escapes the check (they are the same 64-bit id; see gmail_thread_key).
            if key.startswith("gmail:thread:"):
                gk = gmail_thread_key(key)
                if gk is not None:
                    key = f"gmail:thread:{gk}"
            refs.setdefault(key, []).append(fp)
        src = g("source")
        if ref and src in ("gmail", "outlook"):
            slug_m = GMAIL_OUTLOOK_SLUG_RE.search(os.path.splitext(os.path.basename(fp))[0])
            if slug_m:
                slug_hex = slug_m.group(1).lower()
                scheme = "gmail:thread:" if src == "gmail" else "outlook:thread:"
                thrid = thread_id(ref, scheme=scheme)
                if thrid is None:
                    warns.append(("slug-ref-unresolvable",
                                  f"{fp}: {src} note's ref '{ref}' is not a {scheme}<id> ref, "
                                  f"can't check filename hex suffix '{slug_hex}'"))
                else:
                    thrid_hex = thrid.lower()
                    dec_form = None
                    if src == "gmail":
                        # base-agnostic (CLAUDE.md rule): mbox decimal thrid == connector hex id
                        # are the SAME thread — check both renderings of the canonical id. Outlook
                        # has no such dual-base history.
                        canon = gmail_thread_key(thrid)
                        if canon is not None:
                            thrid_hex = format(canon, "x")
                            dec_form = str(canon)
                    match = thrid_hex.endswith(slug_hex) or (dec_form is not None
                                                              and dec_form.endswith(slug_hex))
                    if not match:
                        # Not a suffix — is it a substring anywhere (same thread, worker just took
                        # the wrong six characters of it), or absent entirely (possible wrong-thread
                        # mis-key)? Audit found 9/11 recent cases are the former (e.g. 'c0ffee' sits
                        # inside '12c0ffee34ab') — the ref is correct, this is cosmetic, not a mis-key.
                        off_convention = (slug_hex in thrid_hex
                                          or (dec_form is not None and slug_hex in dec_form))
                        ingested = fm_get(head, "provenance.ingested")
                        # Legacy: pre-cutoff notes are a known back-catalog convention-drift backlog
                        # (see SLUG_REF_RULE_SINCE comment) — counted, not individually listed, so
                        # they don't bury the small number of actionable recent warnings. A missing
                        # `provenance.ingested` is treated as legacy too (conservative default).
                        if ingested and ingested >= SLUG_REF_RULE_SINCE:
                            if off_convention:
                                off_convention_counts[src] = off_convention_counts.get(src, 0) + 1
                            elif fp in SLUG_REF_AUDITED_OK:
                                audited_benign_count += 1
                            else:
                                warns.append(("slug-ref-mismatch",
                                    f"{fp}: filename hex suffix '{slug_hex}' doesn't match "
                                    f"{src} thread id in ref '{ref}' (wrong-thread slug/ref mismatch)"))
                        else:
                            legacy_slug_mismatch_counts[src] = legacy_slug_mismatch_counts.get(src, 0) + 1
        tg = g("tags") or ""
        for t in re.findall(r'[A-Za-z0-9_-]+', tg):
            if t and not t.isdigit() and t not in VOCAB:
                warns.append(("off-vocab-tag", f"{fp}: off-vocab tag '{t}'"))
        # typed fields: format/enum, not just presence
        date = g("date")
        if date and not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            errors.append(f"{fp}: date '{date}' not ISO YYYY-MM-DD")
        st = g("status")
        if st and st not in STATUS_VOCAB:
            warns.append(("bad-status", f"{fp}: status '{st}' not in {sorted(STATUS_VOCAB)}"))
        src = g("source")
        if src and src not in SOURCE_VOCAB:
            warns.append(("bad-source", f"{fp}: source '{src}' not in {sorted(SOURCE_VOCAB)}"))
        ntype = g("type")
        if ntype and ntype not in TYPE_VOCAB:
            warns.append(("bad-type", f"{fp}: type '{ntype}' not in {sorted(TYPE_VOCAB)}"))
        upd = g("updated")
        if upd and not re.match(r'^\d{4}-\d{2}-\d{2}$', upd):
            errors.append(f"{fp}: updated '{upd}' not ISO YYYY-MM-DD")
        # verified = human-confirmed epistemic signal (schema.md); WARN-only like status/source —
        # a bad date shouldn't block a sync. null/~/absent = not yet verified.
        ver = g("verified")
        if ver and ver not in ("null", "~") and not re.match(r'^\d{4}-\d{2}-\d{2}$', ver):
            warns.append(("bad-verified", f"{fp}: verified '{ver}' not ISO YYYY-MM-DD"))
        for field in ("related", "supersedes", "superseded_by"):
            for tgt in re.findall(r'\[\[([^\]]+)\]\]', g(field) or ""):
                if tgt not in all_slugs:
                    errors.append(f"{fp}: {field} wikilink [[{tgt}]] has no target file")
    # duplicate provenance ref = a source item distilled into two notes — the artifact of a
    # wrong-scope/re-distill ingest, the cheapest net for that whole bug class.
    # EXEMPTION: the chat-DB sources (iMessage AND WhatsApp) are intentionally multi-note-per-chunk
    # (ref is chunk-granular, `<source>:<guid>:<chunk_id>`) — one chunk yields several durable-event
    # notes that SHARE that source pointer. A shared imessage:/whatsapp: ref is BY DESIGN, not a dup.
    # This exemption is PERMANENT: the ref is a chunk-level SOURCE pointer, deliberately not
    # note-unique, so we never migrate it. Dup-INGEST (the same chunk distilled twice) is prevented
    # upstream instead — {imsg,whatsapp}_worklist.py skips any chunk with a done-marker
    # (_sources/<source>-done.d/<chunk_id>) — with the build_index.py filename slug-collision WARN.
    for ref, paths in sorted(refs.items()):
        if len(paths) > 1 and not (ref.startswith("imessage:") or ref.startswith("whatsapp:")):
            errors.append(f"duplicate provenance ref '{ref}' in {len(paths)} notes: "
                          + ", ".join(sorted(paths)))
    print(f"linted {len(files)} notes — {len(errors)} errors, {len(warns)} warnings")
    # Grouped by category (see WARN_CAP_PER_CATEGORY comment above) so a large category (e.g. the
    # slug-ref-mismatch backlog) can never push a small one (e.g. off-vocab-tag) out of the report.
    by_cat = {}
    for cat, msg in warns:
        by_cat.setdefault(cat, []).append(msg)

    def _breakdown(counts):
        return " / ".join(f"{counts[s]} {s}"
                          for s in sorted(counts, key=counts.get, reverse=True))

    legacy_total = sum(legacy_slug_mismatch_counts.values())
    off_convention_total = sum(off_convention_counts.values())
    for cat in sorted(by_cat):
        msgs = by_cat[cat]
        print(f"  -- {cat}: {len(msgs)} --")
        for m in msgs[:WARN_CAP_PER_CATEGORY]:
            print("  WARN", m)
        if len(msgs) > WARN_CAP_PER_CATEGORY:
            print(f"  ... {len(msgs) - WARN_CAP_PER_CATEGORY} more {cat} warnings not shown")
        if cat == "slug-ref-mismatch":
            if audited_benign_count:
                print(f"  slug-ref-mismatch (audited-benign): {audited_benign_count} notes — "
                      f"ref source-verified correct, slug-only defect (see SLUG_REF_AUDITED_OK)")
            if off_convention_total:
                print(f"  slug-off-convention: {off_convention_total} notes "
                      f"({_breakdown(off_convention_counts)}) — slug is a non-suffix substring of "
                      f"the same thread id — cosmetic, not a mis-key")
            if legacy_total:
                print(f"  WARN slug-ref-mismatch (legacy, ingested < {SLUG_REF_RULE_SINCE}): "
                      f"{legacy_total} notes ({_breakdown(legacy_slug_mismatch_counts)}) — known "
                      f"back-catalog convention drift, audit backlog, not individually listed")
    if "slug-ref-mismatch" not in by_cat and (legacy_total or off_convention_total or audited_benign_count):
        print(f"  -- slug-ref-mismatch (legacy, ingested < {SLUG_REF_RULE_SINCE}): {legacy_total} --")
        if audited_benign_count:
            print(f"  slug-ref-mismatch (audited-benign): {audited_benign_count} notes — "
                  f"ref source-verified correct, slug-only defect (see SLUG_REF_AUDITED_OK)")
        if off_convention_total:
            print(f"  slug-off-convention: {off_convention_total} notes "
                  f"({_breakdown(off_convention_counts)}) — slug is a non-suffix substring of "
                  f"the same thread id — cosmetic, not a mis-key")
        if legacy_total:
            print(f"  WARN slug-ref-mismatch (legacy, ingested < {SLUG_REF_RULE_SINCE}): "
                  f"{legacy_total} notes ({_breakdown(legacy_slug_mismatch_counts)}) — known "
                  f"back-catalog convention drift, audit backlog, not individually listed")
    for e in errors[:100]: print("  ERR ", e)
    sys.exit(1 if errors else 0)

if __name__ == "__main__":
    main()

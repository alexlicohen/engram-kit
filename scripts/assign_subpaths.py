#!/usr/bin/env python3
"""Assign `subpath:` frontmatter to the subpath-less notes in the store's LARGE domains, so a
per-domain MOC can be split into agent-openable sub-clusters. Deterministic + idempotent.

    python3 scripts/assign_subpaths.py

**Edit the rule tables below for your lab's large domains.** The pattern: list the domains big
enough to need sub-clustering in BIG_DOMAINS, then give each one a first-match-wins rule set in
`classify()`. Rules may key off `tags` (cheap and stable — preferred), off the note TITLE via a
regex table (for a domain whose notes carry mostly note-TYPE tags, so the subject only appears in
the title), or a hybrid of the two. A domain whose MOC still fits in one read needs no entry at
all; leaving it out is the correct default.

The shipped example is deliberately minimal — ONE domain, tag-driven only:
  research: papers->papers ; grants->grants ; {lab-meeting,1on1,onboarding,hiring}->lab-ops ;
            else root

GUARANTEES:
  * Only classifies notes whose `subpath:` is empty/null/~ — an already-set non-empty subpath
    is PRESERVED untouched.
  * Edits ONLY the `subpath:` frontmatter line — never the body or any other field.
  * Idempotent: a second run makes zero changes (classified notes now carry a non-empty subpath
    and are skipped; root notes re-derive root and are not rewritten).
  * No file is moved — `subpath` is decoupled from physical location (as designed).

Tag/title source of truth: `_manifest/index.jsonl` (canonically parsed), keyed by path, with a
frontmatter fallback for any note missing from a stale index. Domain/tag constants come from the
single owners in scripts/_lib/ — nothing hardcoded here beyond the domain-specific rule tables,
whose tags are asserted against TAG_VOCAB to catch vocab drift.
"""
import os
import sys
import re
import json
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS, TAG_VOCAB
from _lib.notes import iter_notes, split_frontmatter, get_field, note_slug

# The large domains that get sub-clustered. A strict subset of the canonical DOMAINS list
# (imported, not hardcoded) — assert membership so a domain rename can't silently no-op this.
# Add a domain here only once its MOC is too big for a single read, and give it a rule set below.
BIG_DOMAINS = ["research"]
assert all(d in DOMAINS for d in BIG_DOMAINS), \
    f"BIG_DOMAINS not all in canonical DOMAINS: {[d for d in BIG_DOMAINS if d not in DOMAINS]}"

# Tag-driven rule sets, one entry per BIG_DOMAINS member. Asserted against TAG_VOCAB so a vocab
# rename surfaces here instead of silently sending everything to root.
_RES_LABOPS = {"lab-meeting", "1on1", "onboarding", "hiring"}
_RULE_TAGS = {"papers", "grants"} | _RES_LABOPS
_missing_vocab = sorted(t for t in _RULE_TAGS if t not in TAG_VOCAB)
assert not _missing_vocab, f"rule tags absent from TAG_VOCAB (vocab drift): {_missing_vocab}"


def classify(dom, tags, title):
    """Return the subpath for a subpath-less note (first match wins); '' == root/unfiled.

    `title` is unused by the shipped tag-only rules — it is in the signature because a
    title-regex or hybrid rule set (see the module docstring) needs it.
    """
    tags = set(tags)
    if dom == "research":
        if "papers" in tags:
            return "papers"
        if "grants" in tags:
            return "grants"
        if tags & _RES_LABOPS:
            return "lab-ops"
        return ""
    return ""


def _fallback_tags(head):
    """Parse `tags:` from frontmatter for notes missing from index.jsonl (stale-index safety)."""
    m = re.search(r'^tags:\s*(.*)$', head, re.M)
    if not m:
        return []
    s = m.group(1).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip().strip('"').strip("'") for x in s.split(",") if x.strip()]


def _load_index():
    by_path = {}
    p = "_manifest/index.jsonl"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                by_path[r["path"]] = r
    return by_path


def main():
    idx = _load_index()
    changed = 0
    preserved = 0
    used_fallback = 0
    report = collections.defaultdict(list)   # (dom, sub) -> [titles]
    for fp in iter_notes(BIG_DOMAINS):
        dom = fp.split("/")[0]
        txt = open(fp, encoding="utf-8").read()
        head, body = split_frontmatter(txt)
        if head is None:
            continue   # malformed frontmatter — lint_store.py owns reporting this
        cur = get_field(head, "subpath")
        cur = "" if cur in ("", "null", "~") else cur
        rec = idx.get(fp)
        if rec is not None:
            tags, title = rec.get("tags", []), rec.get("title", "")
        else:
            tags, title = _fallback_tags(head), get_field(head, "title")
            used_fallback += 1
        if cur:
            sub = cur
            preserved += 1
        else:
            sub = classify(dom, tags, title)
            if sub:
                new_head = re.sub(r'(?m)^subpath:.*$', lambda m: f"subpath: {sub}", head, count=1)
                if new_head != head:
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(new_head + body)
                    changed += 1
        report[(dom, sub)].append(title or note_slug(fp))

    # ---- report ----
    print(f"assign_subpaths: {changed} notes newly classified, {preserved} preserved "
          f"(already had a subpath), {used_fallback} used frontmatter-tag fallback.\n")
    for dom in BIG_DOMAINS:
        subs = sorted(k for k in report if k[0] == dom)
        total = sum(len(report[k]) for k in subs)
        print(f"=== {dom} ({total} notes) ===")
        for k in sorted(subs, key=lambda k: (-len(report[k]), k[1] or "")):
            titles = sorted(report[k])
            label = k[1] or "(root)"
            print(f"  {label:14} {len(titles):5}")
            for t in titles[:5]:
                print(f"       - {t[:90]}")
        print()


if __name__ == "__main__":
    main()

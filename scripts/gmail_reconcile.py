#!/usr/bin/env python3
"""Post-wave reconciliation / maintenance sweep for Gmail-distilled notes.

Idempotent — safe to re-run after each wave. Operates only on notes whose provenance
ref is gmail:thread:* (won't touch Granola/Obsidian notes). Steps:

  1. type:event backfill   — add `type: event` to any gmail note lacking it.
  2. off-vocab tag strip    — drop tags not in the controlled vocab (e.g. category-label
                              leaks like 'finance', 'personal-topic').
  3. broken-wikilink fix    — in related/supersedes/superseded_by, keep only [[targets]]
                              that resolve to an existing note stem; blank otherwise.
  4. duplicate-thread dedup — when 2+ notes share one gmail:thread id, KEEP the best
                              (most body content; tie-break: hardened -<thrid6> name, then
                              non-unsorted domain) and MOVE the rest to _archive/ (never
                              delete). Every decision is printed.

Usage:
  python3 scripts/gmail_reconcile.py            # apply
  python3 scripts/gmail_reconcile.py --dry-run  # report only (no writes/moves)
"""
import glob, re, os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import TAG_VOCAB as VOCAB
from _lib.provenance import parse_ref, thread_id

DRY = '--dry-run' in sys.argv


def gmail_notes():
    for f in set(glob.glob('*/*.md')) | set(glob.glob('*/*/*.md')):
        if f.startswith(('_sources', '_archive')):
            continue
        try:
            t = open(f, encoding='utf-8').read()
        except OSError:
            continue
        if 'gmail:thread:' in t[:1500]:
            yield f, t


def all_slugs():
    s = set()
    for f in glob.glob('*/**/*.md', recursive=True) + glob.glob('*.md'):
        if f.startswith(('_sources', '_archive')):
            continue
        s.add(os.path.splitext(os.path.basename(f))[0])
    return s


def split_fm(t):
    if not t.startswith('---') or '\n---' not in t[3:]:
        return None, None, None
    end = t.index('\n---', 3)
    return t[:end], t[end:], end


def reconcile():
    slugs = all_slugs()
    n_type = n_tag = n_link = 0
    by_thr = {}
    for f, t in gmail_notes():
        head, rest, end = split_fm(t)
        if head is None:
            continue
        orig = t
        # 1. type:event
        if not re.search(r'^type:\s', head, re.M):
            if re.search(r'^subpath:.*$', head, re.M):
                head = re.sub(r'^(subpath:.*)$', r'\1\ntype: event', head, count=1, flags=re.M)
            else:
                head = re.sub(r'^(domain:.*)$', r'\1\ntype: event', head, count=1, flags=re.M)
            n_type += 1
        # 2. off-vocab tags
        m = re.search(r'^tags:\s*\[(.*?)\]\s*$', head, re.M)
        if m:
            tags = [x.strip() for x in m.group(1).split(',') if x.strip()]
            keep = [x for x in tags if x in VOCAB]
            if keep != tags:
                head = head[:m.start()] + 'tags: [' + ', '.join(keep) + ']' + head[m.end():]
                n_tag += 1
        # 3. broken wikilinks
        for field in ('related', 'supersedes', 'superseded_by'):
            fm = re.search(rf'^{field}:\s*(.+?)\s*$', head, re.M)
            if not fm:
                continue
            targets = re.findall(r'\[\[\s*"?(.*?)"?\s*\]\]', fm.group(1))
            if not targets:
                continue
            valid = [tg.strip() for tg in targets if tg.strip() in slugs]
            new = ('[' + ', '.join(f'"[[{v}]]"' for v in valid) + ']') if valid else '[]'
            if new != fm.group(1).strip():
                head = head[:fm.start(1)] + new + head[fm.end(1):]
                n_link += 1
        t = head + rest
        if t != orig and not DRY:
            open(f, 'w', encoding='utf-8').write(t)
        # collect for dedup
        tid = thread_id(parse_ref(t[:1500]))
        if tid:
            body = t[end:] if end else t
            by_thr.setdefault(tid, []).append((f, len(body)))

    # 4. dedup
    dups = {k: v for k, v in by_thr.items() if len(v) > 1}
    archived = 0
    print(f"sweeps: type+{n_type}  tags-fixed {n_tag}  links-fixed {n_link}")
    print(f"duplicate threads: {len(dups)}")
    for thr, notes in dups.items():
        # keeper: most content; tie-break hardened-suffix, then non-unsorted
        def score(item):
            f, blen = item
            stem = os.path.basename(f)[:-3]
            return (blen, bool(re.search(r'-\d{6}$', stem)), not f.startswith('unsorted/'))
        ranked = sorted(notes, key=score, reverse=True)
        keep = ranked[0][0]
        print(f"  thread {thr}: KEEP {keep}")
        for f, _ in ranked[1:]:
            dest = os.path.join('_archive', f)
            print(f"      archive -> {dest}")
            if not DRY:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(f, dest)
            archived += 1
    print(f"archived {archived} duplicate notes" + ("  [DRY-RUN]" if DRY else ""))


if __name__ == '__main__':
    reconcile()

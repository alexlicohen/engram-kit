#!/usr/bin/env python3
"""Build the Granola RE-DISTILL work-list (resumable).

Stage 1 distilled all Granola meetings from the OLD, lossy summary template. The account owner regenerated
the summaries with an improved (far richer) template, so we re-distill IN PLACE — overwriting
each existing note's BODY + participants from the new summary while PRESERVING its curated
frontmatter (related/supersedes/domain/aliases added by the Stage-E synthesis pass).

This emits one record per existing granola note: {id, fp, title, date}. The distiller agent
re-reads `fp` to preserve its exact frontmatter, fetches the new summary via the Granola
connector by `id`, regenerates the body + participant roster, and writes back to `fp`.

Resumable: a note is done once `_sources/granola-redistill-done.d/<id>` exists.

Usage: python3 scripts/granola_redistill_worklist.py
"""
import os, re, glob, json, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.provenance import parse_ref, thread_id

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, '_sources/granola-redistill.jsonl')
DONE_D = os.path.join(ROOT, '_sources/granola-redistill-done.d')
SKIP = ('_archive/', '_sources/', '_manifest/', '_gmail-pending/', '_granola-pending/')
TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.M)
DATE_RE = re.compile(r'^date:\s*([0-9-]+)\s*$', re.M)


def main():
    os.makedirs(DONE_D, exist_ok=True)
    done = {os.path.basename(p) for p in glob.glob(os.path.join(DONE_D, '*'))}
    n_total = n_done = n_pending = 0
    with open(RUN, 'w', encoding='utf-8') as out:
        for fp in sorted(glob.glob(os.path.join(ROOT, '**/*.md'), recursive=True)):
            rel = os.path.relpath(fp, ROOT)
            if rel.startswith(SKIP):
                continue
            head = open(fp, encoding='utf-8').read(1500)
            mid = thread_id(parse_ref(head), "granola:")
            if not mid:
                continue
            n_total += 1
            if mid in done:
                n_done += 1
                continue
            title = (TITLE_RE.search(head) or [None, None])[1] if TITLE_RE.search(head) else None
            date = (DATE_RE.search(head).group(1) if DATE_RE.search(head) else None)
            out.write(json.dumps({'id': mid, 'fp': rel, 'title': title, 'date': date},
                                 ensure_ascii=False) + '\n')
            n_pending += 1
    print(f"granola notes {n_total} | done {n_done} | PENDING {n_pending} -> {RUN}")


if __name__ == '__main__':
    main()

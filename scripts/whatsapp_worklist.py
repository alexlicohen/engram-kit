#!/usr/bin/env python3
"""Compute the PENDING WhatsApp distillation work-list (idempotent / resumable).

The WhatsApp twin of imsg_worklist.py — a thin wrapper over the SAME single-owner
`imsg_worklist.compute_worklist`, but pointed at the WhatsApp done-marker dir, the `whatsapp:`
ref prefix, and the `_archive/whatsapp-crash-orphans` reconcile dir. Everything else (marker-based
resume, crash-orphan reconciliation, the never-archive-a-tracked-note data-loss guard) is reused
verbatim.

pending = <whatsapp_extract's run.jsonl>  MINUS  chunks already distilled
          (a done-marker exists at _sources/whatsapp-done.d/<chunk_id>)

Usage:
  python3 scripts/whatsapp_worklist.py [wave.jsonl] [out-pending.jsonl]
    wave.jsonl        default _sources/whatsapp-run.jsonl (the backfill wave)
    out-pending.jsonl default derived: _sources/<base>-pending.jsonl
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ on path
import imsg_worklist  # single owner of compute_worklist + the resume/reconcile logic
from _lib.chunking import default_wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONE = os.path.join(ROOT, '_sources/whatsapp-done.d')
DEFAULT_WAVE = os.path.join(ROOT, '_sources/whatsapp-run.jsonl')
ARCHIVE_REL = os.path.join('_archive', 'whatsapp-crash-orphans')


def main():
    pos = [a for a in sys.argv[1:] if not a.startswith('-')]
    wave = pos[0] if len(pos) > 0 else default_wave('whatsapp', ROOT)
    out = pos[1] if len(pos) > 1 else imsg_worklist.default_out(wave)

    print(f"wave: {os.path.relpath(wave, ROOT)}", file=sys.stderr)

    res = imsg_worklist.compute_worklist(
        wave, out, root=ROOT, done_dir=DONE, source='whatsapp', archive_rel=ARCHIVE_REL)

    print(f"total {res['total']} | done {res['done']} | PENDING {res['pending']} -> {out}")
    if res['reconciled']:
        sys.stderr.write(f"reconciled {len(res['reconciled'])} untracked crash-orphan note(s) -> "
                         f"{ARCHIVE_REL}/ (will be re-distilled):\n")
        for ref, src, dst in res['reconciled'][:20]:
            sys.stderr.write(f"  {os.path.relpath(src, ROOT)} -> {os.path.relpath(dst, ROOT)}  [{ref}]\n")
    if res.get('reconcile_skipped'):
        sys.stderr.write(f"WARNING: {len(res['reconcile_skipped'])} TRACKED note(s) share a pending "
                         f"chunk's ref and were NOT archived (anomalous; investigate):\n")
        for ref, src, why in res['reconcile_skipped'][:20]:
            sys.stderr.write(f"  {os.path.relpath(src, ROOT)}  [{ref}] ({why})\n")
    orphans = sorted(res['done_markers'] - res['seen'])
    if orphans:
        sys.stderr.write(f"note: {len(orphans)} done-marker(s) not in this wave (ignored): "
                         + ", ".join(orphans[:12]) + ("…" if len(orphans) > 12 else "") + "\n")


if __name__ == '__main__':
    main()

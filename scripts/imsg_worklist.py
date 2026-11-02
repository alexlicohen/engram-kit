#!/usr/bin/env python3
"""Compute the PENDING iMessage distillation work-list (idempotent / resumable).

pending = <a chunk wave (imsg_extract's run.jsonl)>  MINUS  chunks already distilled
          (a done-marker exists at _sources/imsg-done.d/<chunk_id>)

This is the resume `imsg_distill.mjs` never had: the distiller WRITES a marker per finished chunk
but nothing READ them, so a re-run reprocessed every chunk. This script CONSUMES the markers —
regenerate the work-list and an interrupted run continues where it left off.

Done-detection keys on the marker (deterministic: distiller writes exactly `imsg-done.d/<chunk_id>`),
NOT on the note provenance ref. iMessage refs are chunk-granular (`imessage:<guid>:c<n>`) and one
chunk intentionally yields several notes sharing that ref, and the backfill agents wrote the ref
inconsistently (`:c0` vs `:c<chat>_c000`) — so the ref is unreliable for done-detection. The marker
filename is not.

Usage:
  python3 scripts/imsg_worklist.py [wave.jsonl] [out-pending.jsonl]
    wave.jsonl        default _sources/imessage-run.jsonl (the backfill wave)
    out-pending.jsonl default derived: _sources/<base>-pending.jsonl
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ -> _lib importable
from _lib.imsg_markers import chunk_ref, reconcile_orphans
from _lib.chunking import default_wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONE = os.path.join(ROOT, '_sources/imsg-done.d')
DEFAULT_WAVE = os.path.join(ROOT, '_sources/imessage-run.jsonl')


def default_out(wave):
    base = os.path.basename(wave)
    if base.endswith('-run.jsonl'):
        stem = base[:-len('-run.jsonl')]
    elif base.endswith('.jsonl'):
        stem = base[:-len('.jsonl')]
    else:
        stem = base
    return os.path.join(ROOT, '_sources', f'{stem}-pending.jsonl')


def compute_worklist(wave, out, root=ROOT, done_dir=None, source="imessage", archive_rel=None):
    """Compute the pending work-list AND reconcile crash-orphans, returning a summary dict.

    Pending = wave chunks with no `<done_dir>/<chunk_id>` marker. Before those chunks are
    re-distilled, `reconcile_orphans` archives any store notes already bearing a pending chunk's
    canonical ref — debris from a crashed prior attempt (a completed chunk has a code-written
    marker and is skipped here, so its notes are never targeted). This is what makes a re-emit
    idempotent: the re-distill can only REPLACE, never accumulate a silent dup (finding 2).

    `source`/`done_dir`/`archive_rel` default to the iMessage regime; the WhatsApp connector passes
    source='whatsapp' + its own done-dir + `_archive/whatsapp-crash-orphans` so the ref prefix and
    the crash-orphan archive match the source. Factored out of main() so the regression test can
    drive it against a synthetic temp store.
    """
    done_dir = done_dir if done_dir is not None else os.path.join(root, '_sources/imsg-done.d')
    done_markers = set(os.listdir(done_dir)) if os.path.isdir(done_dir) else set()

    total = done = 0
    seen = set()
    pending_recs = []
    with open(wave, encoding='utf-8') as wf:
        for line in wf:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            total += 1
            cid = r['chunk_id']
            seen.add(cid)
            if os.path.exists(os.path.join(done_dir, cid)):
                done += 1
                continue
            pending_recs.append(r)

    # Reconcile crash-orphans for the chunks about to be re-emitted (keyed on the chunk-UNIQUE
    # canonical ref; only wave lines carrying guid+chunk_id are reconcilable, which the backfill/
    # incremental waves always are). Only genuine UNTRACKED debris is archived; a TRACKED/committed
    # note sharing a pending ref is skipped + surfaced (never data-loss). No-op for fresh chunks.
    pending_refs = {chunk_ref(r['guid'], r['chunk_id'], source)
                    for r in pending_recs if r.get('guid') and r.get('chunk_id')}
    rec = reconcile_orphans(root, pending_refs,
                            **({'archive_rel': archive_rel} if archive_rel else {}))

    with open(out, 'w', encoding='utf-8') as o:
        for r in pending_recs:
            o.write(json.dumps(r, ensure_ascii=False) + '\n')

    return dict(total=total, done=done, pending=len(pending_recs), out=out,
                reconciled=rec['moved'], reconcile_skipped=rec['skipped'],
                seen=seen, done_markers=done_markers)


def main():
    pos = [a for a in sys.argv[1:] if not a.startswith('-')]
    wave = pos[0] if len(pos) > 0 else default_wave('imessage', ROOT)
    out = pos[1] if len(pos) > 1 else default_out(wave)

    print(f"wave: {os.path.relpath(wave, ROOT)}", file=sys.stderr)
    res = compute_worklist(wave, out)

    print(f"total {res['total']} | done {res['done']} | PENDING {res['pending']} -> {out}")
    # Crash-orphan reconciliation — surface loudly (an UNTRACKED debris note was moved out).
    if res['reconciled']:
        sys.stderr.write(f"reconciled {len(res['reconciled'])} untracked crash-orphan note(s) for "
                         f"re-emitted chunk(s) -> _archive/imsg-crash-orphans/ (will be re-distilled):\n")
        for ref, src, dst in res['reconciled'][:20]:
            sys.stderr.write(f"  {os.path.relpath(src, ROOT)} -> {os.path.relpath(dst, ROOT)}  [{ref}]\n")
    # A TRACKED/committed note that matches a pending chunk's ref is ANOMALOUS and was NOT touched
    # (marker absence != debris — markers are gitignored/re-derivable). Investigate; do not ignore.
    if res.get('reconcile_skipped'):
        sys.stderr.write(f"WARNING: {len(res['reconcile_skipped'])} TRACKED note(s) share a pending "
                         f"chunk's ref and were NOT archived (anomalous — a committed note vs a "
                         f"'pending' chunk, e.g. a fresh clone / rebuilt _sources; investigate):\n")
        for ref, src, why in res['reconcile_skipped'][:20]:
            sys.stderr.write(f"  {os.path.relpath(src, ROOT)}  [{ref}] ({why})\n")
    # Orphan markers = a done-marker with no chunk in this wave (e.g. a chunk the extract dropped on
    # a later selection pass). Harmless to a wave-driven work-list (we iterate the wave, not the
    # marker dir) but worth surfacing so a polluted done-dir doesn't hide silently.
    orphans = sorted(res['done_markers'] - res['seen'])
    if orphans:
        sys.stderr.write(f"note: {len(orphans)} done-marker(s) not in this wave (ignored): "
                         + ", ".join(orphans[:12]) + ("…" if len(orphans) > 12 else "") + "\n")


if __name__ == '__main__':
    main()

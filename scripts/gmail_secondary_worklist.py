#!/usr/bin/env python3
"""secondary pass: emit the pending distillation run-list (resumable + idempotent).

pending = KEEP threads  −  cross-account dups (already covered by the personal pass)
                        −  already-done (note written OR drop marker on disk)

Cross-account dedup: a secondary thread whose RFC822 Message-IDs are >=50% present in the
personal pass's KEPT threads is the SAME conversation already processed personally -> skip
(it either became a personal note or was judged non-durable there; either way don't redo it).
We use personal KEPT threads, NOT the whole personal mailbox, because a secondary-business
thread the owner answered from the secondary address can sit in the personal mailbox but was DROPPED
by the personal selector (no personal Sent) — those must still be distilled here.

Dedup decisions are made durable as drop markers (reason "dup:personal-account") so they are
computed once and the run is resumable. Done-detection keys off the collision-safe filename
suffix `-sec<thrid_last8>` (distinct from the personal pass's `-<thrid_last6>`) + drop markers.

Usage: python3 scripts/gmail_secondary_worklist.py
"""
import sys, os, re, json, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.constants import DOMAINS
from _lib.provenance import parse_ref, thread_id

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = os.path.join(ROOT, '_sources/gmail-secondary-keep.jsonl')
RUN = os.path.join(ROOT, '_sources/gmail-secondary-run.jsonl')
DROPS_D = os.path.join(ROOT, '_sources/gmail-secondary-drops.d')
P_KEEP = os.path.join(ROOT, '_sources/gmail-keep.jsonl')
P_INDEX = os.path.join(ROOT, '_sources/gmail-index.jsonl')
# Done-detection keys off the provenance ref written INSIDE each note, not the filename — the
# distiller occasionally emits a wrong/garbled `-sec<...>` suffix, so filename matching misses notes
# (a missed note gets re-queued forever, then re-distilled into a duplicate). secondary KEEP thrids
# (decimal X-GM-THRID from the mbox) are all numeric; the shared parse is permissive (also matches hex gmail ids), which only adds
# non-matching ids to `done` and never changes which numeric KEEP thrid is marked done.


def personal_kept_msgids():
    """RFC822 Message-IDs of the PRIMARY pass's kept threads (its mbox backfill's gmail-keep.jsonl +
    gmail-index.jsonl). Absent when the primary account was only ever synced through the connector:
    then there is nothing to dedup against and every secondary thread is distilled here."""
    if not (os.path.exists(P_KEEP) and os.path.exists(P_INDEX)):
        sys.stderr.write("cross-account dedup SKIPPED: no primary-pass gmail-keep.jsonl / "
                         "gmail-index.jsonl in _sources/\n")
        return set()
    kept = set()
    for line in open(P_KEEP, encoding='utf-8'):
        try:
            kept.add(json.loads(line)['thrid'])
        except Exception:
            pass
    mids = set()
    for line in open(P_INDEX, encoding='utf-8'):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('thrid') in kept and r.get('msgid'):
            mids.add(r['msgid'].strip())
    return mids


def done_state():
    """Return (done_thrids, drops). A thread is done iff a LIVE note carries its provenance ref
    `gmail:thread:<thrid>` — authoritative even when the distiller wrote a wrong/garbled filename
    suffix (which it occasionally does), unlike filename-based matching."""
    done, drops = set(), set()
    for dom in DOMAINS:
        for fp in glob.glob(os.path.join(ROOT, dom, '**/*.md'), recursive=True):
            try:
                with open(fp, encoding='utf-8') as fh:
                    for ln in fh:
                        tid = thread_id(parse_ref(ln))
                        if tid:
                            done.add(tid)
                            break
                        if ln.startswith('## '):   # past frontmatter, no ref -> stop
                            break
            except Exception:
                continue
    if os.path.isdir(DROPS_D):
        for fp in glob.glob(os.path.join(DROPS_D, '*.json')):
            drops.add(os.path.splitext(os.path.basename(fp))[0])
    return done, drops


def main():
    os.makedirs(DROPS_D, exist_ok=True)
    pmids = personal_kept_msgids()
    sys.stderr.write(f"personal-kept msgids: {len(pmids):,}\n")
    done_thrids, done_drops = done_state()

    n_total = n_dup = n_done = n_pending = 0
    with open(RUN, 'w', encoding='utf-8') as out:
        for line in open(KEEP, encoding='utf-8'):
            d = json.loads(line)
            t = str(d['thrid'])
            n_total += 1
            # already finished (note carries this thread's provenance, or any drop marker)
            if t in done_drops or t in done_thrids:
                n_done += 1
                continue
            # cross-account dedup -> durable drop marker, skip
            mids = [m.strip() for m in (d.get('msgids') or []) if m]
            frac = (sum(1 for m in mids if m in pmids) / len(mids)) if mids else 0.0
            if frac >= 0.5:
                with open(os.path.join(DROPS_D, f"{t}.json"), 'w', encoding='utf-8') as mk:
                    json.dump({"thrid": t, "reason": "dup:personal-account"}, mk)
                n_dup += 1
                continue
            out.write(json.dumps(d, ensure_ascii=False) + '\n')
            n_pending += 1

    sys.stderr.write(
        f"KEEP {n_total:,} | done {n_done:,} | dedup-skip {n_dup:,} | "
        f"PENDING {n_pending:,} -> {RUN}\n")


if __name__ == '__main__':
    main()

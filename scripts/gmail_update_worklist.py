#!/usr/bin/env python3
"""Build the Gmail THREAD-UPDATE work-list — GROWN threads to redistill IN PLACE (resumable).

The incremental sync runs `search_threads(after:<watermark> ...)`,
which returns a thread ONLY if it has post-watermark activity. So a thread in the incremental KEEP
wave (`_sources/gmail-wave.jsonl`) whose canonical `gmail_thread_key` is ALREADY in the store is a
GROWN thread: it was distilled once, then new messages were appended, and that new content is not yet
captured. `gmail_worklist.py` correctly drops those from the NEW-note wave (they are "already-done")
— THIS script routes them to the redistill-in-place (thread-update) flow instead of losing them.

    pending = { KEEP-wave threads whose canonical key IS in the store }   (= grown threads)
              MINUS dropped / PHI-held threads (same exclusions as gmail_worklist)
              MINUS threads already updated this pass (a `_sources/gmail-update-done.d/<key>` marker)
                    UNLESS that marker is STALE: the thread's newest message is later than the note's
                    `updated:` (fallback `date:`), i.e. it grew AGAIN after that update (the
                    grown-thread marker trap). A stale marker is kept on disk, never deleted; the
                    thread is re-emitted and a `stale-marker <thrid>` line printed.

"done" is derived from the notes + the done-markers, so an interrupted run resumes just by
regenerating the work-list — no workflow-internal state. Emits one record per grown thread:
{thrid, key, fp, title, date} — thrid = the connector HEX id (the distiller's get_thread arg),
key = the canonical INT key as a string (the marker name the distiller writes verbatim, so the
canonical-key derivation stays single-owner here in Python and the agent never recomputes it),
fp = the existing note path (root-relative) to rewrite in place, title/date read from the note head.

Usage:
  python3 scripts/gmail_update_worklist.py [<in-wave.jsonl>] [<out-update.jsonl>] \
        [--drops _sources/gmail-drops.jsonl] [--phi-report _sources/gmail-phi-report.jsonl] \
        [--root <repo-root>]
Defaults: in = _sources/gmail-wave.jsonl, out = _sources/gmail-update.jsonl (the same KEEP wave
gmail_worklist reads).
"""
import argparse
import email.utils
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.provenance import gmail_thread_key      # noqa: E402
from _lib.notes import gmail_done_map             # noqa: E402  (single-owner done-map scan)
from gmail_worklist import exclusion_keys         # noqa: E402  (single-owner drop/phi exclusions)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.M)
DATE_RE = re.compile(r'^date:\s*([0-9-]+)\s*$', re.M)
UPDATED_RE = re.compile(r'^updated:\s*([0-9-]+)\s*$', re.M)
# `THREAD <thrid> | N msg | <start>..<end> | labels: ...` — see gmail_render.py's header format.
HEADER_RANGE_RE = re.compile(r'^THREAD\s+\S+\s+\|\s+\d+\s+msg\s+\|\s+[0-9-]+\.\.([0-9-]+)\s+\|', re.M)
# `--- MSG n | <date> | From: ...` — one per message in the render (ISO from the connector renderer,
# RFC 2822 from the mbox backfill renderer).
MSG_DATE_RE = re.compile(r'^--- MSG \d+ \| (.*?) \| From:', re.M)


def head_fields(abspath):
    """(title, date) read from a note's frontmatter head — mirrors granola_redistill_worklist."""
    try:
        head = open(abspath, encoding='utf-8', errors='replace').read(1500)
    except OSError:
        return None, None
    tm = TITLE_RE.search(head)
    dm = DATE_RE.search(head)
    return (tm.group(1) if tm else None), (dm.group(1) if dm else None)


def note_known_date(abspath):
    """The existing note's own last-known coverage date, day-granular ('YYYY-MM-DD'), or None.

    = max(frontmatter `date:`, frontmatter `updated:`) — `date:` is the thread date as of the note's
    original distillation (frozen thereafter), `updated:` is stamped only when the note has since been
    redistilled in place (the gmail-update flow). Either is a lower bound on what the note already
    captures, so this is the right per-thread threshold for "did the wave actually add anything new" —
    unlike the global `gmail_personal.watermark_date`, which by the time THIS script runs has already
    been bumped forward by the main new-thread sync (SYNC.md: "the main gmail sync (step 6) owns
    `gmail_personal.watermark_date`" and this update flow runs AFTER that commit), so comparing a
    wave's newest-message date against it compares against the WRONG (next) watermark, not the one the
    wave was actually fetched under.
    """
    try:
        head = open(abspath, encoding='utf-8', errors='replace').read(1500)
    except OSError:
        return None
    dm = DATE_RE.search(head)
    um = UPDATED_RE.search(head)
    dates = [m.group(1)[:10] for m in (dm, um) if m]
    return max(dates) if dates else None


def _day(ts):
    """'YYYY-MM-DD' from an ISO or RFC 2822 message timestamp, or None if unparseable."""
    ts = (ts or '').strip()
    if re.match(r'\d{4}-\d{2}-\d{2}', ts):
        return ts[:10]
    try:
        return email.utils.parsedate_to_datetime(ts).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def newest_message_date(record, root, threads_dirname='gmail-threads'):
    """The kept-thread's newest message date, day-granular ('YYYY-MM-DD'), or None if unknown.

    = the max of every date available: the wave record's own `date` field (the connector-search date,
    which in practice IS the last message's date), and — from the rendered thread
    `_sources/<threads_dirname>/<thrid>.txt` (gmail_render.py / mbox_extract.py format) — the header's
    `..<end>` half and every `--- MSG n | <ts> |` line. Max, not first-found: a lagging record date
    must not hide a newer rendered message (under-reading "newest" is what loses a grown thread;
    over-reading costs one redundant in-place redistill).
    """
    dates = []
    d = record.get('date')
    if d:
        dates.append(d[:10])
    thrid = record.get('thrid')
    if thrid:
        txt_path = os.path.join(root, '_sources', threads_dirname, f'{thrid}.txt')
        try:
            text = open(txt_path, encoding='utf-8', errors='replace').read()
        except OSError:
            text = ''
        m = HEADER_RANGE_RE.match(text)
        if m:
            dates.append(m.group(1)[:10])
        dates.extend(filter(None, (_day(t) for t in MSG_DATE_RE.findall(text))))
    return max(dates) if dates else None


def main():
    ap = argparse.ArgumentParser(description="Build the Gmail grown-thread update work-list.")
    ap.add_argument('in_wave', nargs='?', default=None,
                    help="incremental KEEP wave (default _sources/gmail-wave.jsonl)")
    ap.add_argument('out', nargs='?', default=None,
                    help="update work-list output (default _sources/gmail-update.jsonl)")
    ap.add_argument('--drops', default=None, help="a gmail-drops jsonl to exclude")
    ap.add_argument('--phi-report', dest='phi_report', default=None,
                    help="a gmail-phi-report jsonl; level high|medium threads are held")
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--no-watermark-filter', action='store_true',
                    help="don't exclude kept threads whose newest message is no newer than the "
                         "existing note's own last-known date (opt-out of the not-grown filter)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    src_dir = os.path.join(root, '_sources')
    in_wave = args.in_wave or os.path.join(src_dir, 'gmail-wave.jsonl')
    in_wave = in_wave if os.path.isabs(in_wave) else os.path.join(root, in_wave)
    out_path = args.out or os.path.join(src_dir, 'gmail-update.jsonl')
    out_path = out_path if os.path.isabs(out_path) else os.path.join(root, out_path)

    done_dir = os.path.join(src_dir, 'gmail-update-done.d')
    os.makedirs(done_dir, exist_ok=True)
    done_markers = set(os.listdir(done_dir))     # marker filename == str(canonical key)

    done_map = gmail_done_map(root)              # canonical key -> existing note path (single owner)
    drops, phi_hold = exclusion_keys(os.path.dirname(os.path.abspath(in_wave)),
                                     args.drops, args.phi_report)
    filter_not_grown = not args.no_watermark_filter

    keep = grown = already_updated = not_grown = stale_marker = 0
    with open(out_path, 'w', encoding='utf-8') as out:
        for line in open(in_wave, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            keep += 1
            k = gmail_thread_key(r['thrid'])
            if k is None:
                continue                          # unparseable id — not a known thread
            if k not in done_map:
                continue                          # NEW thread -> gmail_worklist's job, not ours
            if k in drops or k in phi_hold:
                continue                          # excluded (dropped as non-durable / PHI-held)
            fp = done_map[k]
            has_marker = str(k) in done_markers
            if has_marker or filter_not_grown:
                known = note_known_date(os.path.join(root, fp))
                newest = newest_message_date(r, root)
            if has_marker:
                # resumable: already redistilled — unless the thread grew AGAIN after that update
                # (marker trap). Unknown dates can't prove staleness -> trust the marker.
                if newest is None or known is None or newest <= known:
                    already_updated += 1
                    continue
                print(f"stale-marker {r['thrid']} (newest {newest} > note's last-known {known}; "
                      f"marker kept, thread re-emitted as grown)")
                stale_marker += 1
            elif filter_not_grown:
                if newest is None or known is None:
                    print(f"WARN {r['thrid']}: no newest-message date and/or no known note date "
                          f"found — keeping as grown rather than guessing", file=sys.stderr)
                elif newest <= known:
                    print(f"not-grown {r['thrid']} (newest {newest} <= note's last-known {known})")
                    not_grown += 1
                    continue
            title, date = head_fields(os.path.join(root, fp))
            out.write(json.dumps({'thrid': r['thrid'], 'key': str(k), 'fp': fp,
                                  'title': title, 'date': date}, ensure_ascii=False) + '\n')
            grown += 1

    print(f"keep {keep} | grown(update) {grown} | already-updated {already_updated} | "
          f"not-grown {not_grown} | stale-marker {stale_marker} -> {os.path.relpath(out_path, root)}")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Stage B: extract KEEP-chat messages from chat.db -> chunked text files for distillation.

Reads chat.db READ-ONLY. For each chat in imessage-keep.jsonl, pulls messages chronologically,
decodes text from `attributedBody` (99.6% of msgs store text only there), renders sender +
date + text, and writes ~WINDOW-message chunk files to _sources/imessage/. Large chats span
many chunks. Emits one record per chunk to _sources/imessage-run.jsonl (the distiller's work-list).

Usage: python3 scripts/imsg_extract.py [--limit N]
"""
import os, sys, json, sqlite3, re, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ on path for _lib
from _lib.imsg_time import since_to_apple_ns, apple_ns_to_iso_second
DB = os.path.expanduser('~/Library/Messages/chat.db')
WINDOW = 500          # messages per chunk
MAXMSG = 1200         # cap per-message chars
APPLE_EPOCH = 978307200

# Incremental mode: `--since <ISO-8601 UTC timestamp | YYYY-MM-DD>` extracts only messages after the
# watermark, from the -incr keep-list, into -incr paths (never clobbers the backfill's run-list/
# chunks). A bare date is midnight UTC (back-compat); a timestamp is exact — chat.db is ns-granular
# and the query is strict `>`, so a same-day re-sync with the exact last ts fetches only
# strictly-newer messages. The ns math is single-owned in _lib/imsg_time.py (integer, exact).
SINCE = sys.argv[sys.argv.index('--since') + 1] if '--since' in sys.argv else None
SINCE_NS = since_to_apple_ns(SINCE)
_sfx = '-incr' if SINCE else ''
KEEP = os.path.join(ROOT, f'_sources/imessage{_sfx}-keep.jsonl')
OUTDIR = os.path.join(ROOT, f'_sources/imessage{_sfx}')
RUN = os.path.join(ROOT, f'_sources/imessage{_sfx}-run.jsonl')
# Max message timestamp this extract PROCESSED — the iMessage watermark. Ceil to the second so the
# strict-`>` `--since` re-fetches nothing already seen (no overlap). Both modes write this canonical
# sidecar; bump_watermark.py --source imessage reads it. See _manifest/SYNC.md "Watermark granularity".
MAXTS = os.path.join(ROOT, '_sources/imessage.maxts')


def decode_attributed_body(blob):
    if not blob or b'NSString' not in blob:
        return None
    s = blob.split(b'NSString', 1)[1]
    i = s.find(b'\x2b')
    if i == -1:
        return None
    s = s[i + 1:]
    if not s:
        return None
    n = s[0]
    if n == 0x81:
        n = int.from_bytes(s[1:3], 'little'); s = s[3:]
    elif n == 0x82:
        n = int.from_bytes(s[1:5], 'little'); s = s[5:]
    else:
        s = s[1:]
    return s[:n].decode('utf-8', 'replace')


def msg_text(text, ab, has_att):
    t = text if (text and text.strip()) else (decode_attributed_body(ab) or '')
    t = t.replace('￼', '').strip()      # strip object-replacement (attachment) chars
    if len(t) > MAXMSG:
        t = t[:MAXMSG] + '…'
    if not t and has_att:
        return '[attachment]'
    return t


def iso_dt(ns):
    if not ns:
        return ''
    return datetime.datetime.fromtimestamp(
        ns / 1e9 + APPLE_EPOCH, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')


# chunk_id_for is the single-owner chunk-id naming, now in _lib/chunking.py (shared with WhatsApp).
# Re-exported here so `from imsg_extract import chunk_id_for` keeps resolving (tests + old callers).
from _lib.chunking import chunk_id_for  # noqa: E402,F401


def main():
    limit = int(sys.argv[sys.argv.index('--limit') + 1]) if '--limit' in sys.argv else None
    os.makedirs(OUTDIR, exist_ok=True)
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    handle_map = dict(c.execute("SELECT ROWID, id FROM handle").fetchall())
    chats = [json.loads(l) for l in open(KEEP, encoding='utf-8')]
    n_chunks = n_chats = 0
    max_ns = 0                         # max message.date processed this run -> the watermark
    with open(RUN, 'w', encoding='utf-8') as run:
        for ch in chats:
            rows = c.execute(
                "SELECT m.date, m.is_from_me, m.handle_id, m.text, m.attributedBody, m.cache_has_attachments "
                "FROM message m JOIN chat_message_join j ON m.ROWID=j.message_id "
                "WHERE j.chat_id=? AND m.date>? ORDER BY m.date", (ch['chat_id'], SINCE_NS)).fetchall()
            if not rows:
                continue
            n_chats += 1
            max_ns = max(max_ns, rows[-1][0] or 0)   # rows are ORDER BY m.date ASC -> last is newest
            who = ch['display_name'] or (ch['handles'][0] if ch['handles'] else ch['identifier'])
            parts = ('GROUP: ' + (ch['display_name'] or 'unnamed') + ' [' + ', '.join(ch['handles']) + ']') \
                if ch['is_group'] else ('1:1 with ' + (ch['handles'][0] if ch['handles'] else ch['identifier']))
            for ci in range(0, len(rows), WINDOW):
                chunk = rows[ci:ci + WINDOW]
                cnum = ci // WINDOW
                ds, de = iso_dt(chunk[0][0])[:10], iso_dt(chunk[-1][0])[:10]
                lines = [f"iMESSAGE CHUNK | chat_id={ch['chat_id']} guid={ch['guid']} chunk={cnum}",
                         f"{parts}", f"messages {ci+1}-{ci+len(chunk)} of {len(rows)} | {ds}..{de}",
                         "=" * 72]
                for date, fromme, hid, text, ab, att in chunk:
                    sender = 'Me' if fromme else handle_map.get(hid, '?')
                    t = msg_text(text, ab, att)
                    if not t:
                        continue
                    lines.append(f"[{iso_dt(date)}] {sender}: {t}")
                fp = os.path.join(OUTDIR, f"{ch['chat_id']}__c{cnum:03d}.txt")
                open(fp, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
                # chunk_id must be unique across backfill/incremental AND across successive
                # incremental syncs (Bugs 2 + 3) — the whole rationale + `--since` namespacing lives
                # in chunk_id_for(). Only the chunk_id (marker/ref/RESULT stem) carries the namespace;
                # the ephemeral, gitignored .txt keeps its {chat}__c{n} name in the -incr dir (it is
                # regenerated and consumed each run, so a same-name overwrite across runs is harmless).
                chunk_id = chunk_id_for(ch['chat_id'], cnum, SINCE)
                run.write(json.dumps({
                    'chunk_id': chunk_id,
                    'chat_id': ch['chat_id'], 'guid': ch['guid'], 'is_group': ch['is_group'],
                    'who': who, 'date_start': ds, 'date_end': de,
                    'fp': os.path.relpath(fp, ROOT), 'n_msgs': len(chunk),
                }, ensure_ascii=False) + '\n')
                n_chunks += 1
            if limit and n_chats >= limit:
                break
    print(f"extracted {n_chats} chats -> {n_chunks} chunks (window {WINDOW}) -> {OUTDIR}")
    print(f"work-list -> {RUN}")
    if max_ns:
        maxts = apple_ns_to_iso_second(max_ns, ceil=True)
        with open(MAXTS, 'w', encoding='utf-8') as f:
            f.write(maxts + '\n')
        print(f"max processed message ts (watermark, ceil-to-second) -> {maxts}  "
              f"[{os.path.relpath(MAXTS, ROOT)}]")


if __name__ == '__main__':
    main()

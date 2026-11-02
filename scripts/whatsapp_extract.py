#!/usr/bin/env python3
"""WhatsApp Desktop (macOS) → chunked text files for distillation — the WhatsApp analog of imsg_extract.

Reads the native Mac app's `ChatStorage.sqlite` READ-ONLY (it is PLAINTEXT SQLite, Core Data / ZWA*
schema, in ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/). Emits per-chunk text
files + a work-list `run.jsonl` in the SAME shape imsg_worklist.py + the distiller already consume,
plus a `whatsapp.maxts` sidecar (the watermark). So the existing resume/dedup/wave-contract plumbing
is reused; only the source connector changes.

Incremental: `--since <ISO-8601 UTC | YYYY-MM-DD>` filters ZWAMESSAGE.ZMESSAGEDATE. WhatsApp stores
that as a Cocoa/Core-Data timestamp (SECONDS since 2001-01-01 UTC); +978307200 = Unix. The watermark
is a TIMESTAMP, same convention as iMessage. chunk_ids are namespaced by the run's --since watermark
via the single-owner imsg_extract.chunk_id_for() (Bug-2/3-safe from day one).

Keep/drop (a chat is KEPT if): JID is a real person (@s.whatsapp.net) or a group (@g.us), AND it has
>= min_keep in-window messages — min_keep is 1 in INCREMENTAL mode (capture every post-watermark
message, mirroring imsg_select) and MINMSG in BACKFILL. Dropped: @broadcast / status / non-person JIDs.

Usage:
  python3 scripts/whatsapp_extract.py --since 2026-07-01T00:00:00Z     # incremental delta
  python3 scripts/whatsapp_extract.py                                  # full backfill
  python3 scripts/whatsapp_extract.py --db /path/to/ChatStorage.sqlite # override (e.g. a snapshot)
"""
import os, sys, json, sqlite3, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for _lib + imsg_extract
from _lib.chunking import chunk_id_for   # single owner of the (Bug-2/3-safe) chunk_id naming
from _lib.imsg_time import APPLE_EPOCH as COCOA_EPOCH   # 2001-01-01 epoch — single owner (dedup)

DEFAULT_DB = os.path.expanduser(
    '~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite')
WINDOW = 500                     # messages per chunk
MAXMSG = 1200                    # cap per-message chars
MINMSG = 5                       # BACKFILL only: skip trivial contacts with < this many total msgs


def min_keep(since):
    """In-window messages a chat needs to be kept. INCREMENTAL (`--since` set): 1 — every
    post-watermark message MUST be captured, else the watermark (computed over kept chats) advances
    past a dropped chat's messages and they are never re-queried = silent PERMANENT loss (the HIGH
    the 2026-07-06 review caught). BACKFILL (no --since): MINMSG, to skip trivial contacts. Mirrors
    scripts/imsg_select.py `min_keep = 1 if SINCE else MIN_MSGS`."""
    return 1 if since else MINMSG


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


SINCE = arg('--since')
DB = arg('--db', DEFAULT_DB)
_sfx = '-incr' if SINCE else ''
OUTDIR = os.path.join(ROOT, f'_sources/whatsapp{_sfx}')
RUN = os.path.join(ROOT, f'_sources/whatsapp{_sfx}-run.jsonl')
MAXTS = os.path.join(ROOT, '_sources/whatsapp.maxts')


def since_to_cocoa(s):
    """ISO-8601 UTC timestamp OR bare YYYY-MM-DD (midnight UTC) -> Cocoa seconds. None -> -1 (all)."""
    if not s:
        return -1.0
    s = s.strip()
    if 'T' in s:
        dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    else:
        dt = datetime.datetime.fromisoformat(s + 'T00:00:00+00:00')
    return dt.timestamp() - COCOA_EPOCH


def cocoa_to_iso(cocoa, ceil=False):
    unix = cocoa + COCOA_EPOCH
    if ceil and unix != int(unix):
        unix = int(unix) + 1
    return datetime.datetime.fromtimestamp(int(unix), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def iso_dt(cocoa):
    return datetime.datetime.fromtimestamp(
        cocoa + COCOA_EPOCH, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')


def kind_of(jid):
    if not jid:
        return 'other'
    if jid.endswith('@g.us'):
        return 'group'
    if jid.endswith('@s.whatsapp.net'):
        return '1:1'
    return 'other'   # @broadcast, status@broadcast, newsletters, lid, etc. -> dropped


def msg_text(ztext, mtype):
    t = (ztext or '').replace('￼', '').strip()
    if len(t) > MAXMSG:
        t = t[:MAXMSG] + '…'
    if not t:
        return '[media]' if mtype and mtype != 0 else ''
    return t


def main():
    limit = int(arg('--limit', 0)) or None
    os.makedirs(OUTDIR, exist_ok=True)
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    since_cocoa = since_to_cocoa(SINCE)

    # chats ordered by most recent; keep only real people/groups
    chats = c.execute(
        "SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION ORDER BY ZLASTMESSAGEDATE DESC"
    ).fetchall()
    n_chats = n_chunks = dropped_small = dropped_kind = 0
    max_cocoa = -1.0
    with open(RUN, 'w', encoding='utf-8') as run:
        for pk, jid, pname in chats:
            k = kind_of(jid)
            if k == 'other':
                dropped_kind += 1
                continue
            rows = c.execute(
                "SELECT ZMESSAGEDATE, ZISFROMME, ZPUSHNAME, ZFROMJID, ZTEXT, ZMESSAGETYPE "
                "FROM ZWAMESSAGE WHERE ZCHATSESSION=? AND ZMESSAGEDATE>? ORDER BY ZMESSAGEDATE",
                (pk, since_cocoa)).fetchall()
            if len(rows) < min_keep(SINCE):
                if rows:
                    dropped_small += 1
                continue
            n_chats += 1
            max_cocoa = max(max_cocoa, rows[-1][0] or -1.0)
            name = pname or (jid.split('@')[0] if jid else str(pk))
            header = (f"GROUP: {name}" if k == 'group' else f"1:1 with {name}")
            for ci in range(0, len(rows), WINDOW):
                chunk = rows[ci:ci + WINDOW]
                cnum = ci // WINDOW
                ds, de = iso_dt(chunk[0][0])[:10], iso_dt(chunk[-1][0])[:10]
                lines = [f"WHATSAPP CHUNK | chat_id={pk} jid={jid} chunk={cnum}",
                         header, f"messages {ci+1}-{ci+len(chunk)} of {len(rows)} | {ds}..{de}",
                         "=" * 72]
                for zdate, fromme, push, fromjid, ztext, mtype in chunk:
                    sender = 'Me' if fromme else (push or (fromjid or '?').split('@')[0])
                    t = msg_text(ztext, mtype)
                    if not t:
                        continue
                    lines.append(f"[{iso_dt(zdate)}] {sender}: {t}")
                fp = os.path.join(OUTDIR, f"{pk}__c{cnum:03d}.txt")
                open(fp, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
                run.write(json.dumps({
                    'chunk_id': chunk_id_for(pk, cnum, SINCE),
                    'chat_id': pk, 'guid': jid, 'is_group': (k == 'group'),
                    'who': name, 'date_start': ds, 'date_end': de,
                    'fp': os.path.relpath(fp, ROOT), 'n_msgs': len(chunk),
                }, ensure_ascii=False) + '\n')
                n_chunks += 1
            if limit and n_chats >= limit:
                break
    print(f"KEEP chats: {n_chats} -> {n_chunks} chunks (window {WINDOW})  "
          f"[dropped {dropped_kind} non-person JIDs, {dropped_small} under {min_keep(SINCE)} msgs in-window]")
    print(f"work-list -> {os.path.relpath(RUN, ROOT)}")
    if max_cocoa >= 0:
        maxts = cocoa_to_iso(max_cocoa, ceil=True)
        with open(MAXTS, 'w', encoding='utf-8') as f:
            f.write(maxts + '\n')
        print(f"max processed message ts (watermark, ceil-to-second) -> {maxts}  "
              f"[{os.path.relpath(MAXTS, ROOT)}]")


if __name__ == '__main__':
    main()

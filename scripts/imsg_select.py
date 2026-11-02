#!/usr/bin/env python3
"""Stage A: build the iMessage KEEP work-list from chat.db (high-value scope).

Scope (the owner, 2026-06-30): keep conversations with >=20 messages, EXCLUDING automated senders
(short codes / alphanumeric sender-IDs like 2FA, banks, delivery, spam). Group chats are always
kept (not automated). Email-based iMessage contacts (handle has '@') are real people -> kept.

Reads chat.db READ-ONLY. Emits one record per kept chat to _sources/imessage-keep.jsonl.

Usage: python3 scripts/imsg_select.py
"""
import os, json, sqlite3, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ on path for _lib
from _lib.imsg_time import since_to_apple_ns
from _lib.imsg_markers import excluded_chat_guids
DB = os.path.expanduser('~/Library/Messages/chat.db')
MIN_MSGS = 20
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds

# Incremental mode: `--since <ISO-8601 UTC timestamp | YYYY-MM-DD>` keeps any chat with NEW messages
# after the watermark (no 20-msg threshold) and writes to a separate -incr keep-list so it never
# clobbers the backfill. A bare date is treated as midnight UTC (back-compat); an exact timestamp
# lets a same-day re-sync fetch only strictly-newer messages. The ns math (integer, no float
# precision loss above 2^53) is single-owned in _lib/imsg_time.py.
SINCE = None
if '--since' in sys.argv:
    SINCE = sys.argv[sys.argv.index('--since') + 1]
SINCE_NS = since_to_apple_ns(SINCE)
OUT = os.path.join(ROOT, '_sources/imessage-incr-keep.jsonl' if SINCE else '_sources/imessage-keep.jsonl')


def is_automated(handle_id):
    """True if a 1:1 partner handle is a machine sender (short code or alphanumeric sender-id)."""
    if not handle_id:
        return True
    h = handle_id.strip()
    if '@' in h:
        return False                      # email-based iMessage contact = real person
    digits = re.sub(r'\D', '', h)
    if re.search(r'[A-Za-z]', h):
        return True                       # alphanumeric sender ID (e.g. 'ATT', 'VERIZON')
    if 0 < len(digits) <= 6:
        return True                       # short code (2FA/bank/marketing)
    return False


def main():
    # owner-confirmed PHI conversations to NEVER (re)ingest — skipped at CHAT granularity so a re-sync
    # can't re-select a physician-patient thread (chat.db guid == the ref's `any;±;<id>` guid-part).
    excluded = excluded_chat_guids(ROOT)
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    # message counts per chat (incremental: only messages after the watermark)
    if SINCE:
        counts = dict(c.execute(
            "SELECT j.chat_id, count(*) FROM chat_message_join j JOIN message m ON j.message_id=m.ROWID "
            "WHERE m.date > ? GROUP BY j.chat_id", (SINCE_NS,)).fetchall())
    else:
        counts = dict(c.execute(
            "SELECT chat_id, count(*) FROM chat_message_join GROUP BY chat_id").fetchall())
    min_keep = 1 if SINCE else MIN_MSGS
    # handles per chat
    handles = {}
    for cid, hid in c.execute(
            "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON chj.handle_id=h.ROWID"):
        handles.setdefault(cid, []).append(hid)
    kept = dropped_auto = dropped_small = dropped_excluded = 0
    with open(OUT, 'w', encoding='utf-8') as out:
        for rowid, guid, ident, disp, style, svc in c.execute(
                "SELECT ROWID, guid, chat_identifier, display_name, style, service_name FROM chat"):
            if guid in excluded:
                dropped_excluded += 1        # PHI-excluded conversation — never (re)ingest
                continue
            n = counts.get(rowid, 0)
            if n < min_keep:
                dropped_small += 1
                continue
            hs = sorted(set(handles.get(rowid, [])))
            is_group = (style == 43)
            if not is_group and (not hs or is_automated(hs[0])):
                dropped_auto += 1
                continue
            # date range
            dr = c.execute(
                "SELECT MIN(m.date), MAX(m.date) FROM message m "
                "JOIN chat_message_join j ON m.ROWID=j.message_id WHERE j.chat_id=? AND m.date>0",
                (rowid,)).fetchone()
            def iso(ns):
                if not ns:
                    return None
                import datetime
                return datetime.datetime.fromtimestamp(
                    ns / 1e9 + APPLE_EPOCH, datetime.timezone.utc).date().isoformat()
            rec = {
                'chat_id': rowid,
                'guid': guid,
                'is_group': is_group,
                'display_name': disp or None,
                'identifier': ident,
                'handles': hs,
                'n_msgs': n,
                'date_start': iso(dr[0]),
                'date_end': iso(dr[1]),
            }
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            kept += 1
    print(f"KEEP chats: {kept}  (dropped {dropped_auto} automated 1:1, {dropped_small} under "
          f"{MIN_MSGS} msgs, {dropped_excluded} PHI-excluded)")
    print(f"-> {OUT}")


if __name__ == '__main__':
    main()

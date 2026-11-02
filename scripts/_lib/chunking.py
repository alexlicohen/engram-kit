"""Single owner of the chat-DB chunk_id naming — shared by the iMessage and WhatsApp extractors
(and any future chat-DB source). Moved here from imsg_extract.py so whatsapp_extract imports it from
`_lib` rather than depending on a sibling *script* being importable (the "consolidate the extractors
into _lib" cleanup, 2026-07-07). imsg_extract re-exports it for back-compat, so
`from imsg_extract import chunk_id_for` still resolves.

    from _lib.chunking import chunk_id_for
"""
import os
import re


def default_wave(source, root):
    """The wave a BARE worklist invocation should consume: the NEWEST (by mtime) of the
    incremental run `<source>-incr-run.jsonl` (written by an incremental extract) and the backfill
    run `<source>-run.jsonl`. This kills the false-'0 pending' class of bug: after an incremental
    extract, a bare worklist used to read the stale BACKFILL wave (all chunks done) and report
    nothing pending, silently dropping the new messages. Whichever extract ran most recently wrote
    the newest run file, so its wave wins. An explicit wave argument always overrides this.
    """
    src = os.path.join(root, '_sources')
    incr = os.path.join(src, f'{source}-incr-run.jsonl')
    base = os.path.join(src, f'{source}-run.jsonl')
    present = [p for p in (incr, base) if os.path.exists(p)]
    if not present:
        return base  # neither exists → return the canonical name so the caller errors loudly
    return max(present, key=os.path.getmtime)


def chunk_id_for(chat_id, cnum, since=None):
    """The chunk_id — which is ALSO the done-marker name, the RESULT-file name, and the
    `<source>:<guid>:<chunk_id>` ref stem; treated as an OPAQUE string everywhere downstream (no code
    parses its shape). It must be unique across TWO axes so a stale done-marker in the shared
    `*-done.d/` dir can never mask genuinely-new work:

      * Bug 2 (incremental vs BACKFILL): a delta chunk must not reuse a backfill's `{chat}_c{n}` id,
        or the backfill's marker would skip it.
      * Bug 3 (incremental vs INCREMENTAL, successive syncs): every incremental run numbers `cnum`
        from 0, so a chat with new activity in two consecutive syncs would reuse `{chat}_incr_c000`
        and the FIRST run's marker would silently mask the SECOND run's genuinely-new messages
        (found 2026-07-05: chat 12 had 33 new msgs skipped). Namespacing by the run's `--since`
        watermark — which strictly advances every sync — makes ids unique ACROSS runs, while staying
        IDENTICAL on a RESUME of the same window (same `--since` -> same ids -> done-markers resume).

    `since` is the raw `--since` value (ISO-8601 UTC timestamp or bare date); None = backfill mode.
    """
    if since is None:
        return f"{chat_id}_c{cnum:03d}"                    # backfill (unchanged)
    tag = re.sub(r"[^0-9A-Za-z]", "", since)               # 2026-07-02T20:02:16Z -> 20260702T200216Z
    return f"{chat_id}_incr{tag}_c{cnum:03d}"

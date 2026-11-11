#!/usr/bin/env python3
"""Render a Gmail CONNECTOR thread (get_thread JSON) to the standard `_sources/gmail-threads/<thrid>.txt`
format the Gmail distiller reads — the connector-path counterpart to the mbox backfill renderer, which
was the only one that existed (v3.6 sync finding: SYNC.md said "render kept threads" but no script owned
the connector path, so it was done ad-hoc inline).

The orchestrator calls the Gmail MCP `get_thread(FULL_CONTENT)` for each KEEP thread, saves the JSON,
and pipes it here. Prefers `plaintextBody`; falls back to a light HTML→text strip. Matches the backfill
`.txt` shape exactly: a header line + subject + participants + a rule, then one `--- MSG n | date |
From | To ---` block per message (bodies capped), each ending in a `[attachments: a, b]` line when the
message has named attachments (the backfill renderer's convention).

    python3 scripts/gmail_render.py <thread.json>            # one get_thread response
    python3 scripts/gmail_render.py --stdin < resp.json      # same, from stdin
    python3 scripts/gmail_render.py <resp.json> --out-dir _sources/gmail-threads   # default out dir

`<thread.json>` may be a single get_thread object `{id, messages:[...]}`, a list of them, or a
search_threads page `{threads:[...]}`. Writes one `<thrid>.txt` per thread and prints the paths.
Read-only except the rendered `.txt` files.

METADATA RENDERS (added 2026-10-11). Renders are metadata for the Python steps only (un-drop check,
not-grown dates); no step reads their message text. A thread with NO body field on any message (a
get_thread `METADATA_ONLY`/`MINIMAL` response, or a search_threads thread) renders the same header and
`--- MSG n | date | From | To ---` lines with a placeholder body, and the header carries
`| render: metadata`. Small metadata JSON is cheap to relay verbatim, unlike a full body. Because the
relaying agent could still retype it, a metadata thread is rendered only if it passes `metadata_problem`:
every message has `id`/`historyId`/`date`/`sender` (a genuine connector dump carries `historyId`), `date`
agrees with `internalDate` when present, and `messageCount` (search_threads) equals the messages shown.
search_threads returns only a thread's newest 5 messages, and a truncated render could hide an
earlier post-drop reply from the owner, so it is refused (fetch `get_thread(METADATA_ONLY)` instead). A
refused thread is skipped, its old render is left alone, and the exit code is 2. A missing render already fails
closed in both worklists.

UNREACHABLE MESSAGES (added 2026-11-10, the owner approved). `get_thread` omits messages the connector cannot
return (drafts, and trashed/deleted ones Gmail still counts), so `messageCount` can exceed the messages
shown by 1 on a thread that is NOT truncated; such a thread was refused forever. `--allow-missing
<thrid>=<N>` (repeatable) accepts exactly that thread with exactly that shortfall, N in
1..MAX_ALLOW_MISSING. Pass it only after a second `get_thread(METADATA_ONLY)` returns the same
shortfall; never for a search_threads page (its newest-5 cap is real truncation). A different shortfall,
or an override on a thread with none, is still refused, and the header records `| missing: N`.
The override is honoured only when the input file is ONE bare get_thread object: a page, or a list
(a page with its wrapper stripped), is refused. A single thread lifted out of a page has the same shape
as a get_thread response, so an override on a thread showing exactly SEARCH_PAGE_CAP messages is refused
too (it cannot be told from the newest-5 cut); that thread stays pending. An override that matches no
thread, or names a full-content thread, is reported as UNUSED.
"""
import argparse
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for _lib
from _lib.emailtext import strip_html   # noqa: E402  (single owner of the HTML→text strip)
MAXBODY = 6000   # cap per-message body (a single promo email can be huge; the substance is up top)
BODY_KEYS = ("plaintextBody", "plainTextBody", "plain_body", "textBody", "body", "text")
METADATA_BODY = "[metadata-only render: body not fetched]"
MAX_ALLOW_MISSING = 2   # --allow-missing ceiling: a larger gap is truncation, not an unreachable message
SEARCH_PAGE_CAP = 5     # search_threads returns a thread's newest 5 messages


def _body(m):
    for k in BODY_KEYS:
        if m.get(k):
            return m[k].strip()
    return strip_html(m.get("htmlBody", ""))


def is_metadata(thr):
    """True when no message carries a body field at all (METADATA_ONLY / MINIMAL / search_threads).
    Key PRESENCE, not truthiness: an empty plaintextBody is a full fetch of an empty message."""
    return all(not any(k in m for k in BODY_KEYS + ("htmlBody",)) for m in thr.get("messages") or [])


def _iso_utc(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def shortfall(thr):
    """messageCount minus the messages shown (0 when the response carries no messageCount)."""
    if "messageCount" not in thr:
        return 0
    return int(thr["messageCount"]) - len(thr.get("messages") or [])


def override_problem(thr, single):
    """Why --allow-missing must not be honoured for this thread, or None. `single` = the input file
    was one bare get_thread object. See UNREACHABLE MESSAGES in the module docstring."""
    if not single:
        return ("--allow-missing is accepted only for a single get_thread object, never for a "
                "search_threads page or a list of threads")
    if len(thr.get("messages") or []) == SEARCH_PAGE_CAP:
        return (f"--allow-missing refused: exactly {SEARCH_PAGE_CAP} messages shown, which cannot be "
                "told from a search_threads newest-5 cut; the thread stays pending")
    return None


def metadata_problem(thr, allow_missing=0):
    """Why a metadata thread must not be rendered, or None if it is fit. See the module docstring.
    `allow_missing` is THIS thread's --allow-missing value (0 = none): it excuses a shortfall of
    exactly that size and nothing else."""
    msgs = thr.get("messages") or []
    gap = shortfall(thr)
    if allow_missing:
        if not 1 <= allow_missing <= MAX_ALLOW_MISSING:
            return f"--allow-missing {allow_missing} is outside 1..{MAX_ALLOW_MISSING}"
        if gap != allow_missing:
            return (f"--allow-missing {allow_missing} does not match the shortfall {gap} "
                    f"({len(msgs)} of {thr.get('messageCount', len(msgs))} messages shown)")
    elif gap != 0:
        return (f"truncated: {len(msgs)} of {thr['messageCount']} messages shown "
                "(search_threads caps at 5; get_thread omits drafts and trashed messages) — fetch "
                "get_thread(METADATA_ONLY); if the same shortfall repeats, see --allow-missing")
    for i, m in enumerate(msgs, 1):
        missing = [k for k in ("id", "historyId", "date", "sender") if not m.get(k)]
        if missing:
            return f"MSG {i} missing {','.join(missing)} — not a verbatim connector dump"
        dt = _iso_utc(m["date"])
        if dt is None or dt.tzinfo is None:
            return f"MSG {i} date {m['date']!r} is not an ISO-8601 UTC timestamp"
        if m.get("internalDate") not in (None, ""):
            want = datetime.datetime.fromtimestamp(int(m["internalDate"]) / 1000, datetime.timezone.utc)
            if abs((dt - want).total_seconds()) > 1:
                return f"MSG {i} date {m['date']} disagrees with internalDate {m['internalDate']}"
    return None


def _attachments(m):
    """`[attachments: a, b]` for a connector message's named attachments ('' if none) — the same
    trailing line mbox_extract.message_text appends to backfill renders (first 20 names)."""
    names = [str(a.get("filename")).strip() for a in (m.get("attachments") or [])
             if isinstance(a, dict) and str(a.get("filename") or "").strip()]
    return ("\n[attachments: " + ", ".join(names[:20]) + "]") if names else ""


def render(thr, missing=0):
    """Return (thrid, rendered_text) for one get_thread object. thrid is None (skipped by main) if the
    object has no id; empty text if it has no messages. `missing` (an accepted --allow-missing
    shortfall) is recorded in the header."""
    thrid = thr.get("id")
    msgs = thr.get("messages") or []
    if not thrid or not msgs:
        return thrid, ""
    meta = is_metadata(thr)
    parts = set()
    labels = set()
    for m in msgs:
        if m.get("sender"):
            parts.add(m["sender"])
        parts.update(m.get("toRecipients") or [])
        labels.update(m.get("labelIds") or [])   # union across ALL messages, not just the first
    hdr = [
        f"THREAD {thrid} | {len(msgs)} msg | {msgs[0].get('date','')[:10]}..{msgs[-1].get('date','')[:10]}"
        f" | labels: {','.join(sorted(labels))}" + (" | render: metadata" if meta else "")
        + (f" | missing: {missing}" if missing else ""),
        f"subject: {msgs[0].get('subject', '')}",
        "participants: " + ", ".join(sorted(p for p in parts if p)),
        "=" * 72,
    ]
    for i, m in enumerate(msgs, 1):
        hdr.append(f"\n--- MSG {i} | {m.get('date','')} | From: {m.get('sender','')} | "
                   f"To: {', '.join(m.get('toRecipients') or [])} ---\n"
                   f"{METADATA_BODY if meta else _body(m)[:MAXBODY]}{_attachments(m)}")
    return thrid, "\n".join(hdr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="?", help="get_thread JSON file (object or list); omit with --stdin")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "_sources", "gmail-threads"))
    ap.add_argument("--allow-missing", action="append", default=[], metavar="THRID=N",
                    help="accept THRID with exactly N messages the connector cannot return "
                         "(repeatable; see the module docstring)")
    a = ap.parse_args()
    allow = {}
    for spec in a.allow_missing:
        thrid_, _, n = spec.partition("=")
        if not thrid_ or not n.isdigit() or int(n) < 1:
            ap.error(f"--allow-missing wants THRID=N with N >= 1, got {spec!r}")
        allow[thrid_] = int(n)
    raw = sys.stdin.read() if a.stdin or not a.json else open(a.json, encoding="utf-8").read()
    data = json.loads(raw)
    # The override is for ONE bare get_thread object. A page, or a list (a page whose wrapper was
    # stripped), carries search_threads' newest-5 cut, which is real truncation.
    single = isinstance(data, dict) and "threads" not in data
    if isinstance(data, dict) and "threads" in data:
        data = data["threads"] or []   # a search_threads page
    threads = data if isinstance(data, list) else [data]
    os.makedirs(a.out_dir, exist_ok=True)
    refused = 0
    used = set()
    for thr in threads:
        meta = is_metadata(thr)
        n_allow = allow.get(thr.get("id"), 0) if meta else 0
        thrid, text = render(thr, missing=n_allow)
        if not text:
            print(f"SKIP {thrid or '<no id>'} (no id or no messages)", file=sys.stderr)
            continue
        if meta:
            if n_allow:
                used.add(thrid)
            why = override_problem(thr, single) if n_allow else None
            why = why or metadata_problem(thr, n_allow)
            if why:
                print(f"REFUSED {thrid}: {why}", file=sys.stderr)
                refused += 1
                continue
            out = os.path.join(a.out_dir, f"{thrid}.txt")
            open(out, "w", encoding="utf-8").write(text)
            print(f"{thrid} -> {os.path.relpath(out, ROOT)} ({len(text):,} chars, metadata)")
            continue
        # warn if EVERY message body rendered empty (unknown body key / all-boilerplate HTML) — the
        # distiller would otherwise make a hollow note from a header-only render.
        if all(not _body(m) for m in thr.get("messages", [])):
            print(f"WARN {thrid}: all message bodies rendered empty — check the connector body field",
                  file=sys.stderr)
        out = os.path.join(a.out_dir, f"{thrid}.txt")
        open(out, "w", encoding="utf-8").write(text)
        print(f"{thrid} -> {os.path.relpath(out, ROOT)} ({len(text):,} chars)")
    for thrid_ in sorted(set(allow) - used):
        print(f"UNUSED --allow-missing {thrid_}: no metadata thread with that id in the input "
              "(wrong id, or a full-content thread)", file=sys.stderr)
    if refused:
        sys.exit(2)


if __name__ == "__main__":
    main()

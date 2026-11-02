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


def metadata_problem(thr):
    """Why a metadata thread must not be rendered, or None if it is fit. See the module docstring."""
    msgs = thr.get("messages") or []
    if "messageCount" in thr and int(thr["messageCount"]) != len(msgs):
        return (f"truncated: {len(msgs)} of {thr['messageCount']} messages shown "
                "(search_threads caps at 5) — fetch get_thread(METADATA_ONLY)")
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


def render(thr):
    """Return (thrid, rendered_text) for one get_thread object. thrid is None (skipped by main) if the
    object has no id; empty text if it has no messages."""
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
        f" | labels: {','.join(sorted(labels))}" + (" | render: metadata" if meta else ""),
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
    a = ap.parse_args()
    raw = sys.stdin.read() if a.stdin or not a.json else open(a.json, encoding="utf-8").read()
    data = json.loads(raw)
    if isinstance(data, dict) and "threads" in data:
        data = data["threads"] or []   # a search_threads page
    threads = data if isinstance(data, list) else [data]
    os.makedirs(a.out_dir, exist_ok=True)
    refused = 0
    for thr in threads:
        thrid, text = render(thr)
        if not text:
            print(f"SKIP {thrid or '<no id>'} (no id or no messages)", file=sys.stderr)
            continue
        if is_metadata(thr):
            why = metadata_problem(thr)
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
    if refused:
        sys.exit(2)


if __name__ == "__main__":
    main()

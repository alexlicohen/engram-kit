#!/usr/bin/env python3
"""Stream a Gmail-Takeout mbox and emit one JSON record per message (headers only).

Memory-safe for arbitrarily large mboxes: iterates line by line, never holds more
than one message's header block in memory, and writes records through immediately.
Body bytes are skipped (not parsed) — only header fields needed for triage/extraction
are captured, plus the [start, end) byte offsets so a later pass can seek+read the
full message without re-scanning the whole file.

Gmail Takeout specifics this relies on:
  - message separator lines look like:  From <THRID>@xxx <asctime>
    (the literal '@xxx' envelope placeholder makes the boundary unambiguous, so body
     lines that happen to start with 'From ' are NOT misdetected as new messages).
  - X-GM-THRID    : authoritative Gmail thread id (group threads by this).
  - X-Gmail-Labels: the real Gmail labels (incl. custom ones) — primary triage signal.

Usage:
  python3 scripts/mbox_index.py <mbox-path> <out.jsonl>
"""
import sys, os, re, json, email, email.utils, email.header

BOUNDARY = re.compile(rb'^From \d+@xxx ')          # Gmail Takeout envelope line
ANY_FROM = re.compile(rb'^From ')                   # to count non-Gmail-style boundaries (anomaly check)


_FOLD = re.compile(r'\r?\n[ \t]*')   # RFC5322 folding-whitespace -> single space


def _unfold(s):
    return _FOLD.sub(' ', s).strip() if s else s


def _dec(raw):
    """RFC2047-decode + unfold a header value to str. NEVER returns a Header/bytes.

    compat32's Message.get() can return an email.header.Header for some 8-bit/malformed
    values; decode it by hand into plain str so json.dumps can always serialize it.
    """
    if raw is None:
        return None
    try:
        parts = []
        for txt, enc in email.header.decode_header(raw):
            if isinstance(txt, bytes):
                try:
                    parts.append(txt.decode(enc or 'utf-8', 'replace'))
                except (LookupError, TypeError):
                    parts.append(txt.decode('utf-8', 'replace'))
            else:
                parts.append(txt)
        return _unfold(''.join(parts))
    except Exception:
        return _unfold(str(raw))      # last resort — force str, never a Header


def _g(msg, name):
    """msg.get(name) coerced to a stripped str, or None (Header-safe)."""
    v = msg.get(name)
    return str(v).strip() if v is not None else None


def _addrs(msg, field):
    out = []
    for _name, addr in email.utils.getaddresses(msg.get_all(field, [])):
        if addr:
            out.append(addr.lower())
    return out


def _build_rec(header_bytes, start, end):
    msg = email.message_from_bytes(header_bytes)
    labels_raw = _unfold(_g(msg, 'X-Gmail-Labels'))
    labels = [l.strip() for l in labels_raw.split(',') if l.strip()] if labels_raw else []
    from_pairs = email.utils.getaddresses(msg.get_all('From', []))
    from_name = _dec(from_pairs[0][0]) if from_pairs else None
    from_addr = (from_pairs[0][1].lower() if from_pairs and from_pairs[0][1] else None)
    date_raw = _g(msg, 'Date')
    date_iso = None
    if date_raw:
        try:
            dt = email.utils.parsedate_to_datetime(date_raw)
            date_iso = dt.date().isoformat() if dt else None
        except Exception:
            date_iso = None
    thrid = _g(msg, 'X-GM-THRID') or None
    rec = {
        "thrid": thrid,
        "labels": labels,
        "msgid": _g(msg, 'Message-ID') or None,
        "from_name": from_name,
        "from_addr": from_addr,
        "to": _addrs(msg, 'To'),
        "cc": _addrs(msg, 'Cc'),
        "subject": _dec(msg.get('Subject')),
        "date": date_iso,
        "date_raw": str(date_raw) if date_raw is not None else None,
        "list_id": _g(msg, 'List-Id') or None,
        "list_unsub": bool(msg.get('List-Unsubscribe')),
        "precedence": (_g(msg, 'Precedence') or '').lower() or None,
        "auto_submitted": (_g(msg, 'Auto-Submitted') or '').lower() or None,
        "in_reply_to": _g(msg, 'In-Reply-To') or None,
        "start": start,
        "end": end,
    }
    return rec


def emit(out, start, end, header_bytes, stats):
    """Build + write one JSON record. A parse/serialize failure on a single message
    becomes an `error` row (with valid start/end) — it never aborts the run."""
    try:
        rec = _build_rec(header_bytes, start, end)
        line = json.dumps(rec, ensure_ascii=False)
        thrid = rec.get('thrid')
    except Exception as e:
        stats['errors'] += 1
        thrid = None
        line = json.dumps({"thrid": None, "labels": [], "subject": None,
                           "from_addr": None, "to": [], "cc": [], "date": None,
                           "error": f"{type(e).__name__}: {str(e)[:200]}",
                           "start": start, "end": end}, ensure_ascii=False)
    out.write(line + "\n")
    stats['msgs'] += 1
    if thrid:
        stats['thrids'].add(thrid)
    bucket = stats['msgs'] // 25000
    if bucket > stats['last_bucket']:
        stats['last_bucket'] = bucket
        pct = 100.0 * end / stats['total']
        sys.stderr.write(
            f"\r  {stats['msgs']:>8,} msgs  {len(stats['thrids']):>8,} threads  "
            f"{end/1e9:6.1f}/{stats['total']/1e9:.1f} GB ({pct:4.1f}%)")
        sys.stderr.flush()


def main():
    mbox_path, out_path = sys.argv[1], sys.argv[2]
    # optional: resume by seeking to a byte offset that is the START of a boundary line,
    # appending to the existing index instead of truncating it.
    start_at = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    out_mode = 'a' if start_at > 0 else 'w'
    total = os.path.getsize(mbox_path)
    stats = {'msgs': 0, 'thrids': set(), 'odd_from': 0, 'errors': 0,
             'total': total, 'last_bucket': 0}
    offset = start_at
    cur_start = None
    in_headers = False
    header_lines = []

    with open(mbox_path, 'rb') as f, open(out_path, out_mode, encoding='utf-8') as out:
        if start_at:
            f.seek(start_at)
        for line in f:
            if BOUNDARY.match(line):
                if cur_start is not None:
                    emit(out, cur_start, offset, b''.join(header_lines), stats)
                cur_start = offset
                in_headers = True
                header_lines = []
            else:
                if ANY_FROM.match(line) and not in_headers:
                    stats['odd_from'] += 1     # a 'From ' line that isn't a Gmail boundary, at body level
                if in_headers:
                    if line in (b'\n', b'\r\n'):
                        in_headers = False
                    else:
                        header_lines.append(line)
            offset += len(line)
        if cur_start is not None:
            emit(out, cur_start, offset, b''.join(header_lines), stats)

    sys.stderr.write(
        f"\nDONE: {stats['msgs']:,} messages, {len(stats['thrids']):,} threads, "
        f"{stats['errors']:,} parse-error rows, "
        f"{stats['odd_from']:,} body-level 'From ' lines (informational).\n")


if __name__ == '__main__':
    main()

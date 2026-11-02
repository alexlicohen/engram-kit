#!/usr/bin/env python3
"""Stage C: extract KEEP-thread bodies from the mbox via byte offsets -> text files.

Random-access: for each thread in gmail-keep.jsonl, seek to each message's
[start, end) offset, parse its MIME, pull a plain-text rendering (text/plain, or
html stripped to text), and write one consolidated, distillation-ready text file
per thread to a gitignored output dir. One thread held in memory at a time.

Usage: python3 scripts/mbox_extract.py <mbox> <gmail-keep.jsonl> <out-dir> [--limit N]
"""
import sys, os, json, re, email, email.policy, html, quopri, base64

MAXMSG = 8000        # cap per-message body chars (quote chains bloat)
MAXTHREAD = 48000    # cap whole-thread file chars

_TAG = re.compile(r'(?is)<(script|style).*?>.*?</\1>')
_BR = re.compile(r'(?i)<\s*(br|/p|/div|/tr|/li)\s*/?>')
_ANYTAG = re.compile(r'(?s)<[^>]+>')
_WS = re.compile(r'[ \t]+')
_BLANKS = re.compile(r'\n\s*\n\s*\n+')


def html_to_text(s):
    s = _TAG.sub(' ', s)
    s = _BR.sub('\n', s)
    s = _ANYTAG.sub('', s)
    s = html.unescape(s)
    return s


def part_text(part):
    ctype = part.get_content_type()
    if ctype not in ('text/plain', 'text/html'):
        return None
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return None
    if payload is None:
        return None
    charset = part.get_content_charset() or 'utf-8'
    try:
        text = payload.decode(charset, 'replace')
    except (LookupError, TypeError):
        text = payload.decode('utf-8', 'replace')
    if ctype == 'text/html':
        text = html_to_text(text)
    return text


def message_text(raw):
    """Return (from, to, date, subject, body) for one raw RFC822 message (bytes)."""
    msg = email.message_from_bytes(raw)
    frm = str(msg.get('From') or '')
    to = str(msg.get('To') or '')
    date = str(msg.get('Date') or '')
    subj = str(msg.get('Subject') or '')
    plain, htmltxt, atts = [], [], []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            fn = part.get_filename()
            disp = (part.get('Content-Disposition') or '')
            if fn or 'attachment' in disp.lower():
                if fn:
                    atts.append(str(fn))
                continue
            t = part_text(part)
            if t is None:
                continue
            (plain if part.get_content_type() == 'text/plain' else htmltxt).append(t)
    else:
        t = part_text(msg)
        if t is not None:
            (plain if msg.get_content_type() == 'text/plain' else htmltxt).append(t)
    body = '\n'.join(plain) if plain else '\n'.join(htmltxt)
    body = _WS.sub(' ', body)
    body = _BLANKS.sub('\n\n', body).strip()
    if len(body) > MAXMSG:
        body = body[:MAXMSG] + '\n…[truncated]'
    if atts:
        body += '\n[attachments: ' + ', '.join(atts[:20]) + ']'
    return frm, to, date, subj, body


def extract_thread(f, rec):
    parts = []
    for start, end in rec['offsets']:
        f.seek(start)
        raw = f.read(end - start)
        # drop the leading "From " envelope line
        nl = raw.find(b'\n')
        if nl != -1 and raw[:5] == b'From ':
            raw = raw[nl + 1:]
        try:
            frm, to, date, subj, body = message_text(raw)
        except Exception as e:
            frm = to = date = subj = ''
            body = f'[extract-error: {type(e).__name__}]'
        parts.append((date, frm, to, subj, body))
    head = (f"THREAD {rec['thrid']} | {rec['n_msgs']} msg | "
            f"{rec.get('date_start')}..{rec.get('date_end')} | "
            f"labels: {', '.join(rec.get('labels') or []) or '(none)'}\n"
            f"subject: {rec.get('subject')}\n"
            f"participants: {', '.join(rec.get('participants') or [])}\n"
            + "=" * 72 + "\n")
    out = [head]
    for i, (date, frm, to, subj, body) in enumerate(parts, 1):
        out.append(f"\n--- MSG {i} | {date} | From: {frm} | To: {to} ---\n{body}\n")
    text = ''.join(out)
    if len(text) > MAXTHREAD:
        text = text[:MAXTHREAD] + '\n…[thread truncated]'
    return text


def main():
    mbox_path, keep_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    limit = None
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with open(mbox_path, 'rb') as f:
        for line in open(keep_path, encoding='utf-8'):
            rec = json.loads(line)
            text = extract_thread(f, rec)
            with open(os.path.join(out_dir, f"{rec['thrid']}.txt"), 'w', encoding='utf-8') as o:
                o.write(text)
            n += 1
            if n % 1000 == 0:
                sys.stderr.write(f"\r  extracted {n:,} threads")
                sys.stderr.flush()
            if limit and n >= limit:
                break
    sys.stderr.write(f"\nDONE: extracted {n:,} thread files to {out_dir}\n")


if __name__ == '__main__':
    main()

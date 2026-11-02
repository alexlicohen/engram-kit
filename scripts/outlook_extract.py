#!/usr/bin/env python3
"""Parse the Outlook `.eml` export (`_sources/outlook/*.eml`) into a per-message index.

Outlook "Save as .eml" writes ONE RFC822 message per file (unlike the Gmail mbox, which is one
big file with byte-offsets). So this is the Outlook counterpart to gmail's mbox index step: it
decodes each message's headers + body and emits one JSON record per message to
`_sources/outlook-index.jsonl`. `scripts/outlook_select.py` then groups these into threads,
applies the keep/drop + PHI screen, and renders the thread `.txt` files the distiller reads.

Threading data captured (Outlook/Exchange is richer than a raw mbox): every message here has a
`Message-ID`; ~70% carry an Exchange `Thread-Index` (its first 22 decoded bytes are the
conversation identity — children append 5-byte blocks); ~half carry `References`/`In-Reply-To`.
outlook_select unions on all three.

De-identification note: work email at a clinical institution can be largely clinical. This
step only EXTRACTS to the gitignored `_sources/` (never committed); the PHI screen
(`phi_scan.py`) + the worklist exclusion + the distiller's own gate are what keep clinical-patient
PHI out of the committed store. Nothing here writes into a domain dir.

    python3 scripts/outlook_extract.py [--src _sources/outlook] [--out _sources/outlook-index.jsonl]
"""
import argparse
import base64
import binascii
import email
import email.header
import email.utils
import json
import os
import re
import sys
from email import policy
from email.parser import BytesParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # scripts/ on path for _lib
from _lib.emailtext import strip_html   # noqa: E402  (single owner of the HTML→text strip)
from _lib.actions import self_emails     # noqa: E402  (single owner of the owner's addresses)
MAXBODY = 8000   # cap the stored body; a service email/newsletter can be huge, the substance is up top

# The owner's identities (From == one of these -> the message is owner-sent): EVERY address on the
# `self` row of _manifest/people.md (work aliases + any personal address the owner Cc's from), so a
# work export that spans several addresses still counts each one. Never hard-code them here.
OWNER_RX = re.compile(r'^(?:' + '|'.join(re.escape(a) for a in sorted(self_emails(ROOT))) + r')$',
                      re.I)


def _decode_hdr(val):
    """Decode an RFC2047-encoded header (=?utf-8?B?..?=, CJK 回复:, etc.) to a plain str.
    policy.default usually decodes already, but be defensive for odd encodings."""
    if val is None:
        return ""
    try:
        s = str(val)
    except Exception:
        s = repr(val)
    if "=?" in s:  # residual encoded-word -> decode explicitly
        try:
            parts = email.header.decode_header(s)
            s = "".join(
                (b.decode(enc or "utf-8", "replace") if isinstance(b, bytes) else b)
                for b, enc in parts)
        except Exception:
            pass
    return s.replace("\r", "").replace("\n", " ").strip()


def _addrs(msg, field):
    """[(display_name, addr_lower), ...] for a header field (To/Cc/From), addresses only where present."""
    raw = msg.get_all(field, [])
    out = []
    for name, addr in email.utils.getaddresses([_decode_hdr(v) for v in raw]):
        addr = (addr or "").strip().lower()
        if addr:
            out.append((_decode_hdr(name), addr))
    return out


def _iso_date(msg):
    """RFC822 Date -> 'YYYY-MM-DD' (UTC) + full ISO instant, or (None, None) if unparseable."""
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
    except Exception:
        return None, None
    if dt is None:
        return None, None
    try:
        import datetime
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt = dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None, None
    return dt.strftime("%Y-%m-%d"), dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _thread_index_guid(msg):
    """The Exchange conversation identity: first 22 bytes of the base64 Thread-Index, hex-encoded.
    All messages in one Exchange conversation share these 22 bytes (6-byte header + 16-byte GUID);
    replies append 5-byte child blocks. None when absent/undecodable."""
    ti = msg.get("Thread-Index")
    if not ti:
        return None
    try:
        raw = base64.b64decode(str(ti).strip() + "===")  # pad tolerantly
    except (binascii.Error, ValueError):
        return None
    if len(raw) < 22:
        return None
    return raw[:22].hex()


def _msgids(val):
    """All <...> message-ids in a References/In-Reply-To/Message-ID header value (angle-brackets
    stripped, lowercased). Robust to folding/whitespace."""
    if not val:
        return []
    return [m.strip("<>").strip().lower() for m in re.findall(r'<[^>]+>', str(val))]


def _body(msg):
    """Best-effort plaintext body: prefer text/plain, else strip the text/html part."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return (part.get_content() or "").strip()
    except Exception:
        pass
    try:
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            return strip_html(part.get_content() or "")
    except Exception:
        pass
    # last resort: walk parts
    try:
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                return (part.get_content() or "").strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return strip_html(part.get_content() or "")
    except Exception:
        pass
    return ""


def parse_eml(path):
    with open(path, "rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    subject = _decode_hdr(msg.get("Subject"))
    fname = _decode_hdr(msg.get("From"))
    from_pairs = _addrs(msg, "From")
    from_addr = from_pairs[0][1] if from_pairs else ""
    from_name = (from_pairs[0][0] if from_pairs else "") or fname
    date, ts = _iso_date(msg)
    labels = []
    if "[EXTERNAL]" in subject:
        labels.append("External")
    if "[Bulk]" in subject:
        labels.append("Bulk")
    if "[SUSPICIOUS]" in subject:
        labels.append("Suspicious")
    sent = bool(from_addr and OWNER_RX.match(from_addr))
    if sent:
        labels.append("Sent")
    return {
        "file": os.path.basename(path),
        "from_addr": from_addr,
        "from_name": from_name,
        "to": [a for _, a in _addrs(msg, "To")],
        "cc": [a for _, a in _addrs(msg, "Cc")],
        "date": date,
        "ts": ts,
        "subject": subject,
        "thread_topic": _decode_hdr(msg.get("Thread-Topic")) or None,
        "msgid": (_msgids(msg.get("Message-ID")) or [None])[0],
        "refs": _msgids(msg.get("References")) + _msgids(msg.get("In-Reply-To")),
        "thridx_guid": _thread_index_guid(msg),
        "sent": sent,
        "labels": labels,
        "body": _body(msg)[:MAXBODY],
    }


def main():
    ap = argparse.ArgumentParser(description="Parse the Outlook .eml export into a per-message index.")
    ap.add_argument("--src", default=os.path.join(ROOT, "_sources", "outlook"))
    ap.add_argument("--out", default=os.path.join(ROOT, "_sources", "outlook-index.jsonl"))
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.src) if f.lower().endswith(".eml"))
    n_ok = n_err = n_sent = n_nodate = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for f in files:
            try:
                rec = parse_eml(os.path.join(args.src, f))
            except Exception as e:
                n_err += 1
                print(f"PARSE-ERROR {f}: {e}", file=sys.stderr)
                continue
            n_ok += 1
            n_sent += rec["sent"]
            n_nodate += rec["date"] is None
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"parsed {n_ok:,}/{len(files):,} .eml  (errors {n_err}, sent {n_sent:,}, no-date {n_nodate})")
    print(f"index -> {os.path.relpath(args.out, ROOT)}")


if __name__ == "__main__":
    main()

"""Single owner for provenance-ref extraction — the resumability keystone. Every worklist
derives "already distilled" by matching a note's provenance ref against a to-do list, so they
must all read the ref the same way.

    parse_ref(text)             -> first `ref:` value (full string) or None
    thread_id(ref, scheme=...)  -> the id after a scheme prefix, or None
    gmail_thread_key(id_or_ref) -> canonical INT for a Gmail thread id, base-agnostic (decimal
                                   X-GM-THRID == hex connector id are the SAME thread)

parse_ref is deliberately PERMISSIVE (not digits-only) so it matches every current ref scheme:
gmail:thread:<id> (numeric or hex), granola:<uuid>, imessage:<guid>:c<n>, obsidian:<path>,
gdrive:<fid>.
"""
import re

_REF_RE = re.compile(r'^\s*ref:\s*"?([^"\n]+?)"?\s*$', re.M)


def parse_ref(text):
    """Return the first provenance ref value (e.g. 'gmail:thread:123', 'granola:abc-def',
    'imessage:<guid>:c0', 'obsidian:<path>', 'gdrive:<fid>') or None."""
    m = _REF_RE.search(text)
    return m.group(1) if m else None


def thread_id(ref, scheme="gmail:thread:"):
    """Return the id after `scheme` (default the gmail-thread scheme), or None if `ref` is
    falsy or does not start with `scheme`."""
    if ref and ref.startswith(scheme):
        return ref[len(scheme):]
    return None


def gmail_thread_key(id_or_ref):
    """Canonical INTEGER key for a Gmail thread id — base-agnostic dedup keystone.

    Gmail expresses ONE thread's 64-bit id two ways: the API/connector (`get_thread`,
    `search_threads`) returns it in HEX ('19f0aaaa0000d728'); Takeout/mbox X-GM-THRID writes the
    SAME id in DECIMAL ('1869181492479973160'). For example (fictional ids):
    get_thread('19f0aaaa0000d728') and mbox thrid 1869181492479973160 name the same thread.
    Backfill notes stored decimal refs; connector-driven incremental notes store hex — so a raw
    STRING compare reads the same thread as two different threads (the 2026-06-30 Gmail dup
    false-negatives across a whole backfill). Canonicalizing to the integer fixes it. This supersedes the
    old (mis-diagnosed) "connector thrid != mbox X-GM-THRID; needs Message-ID dedup" theory — they are
    the same id, and the connector does not even expose RFC822 Message-IDs.

    Accepts a bare id or a full 'gmail:thread:<id>' ref; returns None if not a gmail thread id.
    Disambiguation is airtight for real Gmail thrids (~2^60-2^64): hex form is <=16 chars, decimal
    form is >=17 digits, so `len<=16 & all-hex -> base 16`, else `all-digits -> base 10`."""
    if not id_or_ref:
        return None
    s = id_or_ref.strip()
    if s.startswith("gmail:thread:"):
        s = s[len("gmail:thread:"):].strip()
    if not s:
        return None
    low = s.lower()
    if len(low) <= 16 and re.fullmatch(r'[0-9a-f]+', low):
        return int(low, 16)      # hex — Gmail API / connector form
    if s.isdigit():
        return int(s, 10)        # decimal — Takeout/mbox X-GM-THRID form
    return None                  # not a recognizable gmail thread id

"""Shared note-corpus helpers: deterministic iteration + frontmatter access. Owner for the
recurring "iterate every domain note", "split frontmatter", "read a scalar field", and
"note slug" patterns. Adopt where it's a clean drop-in; do NOT force it if it would change any
generated output.

    from _lib.notes import iter_notes, split_frontmatter, get_field, note_slug
"""
import glob
import os
import re

from _lib.constants import DOMAINS
from _lib.provenance import parse_ref, gmail_thread_key


def gmail_done_map(root):
    """{canonical gmail_thread_key (int) -> root-relative note path} for every domain note whose
    provenance ref is a `gmail:thread:` ref. SINGLE OWNER of the "gmail threads already distilled
    on disk" scan: gmail_worklist derives its already-done key SET from this (dedup), and
    gmail_update_worklist needs the key -> note-path mapping (to redistill GROWN threads in place).
    Both must read the store the same way, so neither pastes its own scan.

    The key is base-agnostic: gmail_thread_key collapses the connector HEX id and the mbox X-GM-THRID
    DECIMAL form of the SAME 64-bit thread to one integer (that is what lets a hex wave thrid match a
    decimal-ref backfill note). On the rare pre-dedup duplicate (two notes, one thread), the
    lexicographically-first path wins (deterministic via sorted()); gmail_reconcile archives the dup.
    Scan shape (DOMAINS glob, 1500-char head, _index.md skip) is IDENTICAL to the old
    gmail_worklist.done_thrids so `set(gmail_done_map(root)) == the previous done set`."""
    m = {}
    for d in DOMAINS:
        for fp in sorted(glob.glob(os.path.join(root, d, '**', '*.md'), recursive=True)):
            if os.path.basename(fp) == '_index.md':
                continue
            try:
                head = open(fp, encoding='utf-8', errors='replace').read(1500)
            except OSError:
                continue
            k = gmail_thread_key(parse_ref(head))
            if k is not None:
                m.setdefault(k, os.path.relpath(fp, root))
    return m


def iter_notes(tops=None):
    """Yield every `<top>/**/*.md` note (excluding `_index.md` MOCs), globally sorted by path
    for deterministic order (matches build_index.py's `sorted(...)` flat ordering)."""
    if tops is None:
        tops = DOMAINS  # NOT a mutable default — DOMAINS is a shared module-level list imported
                         # by ~15 other scripts; binding it as a default arg would alias (not copy)
                         # it, so any future in-place mutation inside this function would corrupt
                         # every other importer's view of it.
    for fp in sorted(f for d in tops
                     for f in glob.glob(f"{d}/**/*.md", recursive=True)
                     if not f.endswith("_index.md")):
        yield fp


def split_frontmatter(txt):
    """Return (head, body): head is the frontmatter block (incl. the leading '---'); body
    starts at the closing '\\n---'. GUARDED — returns (None, None) if there is no valid
    frontmatter."""
    if not txt.startswith("---") or "\n---" not in txt[3:]:
        return None, None
    i = txt.index("\n---", 3)
    return txt[:i], txt[i:]


def get_field(head, key):
    """Scalar frontmatter getter, quote-stripped — matches build_index.py's parse.g semantics
    (returns '' when the key is absent)."""
    m = re.search(rf'^{key}:\s*(.*)$', head, re.M)
    return m.group(1).strip().strip('"') if m else ""


def note_slug(path):
    """Bare note slug = basename without the .md extension."""
    return os.path.splitext(os.path.basename(path))[0]


def section(body, name):
    """Extract a `## <name>` section's bullet/checkbox lines from a note body (rstripped,
    blank lines dropped). Moved verbatim from regen.py (was regen.py:50-54) — single owner,
    reused by engram_tasks.py's `## Action items` extraction."""
    m = re.search(rf'^##\s+{name}[^\n]*\n(.*?)(?=^##\s|\Z)', body, re.M | re.S)
    if not m:
        return []
    return [ln.rstrip() for ln in m.group(1).strip().split("\n")
            if ln.strip() and ln.strip()[0] in "-*[ "]

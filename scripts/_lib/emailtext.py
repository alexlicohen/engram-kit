"""Single owner for the light HTML→text strip shared by the email renderers (gmail_render.py's
connector path and outlook_extract.py's .eml path). Was byte-duplicated in both; centralized here
per CLAUDE.md "one owner per decision" (a code-review finding, 2026-07-06).

    from _lib.emailtext import strip_html
"""
import html
import re


def strip_html(h):
    """Best-effort HTML→plaintext: drop script/style/head, turn block tags into newlines, unescape
    entities, collapse runs of whitespace. Not a parser — good enough to distill an email body."""
    if not h:
        return ""
    h = re.sub(r'(?is)<(script|style|head).*?</\1>', ' ', h)
    h = re.sub(r'(?i)<br\s*/?>', '\n', h)
    h = re.sub(r'(?i)</(p|div|li|tr|h[1-6])>', '\n', h)
    h = re.sub(r'<[^>]+>', ' ', h)
    h = html.unescape(h)
    h = re.sub(r'[ \t]+', ' ', h)
    h = re.sub(r'\n[ \t]+', '\n', h)
    h = re.sub(r'\n{3,}', '\n\n', h)
    return "\n".join(l.rstrip() for l in h.splitlines()).strip()

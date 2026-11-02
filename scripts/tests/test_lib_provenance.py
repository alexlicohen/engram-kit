#!/usr/bin/env python3
"""Guard test for the single-owner provenance-ref parser (_lib/provenance.py::parse_ref).

parse_ref is the resumability keystone (every worklist derives "already distilled" from it) and
the dup-ref lint keys on it. It must match every current ref scheme and — critically (finding 5)
— must NOT be fooled by look-alikes (`href:` / `xref:`) or return a value for a note that has no
real `ref:`. A false positive there would let an unparseable-ref note escape dup detection; a
false negative would make a real note look un-distilled and get re-ingested.

Hermetic: pure in-memory strings, no store/_sources access. Run: python3 -m pytest this_file.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)  # scripts/ on path for _lib.*

from _lib.provenance import parse_ref, thread_id, gmail_thread_key  # noqa: E402


def fm(ref_line):
    """A minimal note frontmatter head with a nested provenance block (real note shape)."""
    return (
        "---\n"
        'title: "x"\n'
        "provenance:\n"
        "  method: distilled\n"
        f"{ref_line}\n"
        "status: done\n"
        "---\n"
    )


class TestParseRefSchemes(unittest.TestCase):
    def test_matches_every_scheme(self):
        cases = {
            'gmail:thread:': '  ref: "gmail:thread:18f2c0abed1234"',
            'granola:':      '  ref: "granola:5c2f9e7a-1b3d-4a2e-9f88-0c1122334455"',
            'imessage:guid:c':  '  ref: "imessage:AB12-GUID:c0"',
            'imessage:guid:chunk':  '  ref: "imessage:AB12-GUID:12_c000"',
            'obsidian:':     '  ref: "obsidian:Projects/Estate/trust-notes.md"',
            'gdrive:':       '  ref: "gdrive:1AbCdEf_GhIjKlMnOpQrStUv"',
        }
        expected = {
            'gmail:thread:': 'gmail:thread:18f2c0abed1234',
            'granola:':      'granola:5c2f9e7a-1b3d-4a2e-9f88-0c1122334455',
            'imessage:guid:c':  'imessage:AB12-GUID:c0',
            'imessage:guid:chunk':  'imessage:AB12-GUID:12_c000',
            'obsidian:':     'obsidian:Projects/Estate/trust-notes.md',
            'gdrive:':       'gdrive:1AbCdEf_GhIjKlMnOpQrStUv',
        }
        for key, line in cases.items():
            self.assertEqual(parse_ref(fm(line)), expected[key], f"scheme {key}")

    def test_unquoted_ref_matches(self):
        self.assertEqual(parse_ref(fm("  ref: granola:abc-123")), "granola:abc-123")

    def test_first_ref_wins(self):
        head = fm('  ref: "gmail:thread:AAA"') + '  ref: "gmail:thread:BBB"\n'
        self.assertEqual(parse_ref(head), "gmail:thread:AAA")

    # ---- finding 5: look-alikes and malformed heads must return None (not a false ref) --------
    def test_href_is_not_a_ref(self):
        self.assertIsNone(parse_ref(fm('  href: "http://example.com/x"')))

    def test_xref_is_not_a_ref(self):
        self.assertIsNone(parse_ref(fm('  xref: "gmail:thread:CCC"')))

    def test_reference_word_is_not_a_ref(self):
        self.assertIsNone(parse_ref(fm('  reference: "gmail:thread:DDD"')))

    def test_inline_ref_not_at_line_start_is_not_matched(self):
        # a body mention "see ref: foo" must not be picked up as a provenance ref
        self.assertIsNone(parse_ref("---\ntitle: x\nbody mentions ref: nope here\n---\n"))

    def test_no_ref_returns_none(self):
        head = "---\ntitle: x\nprovenance:\n  method: distilled\nstatus: done\n---\n"
        self.assertIsNone(parse_ref(head))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_ref(""))


class TestThreadId(unittest.TestCase):
    def test_gmail_thread_id(self):
        self.assertEqual(thread_id("gmail:thread:123abc"), "123abc")

    def test_scheme_mismatch_returns_none(self):
        self.assertIsNone(thread_id("granola:xyz"))

    def test_none_ref(self):
        self.assertIsNone(thread_id(None))


class TestGmailThreadKey(unittest.TestCase):
    """Canonical Gmail thread-id key: the mbox X-GM-THRID (decimal) and the connector/API id (hex)
    are the SAME 64-bit thread id, so dedup must collapse them. A raw string compare does not — that
    was the 2026-06-30 cross-base dup false-negatives (three thread pairs in one store)."""
    # The same 64-bit id written in both bases (the connector's hex id and the mbox X-GM-THRID).
    PAIRS = [
        ("19f0aaaa0000d728", "1869181492479973160"),
        ("19e0bbbb00006aaa", "1864696657564691114"),  # a second pair
    ]

    def test_decimal_hex_equivalence(self):
        for hexid, decid in self.PAIRS:
            self.assertEqual(gmail_thread_key(hexid), gmail_thread_key(decid),
                             f"{hexid} (hex) must key-equal {decid} (decimal)")
            self.assertEqual(gmail_thread_key(hexid), int(decid))

    def test_accepts_full_ref_and_bare_id(self):
        self.assertEqual(gmail_thread_key("gmail:thread:1869181492479973160"),
                         gmail_thread_key("19f0aaaa0000d728"))

    def test_non_gmail_and_empty_return_none(self):
        for x in (None, "", "granola:abc-def", "imessage:GUID:c0",
                  "obsidian:path/x.md", "gmail:thread:"):
            self.assertIsNone(gmail_thread_key(x))

    def test_hex_all_digits_parsed_base16(self):
        # a 16-char all-digit token is a connector HEX id (its decimal reading would be an
        # impossibly-small thrid), so it must parse base-16 — the disambiguation-by-length rule.
        self.assertEqual(gmail_thread_key("1234567890123456"), int("1234567890123456", 16))

    def test_decimal_thrid_parsed_base10(self):
        # a >=17-digit all-decimal token is an mbox X-GM-THRID -> base-10.
        self.assertEqual(gmail_thread_key("1864696657564691114"), 1864696657564691114)


if __name__ == "__main__":
    unittest.main(verbosity=2)

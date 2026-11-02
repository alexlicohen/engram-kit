#!/usr/bin/env python3
"""The mail selectors' configuration seam: scripts/_lib/mail_routing.py (routing rules, PHI-screen
domains, clinic task senders) and _lib/actions.self_emails (the owner's addresses).

Hermetic: Routing objects are built from dicts; the self-row test writes a throwaway people.md.
Run: python3 -m pytest scripts/tests/test_mail_routing.py -q
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)
from _lib import mail_routing as MR      # noqa: E402
from _lib.actions import self_emails     # noqa: E402

CFG = {
    "phi_screen_sender_domains": [r"(^|\.)hospital\.example$"],
    "clinical_task_senders": ["Nurse@Hospital.Example"],
    "domain_rules": [
        {"domain": "ventures", "sender_domains": [r"(^|\.)mycompany\.example$"]},
        {"domain": "finance", "labels": [r"\btaxes\b"], "subjects": [r"\bk-1\b"]},
        {"domain": "personal", "subpath": "travel", "subjects": [r"\bitinerary\b"]},
        {"domain": "personal", "sender_addresses": ["Sibling@Example.com"]},
    ],
}


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.r = MR.Routing(CFG)

    def test_first_matching_rule_wins(self):
        self.assertEqual(self.r.route(sender_domains=["mycompany.example"], subjects=["K-1 ready"]),
                         ("ventures", None))
        self.assertEqual(self.r.route(subjects=["Your K-1 is ready"]), ("finance", None))
        self.assertEqual(self.r.route(labels=["Taxes"]), ("finance", None))
        self.assertEqual(self.r.route(subjects=["Trip itinerary"]), ("personal", "travel"))

    def test_exact_addresses_are_case_insensitive(self):
        self.assertEqual(self.r.route(addresses=["sibling@example.com"]), ("personal", None))

    def test_no_match_is_none(self):
        self.assertEqual(self.r.route(subjects=["hello"], sender_domains=["elsewhere.example"]),
                         (None, None))

    def test_phi_screen_and_task_senders(self):
        self.assertTrue(self.r.phi_screen("hospital.example"))
        self.assertTrue(self.r.phi_screen("dept.hospital.example"))
        self.assertFalse(self.r.phi_screen("nothospital.example"))
        self.assertFalse(self.r.phi_screen(""))
        self.assertEqual(self.r.clinical_task_senders, frozenset({"nurse@hospital.example"}))

    def test_empty_config_screens_and_routes_nothing(self):
        r = MR.Routing({})
        self.assertFalse(r.phi_screen("hospital.example"))
        self.assertEqual(r.route(subjects=["anything"]), (None, None))
        self.assertEqual(r.clinical_task_senders, frozenset())

    def test_unknown_domain_fails_loudly(self):
        with self.assertRaises(ValueError):
            MR.Routing({"domain_rules": [{"domain": "no-such-domain", "subjects": ["x"]}]})

    def test_bad_regex_fails_loudly(self):
        with self.assertRaises(ValueError):
            MR.Routing({"phi_screen_sender_domains": ["("]})

    def test_shipped_config_loads_and_screens_nothing_by_default(self):
        r = MR.load()
        self.assertFalse(r.phi_screen("hospital.example"),
                         "a fresh store must not ship a PHI-screen institution")
        self.assertEqual(r.clinical_task_senders, frozenset())

    def test_missing_file_is_an_empty_config(self):
        r = MR.load(os.path.join(tempfile.gettempdir(), "no-such-mail-routing.json"))
        self.assertEqual(r.route(subjects=["x"]), (None, None))

    def test_load_reads_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "mail_routing.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(CFG, f)
            self.assertEqual(MR.load(p).route(sender_domains=["mycompany.example"]),
                             ("ventures", None))


class TestSelfEmails(unittest.TestCase):
    def test_addresses_come_from_the_self_row_only(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "_manifest"))
            with open(os.path.join(root, "_manifest", "people.md"), "w", encoding="utf-8") as f:
                f.write("| Canonical name | Aliases / emails | Relationship | Verify? |\n"
                        "|---|---|---|---|\n"
                        "| Pat Owner | Pat@Example.com; pat.owner@work.example; \"Patty\" | self | — |\n"
                        "| Sam Other | sam@example.org | friend | — |\n")
            self.assertEqual(self_emails(root), {"pat@example.com", "pat.owner@work.example"})

    def test_no_self_row_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self_emails(root), set())

    def test_the_selectors_use_the_store_self_row(self):
        import gmail_select
        root = os.path.dirname(_SCRIPTS)
        self.assertEqual(gmail_select.SELF, self_emails(root))


if __name__ == "__main__":
    unittest.main()

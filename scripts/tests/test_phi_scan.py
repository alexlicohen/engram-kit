#!/usr/bin/env python3
"""Guard tests for the PHI heuristic (scripts/phi_scan.py::scan) — engram's one hard sensitivity
rule is "no clinical-patient PHI in the store", and scan() is the single-owner detector both the
ingest screen (phi_scan.py) and the store audit (phi_scan_store.py) use.

The heuristic over-flags by design (recall > precision): a flag routes a note to human review, it
is not a leak. These tests pin the load-bearing behaviors:
  * a STRONG identifier (MRN / SSN / labeled DOB) flags high;
  * the Phase-4 unlabeled-DOB-near-clinical detector fires on a synthetic patient note;
  * the Phase-4 named-minor + diagnosis detector fires on the "school letter" class;
  * academic-dominated text (grants/faculty) does NOT flag (the down-weight works);
  * an own/family bare DOB in a financial note stays benign (the precision guard: a DOB-shaped
    date with <2 clinical context terms does not trip the unlabeled-DOB detector).

scan() never returns matched PHI text — only which signal fired — so the assertions inspect the
`strong`/`phi`/`level` summary, never a substring. Hermetic: in-memory strings. Run: pytest.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
sys.path.insert(0, _SCRIPTS)  # scripts/ on path

import phi_scan  # noqa: E402

scan = phi_scan.scan


class TestStrongIdentifiers(unittest.TestCase):
    def test_ssn_flags_high(self):
        r = scan("Please forward the paperwork; the number on file is 123-45-6789.")
        self.assertTrue(r["phi"])
        self.assertEqual(r["level"], "high")
        self.assertIn("ssn", r["strong"])

    def test_mrn_flags_high(self):
        r = scan("Chart pulled under MRN 00845321 for the follow-up.")
        self.assertTrue(r["phi"])
        self.assertEqual(r["level"], "high")
        self.assertIn("mrn", r["strong"])

    def test_labeled_dob_flags_high(self):
        r = scan("Intake form — DOB: 05/20/1980, insurance verified.")
        self.assertTrue(r["phi"])
        self.assertEqual(r["level"], "high")
        self.assertIn("dob", r["strong"])


class TestPhase4Detectors(unittest.TestCase):
    def test_unlabeled_dob_near_clinical_flags(self):
        text = (
            "The patient presented with new-onset seizure. Diagnosis of epilepsy was "
            "confirmed and a medication dose started. Born 06/15/2010; workup ongoing."
        )
        r = scan(text)
        self.assertTrue(r["phi"], r)
        self.assertIn("dob_unlabeled", r["strong"], r)
        # proves it is the UNLABELED detector, not a labeled "DOB:" strong hit
        self.assertNotIn("dob", r["strong"], r)

    def test_ctx_matches_inflections(self):
        """1.4: the CTX regex must catch inflected clinical terms. `epilep`/`prescrib` matched
        NOTHING before (trailing \\b), and `seizure` missed the plural — now fixed."""
        import phi_scan as ps
        for w in ("seizures", "epilepsy", "epileptic", "prescribed", "prescribing",
                  "symptoms", "lesions", "diagnoses"):
            self.assertTrue(ps.CTX.search(w), f"CTX should match inflected {w!r}")
        # and inflected-only clinical density near an unlabeled DOB now flags (was under-detected)
        text = ("Child has recurrent seizures; epilepsy suspected, several medications "
                "prescribed. Born 03/12/2011; more symptoms noted.")
        self.assertTrue(scan(text)["phi"], "inflected-only clinical note should flag")

    def test_named_minor_plus_diagnosis_flags(self):
        text = (
            "Dear team,\n\n"
            "Johnny Appleseed, a pediatric patient in kindergarten, was recently diagnosed "
            "with autism. His IEP meeting is scheduled to review support for his diagnosis."
        )
        r = scan(text)
        self.assertTrue(r["phi"], r)
        self.assertIn("named_minor_dx", r["strong"], r)


class TestBenignCases(unittest.TestCase):
    def test_academic_text_does_not_flag(self):
        text = (
            "The R01 grant study section reviewed the manuscript. Faculty co-authors discussed "
            "the abstract, poster, and conference. The committee and reviewer praised the "
            "seminar and curriculum for the K award."
        )
        r = scan(text)
        self.assertFalse(r["phi"], r)
        self.assertEqual(r["strong"], [], r)

    def test_own_family_dob_in_financial_note_benign(self):
        """A DOB-shaped date in an estate/financial note with <2 clinical context terms must NOT
        trip the unlabeled-DOB detector (out-of-scope by rule: own/family DOB is not PHI)."""
        text = (
            "Estate planning: update the will and rebalance the investment account. "
            "Beneficiary born 03/12/1975. Tax filing due; budget spreadsheet under review."
        )
        r = scan(text)
        self.assertFalse(r["phi"], r)
        self.assertNotIn("dob_unlabeled", r["strong"], r)


if __name__ == "__main__":
    unittest.main(verbosity=2)

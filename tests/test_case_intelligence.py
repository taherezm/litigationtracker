#!/usr/bin/env python3
"""Regression checks for public case-level intelligence."""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import discover_cases, regenerate_case_summaries  # noqa: E402
from scripts.case_intelligence import (  # noqa: E402
    generate_case_summary,
    refresh_case_intelligence,
    select_latest_meaningful_event,
    validate_case_summary,
)
from scripts.validate_tracker_data import validate_cases  # noqa: E402


class CaseIntelligenceTests(unittest.TestCase):
    def test_boilerplate_tracker_monitoring_summary_is_rejected(self) -> None:
        case = self.case_fixture(
            summary=(
                "Example asserts copyright infringement claims against AI Co. "
                "The tracker is monitoring the case for rulings on how intellectual property doctrines "
                "apply to AI development and use."
            )
        )
        case["case_intelligence"] = self.intelligence_fixture()

        self.assertTrue(any("banned boilerplate" in error for error in validate_case_summary(case)))

    def test_generic_catch_all_summary_is_rejected(self) -> None:
        case = self.case_fixture(
            summary=(
                "Example asserts copyright infringement claims against AI Co. in a dispute involving "
                "artificial intelligence systems, model outputs, or training data."
            )
        )
        case["case_intelligence"] = self.intelligence_fixture()

        self.assertTrue(any("banned boilerplate" in error for error in validate_case_summary(case)))

    def test_malformed_classifier_text_is_rejected(self) -> None:
        case = self.case_fixture(
            summary=(
                "Doe asserts unspecified ip or privacy claims against ai developer claims against X.AI Corp."
            )
        )
        case["case_intelligence"] = self.intelligence_fixture()

        self.assertTrue(any("banned boilerplate" in error for error in validate_case_summary(case)))

    def test_stayed_case_summary_and_posture_mention_stay(self) -> None:
        case = self.case_fixture(summary="")
        case["status"] = "active"
        case["docket_entries"] = [
            {
                "entry_number": "13",
                "date": "2026-06-09",
                "raw_text": (
                    "ORDER granting stipulation. The case is stayed until the related motion is resolved. "
                    "Case management conference set for 9/1/2026."
                ),
                "summary": (
                    "The court granted a stipulation staying the case until a related motion is resolved "
                    "and set a case management conference."
                ),
                "significance": "significant_ruling",
            }
        ]

        refresh_case_intelligence(case, [])

        self.assertEqual(case["status"], "stayed")
        self.assertEqual(case["case_intelligence"]["procedural_stage"], "stayed")
        self.assertIn("stay", case["plain_language_summary"].lower())
        self.assertIn("stay", case["case_intelligence"]["current_posture"].lower())
        self.assertEqual(validate_case_summary(case), [])

    def test_meaningful_event_selection_ignores_later_admin_noise(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "1",
                "date": "2026-01-01",
                "raw_text": "COMPLAINT filed by Author against AI Co.",
                "summary": "Author filed a complaint against AI Co.",
                "significance": "minor_update",
            },
            {
                "entry_number": "2",
                "date": "2026-01-15",
                "raw_text": "Defendant filed a motion to dismiss the complaint.",
                "summary": "AI Co. filed a motion to dismiss the complaint.",
                "significance": "minor_update",
            },
            {
                "entry_number": "3",
                "date": "2026-02-01",
                "raw_text": "MOTION for leave to appear pro hac vice filed by attorney.",
                "summary": "An attorney filed a pro hac vice motion.",
                "significance": "minor_update",
            },
        ]

        event = select_latest_meaningful_event(case, [])

        self.assertIsNotNone(event)
        self.assertEqual(event["entry_number"], "2")
        self.assertEqual(event["event_type"], "motion_to_dismiss")

    def test_newly_filed_limited_facts_get_transparent_fallback(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = []

        refresh_case_intelligence(case, [])

        self.assertIn("Newly filed case", case["plain_language_summary"])
        self.assertIn("available parsed materials do not yet identify", case["plain_language_summary"])
        self.assertEqual(case["case_intelligence"]["confidence_level"], "low")
        self.assertTrue(case["case_intelligence"]["missing_information"])
        self.assertEqual(validate_case_summary(case), [])

    def test_regeneration_preserves_entries_and_key_rulings(self) -> None:
        case = self.case_fixture(summary="The tracker is monitoring this case.")
        case["docket_entries"] = [
            {
                "entry_number": "7",
                "date": "2026-03-01",
                "raw_text": "ORDER denying motion to dismiss.",
                "summary": "The court denied the motion to dismiss.",
                "significance": "significant_ruling",
            }
        ]
        case["key_rulings"] = [
            {
                "date": "2026-03-01",
                "description": "The court denied the motion to dismiss.",
                "summary": "The court denied the motion to dismiss.",
                "significance": "significant_ruling",
            }
        ]
        original_entries = deepcopy(case["docket_entries"])
        original_rulings = deepcopy(case["key_rulings"])

        cases = regenerate_case_summaries.regenerate_cases([case], [])

        self.assertEqual(cases[0]["docket_entries"], original_entries)
        self.assertEqual(cases[0]["key_rulings"], original_rulings)
        self.assertIn("case_intelligence", cases[0])
        self.assertNotIn("The tracker is monitoring", cases[0]["plain_language_summary"])

    def test_discovered_case_gets_intelligence_and_passes_validation(self) -> None:
        candidate = {
            "docket_number": "1:26-cv-00001",
            "docket_id": "12345",
            "case_name": "Author v. AI Co.",
            "court": "District Court, N.D. California",
            "date_filed": "2026-01-01",
            "parties": "",
            "snippet": "",
            "raw": {},
        }
        docket = {
            "docket_number": "1:26-cv-00001",
            "case_name": "Author v. AI Co.",
            "court": "cand",
            "date_filed": "2026-01-01",
            "id": "12345",
        }
        classification = {"claims": ["copyright infringement"], "confidence": "high"}

        case = discover_cases.build_case(candidate, docket, classification, client=None, existing_ids=set())

        self.assertIn("case_intelligence", case)
        self.assertIn("plain_language_summary", case)
        self.assertEqual(validate_cases([case]), [])

    @staticmethod
    def case_fixture(summary: str) -> dict[str, object]:
        return {
            "id": "author-v-ai-co",
            "name": "Author v. AI Co.",
            "court": "District Court, N.D. California",
            "docket_number": "1:26-cv-00001",
            "date_filed": "2026-01-01",
            "claims": ["copyright infringement"],
            "status": "active",
            "procedural_posture": "Filed",
            "parties": {"plaintiff": "Author", "defendant": "AI Co."},
            "key_rulings": [],
            "docket_entries": [],
            "plain_language_summary": summary,
        }

    @staticmethod
    def intelligence_fixture() -> dict[str, object]:
        case = CaseIntelligenceTests.case_fixture(summary="")
        refresh_case_intelligence(case, [])
        return case["case_intelligence"]


if __name__ == "__main__":
    unittest.main()

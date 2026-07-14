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
    build_case_intelligence,
    generate_case_summary,
    normalize_claims,
    refresh_case_intelligence,
    score_event_text,
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

        self.assertIn("initial pleading stage", case["plain_language_summary"])
        self.assertIn("retrieved entries do not yet supply substantive pleading detail", case["plain_language_summary"])
        self.assertEqual(case["case_intelligence"]["confidence_level"], "low")
        self.assertTrue(case["case_intelligence"]["missing_information"])
        self.assertEqual(validate_case_summary(case), [])

    def test_dense_summary_integrates_posture_event_and_docket_reference(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "12",
                "date": "2026-06-22",
                "raw_text": "Plaintiff filed an opposition to Defendant's motion to dismiss.",
                "summary": "Plaintiff opposed Defendant's motion to dismiss and asked the court to sustain the complaint.",
                "significance": "minor_update",
            }
        ]

        refresh_case_intelligence(case, [])

        summary = case["plain_language_summary"]
        self.assertIn("A motion to dismiss is pending.", summary)
        self.assertIn("(Dkt. 12)", summary)
        self.assertNotIn("The case matters because", summary)
        self.assertNotIn("Latest meaningful event", summary)
        self.assertEqual(validate_case_summary(case), [])

    def test_issue_extraction_ignores_audio_from_appellate_logistics(self) -> None:
        case = self.case_fixture(summary="")
        case["name"] = "Amazon.com Services, LLC v. Perplexity AI, Inc."
        case["parties"] = {"plaintiff": "Amazon.com Services, LLC", "defendant": "Perplexity AI, Inc."}
        case["procedural_posture"] = "Appeal"
        case["docket_entries"] = [
            {
                "entry_number": "60",
                "date": "2026-06-11",
                "raw_text": "Oral argument held. Audio and video recordings are available on the court website.",
                "summary": "A Ninth Circuit panel heard oral argument; audio and video recordings are available.",
                "significance": "minor_update",
            }
        ]

        intelligence = build_case_intelligence(case, [])

        self.assertIsNone(intelligence["works_or_data_at_issue"])
        self.assertNotEqual(intelligence["claim_category"], "copyright_music_or_audio")
        self.assertEqual(intelligence["confidence_level"], "low")
        self.assertTrue(any("claim classification" in item for item in intelligence["missing_information"]))

    def test_proposed_stay_does_not_mark_action_stayed(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "14",
                "date": "2026-06-24",
                "raw_text": "Court staff terminated the stipulation with proposed order to stay all deadlines because it was re-filed.",
                "summary": "Court staff terminated a stipulation containing a proposed order to stay the action.",
                "significance": "minor_update",
            }
        ]

        refresh_case_intelligence(case, [])

        self.assertEqual(case["status"], "active")
        self.assertNotEqual(case["case_intelligence"]["procedural_stage"], "stayed")

    def test_granted_stipulation_marks_action_stayed(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "12",
                "date": "2026-06-09",
                "raw_text": "ORDER by Judge granting Stipulation to Stay Deadlines and Proceedings.",
                "summary": "The court granted the parties' stipulation and stayed all deadlines pending further order.",
                "significance": "significant_ruling",
            }
        ]

        refresh_case_intelligence(case, [])

        self.assertEqual(case["status"], "stayed")
        self.assertEqual(case["case_intelligence"]["procedural_stage"], "stayed")

    def test_expired_fixed_duration_stay_returns_action_to_active(self) -> None:
        case = self.case_fixture(summary="")
        case["status"] = "stayed"
        case["procedural_posture"] = "Stayed"
        case["docket_last_checked"] = "2026-07-13"
        case["docket_entries"] = [
            {
                "entry_number": "20",
                "date": "2026-06-23",
                "raw_text": "The court grants the stipulation to stay all proceedings for an additional 14 days.",
                "summary": "The court stayed all deadlines for an additional 14 days.",
                "significance": "significant_ruling",
            }
        ]

        refresh_case_intelligence(case, [])

        self.assertEqual(case["status"], "active")
        self.assertNotEqual(case["case_intelligence"]["procedural_stage"], "stayed")

    def test_routine_extension_does_not_displace_substantive_motion_event(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "10",
                "date": "2026-06-01",
                "raw_text": "Plaintiff filed an opposition to Defendant's motion to dismiss.",
                "summary": "Plaintiff opposed the pending motion to dismiss.",
                "significance": "minor_update",
            },
            {
                "entry_number": "11",
                "date": "2026-06-10",
                "raw_text": "ORDER granting stipulation to extend the briefing deadline and reschedule the motion hearing.",
                "summary": "The court extended the briefing deadline and rescheduled the hearing.",
                "significance": "significant_ruling",
            },
        ]

        event = select_latest_meaningful_event(case, [])

        self.assertIsNotNone(event)
        self.assertEqual(event["entry_number"], "10")
        self.assertEqual(event["event_type"], "motion_to_dismiss")

    def test_routine_relation_referral_is_not_a_meaningful_event(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "8",
                "date": "2026-06-10",
                "raw_text": "The court referred this case to another judge as possibly related to multidistrict litigation.",
                "summary": "The court referred this case to Judge S. H. Example as possibly related to multidistrict litigation docket 1:25-md-03143.",
                "significance": "significant_ruling",
            }
        ]

        self.assertIsNone(select_latest_meaningful_event(case, []))

        refresh_case_intelligence(case, [])

        self.assertEqual(case["case_intelligence"]["procedural_stage"], "service_or_initial_admin")
        self.assertNotIn("possibly related", case["plain_language_summary"])

    def test_transfer_receipt_is_not_treated_as_motion_practice(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_entries"] = [
            {
                "entry_number": "23",
                "date": "2026-05-26",
                "raw_text": "CASE TRANSFERRED IN from another district; certified copy of transfer order received.",
                "summary": "The court received the case transfer from another district with certified copies of the transfer order.",
                "significance": "minor_update",
            }
        ]

        self.assertIsNone(select_latest_meaningful_event(case, []))

    def test_magistrate_consent_notice_is_not_a_final_judgment(self) -> None:
        score, event_type = score_event_text(
            "The clerk notified the parties that a magistrate judge is available to handle all proceedings, "
            "including trial and final judgment, if every party consents."
        )

        self.assertLess(score, 60)
        self.assertEqual(event_type, "routine_admin")

    def test_temporary_stay_order_is_active_during_its_stated_duration(self) -> None:
        case = self.case_fixture(summary="")
        case["docket_last_checked"] = "2026-07-13"
        case["docket_entries"] = [
            {
                "entry_number": "29",
                "date": "2026-04-24",
                "raw_text": "ORDER REGARDING TEMPORARY STAY. All deadlines are continued by approximately 90 days.",
                "summary": "The court ordered a temporary stay, continuing all deadlines by approximately 90 days.",
                "significance": "significant_ruling",
            }
        ]

        refresh_case_intelligence(case, [])

        self.assertEqual(case["status"], "stayed")
        self.assertEqual(case["case_intelligence"]["procedural_stage"], "stayed")

    def test_motion_to_dismiss_disposition_is_a_substantive_ruling(self) -> None:
        score, event_type = score_event_text("ORDER denying Defendant's motion to dismiss the complaint.")

        self.assertGreaterEqual(score, 90)
        self.assertEqual(event_type, "significant_ruling")

    def test_oral_argument_request_on_motion_to_dismiss_is_not_an_appeal(self) -> None:
        score, event_type = score_event_text(
            "Defendant moves to dismiss the complaint and requests oral argument on the motion."
        )

        self.assertGreaterEqual(score, 80)
        self.assertEqual(event_type, "motion_to_dismiss")

    def test_completed_appellate_oral_argument_is_an_appeal_event(self) -> None:
        score, event_type = score_event_text(
            "A Ninth Circuit panel comprising three judges heard oral argument in the appeal."
        )

        self.assertGreaterEqual(score, 90)
        self.assertEqual(event_type, "appeal")

    def test_appellate_metadata_fallback_describes_an_appeal_not_a_new_case(self) -> None:
        case = self.case_fixture(summary="")
        case["court"] = "Court of Appeals for the Ninth Circuit"
        case["docket_entries"] = []

        event = select_latest_meaningful_event(case, [])

        self.assertIsNotNone(event)
        self.assertEqual(event["event_type"], "appeal")
        self.assertIn("appeal was docketed", event["summary"])

    def test_generic_importance_scaffolding_is_rejected(self) -> None:
        case = self.case_fixture(
            summary=(
                "Author asserts copyright infringement against AI Co. "
                "The case matters because it may affect copyright law governing AI."
            )
        )
        case["case_intelligence"] = self.intelligence_fixture()

        self.assertTrue(any("banned boilerplate" in error for error in validate_case_summary(case)))

    def test_normalized_claim_labels_are_idempotent(self) -> None:
        claims = normalize_claims(["stockholder derivative and securities claims"])

        self.assertEqual(claims, ["stockholder-derivative and securities claims"])
        self.assertEqual(normalize_claims(claims), claims)

    def test_automated_access_theory_uses_platform_access_category(self) -> None:
        case = self.case_fixture(summary="")
        case["claims"] = ["computer fraud and abuse", "trespass to chattels"]
        case["legal_theories"] = [
            "Computer Fraud and Abuse Act",
            "unauthorized automated access through the Comet web browser",
            "trespass to chattels",
        ]

        intelligence = build_case_intelligence(case, [])

        self.assertEqual(intelligence["claim_category"], "platform_scraping_or_access")
        self.assertEqual(intelligence["technology_or_model_at_issue"], "Comet")

    def test_transparent_low_information_summaries_are_not_duplicate_boilerplate(self) -> None:
        cases: list[dict[str, object]] = []
        for index in range(4):
            case = self.case_fixture(summary="")
            case["id"] = f"author-{index}-v-ai-co-{index}"
            case["name"] = f"Author {index} v. AI Co. {index}"
            case["docket_number"] = f"1:26-cv-0000{index}"
            case["parties"] = {"plaintiff": f"Author {index}", "defendant": f"AI Co. {index}"}
            refresh_case_intelligence(case, [])
            cases.append(case)

        errors = validate_cases(cases)

        self.assertFalse(any("effectively identical" in error for error in errors))

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

#!/usr/bin/env python3
"""Regression checks for complaint source document enrichment."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import source_documents  # noqa: E402
from scripts.case_intelligence import refresh_case_intelligence  # noqa: E402
from scripts.validate_tracker_data import validate_cases  # noqa: E402


class SourceDocumentTests(unittest.TestCase):
    def test_enrich_case_source_documents_selects_complaint_plain_text(self) -> None:
        case = {
            "id": "author-v-openai",
            "name": "Author v. OpenAI, Inc.",
            "courtlistener_docket_id": "123",
        }
        recap_document = {
            "id": 99,
            "document_number": "1",
            "attachment_number": None,
            "description": "Class Action Complaint",
            "is_available": True,
            "filepath_local": "recap/example.pdf",
            "plain_text": (
                "Plaintiffs allege OpenAI copied copyrighted books and used those works as "
                "training data for ChatGPT and other large language models."
            ),
            "ocr_status": "complete",
            "page_count": 42,
        }

        with (
            patch.object(source_documents, "get_json", return_value={"results": [recap_document]}),
            patch.object(source_documents.time, "sleep", return_value=None),
        ):
            enriched = source_documents.enrich_case_source_documents(
                case,
                source_documents.requests.Session(),
                "test-token",
                checked_at="2026-07-07",
            )

        self.assertTrue(enriched)
        self.assertEqual(case["source_documents_status"], "found")
        self.assertEqual(len(case["source_documents"]), 1)
        document = case["source_documents"][0]
        self.assertEqual(document["recap_document_id"], "99")
        self.assertIn("training data", document["text_excerpt"])
        self.assertEqual(document["facts"]["ai_conduct_alleged"], "use of materials as AI training data")
        self.assertEqual(document["facts"]["works_or_data_at_issue"], "books, educational, or publishing materials")
        self.assertEqual(document["facts"]["technology_or_model_at_issue"], "ChatGPT")

    def test_source_documents_raise_case_intelligence_confidence(self) -> None:
        case = self.case_fixture()
        case["source_documents"] = [
            {
                "type": "complaint",
                "source": "courtlistener_recap",
                "recap_document_id": "99",
                "docket_entry_number": "1",
                "description": "Complaint",
                "courtlistener_url": "https://storage.courtlistener.com/recap/example.pdf",
                "text_excerpt": (
                    "Plaintiffs allege OpenAI copied copyrighted books and used those works as "
                    "training data for ChatGPT."
                ),
                "facts": {
                    "ai_conduct_alleged": "use of materials as AI training data",
                    "works_or_data_at_issue": "books, educational, or publishing materials",
                    "technology_or_model_at_issue": "ChatGPT",
                },
            }
        ]

        refresh_case_intelligence(case, [])

        intelligence = case["case_intelligence"]
        self.assertEqual(intelligence["confidence_level"], "high")
        self.assertEqual(intelligence["claim_category"], "copyright_news_or_publishing")
        self.assertIn("training data", case["plain_language_summary"])
        self.assertFalse(any("Specific AI-related conduct" in item for item in intelligence["missing_information"]))
        self.assertEqual(validate_cases([case]), [])

    def test_validation_rejects_full_source_text_leakage(self) -> None:
        case = self.case_fixture()
        case["source_documents"] = [
            {
                "type": "complaint",
                "source": "courtlistener_recap",
                "text_excerpt": "Plaintiff alleges training-data copying.",
                "plain_text": "Full complaint text must not be published.",
            }
        ]
        refresh_case_intelligence(case, [])

        self.assertTrue(any("full document text" in error for error in validate_cases([case])))

    @staticmethod
    def case_fixture() -> dict[str, object]:
        return {
            "id": "author-v-openai",
            "name": "Author v. OpenAI, Inc.",
            "court": "District Court, N.D. California",
            "docket_number": "1:26-cv-00001",
            "date_filed": "2026-01-01",
            "claims": ["copyright infringement"],
            "status": "active",
            "procedural_posture": "Filed",
            "parties": {"plaintiff": "Author", "defendant": "OpenAI, Inc."},
            "key_rulings": [],
            "docket_entries": [],
            "plain_language_summary": "",
        }


if __name__ == "__main__":
    unittest.main()


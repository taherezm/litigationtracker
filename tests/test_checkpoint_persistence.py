#!/usr/bin/env python3
"""Regression checks for durable docket checkpoints."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import summarize, update_dockets  # noqa: E402


class CheckpointPersistenceTests(unittest.TestCase):
    def test_case_since_date_prefers_existing_checkpoint(self) -> None:
        case = {
            "date_filed": "2025-01-01",
            "docket_last_checked": "2026-06-10",
            "docket_entries": [{"entry_number": "1"}],
        }

        self.assertEqual(update_dockets.case_since_date(case, "2026-06-01"), "2026-06-08")

    def test_missing_checkpoint_is_seeded_and_persisted_on_early_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            cases_path = data_dir / "cases.json"
            updates_path = data_dir / "updates.json"
            last_run_path = data_dir / "last_run.json"

            cases_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "case-1",
                            "name": "Seeded Case",
                            "status": "active",
                            "courtlistener_docket_id": "123",
                            "docket_entries": [{"entry_number": "1"}],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            updates_path.write_text("[]", encoding="utf-8")
            last_run_path.write_text(json.dumps({"docket_last_run_date": "2026-06-06"}), encoding="utf-8")

            fetch_calls: list[dict[str, object]] = []

            def fake_fetch_new_entries(*args: object) -> tuple[list[dict[str, object]], bool, bool]:
                fetch_calls.append({"since_date": args[3]})
                return [], False, True

            with (
                patch.object(update_dockets, "CASES_PATH", cases_path),
                patch.object(update_dockets, "UPDATES_PATH", updates_path),
                patch.object(update_dockets, "LAST_RUN_PATH", last_run_path),
                patch.object(update_dockets, "fetch_new_entries", fake_fetch_new_entries),
                patch.dict(os.environ, {"COURTLISTENER_API_KEY": "test-token"}),
            ):
                update_dockets.main()

            persisted_cases = json.loads(cases_path.read_text(encoding="utf-8"))
            persisted_last_run = json.loads(last_run_path.read_text(encoding="utf-8"))

            self.assertEqual(fetch_calls, [{"since_date": "2026-06-04"}])
            self.assertEqual(persisted_cases[0]["docket_last_checked"], "2026-06-06")
            self.assertFalse(persisted_last_run["docket_update_complete"])
            self.assertTrue(persisted_last_run["courtlistener_rate_limited"])

    def test_docket_floor_resolves_to_oldest_valid_checkpoint(self) -> None:
        cases = [
            {"docket_last_checked": "2026-06-12"},
            {"docket_last_checked": "not-a-date"},
            {"docket_last_checked": "2026-06-09"},
        ]

        self.assertEqual(summarize.docket_floor_from_case_checkpoints(cases), "2026-06-09")


if __name__ == "__main__":
    unittest.main()

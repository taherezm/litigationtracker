#!/usr/bin/env python3
"""Pipeline-health validation regressions."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_tracker_data import validate_pipeline_state  # noqa: E402


class PipelineHealthTests(unittest.TestCase):
    TODAY = date(2026, 7, 20)

    def validate(self, state: dict[str, object], *, enforce: bool = False) -> tuple[list[str], str]:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            errors = validate_pipeline_state(
                state,
                enforce_freshness=enforce,
                today=self.TODAY,
                max_staleness_days=2,
            )
        return errors, stderr.getvalue()

    def test_rate_limit_flag_alone_is_nonfatal(self) -> None:
        errors, warning = self.validate(
            {
                "courtlistener_rate_limited": True,
                "discovery_complete": True,
                "discovery_last_run_date": "2026-07-20",
            },
            enforce=True,
        )
        self.assertEqual(errors, [])
        self.assertIn("rate-limited", warning)

    def test_one_day_incomplete_discovery_warns_without_error(self) -> None:
        errors, warning = self.validate(
            {
                "discovery_complete": False,
                "discovery_last_run_date": "2026-07-19",
                "discovery_incomplete_since": "2026-07-19",
                "discovery_phase": "queries",
                "discovery_queries_completed": 7,
                "discovery_queries_total": 36,
            },
            enforce=True,
        )
        self.assertEqual(errors, [])
        self.assertIn("incomplete for 1 day", warning)
        self.assertIn("queries 7/36", warning)

    def test_two_day_grace_boundary_is_nonfatal(self) -> None:
        errors, _ = self.validate(
            {
                "discovery_complete": False,
                "discovery_last_run_date": "2026-07-18",
                "discovery_incomplete_since": "2026-07-18",
            },
            enforce=True,
        )
        self.assertEqual(errors, [])

    def test_three_day_incomplete_discovery_fails_only_when_enforced(self) -> None:
        state = {
            "discovery_complete": False,
            "discovery_last_run_date": "2026-07-17",
            "discovery_incomplete_since": "2026-07-17",
        }
        self.assertEqual(self.validate(state, enforce=False)[0], [])
        errors, _ = self.validate(state, enforce=True)
        self.assertEqual(len(errors), 1)
        self.assertIn("allowed coverage age is 2 day", errors[0])

    def test_old_completed_discovery_fails_when_enforced(self) -> None:
        errors, warning = self.validate(
            {"discovery_complete": True, "discovery_last_run_date": "2026-07-01"},
            enforce=True,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("completed discovery checkpoint", errors[0])
        self.assertIn("completed discovery checkpoint", warning)

    def test_future_completed_checkpoint_fails_when_enforced(self) -> None:
        errors, warning = self.validate(
            {"discovery_complete": True, "discovery_last_run_date": "2026-07-21"},
            enforce=True,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("future-dated", errors[0])
        self.assertIn("future-dated", warning)

    def test_future_incomplete_since_fails_when_enforced(self) -> None:
        errors, warning = self.validate(
            {
                "discovery_complete": False,
                "discovery_last_run_date": "2026-07-20",
                "discovery_incomplete_since": "2026-07-21",
            },
            enforce=True,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("future-dated", errors[0])
        self.assertIn("future-dated", warning)

    def test_missing_or_malformed_incomplete_checkpoint_fails_when_enforced(self) -> None:
        for state in (
            {"discovery_complete": False},
            {
                "discovery_complete": False,
                "discovery_incomplete_since": "not-a-date",
                "discovery_last_run_date": "also-not-a-date",
            },
        ):
            with self.subTest(state=state):
                errors, warning = self.validate(state, enforce=True)
                self.assertEqual(len(errors), 1)
                self.assertIn("without a valid", errors[0])
                self.assertIn("without a valid", warning)


if __name__ == "__main__":
    unittest.main()

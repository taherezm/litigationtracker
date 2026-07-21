#!/usr/bin/env python3
"""Static contract checks for the production workflow."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "scheduled_update.yml"


class WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_quota_heavy_phases_use_separate_schedules(self) -> None:
        self.assertIn("- cron: '17 13 * * *'", self.workflow)
        self.assertIn("- cron: '47 18 * * *'", self.workflow)
        self.assertIn("github.event.schedule == '17 13 * * *' || inputs.phase == 'dockets'", self.workflow)
        self.assertIn("github.event.schedule == '47 18 * * *' || inputs.phase == 'discovery'", self.workflow)

    def test_overlapping_runs_queue_instead_of_replacing_each_other(self) -> None:
        self.assertIn("queue: max", self.workflow)
        self.assertNotIn("cancel-in-progress:", self.workflow)

    def test_tests_run_before_live_api_phases(self) -> None:
        tests_at = self.workflow.index("python -m unittest discover -s tests -v")
        dockets_at = self.workflow.index("python scripts/run_docket_update_passes.py")
        discovery_at = self.workflow.index("python scripts/discover_cases.py")
        self.assertLess(tests_at, dockets_at)
        self.assertLess(tests_at, discovery_at)

    def test_freshness_is_enforced_after_data_publication(self) -> None:
        publish_at = self.workflow.index("- name: Push data to site repo")
        health_at = self.workflow.index("python scripts/validate_tracker_data.py --enforce-pipeline-freshness")
        self.assertLess(publish_at, health_at)

    def test_quota_configuration_is_tier_aware_and_batching_stays_disabled(self) -> None:
        self.assertIn("CL_REQUESTS_PER_HOUR: ${{ vars.CL_REQUESTS_PER_HOUR }}", self.workflow)
        self.assertIn("CL_BATCHED_CHANGE_DETECTION: 0", self.workflow)
        self.assertNotIn("10 / 100 / 250", self.workflow)


if __name__ == "__main__":
    unittest.main()

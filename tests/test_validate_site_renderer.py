#!/usr/bin/env python3
"""Regression checks for the site renderer contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import validate_site_renderer  # noqa: E402


class SiteRendererValidationTests(unittest.TestCase):
    def test_current_contract_allows_removed_public_checkpoint_stat(self) -> None:
        html = "\n".join(
            (validate_site_renderer.SUMMARY_RENDER, *validate_site_renderer.REQUIRED_SNIPPETS)
        )

        self.assertEqual(validate_site_renderer.validation_errors(html), [])
        self.assertNotIn("Dockets Checked Through", validate_site_renderer.REQUIRED_SNIPPETS)
        self.assertNotIn('id="stat-checked"', validate_site_renderer.REQUIRED_SNIPPETS)
        self.assertNotIn(
            "docketCheckedThrough(state.cases)",
            validate_site_renderer.REQUIRED_SNIPPETS,
        )

    def test_reports_every_missing_and_forbidden_snippet(self) -> None:
        html = "\n".join(validate_site_renderer.FORBIDDEN_SNIPPETS)

        errors = validate_site_renderer.validation_errors(html)

        self.assertEqual(
            len(errors),
            1
            + len(validate_site_renderer.REQUIRED_SNIPPETS)
            + len(validate_site_renderer.FORBIDDEN_SNIPPETS),
        )
        for snippet in validate_site_renderer.REQUIRED_SNIPPETS:
            self.assertIn(f"missing expected tracker renderer snippet: {snippet}", errors)
        for snippet in validate_site_renderer.FORBIDDEN_SNIPPETS:
            self.assertIn(
                f"forbidden tracker renderer snippet is still present: {snippet}",
                errors,
            )


if __name__ == "__main__":
    unittest.main()

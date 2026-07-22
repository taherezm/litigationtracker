#!/usr/bin/env python3
"""Validate the site tracker template used by the scheduled publisher."""

from __future__ import annotations

import sys
from pathlib import Path


SUMMARY_RENDER = "escapeHtml(item.plain_language_summary || '')"
REQUIRED_SNIPPETS = (
    "Activity Dates",
    "renderActivityDays(activityDays)",
    "publicEntryText(entry)",
    "Latest Activity",
    "latestActivityDate(state.cases)",
)
FORBIDDEN_SNIPPETS = (
    "Case Timeline",
    "Summary pending.",
    "Needs Review",
    "claim-tags",
    "claim-tag",
    '<span>Updated <strong id="stat-updated">',
    "mostRecentDate(state.cases.map(function (c) { return c.last_updated; }))",
)


def validation_errors(html: str) -> list[str]:
    """Return every renderer-contract violation in deterministic order."""
    errors = []
    render_count = html.count(SUMMARY_RENDER)
    if render_count != 1:
        errors.append(f"expected exactly one rendered case summary, found {render_count}")
    errors.extend(
        f"missing expected tracker renderer snippet: {snippet}"
        for snippet in REQUIRED_SNIPPETS
        if snippet not in html
    )
    errors.extend(
        f"forbidden tracker renderer snippet is still present: {snippet}"
        for snippet in FORBIDDEN_SNIPPETS
        if snippet in html
    )
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: validate_site_renderer.py path/to/tools/litigation-tracker/index.html", file=sys.stderr)
        return 2

    template_path = Path(sys.argv[1])
    html = template_path.read_text(encoding="utf-8")
    errors = validation_errors(html)
    if errors:
        print(f"{template_path}: site tracker renderer validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Site tracker renderer validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

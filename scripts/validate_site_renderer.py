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
)
FORBIDDEN_SNIPPETS = (
    "Case Timeline",
    "Summary pending.",
    "Needs Review",
    "claim-tags",
    "claim-tag",
)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: validate_site_renderer.py path/to/tools/litigation-tracker/index.html", file=sys.stderr)
        return 2

    template_path = Path(sys.argv[1])
    html = template_path.read_text(encoding="utf-8")
    render_count = html.count(SUMMARY_RENDER)
    if render_count != 1:
        print(
            f"{template_path}: expected exactly one rendered case summary, found {render_count}.",
            file=sys.stderr,
        )
        return 1
    for snippet in REQUIRED_SNIPPETS:
        if snippet not in html:
            print(f"{template_path}: missing expected tracker renderer snippet: {snippet}", file=sys.stderr)
            return 1
    for snippet in FORBIDDEN_SNIPPETS:
        if snippet in html:
            print(f"{template_path}: forbidden tracker renderer snippet is still present: {snippet}", file=sys.stderr)
            return 1

    print("Site tracker renderer validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

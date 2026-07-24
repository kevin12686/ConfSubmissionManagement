#!/usr/bin/env python3
"""Validate local Markdown links and heading anchors without extra packages."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CHANGELOG.md",
    *sorted((PROJECT_ROOT / "docs").glob("*.md")),
]
LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:")


def heading_slug(heading: str) -> str:
    """Return the GitHub-style slug used by the project's English headings."""

    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"[`*_~]", "", heading).strip().lower()
    heading = re.sub(r"[^\w\s-]", "", heading)
    heading = re.sub(r"\s+", "-", heading)
    return re.sub(r"-+", "-", heading).strip("-")


def document_anchors(path: Path) -> set[str]:
    """Return heading anchors, including suffixes for duplicate headings."""

    counts: Counter[str] = Counter()
    anchors: set[str] = set()
    for heading in HEADING_PATTERN.findall(path.read_text(encoding="utf-8")):
        base = heading_slug(heading)
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def validate() -> list[str]:
    errors: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}

    for document in DOCUMENTS:
        if not document.exists():
            errors.append(f"Missing document: {document.relative_to(PROJECT_ROOT)}")
            continue

        text = document.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = unquote(raw_target)
            if target.startswith(EXTERNAL_SCHEMES):
                continue

            path_text, separator, anchor = target.partition("#")
            target_path = (
                document
                if not path_text
                else (document.parent / path_text).resolve()
            )
            source_name = document.relative_to(PROJECT_ROOT)

            if not target_path.exists():
                errors.append(f"{source_name}: missing link target {raw_target}")
                continue

            if separator and target_path.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(
                    target_path,
                    document_anchors(target_path),
                )
                if anchor not in anchors:
                    errors.append(
                        f"{source_name}: missing heading anchor {raw_target}"
                    )

    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("Documentation validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Documentation validation passed ({len(DOCUMENTS)} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

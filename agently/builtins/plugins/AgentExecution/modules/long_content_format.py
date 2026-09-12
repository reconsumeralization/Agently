"""CommonMark structure only: no paragraph selection or semantic rewriting."""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt


def chapter_directory(body: str) -> list[dict[str, Any]]:
    """Read actual headings without changing field prose or inserting titles."""
    tokens = MarkdownIt("commonmark").enable("table").parse(body)
    return [
        {"level": int(token.tag[1:]), "title": tokens[index + 1].content}
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0
    ]


def normalize_chapter(body: str, title: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(body)
    lines = body.splitlines(keepends=True)
    headings = [
        (token, tokens[index + 1].content)
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0
    ]
    edits: list[tuple[int, int, str]] = []
    warnings: list[str] = []
    if headings:
        token, text = headings[0]
        assert token.map is not None
        start, end = token.map
        repeated = (
            len(headings) > 1
            and headings[1][1] == title
            and headings[1][0].map is not None
            and not "".join(lines[end : headings[1][0].map[0]]).strip()
        )
        if text == title and not "".join(lines[:start]).strip():
            if repeated:
                warnings.append("ambiguous_consecutive_bound_titles; preserved_titles")
            else:
                edits.append((start, end, ""))
                headings.pop(0)
    # The Host supplies H1 document / H2 chapter headings. Preserve relative depths.
    shift = 3 - min((int(token.tag[1:]) for token, _ in headings), default=3)
    if any(int(token.tag[1:]) + shift > 6 for token, _ in headings):
        warnings.append("heading_depth_not_representable; preserved_original_levels")
        shift = 0
    for token, text in headings:
        assert token.map is not None
        start, end = token.map
        before = "".join(lines[start:end])
        after = "#" * (int(token.tag[1:]) + shift) + " " + text + ("\n" if before.endswith("\n") else "")
        if before != after:
            edits.append((start, end, after))
    for start, end, after in sorted(edits, reverse=True):
        lines[start:end] = [after]
    normalized = "".join(lines)
    parsed = parser.parse(normalized)
    directory = [
        {"level": int(token.tag[1:]), "title": parsed[index + 1].content}
        for index, token in enumerate(parsed)
        if token.type == "heading_open" and token.level == 0
    ]
    return normalized, directory, warnings

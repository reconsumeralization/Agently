"""Tests for enclosing code-fence stripping in scalar output fields.

A model often wraps a single artifact (HTML, SVG, JSON, code) in a fenced
block even when the field should hold the raw value. The flat_markdown and
hybrid parsers must unwrap that outer fence, but only when the content is
*wholly* one fenced block — never when it is prose containing a code block.

Run:
    conda run -n 3.10 python -m pytest tests/test_code_fence_stripping.py
"""

from __future__ import annotations

from agently.builtins.plugins.ResponseParser.modules.code_fence import (
    strip_enclosing_code_fence,
)
from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
    parse_flat_markdown_output,
)
from agently.builtins.plugins.ResponseParser.modules.hybrid import parse_hybrid_output


HTML = "<!DOCTYPE html>\n<html>\n<body><svg></svg></body>\n</html>"


# ── strip_enclosing_code_fence ──────────────────────────────────────────────────

class TestStripEnclosingCodeFence:
    def test_unwraps_language_tagged_fence(self):
        assert strip_enclosing_code_fence(f"```html\n{HTML}\n```") == HTML

    def test_unwraps_bare_fence(self):
        assert strip_enclosing_code_fence(f"```\n{HTML}\n```") == HTML

    def test_unwraps_tilde_fence(self):
        assert strip_enclosing_code_fence(f"~~~svg\n{HTML}\n~~~") == HTML

    def test_unwraps_with_surrounding_whitespace(self):
        assert strip_enclosing_code_fence(f"\n\n```html\n{HTML}\n```\n\n") == HTML

    def test_leaves_plain_prose_untouched(self):
        text = "This is a paragraph.\nNo fences here."
        assert strip_enclosing_code_fence(text) == text

    def test_leaves_prose_with_embedded_block_untouched(self):
        text = "Here is code:\n```py\nx = 1\n```\nDone."
        assert strip_enclosing_code_fence(text) == text

    def test_does_not_merge_two_adjacent_blocks(self):
        text = "```html\n<div></div>\n```\n```css\n.a{}\n```"
        assert strip_enclosing_code_fence(text) == text

    def test_leaves_unterminated_fence_untouched(self):
        text = f"```html\n{HTML}"
        assert strip_enclosing_code_fence(text) == text

    def test_info_string_with_spaces_is_not_a_fence(self):
        text = "``` not really a language tag\nbody\n```"
        assert strip_enclosing_code_fence(text) == text

    def test_non_string_passthrough(self):
        assert strip_enclosing_code_fence(None) is None  # type: ignore[arg-type]

    def test_preserves_inner_blank_lines(self):
        body = "line1\n\nline2"
        assert strip_enclosing_code_fence(f"```\n{body}\n```") == body


# ── flat_markdown integration ────────────────────────────────────────────────────

class TestFlatMarkdownUnwrap:
    schema = {
        "html": (str, "The complete HTML document."),
        "notes": (str, "One-line summary."),
    }

    def test_fenced_html_field_is_unwrapped(self):
        text = f"### html\n```html\n{HTML}\n```\n\n### notes\nSeven layers."
        result = parse_flat_markdown_output(text, self.schema)
        assert result is not None
        assert result["html"] == HTML
        assert not result["html"].startswith("```")
        assert result["notes"] == "Seven layers."

    def test_unfenced_html_field_is_unchanged(self):
        text = f"### html\n{HTML}\n\n### notes\nSeven layers."
        result = parse_flat_markdown_output(text, self.schema)
        assert result is not None
        assert result["html"] == HTML


# ── hybrid integration ───────────────────────────────────────────────────────────

class TestHybridUnwrap:
    def test_fenced_scalar_artifact_is_unwrapped(self):
        schema = {
            "html": (str, "The HTML document."),
            "items": (list, "A list of strings."),
        }
        text = (
            f"### html\n```html\n{HTML}\n```\n\n"
            "### items\n```json\n[\"a\", \"b\"]\n```"
        )
        result = parse_hybrid_output(text, schema)
        assert result is not None
        assert result["html"] == HTML
        assert result["items"] == ["a", "b"]

    def test_json_encoded_scalar_string_is_decoded(self):
        schema = {"title": (str, "A title.")}
        text = '### title\n```json\n"Hello \\"world\\""\n```'
        result = parse_hybrid_output(text, schema)
        assert result is not None
        assert result["title"] == 'Hello "world"'


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))

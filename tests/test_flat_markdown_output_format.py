"""Tests for flat_markdown output format support.

Run:
    conda run -n 3.10 python tests/test_flat_markdown_output_format.py
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
import warnings

import pytest


# ── Test Data ──────────────────────────────────────────────────────────────────

TEST_SCHEMA = {
    "html": (str, "The complete self-contained HTML document."),
    "notes": (str, "One-line summary of the layers represented."),
}

SAMPLE_RESPONSE = """\
Some preamble that should be ignored.

### html
<!DOCTYPE html>
<html>
<body><svg>...</svg></body>
</html>

### notes
Agently framework v4.1.2.x architecture with 7 layers.
"""


# ── prompt.py ──────────────────────────────────────────────────────────────────

class TestOutputFormat:
    def test_flat_markdown_is_valid_output_format(self):
        from agently.types.data.prompt import PromptModel

        m = PromptModel(output=TEST_SCHEMA, output_format="flat_markdown")
        assert m.output_format == "flat_markdown"
        assert isinstance(m.output, dict)

    def test_dict_output_defaults_to_auto_resolution(self):
        """When output_format is None (default), dict output uses auto resolution."""
        from agently.types.data.prompt import PromptModel

        m = PromptModel(output=TEST_SCHEMA, output_format=None)
        assert m.output_format == "flat_markdown"
        assert m.output_format_resolved_from_auto is True

    def test_non_dict_output_with_flat_markdown_warns_and_falls_back(self):
        from agently.types.data.prompt import PromptModel

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            m = PromptModel(output=(str,), output_format="flat_markdown")
            assert m.output_format == "json"
            assert len(w) >= 1
            assert "flat_markdown" in str(w[0].message)

    def test_json_format_still_works(self):
        from agently.types.data.prompt import PromptModel

        m = PromptModel(output=TEST_SCHEMA, output_format="json")
        assert m.output_format == "json"


# ── flat_markdown parser ───────────────────────────────────────────────────────

class TestParseFlatMarkdownOutput:
    def test_parses_sections_correctly(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            parse_flat_markdown_output,
        )

        result = parse_flat_markdown_output(SAMPLE_RESPONSE, TEST_SCHEMA)
        assert result is not None
        assert "html" in result
        assert "notes" in result
        assert "<!DOCTYPE html>" in result["html"]
        assert "Agently framework" in result["notes"]

    def test_returns_none_for_empty_dict_schema(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            parse_flat_markdown_output,
        )

        result = parse_flat_markdown_output("### html\ncontent", {})
        assert result is None

    def test_returns_none_for_non_dict_schema(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            parse_flat_markdown_output,
        )

        result = parse_flat_markdown_output("### html\ncontent", cast(Any, []))
        assert result is None

    def test_ignores_unknown_section_headers(self):
        """Only headers matching known field names are recognized."""
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            parse_flat_markdown_output,
        )

        text = "### unknown\ncontent\n### html\n<div>ok</div>\n### notes\nsummary"
        result = parse_flat_markdown_output(text, TEST_SCHEMA)
        assert result is not None
        # "### unknown" is not a known field, so it won't be parsed as a section boundary
        # The text before "### html" includes "### unknown\ncontent\n"
        assert "html" in result
        assert "notes" in result

    def test_missing_section_returns_empty_dict_entry(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            parse_flat_markdown_output,
        )

        # Only "html" section present, "notes" is missing
        text = "### html\n<div>ok</div>"
        result = parse_flat_markdown_output(text, TEST_SCHEMA)
        assert result is not None
        assert result["html"] == "<div>ok</div>"
        # notes isn't in result because no "### notes" header was found
        assert "notes" not in result

    def test_accepts_model_copied_header_annotations(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            parse_flat_markdown_output,
        )

        text = "### html [text]\n<div>ok</div>\n\n### notes [JSON]\nsummary"
        result = parse_flat_markdown_output(text, TEST_SCHEMA)
        assert result == {"html": "<div>ok</div>", "notes": "summary"}


# ── flat_markdown streaming parser ──────────────────────────────────────────────

class TestFlatMarkdownStreamingParser:
    async def _collect_events(self, parser, chunks):
        events = []
        for chunk in chunks:
            async for ev in parser.parse_chunk(chunk):
                events.append(ev)
        async for ev in parser.flush():
            events.append(ev)
        return events

    @pytest.mark.asyncio
    async def test_streaming_emits_correct_events(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            FlatMarkdownStreamingParser,
        )

        parser = FlatMarkdownStreamingParser(TEST_SCHEMA)
        events = await self._collect_events(parser, [
            "preamble\n### html\n<div>ok</div>\n### notes\nsummary",
        ])

        paths = [e.path for e in events]
        assert "html" in paths
        assert "notes" in paths

    @pytest.mark.asyncio
    async def test_streaming_multiple_chunks(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            FlatMarkdownStreamingParser,
        )

        parser = FlatMarkdownStreamingParser(TEST_SCHEMA)
        events = await self._collect_events(parser, [
            "pre",
            "amble\n### ht",
            "ml\n<di",
            "v>ok</div>\n### ",
            "notes\nsummary",
        ])

        # Should have completion events for both fields
        done_events = [e for e in events if e.event_type == "done"]
        assert len(done_events) >= 1

    @pytest.mark.asyncio
    async def test_empty_schema_produces_no_events(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            FlatMarkdownStreamingParser,
        )

        parser = FlatMarkdownStreamingParser({})
        events = await self._collect_events(parser, ["### html\ncontent"])
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_streaming_header_annotation_keeps_clean_field_path(self):
        from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
            FlatMarkdownStreamingParser,
        )

        parser = FlatMarkdownStreamingParser(TEST_SCHEMA)
        events = await self._collect_events(parser, ["### html [text]\n<div>ok</div>"])
        assert events[0].path == "html"


# ── Integration: ModelRequest.output() with format ──────────────────────────────

class TestModelRequestOutputFormat:
    def test_format_is_stored_in_prompt(self):
        from agently import Agently

        agent = Agently.create_agent("test")
        agent.request.output(TEST_SCHEMA, format="flat_markdown")
        fmt = agent.request.prompt.get("output_format")
        assert fmt == "flat_markdown"

    def test_default_format_is_auto(self):
        from agently import Agently

        agent = Agently.create_agent("test")
        agent.request.output(TEST_SCHEMA)
        fmt = agent.request.prompt.get("output_format")
        assert fmt == "auto"
        assert agent.request.prompt.to_prompt_object().output_format == "flat_markdown"

    def test_flat_markdown_prompt_keeps_header_line_plain(self):
        from agently import Agently

        agent = Agently.create_agent("test")
        agent.request.output(TEST_SCHEMA, format="flat_markdown")
        prompt_text = agent.request.prompt.to_text()

        assert "### html\n<!-- The complete self-contained HTML document. -->" in prompt_text
        assert "### notes\n<!-- One-line summary of the layers represented. -->" in prompt_text
        assert "### html  <!--" not in prompt_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""Tests for hybrid output format, auto resolution, and degradation."""

import pytest
from agently.types.data.prompt import (
    _is_scalar_field_spec,
    _classify_field_spec,
    _resolve_auto_format,
    _should_auto_use_hybrid,
    PromptModel,
)
from agently.builtins.plugins.ResponseParser.modules.hybrid import (
    parse_hybrid_output,
    _extract_json_block,
)


class TestScalarClassification:
    def test_str_tuple_is_scalar(self):
        assert _is_scalar_field_spec((str, "desc")) is True

    def test_int_tuple_is_scalar(self):
        assert _is_scalar_field_spec((int, "age")) is True

    def test_bool_tuple_is_scalar(self):
        assert _is_scalar_field_spec((bool, "active")) is True

    def test_float_tuple_is_scalar(self):
        assert _is_scalar_field_spec((float, "score")) is True

    def test_list_is_not_scalar(self):
        assert _is_scalar_field_spec([(str,)]) is False

    def test_dict_is_not_scalar(self):
        assert _is_scalar_field_spec({"nested": (str,)}) is False

    def test_non_type_tuple_is_not_scalar(self):
        assert _is_scalar_field_spec(("not-a-type", "desc")) is False


class TestClassifyFieldSpec:
    def test_str_is_scalar(self):
        assert _classify_field_spec((str, "desc")) == "scalar"

    def test_int_is_scalar(self):
        assert _classify_field_spec((int,)) == "scalar"

    def test_list_is_complex(self):
        assert _classify_field_spec([(str,)]) == "complex"

    def test_dict_is_complex(self):
        assert _classify_field_spec({"a": (int,)}) == "complex"


class TestResolveAutoFormat:
    def test_all_str_xml_field(self):
        assert _resolve_auto_format({"html": (str, "HTML"), "notes": (str, "Notes")}) == "xml_field"

    def test_mixed_scalar_types_hybrid(self):
        """String fields mixed with typed controls use hybrid in auto mode."""
        assert _resolve_auto_format({"name": (str,), "age": (int,), "active": (bool,)}) == "hybrid"

    def test_string_scalar_plus_complex_hybrid(self):
        assert _resolve_auto_format({"summary": (str,), "items": [(str,)]}) == "hybrid"

    def test_eda_schema_hybrid(self):
        """String fields plus complex records resolve to hybrid."""
        schema = {
            "intent": (str, "must be exactly 'create_schematic'"),
            "title": (str, "short circuit title"),
            "analysis": (str, "one-paragraph circuit description"),
            "components": [{"refdes": (str,), "query": (str,)}],
            "nets": [{"name": (str,), "connections": [{"refdes": (str,)}]}],
        }
        assert _resolve_auto_format(schema) == "hybrid"

    def test_dense_eda_schema_with_string_fields_hybrid(self):
        """Auto uses only structure, not field names or business meaning."""
        schema = {
            "intent": (str, "must be exactly 'create_schematic'"),
            "title": (str, "short circuit title"),
            "components": [{"refdes": (str,), "query": (str,)}],
            "nets": [{"name": (str,), "connections": [{"refdes": (str,)}]}],
        }
        assert _resolve_auto_format(schema) == "hybrid"

    def test_judge_schema_hybrid_without_field_name_semantics(self):
        schema = {
            "rule_results": [{
                "rule_id": (str, "Stable rule id", True),
                "reason": (str, "Concise rationale", True),
                "passed": (bool, "Final boolean", True),
            }],
            "overall_reason": (str, "Concise summary", True),
            "passes": (bool, "Final pass/fail", True),
        }
        assert _resolve_auto_format(schema) == "hybrid"

    def test_auto_hybrid_requires_string_field_and_typed_field(self):
        assert _should_auto_use_hybrid({"label": (str,), "items": [(str,)]}) is True
        assert _should_auto_use_hybrid({"summary": (str,), "items": [(str,)], "count": (int,)}) is True
        assert _should_auto_use_hybrid({"items": [(str,)]}) is False

    def test_all_complex_json(self):
        assert _resolve_auto_format({"items": [(str,)]}) == "json"

    def test_nested_dict_json(self):
        assert _resolve_auto_format({"analysis": {"finding": (str,), "confidence": (int,)}}) == "json"

    def test_empty_dict_json(self):
        assert _resolve_auto_format({}) == "json"

    def test_non_dict_json(self):
        assert _resolve_auto_format("just a string") == "json"
        assert _resolve_auto_format(None) == "json"
        assert _resolve_auto_format([(str,)]) == "json"


class TestPromptModelAuto:
    def test_auto_with_string_and_complex_fields_sets_hybrid_and_flag(self):
        m = PromptModel(output={"analysis": (str,), "items": [(str,)]}, output_format="auto")
        assert m.output_format == "hybrid"
        assert m.output_format_resolved_from_auto is True

    def test_auto_with_generic_mixed_sets_hybrid_and_flag(self):
        m = PromptModel(output={"name": (str,), "items": [(str,)]}, output_format="auto")
        assert m.output_format == "hybrid"
        assert m.output_format_resolved_from_auto is True

    def test_auto_with_string_fields_sets_xml_field_and_flag(self):
        m = PromptModel(output={"name": (str,), "notes": (str,)}, output_format="auto")
        assert m.output_format == "xml_field"
        assert m.output_format_resolved_from_auto is True

    def test_auto_with_string_and_typed_scalar_sets_hybrid_and_flag(self):
        m = PromptModel(output={"name": (str,), "age": (int,)}, output_format="auto")
        assert m.output_format == "hybrid"
        assert m.output_format_resolved_from_auto is True

    def test_auto_with_all_complex_sets_json_and_flag(self):
        m = PromptModel(output={"items": [(str,)]}, output_format="auto")
        assert m.output_format == "json"
        assert m.output_format_resolved_from_auto is True

    def test_explicit_format_no_auto_flag(self):
        m = PromptModel(output={"html": (str,)}, output_format="flat_markdown")
        assert m.output_format == "flat_markdown"
        assert m.output_format_resolved_from_auto is False

    def test_hybrid_with_non_dict_warns_and_falls_back(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            m = PromptModel(output="not dict", output_format="hybrid")
            assert m.output_format == "json"
            assert len(w) == 1 and "hybrid" in str(w[0].message)


class TestExtractJsonBlock:
    def test_json_code_block(self):
        assert _extract_json_block('```json\n[1,2,3]\n```') == '[1,2,3]'

    def test_plain_code_block(self):
        assert _extract_json_block('```\n{"a":1}\n```') == '{"a":1}'

    def test_ignores_non_json_code_block_before_text(self):
        content = (
            "Intro\n"
            "```bash\n"
            "python --version\n"
            "```\n"
            "```json\n"
            "{\"a\": 1}\n"
            "```\n"
        )
        assert _extract_json_block(content) is None

    def test_no_block_returns_none(self):
        assert _extract_json_block("no json here") is None


class TestParseHybridOutput:
    def test_mixed_scalar_and_complex(self):
        text = (
            "### summary\n"
            "A summary paragraph.\n"
            "\n"
            "### items\n"
            "```json\n"
            '[{"id": 1, "name": "item1"}, {"id": 2, "name": "item2"}]\n'
            "```\n"
            "\n"
            "### notes\n"
            "Final notes here.\n"
        )
        schema = {
            "summary": (str, "A summary"),
            "items": [{"id": (int,), "name": (str,)}],
            "notes": (str, "Final notes"),
        }
        result = parse_hybrid_output(text, schema)
        assert result is not None
        assert result["summary"] == "A summary paragraph."
        assert result["notes"] == "Final notes here."
        assert isinstance(result["items"], list)
        assert len(result["items"]) == 2
        assert result["items"][0] == {"id": 1, "name": "item1"}

    def test_scalar_only(self):
        """Degenerate hybrid keeps string fields as text and parses typed scalars as JSON."""
        text = "### name\nAlice\n\n### age\n30\n"
        result = parse_hybrid_output(text, {"name": (str,), "age": (int,)})
        assert result is not None
        assert result["name"] == "Alice"
        assert result["age"] == 30

    def test_missing_json_block_fails_parse(self):
        text = "### items\nsome raw content without json\n"
        assert parse_hybrid_output(text, {"items": [(str,)]}) is None

    def test_malformed_json_fails_parse(self):
        text = "### items\n```json\n{broken json!!!\n```\n"
        assert parse_hybrid_output(text, {"items": [{"id": (int,)}]}) is None

    def test_empty_schema_returns_none(self):
        assert parse_hybrid_output("### x\ntext", {}) is None

    def test_no_sections_returns_none(self):
        assert parse_hybrid_output("just some text", {"field": (str,)}) is None

    def test_accepts_model_copied_header_annotations(self):
        text = (
            "### summary [text]\n"
            "A summary paragraph.\n\n"
            "### items [JSON]\n"
            "```json\n"
            '["one", "two"]\n'
            "```\n"
        )
        result = parse_hybrid_output(text, {"summary": (str,), "items": [(str,)]})
        assert result == {"summary": "A summary paragraph.", "items": ["one", "two"]}

    def test_text_field_preserves_inner_code_fences_and_markdown_headings(self):
        text = (
            "### lesson_script\n"
            "# Lesson\n\n"
            "### 1. Runtime Check\n"
            "Run this command:\n"
            "```bash\n"
            "python --version\n"
            "```\n"
            "Then continue after the code block.\n\n"
            "### items\n"
            "```json\n"
            "[{\"name\": \"Python\", \"passed\": true}]\n"
            "```\n"
        )
        result = parse_hybrid_output(
            text,
            {
                "lesson_script": (str,),
                "items": [{"name": (str,), "passed": (bool,)}],
            },
        )
        assert result is not None
        assert "### 1. Runtime Check" in result["lesson_script"]
        assert "```bash\npython --version\n```" in result["lesson_script"]
        assert "Then continue after the code block." in result["lesson_script"]
        assert result["items"] == [{"name": "Python", "passed": True}]

    def test_unwraps_scalar_same_field_json_wrapper(self):
        text = '### passes\n```json\n{"passes": false}\n```'
        result = parse_hybrid_output(text, {"passes": (bool,)})
        assert result == {"passes": False}

    def test_rejects_complex_placeholder_scaffold(self):
        text = "### rule_results\n<!-- (JSON) <per-rule evidence list> -->"
        assert parse_hybrid_output(text, {"rule_results": [{"passed": (bool,)}]}) is None

    def test_strips_leading_legacy_scaffold_comment_from_text_field(self):
        text = (
            "### environment_check\n"
            "<!-- (text) 一句话说明如何判断环境通过 -->\n"
            "Python imports and vector database initialization both pass.\n"
        )
        result = parse_hybrid_output(text, {"environment_check": (str,)})
        assert result == {
            "environment_check": "Python imports and vector database initialization both pass."
        }

    def test_hybrid_prompt_does_not_emit_html_comment_scaffold(self):
        from agently import Agently

        agent = Agently.create_agent("hybrid-prompt-no-comments")
        agent.request.output(
            {
                "environment_check": (str, "一句话说明如何判断环境通过"),
                "checks": [{"name": (str,), "passed": (bool,)}],
            },
            format="hybrid",
        )
        prompt_text = agent.request.prompt.to_text()
        assert "<!--" not in prompt_text
        assert "### environment_check" in prompt_text


class TestHybridStreamingParser:
    async def _collect_events(self, parser, chunks):
        events = []
        for chunk in chunks:
            async for ev in parser.parse_chunk(chunk):
                events.append(ev)
        async for ev in parser.flush():
            events.append(ev)
        return events

    @pytest.mark.asyncio
    async def test_streaming_header_annotation_keeps_clean_field_path(self):
        from agently.builtins.plugins.ResponseParser.modules.hybrid import HybridStreamingParser

        parser = HybridStreamingParser({"summary": (str,), "items": [(str,)]})
        events = await self._collect_events(parser, ["### summary [text]\nA summary"])
        assert events[0].path == "summary"

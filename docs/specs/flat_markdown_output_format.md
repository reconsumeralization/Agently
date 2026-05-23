# Spec: Output Format — auto / flat_markdown / hybrid / json

**Branch:** `feature/flat-markdown-output-format` / `update/skills-executor-fullfill`
**Date:** 2026-05-23
**Status:** implemented

Expected release: TBD

Implementation anchors:
- `agently/types/data/prompt.py` — `OutputFormat`, `_resolve_auto_format`, `_classify_field_spec`, `output_format_resolved_from_auto`
- `agently/builtins/plugins/ResponseParser/modules/flat_markdown.py` — `parse_flat_markdown_output`, `FlatMarkdownStreamingParser`
- `agently/builtins/plugins/ResponseParser/modules/hybrid.py` — `parse_hybrid_output`, `HybridStreamingParser`
- `agently/builtins/plugins/PromptGenerator/AgentlyPromptGenerator.py` — `_generate_flat_markdown_output_prompt`, `_generate_hybrid_output_prompt`
- `agently/builtins/plugins/ResponseParser/AgentlyResponseParser.py` — flat_markdown / hybrid parsing + streaming integration
- `agently/core/ModelResponseResult.py` — `_try_auto_degradation`
- `agently/core/ModelRequest.py`, `agently/core/Agent.py` — `format` parameter
- `tests/test_flat_markdown_output_format.py`, `tests/test_hybrid_output_format.py`

---

## 1. Overview

`.output()` supports four format modes:

| Format | Behavior | Trigger |
|--------|----------|---------|
| `"json"` | JSON schema in prompt, JSON parse in response | Default, explicit, or auto for complex schemas |
| `"flat_markdown"` | `### field_name` section headers, plain text content | Explicit, or auto for all-scalar schemas |
| `"hybrid"` | `### field_name` headers + `[text]`/`[JSON]` annotations | Explicit, or auto for mixed scalar+complex schemas |
| `"auto"` | Heuristic chooses among the three above; degrades to json on parse failure | Default behavior or explicit `format="auto"` |

## 2. Format Selection (auto)

### 2.1 Heuristic

```
all-scalar (str/int/bool/float only) → flat_markdown
mixed (scalar + list/dict)           → hybrid
all-complex / non-dict               → json
```

Classification helpers in `agently/types/data/prompt.py`:

```python
_SCALAR_TYPES = (str, int, float, bool)

def _is_scalar_field_spec(field_spec: Any) -> bool: ...
def _classify_field_spec(field_spec: Any) -> Literal["scalar", "complex"]: ...
def _resolve_auto_format(output: Any) -> Literal["json", "flat_markdown", "hybrid"]: ...
```

### 2.2 Degradation

When `format="auto"` resolves to `flat_markdown` or `hybrid` and parsing fails, the system automatically degrades to `json` and retries:

1. `PromptModel` records `output_format_resolved_from_auto: bool = True`
2. `ModelResponseResult.async_get_data()` detects parse failure
3. `_try_auto_degradation()` changes format to `"json"` and creates a new `ModelResponse` for retry
4. Degradation only triggers in auto mode, never for explicit format choices

## 3. flat_markdown Format

### 3.1 Scope

Expanded from "flat dict + all-str leaves" to **flat dict + all-scalar leaves** (str, int, bool, float). Pydantic type coercion handles the conversion from model text output to target types.

### 3.2 Prompt Template

```
[OUTPUT REQUIREMENT]:
Data Format: Structured Markdown

Respond in clearly separated sections. Each section MUST start with the exact
markdown header shown below (`### field_name`). Write your content after the
header, then a blank line before the next section.

### field_name
<!-- description -->
(your content here)
```

### 3.3 Parsing

Split response by `### field_name` headers. Header regex accepts optional `[text]`/`[JSON]` suffix (defensive: some models copy prompt annotations into output). Content between headers becomes field values.

### 3.4 Streaming

`FlatMarkdownStreamingParser` buffers chunks, detects `### field_name` headers, emits `StreamingData` deltas per field at coarse granularity. JSON sub-parsing is not needed (scalar fields only).

## 4. hybrid Format

### 4.1 Scope

For mixed schemas: scalar fields (str/int/bool/float) coexist with complex fields (list, nested dict). Scalar fields use plain text content; complex fields use ```json code blocks within the same markdown section structure.

### 4.2 Prompt Template

```
[OUTPUT REQUIREMENT]:
Data Format: Hybrid Structured Markdown + JSON

This format combines plain text sections with JSON code blocks:
- Sections marked with `<!-- (text) -->` expect plain text content.
- Sections marked with `<!-- (JSON) -->` expect a JSON code block.

### field_name
<!-- (text) description -->
(your plain text content here)

### complex_field
<!-- (JSON) description -->
```json
(your JSON content here)
```
```

Format hints are placed in HTML comments **below** the header line (not as `[text]`/`[JSON]` suffixes on the header) to prevent models from copying annotations into their output headers.

### 4.3 Parsing

Two-phase parse in `parse_hybrid_output()`:

1. **Split** by `### field_name` headers (same regex as flat_markdown, with optional `[text]`/`[JSON]` suffix tolerance)
2. **Per-field**:
   - **Complex fields**: extract ```json code block via `_extract_json_block()`, parse with `json5.loads()`. Fallback: store raw string on failure.
   - **Scalar fields**: store plain text. Defensive: if model erroneously wrapped scalar content in ```json```, extract and unwrap simple strings.

### 4.4 Streaming

`HybridStreamingParser` mirrors `FlatMarkdownStreamingParser` — emits text deltas per field at coarse granularity. JSON sub-parsing deferred to `parse_hybrid_output()` during finalisation.

## 5. Cross-Model Test Results (2026-05-23)

6 providers × 12 scenarios = 72 tests.

### 5.1 Providers

| Provider | Model |
|----------|-------|
| DeepSeek | deepseek-v4-flash |
| Qwen (DashScope) | qwen3-32b |
| Qianfan | ernie-5.1 |
| MiniMax | MiniMax-M2.7 |
| GLM (智谱) | glm-5.1 |
| Qwen2.5 (Ollama) | qwen2.5:7b |

### 5.2 Scenarios

| # | Schema | Expected Format |
|---|--------|----------------|
| S1 | 2×str | flat_markdown |
| S2 | 1×str (HTML) | flat_markdown |
| S3 | str+int+bool | flat_markdown |
| S4 | 4×str | flat_markdown |
| S5 | list[str] | json |
| S6 | nested dict | json |
| S7 | 3×int | flat_markdown |
| S8 | 2×str (instant) | flat_markdown |
| S9 | str+list[dict]+str | hybrid |
| S10 | 3×str+list[dict] (EDA) | hybrid |
| S11 | list[dict]+str (instant) | hybrid |
| S12 | bool+list[str]+str | hybrid |

### 5.3 Results

| Provider | Pass | Degradations | Avg Latency |
|----------|------|-------------|-------------|
| DeepSeek | 12/12 | 1 (S7 fm→json) | 4.5s |
| Qwen3-32b | 12/12 | 1 (S7 fm→json) | 23.5s |
| ERNIE-5.1 | 12/12 | 3 (S2,S3,S7 fm→json) | 6.6s |
| MiniMax-M2.7 | 12/12 | 0 | 9.4s |
| GLM-5.1 | 12/12 | 4 (S1,S2,S3,S4,S7 fm→json) | 20.5s |
| Qwen2.5:7b | 12/12 | 0 | 5.2s |

**Overall: 72/72 pass (100%).**

### 5.4 Hybrid Reliability

Hybrid format: **24/24 native parse success** (4 scenarios × 6 providers). Zero degradations.

All 6 models correctly follow the hybrid prompt instructions:
- Distinguish `<!-- (text) -->` from `<!-- (JSON) -->` annotations
- Output valid JSON in ```json code blocks for complex fields
- Output plain text for scalar fields (DeepSeek occasionally wraps scalar content in ```json``` — handled defensively by the parser)

### 5.5 flat_markdown Degradation Patterns

9 degradations total, all rescued by auto degradation to json:

| Scenario | Count | Root Cause |
|----------|-------|-----------|
| S7 (3×int) | 4 | Models output bare numbers without `### field_name` headers |
| S2 (HTML) | 2 | ERNIE/GLM don't follow markdown section format |
| S3 (str+int+bool) | 2 | Same |
| S4 (4×str) | 1 | GLM |

S7 is the weakest flat_markdown scenario — models tend to omit section headers for pure-numeric content. This is a prompt-level improvement area.

### 5.6 Instant Streaming Support

`instant` / `streaming_parse` support is available for structured output modes:

| Format | Instant support | Notes |
|--------|-----------------|-------|
| `json` | Yes | Uses incremental JSON parsing; final repair/parse still happens on completion. |
| `flat_markdown` | Yes | Emits field-level text deltas from `### field_name` sections. |
| `hybrid` | Yes | Emits field-level text deltas; JSON block parsing is deferred to finalization. |
| `auto` | Yes | Uses the streaming parser for the resolved format. If final auto degradation retries as JSON, instant events from the first attempt should be treated as provisional UI state. |
| `text` / plain text | No structured instant paths | Use raw `delta` streaming or final `get_text()` instead. |

Instant scenarios in the 72-case acceptance run covered flat scalar output
(S8) and hybrid mixed output (S11). Durable business decisions should consume
the completed parsed result, not provisional streaming events.

## 6. Files

| File | Change |
|------|--------|
| `agently/types/data/prompt.py` | `OutputFormat` literal, `_resolve_auto_format`, `_classify_field_spec`, `output_format_resolved_from_auto` field |
| `agently/builtins/plugins/ResponseParser/modules/hybrid.py` | New: `parse_hybrid_output`, `HybridStreamingParser`, `_extract_json_block` |
| `agently/builtins/plugins/ResponseParser/modules/flat_markdown.py` | Header regex accepts optional `[text]`/`[JSON]` suffix |
| `agently/builtins/plugins/PromptGenerator/AgentlyPromptGenerator.py` | `_generate_hybrid_output_prompt`, `case "hybrid"` |
| `agently/builtins/plugins/ResponseParser/AgentlyResponseParser.py` | Hybrid branch in `_handle_done_event`, streaming, `async_get_data_object` |
| `agently/core/ModelResponseResult.py` | `_try_auto_degradation()` |
| `agently/core/ModelRequest.py` | `format` parameter literal updated |
| `agently/core/Agent.py` | `format` parameter literal updated |
| `agently/builtins/agent_extensions/SkillsExtension/_SkillsContext.py` | `output_format` literal updated |
| `tests/test_hybrid_output_format.py` | New: hybrid, auto-resolution, and streaming tests |
| `tests/test_flat_markdown_output_format.py` | Updated: non-str scalar, prompt shape, and streaming tests |

## 7. Edge Cases

### 7.1 Schema validation
- `format="hybrid"` requires output to be a `dict`. Non-dict output warns and falls back to json.
- `format="flat_markdown"` requires output to be a `dict`. Same fallback.

### 7.2 Model copies prompt annotations
- Models may copy `[text]`/`[JSON]` suffixes into output headers. Header regex accepts optional suffix: `rf"^###\s+({name})\s*(?:\[(?:text|JSON)\])?\s*$"`.
- Models may wrap scalar content in ```json blocks. `parse_hybrid_output` extracts and unwraps simple strings defensively.

### 7.3 Backward compatibility
- Default `.output(...)` behavior is `auto`: flat scalar dict schemas resolve to `flat_markdown`, mixed scalar-plus-complex schemas resolve to `hybrid`, and all-complex or non-dict schemas resolve to `json`. Explicit `format="json"` preserves the legacy JSON-only path.
- Explicit `format="json"`, `"flat_markdown"`, or `"hybrid"` skips heuristics and degradation.

### 7.4 Degradation scope
- Only triggers when format was auto-resolved (`output_format_resolved_from_auto=True`).
- Explicit format choices never degrade — parse failures propagate as errors.

## 8. Open Questions

1. **Should hybrid be the recommended format for all non-trivial schemas?** Current data says yes (100% native success across 6 models), but flat_markdown is simpler for purely scalar schemas and most models handle it well.
2. **S7 (all-int) prompt improvement?** Models struggle with `### field_name` for pure-numeric fields. Could add explicit "even for numeric values, include the header" instruction.
3. **Field-level format override?** A future enhancement could allow per-field format hints in the schema itself rather than relying on auto-classification.

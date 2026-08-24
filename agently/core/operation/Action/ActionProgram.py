# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic contracts for programmatic Action planning.

This module deliberately has no ActionRuntime, provider, or TaskWorkspace
dependency.  It projects already-scoped Action specs into a stable Python SDK
and validates the two JSON boundaries owned by the host: model program
decisions and program/binding values.
"""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import math
import re
import textwrap
import types
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

from agently.types.data import (
    PROGRAMMATIC_ACTION_ARTIFACT_READ_ID,
    PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION,
    PROGRAMMATIC_ACTION_TRANSPORT_ID,
    ActionDiagnostic,
    ActionSpec,
    ProgrammaticActionCatalog,
    ProgrammaticActionCatalogEntry,
    ProgrammaticActionDecision,
    code_execution_json_bytes,
    validate_code_execution_json_schema_definition,
)


_JSON_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_JSON_SCHEMA_MARKERS = frozenset(
    {
        "$defs",
        "$ref",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "not",
        "oneOf",
        "properties",
    }
)
_PYTHON_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


PROGRAMMATIC_ACTION_ARTIFACT_READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_type": {"type": "string"},
        "content_version": {"type": "string"},
        "data": {},
        "error": {"type": "string"},
        "label": {"type": "string"},
        "locator": {"type": "string"},
        "media_type": {"type": "string"},
        "next_offset": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "ok": {"type": "boolean"},
        "owner": {"const": "action_artifact", "type": "string"},
        "range": {
            "type": "object",
            "properties": {
                "end": {"type": "integer"},
                "offset": {"type": "integer"},
                "read_bytes": {"type": "integer"},
            },
            "required": ["end", "offset", "read_bytes"],
            "additionalProperties": False,
        },
        "result": {},
        "selection_key": {"type": "string"},
        "serialized_media_type": {"const": "application/json", "type": "string"},
        "status": {"enum": ["not_found", "success"], "type": "string"},
        "total_bytes": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "value": {},
    },
    "required": ["ok", "status"],
    "additionalProperties": False,
}


def canonical_lossless_json_bytes(
    value: Any,
    *,
    max_bytes: int | None = None,
    label: str = "value",
) -> bytes:
    """Return canonical UTF-8 JSON bytes, rejecting lossy Python values.

    Accepted values are exactly the JSON data model represented by builtin
    ``dict``/``list`` containers, string keys, finite numbers, strings,
    booleans, and ``None``.  Coercive ``default=str`` serialization is never
    used because it would hide an invalid Action/program boundary.
    """

    if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0):
        raise ValueError("max_bytes must be a positive integer when provided.")

    try:
        encoded = code_execution_json_bytes(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(f"{label} is not lossless UTF-8 JSON: {error}") from error
    if max_bytes is not None and len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds max_bytes: {len(encoded)} > {max_bytes}.")
    return encoded


def validate_lossless_json_value(
    value: Any,
    *,
    max_bytes: int | None = None,
    label: str = "value",
) -> int:
    """Validate one program/binding value and return its canonical byte size."""

    return len(
        canonical_lossless_json_bytes(
            value,
            max_bytes=max_bytes,
            label=label,
        )
    )


def normalize_programmatic_action_decision(
    decision: Any,
    *,
    max_program_bytes: int,
    max_description_bytes: int = 1024,
) -> ProgrammaticActionDecision:
    """Validate the exact final ModelRequest decision for one PTC round."""

    if not isinstance(decision, Mapping):
        raise ValueError("Programmatic Action decision must be an object.")
    allowed_keys = {"next_action", "description", "program"}
    unknown_keys = sorted(str(key) for key in decision.keys() if key not in allowed_keys)
    if unknown_keys:
        raise ValueError("Programmatic Action decision contains unknown fields: " + ", ".join(unknown_keys) + ".")
    missing_keys = sorted(allowed_keys - set(decision.keys()))
    if missing_keys:
        raise ValueError("Programmatic Action decision is missing required fields: " + ", ".join(missing_keys) + ".")
    if isinstance(max_program_bytes, bool) or not isinstance(max_program_bytes, int) or max_program_bytes <= 0:
        raise ValueError("max_program_bytes must be a positive integer.")
    if (
        isinstance(max_description_bytes, bool)
        or not isinstance(max_description_bytes, int)
        or max_description_bytes <= 0
    ):
        raise ValueError("max_description_bytes must be a positive integer.")

    next_action = decision.get("next_action")
    if next_action not in {"execute", "response"}:
        raise ValueError("Programmatic Action next_action must be 'execute' or 'response'.")
    description = decision.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Programmatic Action description must be a non-empty string.")
    description_bytes = _strict_utf8_bytes(description, label="description")
    if len(description_bytes) > max_description_bytes:
        raise ValueError(
            "Programmatic Action description exceeds max_description_bytes: "
            f"{len(description_bytes)} > {max_description_bytes}."
        )

    program = decision.get("program")
    if next_action == "response":
        if program is not None:
            raise ValueError("Programmatic Action response decisions require program=null.")
    else:
        if not isinstance(program, str) or not program.strip():
            raise ValueError("Programmatic Action execute decisions require a non-empty program.")
        program_bytes = _strict_utf8_bytes(program, label="program")
        if len(program_bytes) > max_program_bytes:
            raise ValueError(
                "Programmatic Action program exceeds max_program_bytes: " f"{len(program_bytes)} > {max_program_bytes}."
            )

    return cast(
        ProgrammaticActionDecision,
        {
            "next_action": next_action,
            "description": description,
            "program": program,
        },
    )


def build_programmatic_python_source(program: str) -> str:
    """Wrap one validated async-function body in the provider binding client.

    The returned source is the only program entry point needed by a Python
    binding-capable CodeExecution provider.  Syntax is validated after
    wrapping because top-level ``await`` and ``return`` are intentionally
    function-body syntax, not standalone-module syntax. V1 requires an
    explicit return in that execution scope and rejects nested function/class
    wrappers; their returns cannot settle the reserved program Action.
    """

    if not isinstance(program, str):
        raise ValueError("Programmatic Action program must be a string.")
    if not program.strip():
        raise ValueError("Programmatic Action program must not be empty.")
    _strict_utf8_bytes(program, label="program")
    indented_program = textwrap.indent(program, "    ", predicate=lambda _line: True)
    source = (
        "from __future__ import annotations\n"
        "\n"
        "import asyncio\n"
        "\n"
        "from agently_code_bindings import bindings as actions, execute_program\n"
        "\n"
        "\n"
        "async def _agently_program():\n"
        f"{indented_program}\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    asyncio.run(execute_program(_agently_program))\n"
    )
    try:
        parsed = ast.parse(source, filename="<agently-action-program>", mode="exec")
        compile(parsed, "<agently-action-program>", "exec", dont_inherit=True)
        program_function = next(
            node for node in parsed.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_agently_program"
        )
        nested_declaration = next(
            (
                node
                for node in ast.walk(program_function)
                if node is not program_function
                and isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                )
            ),
            None,
        )
        if nested_declaration is not None:
            raise ValueError("nested def, async def, class, decorator, or wrapper declarations are not allowed")
        if not any(isinstance(node, ast.Return) for node in ast.walk(program_function)):
            raise ValueError("the program execution scope must contain an explicit return statement")
    except (SyntaxError, ValueError, TypeError) as error:
        raise ValueError(f"Programmatic Action program is not a valid async Python body: {error}") from error
    return source


def _strict_utf8_bytes(value: str, *, label: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"Programmatic Action {label} must be valid UTF-8.") from error


def is_safe_python_action_identifier(action_id: str) -> bool:
    """Return whether an Action id is safe as ``actions.<id>`` source."""

    return bool(
        _PYTHON_IDENTIFIER_PATTERN.fullmatch(action_id)
        and not keyword.iskeyword(action_id)
        and not action_id.startswith("_")
    )


def build_programmatic_action_catalog(
    action_specs: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    *,
    renderer_version: str = PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION,
    revision_seed: str | None = None,
    max_description_bytes: int = 2048,
) -> ProgrammaticActionCatalog:
    """Build an eligible, lexically ordered Action catalog and Python SDK.

    The caller must pass its already-scoped Action list.  This helper applies
    only deterministic V1 eligibility rules; it does not grant visibility,
    resolve a dynamic policy, or inspect a provider.
    """

    if not isinstance(renderer_version, str) or not renderer_version.strip():
        raise ValueError("renderer_version must be a non-empty string.")
    if revision_seed is not None and not isinstance(revision_seed, str):
        raise ValueError("revision_seed must be a string when provided.")
    if (
        isinstance(max_description_bytes, bool)
        or not isinstance(max_description_bytes, int)
        or max_description_bytes <= 0
    ):
        raise ValueError("max_description_bytes must be a positive integer.")

    normalized_specs = _normalize_action_specs(action_specs)
    entries: list[ProgrammaticActionCatalogEntry] = []
    diagnostics: list[ActionDiagnostic] = []
    for spec in normalized_specs:
        action_id = str(spec.get("action_id", spec.get("name", "")))
        entry, entry_diagnostics = _project_programmatic_action_spec(
            spec,
            action_id=action_id,
            max_description_bytes=max_description_bytes,
        )
        diagnostics.extend(entry_diagnostics)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda item: item["action_id"])
    sdk = render_programmatic_action_sdk(entries, renderer_version=renderer_version)
    catalog_revision = programmatic_action_catalog_revision(
        entries,
        renderer_version=renderer_version,
        revision_seed=revision_seed,
    )
    return {
        "renderer_version": renderer_version,
        "catalog_revision": catalog_revision,
        "sdk": sdk,
        "entries": entries,
        "diagnostics": diagnostics,
    }


def _normalize_action_specs(
    action_specs: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_specs: list[dict[str, Any]] = []
    if isinstance(action_specs, Mapping):
        is_single_spec = isinstance(action_specs.get("action_id"), str) or (
            isinstance(action_specs.get("name"), str) and isinstance(action_specs.get("kwargs"), Mapping)
        )
        if is_single_spec:
            raw_specs.append(dict(cast(Mapping[str, Any], action_specs)))
        else:
            for action_id, raw_spec in action_specs.items():
                if not isinstance(raw_spec, Mapping):
                    raise ValueError(f"Action catalog entry {action_id!r} must be an object.")
                spec = dict(raw_spec)
                spec.setdefault("action_id", str(action_id))
                raw_specs.append(spec)
    elif isinstance(action_specs, Sequence) and not isinstance(action_specs, (str, bytes, bytearray)):
        for index, raw_spec in enumerate(action_specs):
            if not isinstance(raw_spec, Mapping):
                raise ValueError(f"Action catalog entry at index {index} must be an object.")
            raw_specs.append(dict(raw_spec))
    else:
        raise ValueError("action_specs must be an Action mapping or sequence.")

    seen: set[str] = set()
    for spec in raw_specs:
        action_id = str(spec.get("action_id", spec.get("name", "")))
        if action_id and action_id in seen:
            raise ValueError(f"Duplicate Action id in programmatic catalog: {action_id!r}.")
        if action_id:
            seen.add(action_id)
    return sorted(
        raw_specs,
        key=lambda spec: str(spec.get("action_id", spec.get("name", ""))),
    )


def _project_programmatic_action_spec(
    spec: Mapping[str, Any],
    *,
    action_id: str,
    max_description_bytes: int,
) -> tuple[ProgrammaticActionCatalogEntry | None, list[ActionDiagnostic]]:
    diagnostics: list[ActionDiagnostic] = []
    if not action_id:
        return None, [_ineligible_diagnostic(action_id, "invalid_action_id", "Action id is empty.")]
    action_id_error = _programmatic_action_id_error(action_id)
    if action_id_error is not None:
        return None, [_ineligible_diagnostic(action_id, "invalid_action_id", action_id_error)]
    if action_id == PROGRAMMATIC_ACTION_TRANSPORT_ID:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "reserved_transport",
                "The reserved program transport cannot be called from an Action program.",
            )
        ]

    artifact_read_exception = action_id == PROGRAMMATIC_ACTION_ARTIFACT_READ_ID
    if spec.get("expose_to_model", True) is not True and not artifact_read_exception:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "not_model_visible",
                "Action is not visible in the scoped model Action catalog.",
            )
        ]
    if spec.get("side_effect_level", "read") != "read":
        return None, [
            _ineligible_diagnostic(
                action_id,
                "side_effect_not_read",
                "Programmatic Action V1 exposes read-only Actions.",
            )
        ]
    if spec.get("replay_safe", True) is not True:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "not_replay_safe",
                "Programmatic Action V1 requires replay-safe Actions.",
            )
        ]
    default_policy = spec.get("default_policy", {})
    statically_approval_required = spec.get("approval_required") is True or (
        isinstance(default_policy, Mapping) and default_policy.get("approval_mode") == "always"
    )
    if statically_approval_required:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "approval_required",
                "Programmatic Action V1 excludes statically approval-required Actions.",
            )
        ]
    if spec.get("returns") is None and not artifact_read_exception:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "returns_missing",
                "Action must declare an explicit lossless-JSON returns contract.",
            )
        ]

    try:
        input_schema, required_input_keys = _action_input_schema(spec)
        output_schema = (
            _canonicalize_json_value(PROGRAMMATIC_ACTION_ARTIFACT_READ_OUTPUT_SCHEMA)
            if artifact_read_exception and spec.get("returns") is None
            else _canonical_schema(_descriptor_to_schema(spec.get("returns")))
        )
        canonical_lossless_json_bytes(input_schema, label=f"Action {action_id!r} input schema")
        canonical_lossless_json_bytes(output_schema, label=f"Action {action_id!r} output schema")
        validate_code_execution_json_schema_definition(
            input_schema,
            field_name=f"Action {action_id!r} input schema",
        )
        validate_code_execution_json_schema_definition(
            output_schema,
            field_name=f"Action {action_id!r} output schema",
        )
    except (TypeError, ValueError) as error:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "contract_not_lossless_json",
                f"Action schema is not a supported lossless-JSON contract: {error}",
            )
        ]

    raw_description = spec.get("desc", "")
    if not isinstance(raw_description, str):
        return None, [
            _ineligible_diagnostic(
                action_id,
                "contract_not_lossless_json",
                "Action description must be a string.",
            )
        ]
    try:
        raw_description.encode("utf-8")
    except UnicodeError:
        return None, [
            _ineligible_diagnostic(
                action_id,
                "contract_not_lossless_json",
                "Action description must be valid UTF-8.",
            )
        ]
    description = raw_description
    bounded_description, was_truncated = _bound_utf8_text(
        description,
        max_bytes=max_description_bytes,
    )
    if was_truncated:
        diagnostics.append(
            cast(
                ActionDiagnostic,
                {
                    "source": "ActionProgram",
                    "severity": "warning",
                    "code": "action.programmatic.description_truncated",
                    "message": f"Action {action_id!r} description was bounded for SDK projection.",
                    "meta": {"action_id": action_id, "max_description_bytes": max_description_bytes},
                },
            )
        )
    access_expression = (
        f"actions.{action_id}"
        if is_safe_python_action_identifier(action_id)
        else f"actions[{json.dumps(action_id, ensure_ascii=False)}]"
    )
    return (
        {
            "action_id": action_id,
            "binding_key": action_id,
            "access_expression": access_expression,
            "description": bounded_description,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "required_input_keys": required_input_keys,
            "artifact_read_exception": artifact_read_exception,
        },
        diagnostics,
    )


def _ineligible_diagnostic(action_id: str, reason: str, message: str) -> ActionDiagnostic:
    return cast(
        ActionDiagnostic,
        {
            "source": "ActionProgram",
            "severity": "warning",
            "code": f"action.programmatic.ineligible.{reason}",
            "message": message,
            "meta": {"action_id": action_id},
        },
    )


def _programmatic_action_id_error(action_id: str) -> str | None:
    try:
        action_id_bytes = action_id.encode("utf-8")
    except UnicodeError:
        return "Action id must be valid UTF-8."
    if action_id != action_id.strip():
        return "Action id must not have leading or trailing whitespace."
    if unicodedata.normalize("NFC", action_id) != action_id:
        return "Action id must use canonical NFC Unicode normalization."
    if len(action_id_bytes) > 256:
        return "Action id exceeds the code-binding key limit of 256 UTF-8 bytes."
    if any(unicodedata.category(character).startswith("C") for character in action_id):
        return "Action id must not contain Unicode control characters."
    return None


def _bound_utf8_text(value: str, *, max_bytes: int) -> tuple[str, bool]:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return value, False
    suffix = "…"
    suffix_bytes = suffix.encode("utf-8")
    prefix_bytes = raw[: max(0, max_bytes - len(suffix_bytes))]
    return prefix_bytes.decode("utf-8", errors="ignore") + suffix, True


def _action_input_schema(spec: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    kwargs = spec.get("kwargs", {})
    if not isinstance(kwargs, Mapping):
        raise ValueError("kwargs must be an object.")
    required_raw = spec.get("required_input_keys")
    if required_raw is None:
        required = {
            str(key)
            for key, descriptor in kwargs.items()
            if key != "<*>" and isinstance(descriptor, tuple) and len(descriptor) >= 3 and descriptor[2] is True
        }
    elif isinstance(required_raw, str):
        required = {required_raw}
    elif isinstance(required_raw, (list, tuple, set)):
        required = {str(key) for key in required_raw}
    else:
        raise ValueError("required_input_keys must be a string collection.")

    meta = spec.get("meta", {})
    host_only_raw = meta.get("host_only_input_keys", []) if isinstance(meta, Mapping) else []
    if isinstance(host_only_raw, str):
        host_only = {host_only_raw}
    elif isinstance(host_only_raw, (list, tuple, set)):
        host_only = {str(key) for key in host_only_raw}
    else:
        host_only = set()
    required_host_only = sorted(required & host_only)
    if required_host_only:
        raise ValueError(
            "required inputs are host-only and cannot be supplied by a program: " + ", ".join(required_host_only)
        )

    properties: dict[str, Any] = {}
    additional_properties: bool | dict[str, Any] = False
    declared: set[str] = set()
    for raw_key in sorted(kwargs.keys(), key=lambda key: str(key)):
        if not isinstance(raw_key, str):
            raise ValueError(f"kwargs contains non-string key {raw_key!r}.")
        descriptor = kwargs[raw_key]
        if raw_key == "<*>":
            annotation, _description = _unwrap_descriptor(descriptor)
            additional_schema = _canonical_schema(_descriptor_to_schema(annotation))
            additional_properties = additional_schema if additional_schema else True
            continue
        declared.add(raw_key)
        if raw_key in host_only:
            continue
        annotation, description = _unwrap_descriptor(descriptor)
        property_schema = _canonical_schema(_descriptor_to_schema(annotation))
        if description:
            property_schema = dict(property_schema)
            property_schema["description"] = description
            property_schema = _canonical_schema(property_schema)
        properties[raw_key] = property_schema

    unknown_required = sorted(required - declared)
    if unknown_required:
        raise ValueError("required_input_keys contains undeclared fields: " + ", ".join(unknown_required))
    visible_required = sorted(required - host_only)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional_properties,
    }
    if visible_required:
        schema["required"] = visible_required
    return _canonical_schema(schema), visible_required


def _unwrap_descriptor(descriptor: Any) -> tuple[Any, str]:
    if isinstance(descriptor, tuple):
        annotation = descriptor[0] if descriptor else Any
        description = descriptor[1] if len(descriptor) > 1 and isinstance(descriptor[1], str) else ""
        return annotation, description
    return descriptor, ""


def _descriptor_to_schema(annotation: Any) -> dict[str, Any]:
    if annotation is Any:
        return {}
    if annotation is None or annotation is type(None):
        return {"type": "null"}
    if isinstance(annotation, tuple):
        annotation, _description = _unwrap_descriptor(annotation)
        return _descriptor_to_schema(annotation)
    if isinstance(annotation, str):
        return _string_annotation_to_schema(annotation)
    if isinstance(annotation, Mapping):
        if _looks_like_json_schema(annotation):
            return _canonical_schema(dict(annotation))
        nested_spec: ActionSpec = {"kwargs": dict(annotation), "required_input_keys": []}
        nested_schema, _required = _action_input_schema(nested_spec)
        return nested_schema
    if isinstance(annotation, list):
        if not annotation:
            return {"type": "array"}
        if len(annotation) == 1:
            return {"type": "array", "items": _descriptor_to_schema(annotation[0])}
        return {
            "type": "array",
            "prefixItems": [_descriptor_to_schema(item) for item in annotation],
            "minItems": len(annotation),
            "maxItems": len(annotation),
        }

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        try:
            from pydantic import TypeAdapter

            annotated_schema = TypeAdapter(annotation).json_schema()
        except Exception as error:
            raise ValueError(f"unsupported Annotated JSON contract: {error}") from error
        if not isinstance(annotated_schema, Mapping):
            raise ValueError("Annotated JSON contract did not produce an object schema.")
        return _canonical_schema(annotated_schema)
    if origin in {Union, types.UnionType}:
        return {"anyOf": [_descriptor_to_schema(item) for item in args]}
    if origin is Literal:
        literal_values = list(args)
        canonical_lossless_json_bytes(literal_values, label="Literal values")
        schema: dict[str, Any] = {"enum": literal_values}
        inferred_type = _common_json_scalar_type(literal_values)
        if inferred_type is not None:
            schema["type"] = inferred_type
        return schema
    if origin is list:
        return {
            "type": "array",
            "items": _descriptor_to_schema(args[0] if args else Any),
        }
    if origin is dict:
        key_type = args[0] if args else str
        if key_type not in {str, Any}:
            raise ValueError("JSON object annotations require string keys.")
        value_schema = _descriptor_to_schema(args[1] if len(args) > 1 else Any)
        return {"type": "object", "additionalProperties": value_schema or True}
    if origin is not None:
        raise ValueError(f"unsupported JSON annotation origin: {origin!r}.")

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        enum_values = [member.value for member in annotation]
        canonical_lossless_json_bytes(enum_values, label="Enum values")
        schema = {"enum": enum_values}
        inferred_type = _common_json_scalar_type(enum_values)
        if inferred_type is not None:
            schema["type"] = inferred_type
        return schema
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is list:
        return {"type": "array"}
    if annotation is dict:
        return {"type": "object"}
    if isinstance(annotation, type) and hasattr(annotation, "model_json_schema"):
        schema = annotation.model_json_schema()
        if not isinstance(schema, dict):
            raise ValueError("model_json_schema() did not return an object.")
        return _canonical_schema(schema)
    raise ValueError(f"unsupported lossless-JSON annotation: {annotation!r}.")


def _looks_like_json_schema(value: Mapping[str, Any]) -> bool:
    raw_type = value.get("type")
    if isinstance(raw_type, str) and raw_type in _JSON_SCHEMA_TYPES:
        return True
    if isinstance(raw_type, list) and all(item in _JSON_SCHEMA_TYPES for item in raw_type):
        return True
    return bool(_JSON_SCHEMA_MARKERS & set(value.keys()))


def _string_annotation_to_schema(annotation: str) -> dict[str, Any]:
    text = annotation.strip()
    if not text:
        raise ValueError("empty string annotation")
    union_parts = _split_top_level(text, "|")
    if len(union_parts) > 1:
        return {
            "anyOf": [
                (
                    _string_annotation_to_schema(part)
                    if _is_known_string_annotation(part)
                    else {"const": _strip_literal_quotes(part), "type": "string"}
                )
                for part in union_parts
            ]
        }

    scalar_mapping = {
        "Any": {},
        "any": {},
        "bool": {"type": "boolean"},
        "boolean": {"type": "boolean"},
        "dict": {"type": "object"},
        "float": {"type": "number"},
        "int": {"type": "integer"},
        "integer": {"type": "integer"},
        "list": {"type": "array"},
        "None": {"type": "null"},
        "none": {"type": "null"},
        "null": {"type": "null"},
        "number": {"type": "number"},
        "object": {"type": "object"},
        "str": {"type": "string"},
        "string": {"type": "string"},
    }
    if text in scalar_mapping:
        return dict(scalar_mapping[text])
    if text.startswith("Optional[") and text.endswith("]"):
        return {
            "anyOf": [
                _string_annotation_to_schema(text[len("Optional[") : -1]),
                {"type": "null"},
            ]
        }
    if text.startswith("list[") and text.endswith("]"):
        return {
            "type": "array",
            "items": _string_annotation_to_schema(text[len("list[") : -1]),
        }
    if text.startswith("dict[") and text.endswith("]"):
        parameters = _split_top_level(text[len("dict[") : -1], ",")
        if len(parameters) != 2 or parameters[0].strip() not in {"str", "string", "Any"}:
            raise ValueError(f"unsupported JSON object annotation: {text!r}.")
        value_schema = _string_annotation_to_schema(parameters[1])
        return {"type": "object", "additionalProperties": value_schema or True}
    if text.startswith("Literal[") and text.endswith("]"):
        values = [_parse_literal_value(item) for item in _split_top_level(text[len("Literal[") : -1], ",")]
        canonical_lossless_json_bytes(values, label="Literal values")
        schema: dict[str, Any] = {"enum": values}
        inferred_type = _common_json_scalar_type(values)
        if inferred_type is not None:
            schema["type"] = inferred_type
        return schema
    raise ValueError(f"unsupported string annotation: {text!r}.")


def _is_known_string_annotation(annotation: str) -> bool:
    text = annotation.strip()
    return (
        text
        in {
            "Any",
            "any",
            "bool",
            "boolean",
            "dict",
            "float",
            "int",
            "integer",
            "list",
            "None",
            "none",
            "null",
            "number",
            "object",
            "str",
            "string",
        }
        or (text.startswith("Optional[") and text.endswith("]"))
        or (text.startswith("list[") and text.endswith("]"))
        or (text.startswith("dict[") and text.endswith("]"))
        or (text.startswith("Literal[") and text.endswith("]"))
    )


def _split_top_level(value: str, delimiter: str) -> list[str]:
    depth = 0
    quote: str | None = None
    escaped = False
    parts: list[str] = []
    start = 0
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character in "[({":
            depth += 1
            continue
        if character in "])}":
            depth -= 1
            continue
        if character == delimiter and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _parse_literal_value(value: str) -> Any:
    text = value.strip()
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        parsed = _strip_literal_quotes(text)
    if type(parsed) not in {str, bool, int, float, type(None)}:
        raise ValueError(f"Literal value is not a JSON scalar: {text!r}.")
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError(f"Literal value is not finite: {text!r}.")
    return parsed


def _strip_literal_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return text[1:-1]
        return str(parsed)
    return text


def _common_json_scalar_type(values: list[Any]) -> str | None:
    if values and all(type(value) is str for value in values):
        return "string"
    if values and all(type(value) is bool for value in values):
        return "boolean"
    if values and all(type(value) is int for value in values):
        return "integer"
    if values and all(type(value) in {int, float} for value in values):
        return "number"
    if values and all(value is None for value in values):
        return "null"
    return None


def _canonical_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonicalize_json_value(dict(schema))
    if not isinstance(canonical, dict):
        raise ValueError("JSON schema must be an object.")
    return canonical


def _canonicalize_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value.keys(), key=lambda item: str(item)):
            if not isinstance(key, str):
                raise ValueError(f"schema contains non-string key {key!r}.")
            result[key] = _canonicalize_json_value(value[key])
        if isinstance(result.get("required"), list):
            required = result["required"]
            if not all(isinstance(item, str) for item in required):
                raise ValueError("JSON schema required must contain strings.")
            result["required"] = sorted(set(required))
        return result
    if isinstance(value, list):
        return [_canonicalize_json_value(item) for item in value]
    if type(value) in {str, bool, int, float} or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("schema contains a non-finite number.")
        return value
    raise ValueError(f"schema contains non-JSON value {value!r}.")


def programmatic_action_catalog_revision(
    entries: Sequence[ProgrammaticActionCatalogEntry],
    *,
    renderer_version: str = PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION,
    revision_seed: str | None = None,
) -> str:
    """Return the host-owned SHA-256 revision for one exact SDK catalog."""

    ordered_entries = sorted(entries, key=lambda item: item["action_id"])
    revision_entries = [
        {
            "action_id": entry["action_id"],
            "artifact_read_exception": entry["artifact_read_exception"],
            "binding_key": entry["binding_key"],
            "description": entry["description"],
            "input_schema": entry["input_schema"],
            "output_schema": entry["output_schema"],
            "required_input_keys": entry["required_input_keys"],
        }
        for entry in ordered_entries
    ]
    payload: dict[str, Any] = {
        "entries": revision_entries,
        "renderer_version": renderer_version,
    }
    if revision_seed is not None:
        payload["revision_seed"] = revision_seed
    digest = hashlib.sha256(canonical_lossless_json_bytes(payload, label="programmatic Action catalog")).hexdigest()
    return f"sha256:{digest}"


def render_programmatic_action_sdk(
    entries: Sequence[ProgrammaticActionCatalogEntry],
    *,
    renderer_version: str = PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION,
) -> str:
    """Render byte-stable Python 3.10+ Action bindings for a model prompt."""

    ordered_entries = sorted(entries, key=lambda item: item["action_id"])
    seen: set[str] = set()
    for entry in ordered_entries:
        action_id = entry["action_id"]
        if action_id in seen:
            raise ValueError(f"Duplicate Action id in SDK entries: {action_id!r}.")
        seen.add(action_id)
        canonical_lossless_json_bytes(entry["input_schema"], label=f"Action {action_id!r} input schema")
        canonical_lossless_json_bytes(entry["output_schema"], label=f"Action {action_id!r} output schema")

    contract_payload = {
        entry["action_id"]: {
            "description": entry["description"],
            "input_schema": entry["input_schema"],
            "required_input_keys": entry["required_input_keys"],
            "returns": entry["output_schema"],
        }
        for entry in ordered_entries
    }
    contract_json = canonical_lossless_json_bytes(
        contract_payload,
        label="programmatic Action SDK contract",
    ).decode("utf-8")

    lines = [
        "# Agently programmatic Action SDK (Python 3.10+)",
        f"# Renderer: {renderer_version}",
        "# Generated deterministically from the exact eligible Action catalog.",
        "from __future__ import annotations",
        "",
        "from typing import Final, Literal, Protocol, TypedDict, Union, overload",
        "from typing_extensions import NotRequired",
        "",
        'JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]',
        f"ACTION_CONTRACTS_JSON: Final[str] = {json.dumps(contract_json, ensure_ascii=False)}",
        "",
    ]

    rendered_types: list[tuple[str, str, str]] = []
    for index, entry in enumerate(ordered_entries, start=1):
        prefix = f"_Action{index:04d}"
        type_renderer = _PythonSchemaTypeRenderer(prefix)
        input_type = type_renderer.render(entry["input_schema"], f"{prefix}Input")
        output_type = type_renderer.render(entry["output_schema"], f"{prefix}Output")
        lines.extend(type_renderer.declarations)
        if type_renderer.declarations:
            lines.append("")
        if input_type != f"{prefix}Input":
            lines.append(f"{prefix}Input = {input_type}")
        if output_type != f"{prefix}Output":
            lines.append(f"{prefix}Output = {output_type}")
        if input_type != f"{prefix}Input" or output_type != f"{prefix}Output":
            lines.append("")
        callable_name = f"{prefix}Callable"
        lines.extend(
            [
                f"class {callable_name}(Protocol):",
                f"    async def __call__(self, action_input: {prefix}Input) -> {prefix}Output: ...",
                "",
            ]
        )
        rendered_types.append((prefix, callable_name, entry["action_id"]))

    lines.append("class Actions(Protocol):")
    if not ordered_entries:
        lines.append("    pass")
    else:
        safe_entries = [
            (entry, rendered_types[index])
            for index, entry in enumerate(ordered_entries)
            if is_safe_python_action_identifier(entry["action_id"])
        ]
        exotic_entries = [
            (entry, rendered_types[index])
            for index, entry in enumerate(ordered_entries)
            if not is_safe_python_action_identifier(entry["action_id"])
        ]
        for entry, (prefix, _callable_name, action_id) in safe_entries:
            description_literal = json.dumps(entry["description"], ensure_ascii=False)
            lines.extend(
                [
                    f"    async def {action_id}(self, action_input: {prefix}Input) -> {prefix}Output:",
                    f"        {description_literal}",
                    "        ...",
                    "",
                ]
            )
        for index, (entry, (_prefix, callable_name, action_id)) in enumerate(exotic_entries):
            if len(exotic_entries) > 1:
                lines.append("    @overload")
            lines.append(
                "    def __getitem__(self, action_id: "
                f"Literal[{json.dumps(action_id, ensure_ascii=False)}]) -> {callable_name}: ..."
            )
            if index < len(exotic_entries) - 1:
                lines.append("")
        if exotic_entries:
            lines.append("")

    lines.extend(
        [
            "actions: Actions",
            "",
            "# Call each Action with one JSON object and await its JSON return value:",
        ]
    )
    for entry in ordered_entries:
        lines.append(f"# await {entry['access_expression']}({{...}})")
    return "\n".join(lines).rstrip() + "\n"


class _PythonSchemaTypeRenderer:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.declarations: list[str] = []
        self._object_index = 0

    def render(self, schema: Mapping[str, Any], preferred_name: str) -> str:
        if "const" in schema:
            return _literal_annotation([schema["const"]])
        enum_values = schema.get("enum")
        if isinstance(enum_values, list):
            return _literal_annotation(enum_values)
        for union_key in ("anyOf", "oneOf"):
            union_items = schema.get(union_key)
            if isinstance(union_items, list) and union_items:
                rendered = [
                    self.render(item, self._next_object_name()) if isinstance(item, Mapping) else "JSONValue"
                    for item in union_items
                ]
                return " | ".join(dict.fromkeys(rendered))

        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            rendered = [self.render({**dict(schema), "type": item}, self._next_object_name()) for item in schema_type]
            return " | ".join(dict.fromkeys(rendered))
        if schema_type == "string":
            return "str"
        if schema_type == "boolean":
            return "bool"
        if schema_type == "integer":
            return "int"
        if schema_type == "number":
            return "float"
        if schema_type == "null":
            return "None"
        if schema_type == "array":
            item_schema = schema.get("items", {})
            item_type = (
                self.render(item_schema, self._next_object_name()) if isinstance(item_schema, Mapping) else "JSONValue"
            )
            return f"list[{item_type}]"
        if schema_type == "object" or "properties" in schema:
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping):
                return "dict[str, JSONValue]"
            additional_properties = schema.get("additionalProperties", True)
            if not properties and additional_properties is not False:
                if isinstance(additional_properties, Mapping):
                    value_type = self.render(additional_properties, self._next_object_name())
                    return f"dict[str, {value_type}]"
                return "dict[str, JSONValue]"
            name = preferred_name
            required_raw = schema.get("required", [])
            required = {str(item) for item in required_raw} if isinstance(required_raw, list) else set()
            fields: list[str] = []
            for field_name in sorted(properties.keys(), key=lambda item: str(item)):
                field_schema = properties[field_name]
                annotation = (
                    self.render(field_schema, self._next_object_name())
                    if isinstance(field_schema, Mapping)
                    else "JSONValue"
                )
                if field_name not in required:
                    annotation = f"NotRequired[{annotation}]"
                fields.append(f"        {json.dumps(str(field_name), ensure_ascii=False)}: {annotation},")
            declaration = [
                f"{name} = TypedDict(",
                f"    {json.dumps(name)},",
                "    {",
                *fields,
                "    },",
                ")",
            ]
            self.declarations.extend(declaration)
            return name
        return "JSONValue"

    def _next_object_name(self) -> str:
        self._object_index += 1
        return f"{self.prefix}Object{self._object_index:03d}"


def _literal_annotation(values: list[Any]) -> str:
    if not values or any(type(value) not in {str, bool, int, float, type(None)} for value in values):
        return "JSONValue"
    return "Literal[" + ", ".join(repr(value) for value in values) + "]"


__all__ = [
    "PROGRAMMATIC_ACTION_ARTIFACT_READ_OUTPUT_SCHEMA",
    "build_programmatic_action_catalog",
    "build_programmatic_python_source",
    "canonical_lossless_json_bytes",
    "is_safe_python_action_identifier",
    "normalize_programmatic_action_decision",
    "programmatic_action_catalog_revision",
    "render_programmatic_action_sdk",
    "validate_lossless_json_value",
]

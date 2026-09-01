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

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID
from typing_extensions import TypedDict


CodeExecutionFileRole = Literal["source", "dependency", "input"]
CodeExecutionStepRole = Literal["build", "run"]
CodeExecutionBindingConcurrencyMode = Literal["parallel", "exclusive"]


class CodeExecutionIsolationCapability(TypedDict, total=False):
    process_contained: bool
    host_filesystem_restricted: bool
    privilege_escalation_blocked: bool
    syscalls_restricted: bool
    mechanism: str
    network_mode: str
    container_rootfs_read_only: bool


def required_code_execution_isolation() -> dict[str, bool]:
    return {
        "process_contained": True,
        "host_filesystem_restricted": True,
        "privilege_escalation_blocked": True,
        "syscalls_restricted": True,
    }


class CodeExecutionProviderCapability(TypedDict, total=False):
    languages: list[str]
    toolchains: dict[str, Any]
    isolation: CodeExecutionIsolationCapability
    workspace_access_modes: list[Literal["snapshot", "read_only", "read_write"]]
    network: str
    safety_class: Literal["isolated", "constrained", "trusted_local"]
    build_support: bool
    output_collection: bool
    host_async_bindings: bool
    mechanism: str


CodeExecutionBindingStatus = Literal[
    "success",
    "error",
    "blocked",
    "approval_required",
    "timed_out",
    "cancelled",
    "rejected",
]


class CodeExecutionBindingCallRecord(TypedDict, total=False):
    sequence: int
    binding_key: str
    status: CodeExecutionBindingStatus | str
    request_bytes: int
    response_bytes: int
    elapsed_ms: int
    concurrency_mode: CodeExecutionBindingConcurrencyMode


class CodeExecutionBindingSummary(TypedDict, total=False):
    protocol_frames: int
    rejected_frames: int
    call_count: int
    successful_calls: int
    failed_calls: int
    request_bytes: int
    response_bytes: int


class CodeExecutionResult(TypedDict, total=False):
    ok: bool
    status: Literal["success", "error", "timed_out", "blocked"] | str
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    outputs: list[str]
    log_refs: list[str]
    unsafe: bool
    diagnostics: list[dict[str, Any]]
    value: Any
    logs: list[str]
    logs_truncated: bool
    binding_calls: list[CodeExecutionBindingCallRecord]
    binding_summary: CodeExecutionBindingSummary
    meta: dict[str, Any]


_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_FILES = 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_ARGV = 256
_MAX_ARG_BYTES = 8192
_MAX_ENV = 128
_MAX_PATH_BYTES = 4096
_MAX_EXPECTED_OUTPUTS = 128
_MAX_BINDING_KEY_BYTES = 256
_MAX_BINDING_SCHEMA_BYTES = 256 * 1024
_MAX_BINDING_PROGRAM_BYTES = 8 * 1024 * 1024
_MAX_BINDING_FRAME_BYTES = 4 * 1024 * 1024
_MAX_BINDING_VALUE_BYTES = 4 * 1024 * 1024
_MAX_BINDING_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_BINDING_CALLS = 1024
_MAX_BINDING_PARALLEL_CALLS = 256
_MAX_BINDING_FRAMES = 2048
_MAX_BINDING_LOG_LINES = 4096
_MAX_BINDING_SECONDS = 300.0
_WORKSPACE_URI_PREFIX = "workspace://"
_WORKSPACE_ROLES = frozenset({"source", "build", "output", "logs"})


class CodeExecutionBindingError(Exception):
    """Bounded public failure returned by one host binding."""

    _ALLOWED_STATUSES = frozenset({"error", "blocked", "approval_required", "timed_out", "cancelled"})

    def __init__(
        self,
        message: str,
        *,
        status: Literal[
            "error",
            "blocked",
            "approval_required",
            "timed_out",
            "cancelled",
        ] = "error",
    ) -> None:
        normalized_status = str(status).strip()
        if normalized_status not in self._ALLOWED_STATUSES:
            raise ValueError(f"unsupported code binding error status: {status!r}")
        normalized_message = str(message).strip() or "Host binding failed."
        self.status = normalized_status
        self.public_message = normalized_message[:1000]
        super().__init__(self.public_message)


class CodeExecutionSchemaValidationError(ValueError):
    """A binding input or output did not satisfy its declared JSON Schema."""


def normalize_code_execution_json_value(value: Any) -> Any:
    """Rebuild a lossless JSON value without implicit host-type conversion."""

    active_containers: set[int] = set()

    def normalize(item: Any, *, depth: int) -> Any:
        if depth > 256:
            raise TypeError("code binding values exceed the maximum JSON nesting depth")
        item_type = type(item)
        if item is None or item_type in {str, bool, int}:
            return item
        if item_type is float:
            if not math.isfinite(item):
                raise TypeError("code binding values must not contain non-finite numbers")
            return item
        if item_type not in {list, dict}:
            raise TypeError("code binding lossless JSON values must contain only exact primitives, lists, and objects")
        identity = id(item)
        if identity in active_containers:
            raise TypeError("code binding values must not contain cyclic containers")
        active_containers.add(identity)
        try:
            if item_type is list:
                return [normalize(child, depth=depth + 1) for child in item]
            normalized: dict[str, Any] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise TypeError("code binding object keys must be exact strings")
                normalized[key] = normalize(child, depth=depth + 1)
            return normalized
        finally:
            active_containers.discard(identity)

    return normalize(value, depth=0)


def code_execution_json_bytes(value: Any) -> bytes:
    normalized = normalize_code_execution_json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


_SCHEMA_ANNOTATION_KEYS = frozenset(
    {
        "$anchor",
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "discriminator",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_SCHEMA_ASSERTION_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "definitions",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "if",
        "items",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "prefixItems",
        "properties",
        "propertyNames",
        "required",
        "then",
        "type",
        "uniqueItems",
    }
)
_SUPPORTED_SCHEMA_KEYS = _SCHEMA_ANNOTATION_KEYS | _SCHEMA_ASSERTION_KEYS
_SUPPORTED_JSON_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_SUPPORTED_STRING_FORMATS = frozenset(
    {
        "date",
        "date-time",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "regex",
        "time",
        "uri",
        "uri-reference",
        "uuid",
    }
)


def _schema_error(path: str, message: str) -> CodeExecutionSchemaValidationError:
    return CodeExecutionSchemaValidationError(f"{path}: {message}")


def _validate_schema_definition(schema: Any, *, path: str = "$") -> None:
    if type(schema) is bool:
        return
    if not isinstance(schema, Mapping):
        raise _schema_error(path, "JSON Schema must be an object or boolean")
    unknown = sorted(str(key) for key in schema if str(key) not in _SUPPORTED_SCHEMA_KEYS)
    if unknown:
        raise _schema_error(path, f"unsupported JSON Schema keyword: {unknown[0]!r}")
    raw_type = schema.get("type")
    if raw_type is not None:
        types = [raw_type] if type(raw_type) is str else raw_type
        if (
            type(types) is not list
            or not types
            or any(type(item) is not str or item not in _SUPPORTED_JSON_TYPES for item in types)
            or len(set(types)) != len(types)
        ):
            raise _schema_error(path, "type must contain supported JSON type names")
    raw_format = schema.get("format")
    if raw_format is not None and (type(raw_format) is not str or raw_format not in _SUPPORTED_STRING_FORMATS):
        raise _schema_error(path, f"unsupported asserted string format: {raw_format!r}")
    raw_ref = schema.get("$ref")
    if raw_ref is not None and (
        not isinstance(raw_ref, str)
        or not raw_ref.startswith("#/")
        or any(part in {"", ".", ".."} for part in raw_ref[2:].split("/"))
    ):
        raise _schema_error(path, "only normalized local JSON Pointer $ref values are supported")
    for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
        value = schema.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise _schema_error(path, f"{key} must be an object")
        for child_key, child in value.items():
            if type(child_key) is not str:
                raise _schema_error(path, f"{key} keys must be strings")
            if key == "patternProperties":
                try:
                    re.compile(child_key)
                except re.error as error:
                    raise _schema_error(path, f"invalid patternProperties pattern: {error}") from error
            _validate_schema_definition(child, path=f"{path}.{key}.{child_key}")
    for key in ("additionalProperties", "contains", "else", "if", "items", "not", "propertyNames", "then"):
        if key in schema:
            _validate_schema_definition(schema[key], path=f"{path}.{key}")
    for key in ("allOf", "anyOf", "oneOf"):
        value = schema.get(key)
        if value is None:
            continue
        if type(value) is not list or not value:
            raise _schema_error(path, f"{key} must be a non-empty array of schemas")
        for index, child in enumerate(value):
            _validate_schema_definition(child, path=f"{path}.{key}[{index}]")
    prefix_items = schema.get("prefixItems")
    if prefix_items is not None:
        if type(prefix_items) is not list:
            raise _schema_error(path, "prefixItems must be an array of schemas")
        for index, child in enumerate(prefix_items):
            _validate_schema_definition(
                child,
                path=f"{path}.prefixItems[{index}]",
            )
    required = schema.get("required")
    if required is not None:
        if (
            type(required) is not list
            or any(type(item) is not str for item in required)
            or len(set(required)) != len(required)
        ):
            raise _schema_error(path, "required must be an array of unique strings")
    dependent_required = schema.get("dependentRequired")
    if dependent_required is not None:
        if not isinstance(dependent_required, Mapping):
            raise _schema_error(path, "dependentRequired must be an object")
        for key, names in dependent_required.items():
            if (
                type(key) is not str
                or type(names) is not list
                or any(type(item) is not str for item in names)
                or len(set(names)) != len(names)
            ):
                raise _schema_error(
                    path,
                    "dependentRequired values must be arrays of unique strings",
                )
    enum = schema.get("enum")
    if enum is not None and (type(enum) is not list or not enum):
        raise _schema_error(path, "enum must be a non-empty JSON array")
    for key in (
        "minContains",
        "maxContains",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
    ):
        value = schema.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise _schema_error(path, f"{key} must be a non-negative integer")
    for key in (
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maximum",
        "minimum",
        "multipleOf",
    ):
        value = schema.get(key)
        if value is not None and (
            type(value) not in {int, float} or not math.isfinite(float(value)) or (key == "multipleOf" and value <= 0)
        ):
            raise _schema_error(path, f"{key} must be a finite valid number")
    unique_items = schema.get("uniqueItems")
    if unique_items is not None and type(unique_items) is not bool:
        raise _schema_error(path, "uniqueItems must be a boolean")
    raw_pattern = schema.get("pattern")
    if raw_pattern is not None:
        if not isinstance(raw_pattern, str):
            raise _schema_error(path, "pattern must be a string")
        try:
            re.compile(raw_pattern)
        except re.error as error:
            raise _schema_error(path, f"invalid string pattern: {error}") from error


def validate_code_execution_json_schema_definition(
    schema: Mapping[str, Any] | bool,
    *,
    field_name: str = "schema",
) -> None:
    """Fail closed when a binding schema uses an unsupported assertion."""

    if isinstance(schema, Mapping):
        normalize_code_execution_json_value(dict(schema))
    elif type(schema) is not bool:
        raise CodeExecutionSchemaValidationError(f"$.{field_name}: JSON Schema must be an object or boolean")
    _validate_schema_definition(schema, path=f"$.{field_name}")


def _resolve_local_schema_ref(root: Mapping[str, Any], ref: str) -> Any:
    current: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise _schema_error("$", f"unresolved local JSON Schema reference: {ref!r}")
        current = current[part]
    return current


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _validate_string_format(value: str, expected: str, *, path: str) -> None:
    try:
        if expected == "date":
            date.fromisoformat(value)
        elif expected == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif expected == "time":
            time.fromisoformat(value.replace("Z", "+00:00"))
        elif expected == "uuid":
            UUID(value)
        elif expected in {"ipv4", "ipv6"}:
            parsed = ip_address(value)
            if parsed.version != (4 if expected == "ipv4" else 6):
                raise ValueError("address family mismatch")
        elif expected == "email":
            local, separator, host = value.rpartition("@")
            if not separator or not local or not host or any(character.isspace() for character in value):
                raise ValueError("invalid email")
        elif expected == "hostname":
            if len(value) > 253 or not re.fullmatch(
                r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?",
                value,
            ):
                raise ValueError("invalid hostname")
        elif expected == "duration":
            if not re.fullmatch(
                r"P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?",
                value,
            ):
                raise ValueError("invalid duration")
        elif expected == "regex":
            re.compile(value)
        elif expected == "uri":
            if not urlparse(value).scheme:
                raise ValueError("absolute URI requires a scheme")
        elif expected == "uri-reference":
            urlparse(value)
    except (ValueError, re.error) as error:
        raise _schema_error(path, f"value does not match format {expected!r}") from error


def validate_code_execution_json_schema(
    value: Any,
    schema: Mapping[str, Any] | bool,
    *,
    field_name: str = "value",
) -> None:
    """Validate one lossless JSON value against the supported schema subset.

    Unsupported assertion keywords are rejected when a binding is created;
    they are never silently ignored. The subset covers Agently's ordinary
    Action/Pydantic contracts: local refs, unions, nested objects and arrays,
    numeric/string bounds, patterns, and common formats.
    """

    normalized = normalize_code_execution_json_value(value)
    runtime_schema = _json_value(schema) if isinstance(schema, Mapping) else schema
    validate_code_execution_json_schema_definition(runtime_schema)
    root = runtime_schema

    def validate(instance: Any, active_schema: Any, path: str) -> None:
        if active_schema is True:
            return
        if active_schema is False:
            raise _schema_error(path, f"{field_name} is rejected by the schema")
        assert isinstance(active_schema, Mapping)
        if "$ref" in active_schema:
            if not isinstance(root, Mapping):
                raise _schema_error(path, "local $ref requires an object root schema")
            validate(instance, _resolve_local_schema_ref(root, str(active_schema["$ref"])), path)
        if "allOf" in active_schema:
            for child in active_schema["allOf"]:
                validate(instance, child, path)
        if "anyOf" in active_schema:
            matches = 0
            for child in active_schema["anyOf"]:
                try:
                    validate(instance, child, path)
                except CodeExecutionSchemaValidationError:
                    continue
                matches += 1
            if matches == 0:
                raise _schema_error(path, f"{field_name} does not match any allowed schema")
        if "oneOf" in active_schema:
            matches = 0
            for child in active_schema["oneOf"]:
                try:
                    validate(instance, child, path)
                except CodeExecutionSchemaValidationError:
                    continue
                matches += 1
            if matches != 1:
                raise _schema_error(path, f"{field_name} must match exactly one schema")
        if "not" in active_schema:
            try:
                validate(instance, active_schema["not"], path)
            except CodeExecutionSchemaValidationError:
                pass
            else:
                raise _schema_error(path, f"{field_name} matches a forbidden schema")
        if "if" in active_schema:
            try:
                validate(instance, active_schema["if"], path)
            except CodeExecutionSchemaValidationError:
                branch = active_schema.get("else")
            else:
                branch = active_schema.get("then")
            if branch is not None:
                validate(instance, branch, path)
        if "const" in active_schema and instance != active_schema["const"]:
            raise _schema_error(path, f"{field_name} does not match const")
        if "enum" in active_schema:
            enum = active_schema["enum"]
            if (
                not isinstance(enum, Sequence)
                or isinstance(enum, (str, bytes))
                or not any(instance == item and type(instance) is type(item) for item in enum)
            ):
                raise _schema_error(path, f"{field_name} is not an allowed enum value")
        raw_types = active_schema.get("type")
        if raw_types is not None:
            types = [raw_types] if isinstance(raw_types, str) else list(raw_types)
            if not any(_json_type_matches(instance, str(expected)) for expected in types):
                raise _schema_error(path, f"{field_name} has the wrong JSON type")
        if isinstance(instance, dict):
            length = len(instance)
            if length < int(active_schema.get("minProperties", 0)):
                raise _schema_error(path, f"{field_name} has too few properties")
            if "maxProperties" in active_schema and length > int(active_schema["maxProperties"]):
                raise _schema_error(path, f"{field_name} has too many properties")
            for required_key in active_schema.get("required", ()):
                if required_key not in instance:
                    raise _schema_error(path, f"missing required property {required_key!r}")
            properties = active_schema.get("properties", {})
            patterns = active_schema.get("patternProperties", {})
            additional = active_schema.get("additionalProperties", True)
            for key, item in instance.items():
                matched = False
                if isinstance(properties, Mapping) and key in properties:
                    validate(item, properties[key], f"{path}.{key}")
                    matched = True
                if isinstance(patterns, Mapping):
                    for pattern, child in patterns.items():
                        if re.search(str(pattern), key):
                            validate(item, child, f"{path}.{key}")
                            matched = True
                if not matched:
                    if additional is False:
                        raise _schema_error(path, f"unexpected property {key!r}")
                    if isinstance(additional, Mapping):
                        validate(item, additional, f"{path}.{key}")
                property_names = active_schema.get("propertyNames")
                if property_names is not None:
                    validate(key, property_names, f"{path}.{key}")
            for dependency, names in active_schema.get("dependentRequired", {}).items():
                if dependency in instance:
                    for name in names:
                        if name not in instance:
                            raise _schema_error(path, f"property {dependency!r} requires {name!r}")
            for dependency, child in active_schema.get("dependentSchemas", {}).items():
                if dependency in instance:
                    validate(instance, child, path)
        if isinstance(instance, list):
            length = len(instance)
            if length < int(active_schema.get("minItems", 0)):
                raise _schema_error(path, f"{field_name} has too few items")
            if "maxItems" in active_schema and length > int(active_schema["maxItems"]):
                raise _schema_error(path, f"{field_name} has too many items")
            if active_schema.get("uniqueItems"):
                canonical = [code_execution_json_bytes(item) for item in instance]
                if len(set(canonical)) != len(canonical):
                    raise _schema_error(path, f"{field_name} items must be unique")
            prefix_items = active_schema.get("prefixItems", ())
            for index, child in enumerate(prefix_items):
                if index < length:
                    validate(instance[index], child, f"{path}[{index}]")
            items = active_schema.get("items")
            if items is not None:
                start = len(prefix_items)
                for index in range(start, length):
                    validate(instance[index], items, f"{path}[{index}]")
            contains = active_schema.get("contains")
            if contains is not None:
                matches = 0
                for index, item in enumerate(instance):
                    try:
                        validate(item, contains, f"{path}[{index}]")
                    except CodeExecutionSchemaValidationError:
                        continue
                    matches += 1
                minimum = int(active_schema.get("minContains", 1))
                maximum = active_schema.get("maxContains")
                if matches < minimum or (maximum is not None and matches > int(maximum)):
                    raise _schema_error(path, f"{field_name} contains count is outside bounds")
        if isinstance(instance, str):
            if len(instance) < int(active_schema.get("minLength", 0)):
                raise _schema_error(path, f"{field_name} is too short")
            if "maxLength" in active_schema and len(instance) > int(active_schema["maxLength"]):
                raise _schema_error(path, f"{field_name} is too long")
            if "pattern" in active_schema and not re.search(str(active_schema["pattern"]), instance):
                raise _schema_error(path, f"{field_name} does not match the required pattern")
            if "format" in active_schema:
                _validate_string_format(instance, str(active_schema["format"]), path=path)
        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            number = float(instance)
            if "minimum" in active_schema and number < float(active_schema["minimum"]):
                raise _schema_error(path, f"{field_name} is below minimum")
            if "maximum" in active_schema and number > float(active_schema["maximum"]):
                raise _schema_error(path, f"{field_name} is above maximum")
            if "exclusiveMinimum" in active_schema and number <= float(active_schema["exclusiveMinimum"]):
                raise _schema_error(path, f"{field_name} is below exclusive minimum")
            if "exclusiveMaximum" in active_schema and number >= float(active_schema["exclusiveMaximum"]):
                raise _schema_error(path, f"{field_name} is above exclusive maximum")
            if "multipleOf" in active_schema:
                multiple = float(active_schema["multipleOf"])
                if multiple <= 0 or not math.isclose(
                    number / multiple,
                    round(number / multiple),
                    abs_tol=1e-9,
                ):
                    raise _schema_error(path, f"{field_name} is not a multiple of the declared value")

    validate(normalized, runtime_schema, "$")


@dataclass(frozen=True)
class CodeExecutionBinding:
    binding_key: str
    async_handler: Callable[[Mapping[str, Any]], Awaitable[Any]] = field(
        repr=False,
        compare=False,
    )
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    concurrency_mode: CodeExecutionBindingConcurrencyMode = "exclusive"

    def __post_init__(self) -> None:
        key = unicodedata.normalize("NFC", self.binding_key.strip()) if isinstance(self.binding_key, str) else ""
        if (
            not key
            or len(key.encode("utf-8")) > _MAX_BINDING_KEY_BYTES
            or any(unicodedata.category(character).startswith("C") for character in key)
        ):
            raise ValueError("binding_key must be a bounded canonical string without control characters")
        if not callable(self.async_handler):
            raise TypeError("async_handler must be callable")
        if self.concurrency_mode not in {"parallel", "exclusive"}:
            raise ValueError("concurrency_mode must be 'parallel' or 'exclusive'")
        if not isinstance(self.input_schema, Mapping) or not isinstance(self.output_schema, Mapping):
            raise TypeError("binding input_schema and output_schema must be JSON Schema mappings")
        validate_code_execution_json_schema_definition(
            self.input_schema,
            field_name="input_schema",
        )
        validate_code_execution_json_schema_definition(
            self.output_schema,
            field_name="output_schema",
        )
        frozen_input = _freeze_json_value(self.input_schema)
        frozen_output = _freeze_json_value(self.output_schema)
        if len(code_execution_json_bytes(_json_value(frozen_input))) > _MAX_BINDING_SCHEMA_BYTES:
            raise ValueError("binding input_schema exceeds the size limit")
        if len(code_execution_json_bytes(_json_value(frozen_output))) > _MAX_BINDING_SCHEMA_BYTES:
            raise ValueError("binding output_schema exceeds the size limit")
        object.__setattr__(self, "binding_key", key)
        object.__setattr__(self, "input_schema", frozen_input)
        object.__setattr__(self, "output_schema", frozen_output)


@dataclass(frozen=True)
class CodeExecutionBindingLimits:
    """Runaway-safety limits for one binding-capable code execution.

    ``max_program_bytes`` is provider-owned and bounds the complete immutable
    bundle file set (wrapper, SDK and program), not only a model-authored
    function body. Semantic callers may apply a separate, smaller body limit
    before building the bundle.
    """

    max_program_bytes: int = 1024 * 1024
    max_frame_bytes: int = 1024 * 1024
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024
    max_calls: int = 64
    max_parallel_calls: int = 10
    max_protocol_frames: int = 96
    max_log_bytes: int = 20_000
    max_log_lines: int = 512
    call_timeout_seconds: float = 30.0
    frame_timeout_seconds: float = 5.0
    drain_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        integer_bounds = {
            "max_program_bytes": (
                self.max_program_bytes,
                _MAX_BINDING_PROGRAM_BYTES,
            ),
            "max_frame_bytes": (self.max_frame_bytes, _MAX_BINDING_FRAME_BYTES),
            "max_request_bytes": (
                self.max_request_bytes,
                _MAX_BINDING_VALUE_BYTES,
            ),
            "max_response_bytes": (
                self.max_response_bytes,
                _MAX_BINDING_VALUE_BYTES,
            ),
            "max_total_bytes": (self.max_total_bytes, _MAX_BINDING_TOTAL_BYTES),
            "max_calls": (self.max_calls, _MAX_BINDING_CALLS),
            "max_parallel_calls": (
                self.max_parallel_calls,
                _MAX_BINDING_PARALLEL_CALLS,
            ),
            "max_protocol_frames": (
                self.max_protocol_frames,
                _MAX_BINDING_FRAMES,
            ),
            "max_log_bytes": (self.max_log_bytes, _MAX_BINDING_VALUE_BYTES),
            "max_log_lines": (self.max_log_lines, _MAX_BINDING_LOG_LINES),
        }
        for name, (value, upper) in integer_bounds.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > upper:
                raise ValueError(f"{name} must be between 1 and {upper}")
        if self.max_request_bytes > self.max_frame_bytes:
            raise ValueError("max_request_bytes must not exceed max_frame_bytes")
        if self.max_response_bytes > self.max_frame_bytes:
            raise ValueError("max_response_bytes must not exceed max_frame_bytes")
        if self.max_protocol_frames < self.max_calls + 1:
            raise ValueError("max_protocol_frames must allow max_calls plus program completion")
        for name, value in (
            ("call_timeout_seconds", self.call_timeout_seconds),
            ("frame_timeout_seconds", self.frame_timeout_seconds),
            ("drain_timeout_seconds", self.drain_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < float(value) <= _MAX_BINDING_SECONDS
            ):
                raise ValueError(f"{name} must be greater than zero and at most {_MAX_BINDING_SECONDS}")


def extract_code_toolchain_version(value: str) -> str:
    """Return the first numeric dotted version from observed tool output."""

    match = re.search(r"(?<![0-9])([0-9]+(?:\.[0-9]+)+|[0-9]+)(?![0-9])", str(value))
    return match.group(1) if match is not None else ""


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _normalize_relative_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} path must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field_name} path must not be empty")
    if len(normalized.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError(f"{field_name} path exceeds the size limit")
    if "\\" in normalized:
        raise ValueError(f"{field_name} path must use POSIX separators")
    path = PurePosixPath(normalized)
    parts = path.parts
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} path must be normalized and relative")
    if not parts or parts[0].casefold() == ".agently":
        raise ValueError(f"{field_name} path must not address private .agently data")
    return path.as_posix()


def _normalize_expected_outputs(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("expected_outputs must be a bounded sequence of paths")
    if len(values) > _MAX_EXPECTED_OUTPUTS:
        raise ValueError(f"expected outputs must contain no more than {_MAX_EXPECTED_OUTPUTS} entries")
    normalized: list[str] = []
    folded_paths: set[str] = set()
    for value in values:
        path = _normalize_relative_path(value, field_name="expected output")
        if PurePosixPath(path).parts[0] != "output":
            raise ValueError("expected output paths must be located under output/")
        folded = path.casefold()
        if folded in folded_paths:
            raise ValueError(f"duplicate or case-collision expected output: {path!r}")
        folded_paths.add(folded)
        normalized.append(path)
    return tuple(normalized)


def resolve_code_execution_workspace_uri(
    value: str,
    *,
    roots: Mapping[str, str],
) -> str:
    if not isinstance(value, str):
        raise TypeError("code execution Workspace URI value must be a string")
    if not value.startswith(_WORKSPACE_URI_PREFIX):
        return value
    raw_path = value.removeprefix(_WORKSPACE_URI_PREFIX)
    logical_path = PurePosixPath(raw_path)
    if (
        logical_path.is_absolute()
        or len(logical_path.parts) < 1
        or any(part in {"", ".", ".."} for part in logical_path.parts)
    ):
        raise ValueError("code execution Workspace URI path must be normalized and relative")
    role = logical_path.parts[0]
    if role not in _WORKSPACE_ROLES or role not in roots:
        raise ValueError(f"code execution Workspace URI role has no provider root: {role!r}")
    resolved = Path(str(roots[role]))
    for part in logical_path.parts[1:]:
        resolved = resolved / part
    return str(resolved)


def _freeze_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json_value(item) for item in value)
    raise TypeError("provenance values must be JSON-compatible")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class CodeExecutionFile:
    path: str
    content: bytes
    role: CodeExecutionFileRole = "source"
    sha256: str = ""

    def __post_init__(self) -> None:
        path = _normalize_relative_path(self.path, field_name="file")
        if self.role not in {"source", "dependency", "input"}:
            raise ValueError(f"unsupported file role: {self.role!r}")
        if not isinstance(self.content, bytes):
            raise TypeError("file content must be bytes")
        if len(self.content) > _MAX_FILE_BYTES:
            raise ValueError("file content exceeds the per-file size limit")
        digest = _digest(self.content)
        if self.sha256 and (not _SHA256_PATTERN.fullmatch(self.sha256) or self.sha256 != digest):
            raise ValueError("file sha256 does not match content")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True)
class CodeExecutionRequest:
    language: str
    source_code: bytes | None
    files: tuple[CodeExecutionFile, ...]
    entrypoint: str | None
    args: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    provenance: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        language: str,
        source_code: str | bytes | None = None,
        files: Mapping[str, str | bytes] | None = None,
        entrypoint: str | None = None,
        args: Sequence[str] = (),
        expected_outputs: Sequence[str] = (),
        provenance: Mapping[str, Any] | None = None,
    ) -> "CodeExecutionRequest":
        canonical_language = language.strip().casefold() if isinstance(language, str) else ""
        if not canonical_language:
            raise ValueError("language is required")
        if source_code is not None and not isinstance(source_code, (str, bytes)):
            raise TypeError("source_code must be text or bytes")
        encoded_source = source_code.encode("utf-8") if isinstance(source_code, str) else source_code
        request_files: list[CodeExecutionFile] = []
        for path, content in dict(files or {}).items():
            if not isinstance(content, (str, bytes)):
                raise TypeError("request file values must be text or bytes")
            data = content.encode("utf-8") if isinstance(content, str) else content
            if b"\x00" in data:
                raise ValueError("request files must not contain unsupported NUL bytes")
            request_files.append(CodeExecutionFile(path=str(path), content=data, role="source"))
        if encoded_source is None and not request_files:
            raise ValueError("source_code or files are required")
        if encoded_source is not None and b"\x00" in encoded_source:
            raise ValueError("source_code must not contain unsupported NUL bytes")
        if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
            raise TypeError("args must be a bounded sequence of strings")
        frozen_args = tuple(args)
        if len(frozen_args) > _MAX_ARGV:
            raise ValueError(f"args must contain no more than {_MAX_ARGV} entries")
        for argument in frozen_args:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError("args entries must be strings without NUL bytes")
            if len(argument.encode("utf-8")) > _MAX_ARG_BYTES:
                raise ValueError("args entry exceeds the size limit")
        canonical_entrypoint = (
            _normalize_relative_path(entrypoint, field_name="entrypoint") if entrypoint is not None else None
        )
        frozen_outputs = _normalize_expected_outputs(expected_outputs)
        return cls(
            language=canonical_language,
            source_code=encoded_source,
            files=tuple(request_files),
            entrypoint=canonical_entrypoint,
            args=frozen_args,
            expected_outputs=frozen_outputs,
            provenance=_freeze_json_value(provenance or {}),
        )


@dataclass(frozen=True)
class CodeExecutionStep:
    argv: tuple[str, ...]
    role: CodeExecutionStepRole
    cwd: str = "source"
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)) or not isinstance(self.argv, Sequence):
            raise TypeError("argv must be a bounded sequence of arguments, not a shell string")
        argv = tuple(self.argv)
        if not argv or len(argv) > _MAX_ARGV:
            raise ValueError(f"argv must contain between 1 and {_MAX_ARGV} arguments")
        for argument in argv:
            if not isinstance(argument, str) or not argument or "\x00" in argument:
                raise ValueError("argv entries must be non-empty strings without NUL bytes")
            if len(argument.encode("utf-8")) > _MAX_ARG_BYTES:
                raise ValueError("argv entry exceeds the size limit")
        if self.role not in {"build", "run"}:
            raise ValueError(f"unsupported execution step role: {self.role!r}")
        cwd = _normalize_relative_path(self.cwd, field_name="cwd")
        if len(self.env) > _MAX_ENV:
            raise ValueError("execution environment exceeds the entry limit")
        env: dict[str, str] = {}
        for key, value in self.env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("execution environment keys and values must be strings")
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("execution environment contains an invalid key or value")
            env[key] = value
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "env", MappingProxyType(dict(sorted(env.items()))))


@dataclass(frozen=True)
class CodeExecutionToolchainRequirement:
    tool: str
    minimum_version: str | None = None
    exact_version: str | None = None

    def __post_init__(self) -> None:
        tool = self.tool.strip().casefold() if isinstance(self.tool, str) else ""
        if not tool or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}", tool):
            raise ValueError("toolchain tool must be a canonical identifier")
        if self.minimum_version is not None and self.exact_version is not None:
            raise ValueError("toolchain requirement cannot set both minimum and exact version")
        object.__setattr__(self, "tool", tool)


@dataclass(frozen=True)
class CodeExecutionBundle:
    bundle_id: str
    language: str
    files: tuple[CodeExecutionFile, ...]
    entrypoint: str
    build_steps: tuple[CodeExecutionStep, ...]
    run_step: CodeExecutionStep
    expected_outputs: tuple[str, ...]
    toolchains: tuple[CodeExecutionToolchainRequirement, ...]
    provenance: Mapping[str, Any]
    bundle_digest: str

    @classmethod
    def create(
        cls,
        *,
        language: str,
        files: Sequence[CodeExecutionFile],
        entrypoint: str,
        build_steps: Sequence[CodeExecutionStep],
        run_step: CodeExecutionStep,
        expected_outputs: Sequence[str] = (),
        toolchains: Sequence[CodeExecutionToolchainRequirement] = (),
        provenance: Mapping[str, Any] | None = None,
    ) -> "CodeExecutionBundle":
        canonical_language = language.strip().casefold() if isinstance(language, str) else ""
        if not canonical_language or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}", canonical_language):
            raise ValueError("language must be a canonical identifier")
        frozen_files = tuple(files)
        if not frozen_files or len(frozen_files) > _MAX_FILES:
            raise ValueError(f"bundle files must contain between 1 and {_MAX_FILES} entries")
        if any(not isinstance(item, CodeExecutionFile) for item in frozen_files):
            raise TypeError("bundle files must contain CodeExecutionFile values")
        if sum(len(item.content) for item in frozen_files) > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle files exceed the total size limit")
        folded_paths: set[str] = set()
        for item in frozen_files:
            folded = item.path.casefold()
            if folded in folded_paths:
                raise ValueError(f"duplicate or case-collision file path: {item.path!r}")
            folded_paths.add(folded)

        canonical_entrypoint = _normalize_relative_path(entrypoint, field_name="entrypoint")
        if canonical_entrypoint.casefold() not in folded_paths:
            raise ValueError("entrypoint path must identify one bundle file")

        frozen_build_steps = tuple(build_steps)
        if any(step.role != "build" for step in frozen_build_steps):
            raise ValueError("build_steps may contain only build-role steps")
        if run_step.role != "run":
            raise ValueError("run_step must have the run role")

        frozen_outputs = _normalize_expected_outputs(expected_outputs)

        frozen_toolchains = tuple(toolchains)
        if any(not isinstance(item, CodeExecutionToolchainRequirement) for item in frozen_toolchains):
            raise TypeError("toolchains must contain CodeExecutionToolchainRequirement values")
        frozen_provenance = _freeze_json_value(provenance or {})

        canonical = {
            "language": canonical_language,
            "files": [
                {
                    "path": item.path,
                    "role": item.role,
                    "sha256": item.sha256,
                    "bytes": len(item.content),
                }
                for item in frozen_files
            ],
            "entrypoint": canonical_entrypoint,
            "build_steps": [
                {
                    "argv": list(step.argv),
                    "cwd": step.cwd,
                    "env": dict(step.env),
                    "role": step.role,
                }
                for step in frozen_build_steps
            ],
            "run_step": {
                "argv": list(run_step.argv),
                "cwd": run_step.cwd,
                "env": dict(run_step.env),
                "role": run_step.role,
            },
            "expected_outputs": list(frozen_outputs),
            "toolchains": [
                {
                    "tool": item.tool,
                    "minimum_version": item.minimum_version,
                    "exact_version": item.exact_version,
                }
                for item in frozen_toolchains
            ],
            "provenance": _json_value(frozen_provenance),
        }
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        bundle_digest = _digest(serialized)
        return cls(
            bundle_id=f"bundle-{bundle_digest.removeprefix('sha256:')[:24]}",
            language=canonical_language,
            files=frozen_files,
            entrypoint=canonical_entrypoint,
            build_steps=frozen_build_steps,
            run_step=run_step,
            expected_outputs=frozen_outputs,
            toolchains=frozen_toolchains,
            provenance=frozen_provenance,
            bundle_digest=bundle_digest,
        )


__all__ = [
    "CodeExecutionBinding",
    "CodeExecutionBindingCallRecord",
    "CodeExecutionBindingError",
    "CodeExecutionBindingLimits",
    "CodeExecutionBindingStatus",
    "CodeExecutionBindingSummary",
    "CodeExecutionSchemaValidationError",
    "CodeExecutionBundle",
    "CodeExecutionFile",
    "CodeExecutionFileRole",
    "CodeExecutionProviderCapability",
    "CodeExecutionRequest",
    "CodeExecutionResult",
    "CodeExecutionStep",
    "CodeExecutionStepRole",
    "CodeExecutionToolchainRequirement",
    "code_execution_json_bytes",
    "normalize_code_execution_json_value",
    "resolve_code_execution_workspace_uri",
    "validate_code_execution_json_schema_definition",
    "validate_code_execution_json_schema",
]

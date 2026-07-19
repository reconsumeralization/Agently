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

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast


ContextRole = Literal[
    "instruction",
    "information",
    "example",
    "state",
    "artifact",
    "capability",
    "index",
]
ContextCompleteness = Literal[
    "complete",
    "truncated",
    "ref_only",
    "empty",
    "failed",
    "lossy",
]
ContextPhase = Literal[
    "direct",
    "planning",
    "execution",
    "card",
    "repair",
    "verification",
    "synthesis",
]

_CONTEXT_ROLES = frozenset(
    {
        "instruction",
        "information",
        "example",
        "state",
        "artifact",
        "capability",
        "index",
    }
)
_CONTEXT_COMPLETENESS = frozenset(
    {"complete", "truncated", "ref_only", "empty", "failed", "lossy"}
)


def _require_text(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty.")
    return normalized


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_thaw_value(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], _freeze_value(dict(value or {})))


def _freeze_str_mapping(value: Mapping[str, str] | None) -> Mapping[str, str]:
    frozen = {str(key): str(item) for key, item in dict(value or {}).items()}
    return MappingProxyType(frozen)


def _freeze_source_coverage(
    value: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Mapping[str, Any]]:
    frozen: dict[str, Mapping[str, Any]] = {}
    required_fields = {
        "scope",
        "returned_candidates",
        "exhaustive",
        "continuation_available",
    }
    for raw_binding_id, raw_record in dict(value or {}).items():
        binding_id = _require_text(str(raw_binding_id), "source_coverage binding_id")
        if not isinstance(raw_record, Mapping):
            raise ValueError("source_coverage records must be mappings.")
        record = dict(raw_record)
        unknown = set(record) - required_fields
        missing = required_fields - set(record)
        if unknown or missing:
            raise ValueError(
                "source_coverage records require exactly scope, returned_candidates, "
                "exhaustive, and continuation_available."
            )
        if not isinstance(record["scope"], Mapping):
            raise ValueError("source_coverage scope must be a mapping.")
        returned_candidates = record["returned_candidates"]
        if (
            not isinstance(returned_candidates, int)
            or isinstance(returned_candidates, bool)
            or returned_candidates < 0
        ):
            raise ValueError("source_coverage returned_candidates must be non-negative.")
        exhaustive = record["exhaustive"]
        continuation_available = record["continuation_available"]
        if not isinstance(exhaustive, bool):
            raise ValueError("source_coverage exhaustive must be boolean.")
        if not isinstance(continuation_available, bool):
            raise ValueError("source_coverage continuation_available must be boolean.")
        if exhaustive and continuation_available:
            raise ValueError(
                "source_coverage cannot be exhaustive with continuation available."
            )
        frozen[binding_id] = _freeze_mapping(
            {
                "scope": dict(record["scope"]),
                "returned_candidates": returned_candidates,
                "exhaustive": exhaustive,
                "continuation_available": continuation_available,
            }
        )
    return MappingProxyType(frozen)


def _validate_role(role: str) -> ContextRole:
    if role not in _CONTEXT_ROLES:
        raise ValueError(f"Unknown Context role: {role!r}.")
    return cast(ContextRole, role)


def _validate_completeness(value: str) -> ContextCompleteness:
    if value not in _CONTEXT_COMPLETENESS:
        raise ValueError(f"Unknown Context completeness: {value!r}.")
    return cast(ContextCompleteness, value)


@dataclass(frozen=True)
class ContextBudget:
    max_chars: int = 12000
    max_blocks: int = 64
    max_block_chars: int = 6000

    def __post_init__(self) -> None:
        for name in ("max_chars", "max_blocks", "max_block_chars"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True)
class ContextConsumer:
    consumer_id: str
    model: str | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "consumer_id", _require_text(self.consumer_id, "consumer_id"))
        if self.model is not None:
            object.__setattr__(self, "model", _require_text(self.model, "model"))
        object.__setattr__(self, "capabilities", _freeze_mapping(self.capabilities))


@dataclass(frozen=True)
class ContextReadIntent:
    query: str
    explicit_refs: tuple[str, ...] = ()
    roles: tuple[ContextRole, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _require_text(self.query, "query"))
        object.__setattr__(
            self,
            "explicit_refs",
            tuple(_require_text(item, "explicit_ref") for item in self.explicit_refs),
        )
        object.__setattr__(
            self,
            "roles",
            tuple(_validate_role(str(item)) for item in self.roles),
        )
        object.__setattr__(self, "filters", _freeze_mapping(self.filters))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True)
class ContextCandidate:
    block_key: str
    source_id: str
    source_revision: str
    source_ref: str
    binding_id: str
    role: ContextRole
    summary: str
    estimated_chars: int
    required: bool = False
    priority: int = 0
    completeness: ContextCompleteness = "complete"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("block_key", "source_id", "source_revision", "source_ref", "binding_id"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "role", _validate_role(str(self.role)))
        object.__setattr__(self, "completeness", _validate_completeness(str(self.completeness)))
        if self.estimated_chars < 0:
            raise ValueError("estimated_chars cannot be negative.")
        object.__setattr__(self, "summary", str(self.summary or ""))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_key": self.block_key,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_ref": self.source_ref,
            "binding_id": self.binding_id,
            "role": self.role,
            "summary": self.summary,
            "estimated_chars": self.estimated_chars,
            "required": self.required,
            "priority": self.priority,
            "completeness": self.completeness,
            "metadata": _thaw_value(self.metadata),
        }


@dataclass(frozen=True)
class ContextSourceDescriptor:
    """Source-owned structural projection used to build a Context index."""

    descriptor_key: str
    source_id: str
    source_revision: str
    source_ref: str
    role: ContextRole
    title: str
    summary: str
    estimated_chars: int
    parent_key: str | None = None
    required: bool = False
    priority: int = 0
    index_text: str = ""
    content_digest: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "descriptor_key",
            "source_id",
            "source_revision",
            "source_ref",
            "title",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "role", _validate_role(str(self.role)))
        if (
            not isinstance(self.estimated_chars, int)
            or isinstance(self.estimated_chars, bool)
            or self.estimated_chars < 0
        ):
            raise ValueError("estimated_chars must be a non-negative integer.")
        if self.parent_key is not None:
            object.__setattr__(self, "parent_key", _require_text(self.parent_key, "parent_key"))
        if self.content_digest is not None:
            object.__setattr__(
                self,
                "content_digest",
                _require_text(self.content_digest, "content_digest"),
            )
        object.__setattr__(self, "summary", str(self.summary or ""))
        object.__setattr__(self, "index_text", str(self.index_text or ""))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True)
class ContextSourceDescriptorPage:
    """Revision-pinned descriptor enumeration page."""

    source_id: str
    source_revision: str
    descriptors: tuple[ContextSourceDescriptor, ...] = ()
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _require_text(self.source_id, "source_id"))
        object.__setattr__(
            self,
            "source_revision",
            _require_text(self.source_revision, "source_revision"),
        )
        descriptors = tuple(self.descriptors)
        for descriptor in descriptors:
            if not isinstance(descriptor, ContextSourceDescriptor):
                raise ValueError(
                    "descriptors must contain ContextSourceDescriptor values."
                )
            if descriptor.source_id != self.source_id:
                raise ValueError("descriptor source_id does not match its page.")
            if descriptor.source_revision != self.source_revision:
                raise ValueError("descriptor source_revision does not match its page.")
        object.__setattr__(self, "descriptors", descriptors)
        if self.next_cursor is not None:
            cursor = str(self.next_cursor)
            if not cursor.strip():
                raise ValueError("next_cursor must be a non-empty string when provided.")
            if len(cursor) > 4096:
                raise ValueError("next_cursor cannot exceed 4096 characters.")
            object.__setattr__(self, "next_cursor", cursor)


@dataclass(frozen=True)
class ContextSourceChange:
    """One trusted descriptor mutation in a source-provided change feed."""

    operation: Literal["upsert", "remove"]
    descriptor_key: str
    descriptor: ContextSourceDescriptor | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"upsert", "remove"}:
            raise ValueError("operation must be 'upsert' or 'remove'.")
        object.__setattr__(
            self,
            "descriptor_key",
            _require_text(self.descriptor_key, "descriptor_key"),
        )
        if self.operation == "upsert":
            if not isinstance(self.descriptor, ContextSourceDescriptor):
                raise ValueError("upsert changes require a descriptor.")
            if self.descriptor.descriptor_key != self.descriptor_key:
                raise ValueError("change descriptor_key does not match its descriptor.")
        elif self.descriptor is not None:
            raise ValueError("remove changes cannot include a descriptor.")


@dataclass(frozen=True)
class ContextSourceChangeSet:
    """Validated descriptor delta between two immutable source revisions."""

    source_id: str
    from_revision: str
    to_revision: str
    changes: tuple[ContextSourceChange, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source_id", "from_revision", "to_revision"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        if self.from_revision == self.to_revision:
            raise ValueError("from_revision and to_revision must differ.")
        changes = tuple(self.changes)
        for change in changes:
            if not isinstance(change, ContextSourceChange):
                raise ValueError("changes must contain ContextSourceChange values.")
            descriptor = change.descriptor
            if descriptor is not None:
                if descriptor.source_id != self.source_id:
                    raise ValueError("changed descriptor source_id does not match change set.")
                if descriptor.source_revision != self.to_revision:
                    raise ValueError(
                        "changed descriptor source_revision must equal to_revision."
                    )
        keys = [change.descriptor_key for change in changes]
        if len(keys) != len(set(keys)):
            raise ValueError("changes cannot repeat descriptor_key values.")
        object.__setattr__(self, "changes", changes)


@dataclass(frozen=True)
class ContextSourceRead:
    """Bounded exact readback from canonical source truth."""

    source_id: str
    source_revision: str
    source_ref: str
    content: Any
    completeness: ContextCompleteness
    next_range_start: int | None = None
    content_digest: str | None = None
    refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("source_id", "source_revision", "source_ref"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "content", _freeze_value(self.content))
        object.__setattr__(
            self,
            "completeness",
            _validate_completeness(str(self.completeness)),
        )
        if self.next_range_start is not None and (
            not isinstance(self.next_range_start, int)
            or isinstance(self.next_range_start, bool)
            or self.next_range_start < 0
        ):
            raise ValueError("next_range_start must be a non-negative integer.")
        if self.content_digest is not None:
            object.__setattr__(
                self,
                "content_digest",
                _require_text(self.content_digest, "content_digest"),
            )
        refs = tuple(_require_text(item, "ref") for item in self.refs)
        if len(refs) != len(set(refs)):
            raise ValueError("refs cannot contain duplicates.")
        object.__setattr__(self, "refs", refs)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True)
class ContextBlock:
    block_id: str
    block_key: str
    source_id: str
    source_revision: str
    source_ref: str
    binding_id: str
    role: ContextRole
    content: Any
    completeness: ContextCompleteness
    content_chars: int
    required: bool = False
    refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "block_id",
            "block_key",
            "source_id",
            "source_revision",
            "source_ref",
            "binding_id",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "role", _validate_role(str(self.role)))
        object.__setattr__(self, "completeness", _validate_completeness(str(self.completeness)))
        if self.content_chars < 0:
            raise ValueError("content_chars cannot be negative.")
        object.__setattr__(self, "content", _freeze_value(self.content))
        object.__setattr__(
            self,
            "refs",
            tuple(_require_text(item, "ref") for item in self.refs),
        )
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "block_key": self.block_key,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_ref": self.source_ref,
            "binding_id": self.binding_id,
            "role": self.role,
            "content": _thaw_value(self.content),
            "completeness": self.completeness,
            "content_chars": self.content_chars,
            "required": self.required,
            "refs": list(self.refs),
            "metadata": _thaw_value(self.metadata),
        }


@dataclass(frozen=True)
class ContextOmission:
    block_key: str
    reason: str
    required: bool = False
    source_ref: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_key", _require_text(self.block_key, "block_key"))
        object.__setattr__(self, "reason", _require_text(self.reason, "reason"))
        if self.source_ref is not None:
            object.__setattr__(self, "source_ref", _require_text(self.source_ref, "source_ref"))
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_key": self.block_key,
            "reason": self.reason,
            "required": self.required,
            "source_ref": self.source_ref,
            "details": _thaw_value(self.details),
        }


@dataclass(frozen=True)
class ContextDiagnostic:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_text(self.code, "code"))
        object.__setattr__(self, "message", _require_text(self.message, "message"))
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": _thaw_value(self.details),
        }


@dataclass(frozen=True)
class ContextSourceBindingSnapshot:
    binding_id: str
    source_id: str
    source_kind: str
    source_revision: str
    required: bool = False
    priority: int = 0
    scope: str = "task"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "source_id",
            "source_kind",
            "source_revision",
            "scope",
        ):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_revision": self.source_revision,
            "required": self.required,
            "priority": self.priority,
            "scope": self.scope,
            "metadata": _thaw_value(self.metadata),
        }


@dataclass(frozen=True)
class TaskContextEntrySnapshot:
    entry_id: str
    role: ContextRole
    content: Any
    required: bool = False
    source_ref: str | None = None
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", _require_text(self.entry_id, "entry_id"))
        object.__setattr__(self, "role", _validate_role(str(self.role)))
        object.__setattr__(self, "content", _freeze_value(self.content))
        if self.source_ref is not None:
            object.__setattr__(self, "source_ref", _require_text(self.source_ref, "source_ref"))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "role": self.role,
            "content": _thaw_value(self.content),
            "required": self.required,
            "source_ref": self.source_ref,
            "priority": self.priority,
            "metadata": _thaw_value(self.metadata),
        }


@dataclass(frozen=True)
class TaskContextSnapshot:
    context_id: str
    task_id: str
    revision: int
    bindings: tuple[ContextSourceBindingSnapshot, ...] = ()
    entries: tuple[TaskContextEntrySnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_id", _require_text(self.context_id, "context_id"))
        object.__setattr__(self, "task_id", _require_text(self.task_id, "task_id"))
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer.")
        object.__setattr__(self, "bindings", tuple(self.bindings))
        object.__setattr__(self, "entries", tuple(self.entries))

    @property
    def source_revisions(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                binding.binding_id: binding.source_revision
                for binding in self.bindings
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "task_id": self.task_id,
            "revision": self.revision,
            "bindings": [item.to_dict() for item in self.bindings],
            "entries": [item.to_dict() for item in self.entries],
        }


@dataclass(frozen=True)
class ContextPackage:
    package_id: str
    task_context_id: str
    context_revision: int
    consumer_id: str
    phase: str
    source_revisions: Mapping[str, str]
    source_coverage: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    blocks: tuple[ContextBlock, ...] = ()
    omissions: tuple[ContextOmission, ...] = ()
    diagnostics: tuple[ContextDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        for name in ("package_id", "task_context_id", "consumer_id", "phase"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        if (
            not isinstance(self.context_revision, int)
            or isinstance(self.context_revision, bool)
            or self.context_revision < 0
        ):
            raise ValueError("context_revision must be a non-negative integer.")
        object.__setattr__(self, "source_revisions", _freeze_str_mapping(self.source_revisions))
        object.__setattr__(
            self,
            "source_coverage",
            _freeze_source_coverage(self.source_coverage),
        )
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "omissions", tuple(self.omissions))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @property
    def used_chars(self) -> int:
        return sum(block.content_chars for block in self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "task_context_id": self.task_context_id,
            "context_revision": self.context_revision,
            "consumer_id": self.consumer_id,
            "phase": self.phase,
            "source_revisions": dict(self.source_revisions),
            "source_coverage": _thaw_value(self.source_coverage),
            "blocks": [block.to_dict() for block in self.blocks],
            "omissions": [omission.to_dict() for omission in self.omissions],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "used_chars": self.used_chars,
        }


@dataclass(frozen=True)
class ContextConsumption:
    consumption_id: str
    package_id: str
    request_id: str
    consumer_id: str
    phase: str
    block_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("consumption_id", "package_id", "request_id", "consumer_id", "phase"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        block_ids = tuple(_require_text(item, "block_id") for item in self.block_ids)
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block_ids cannot contain duplicates.")
        object.__setattr__(self, "block_ids", block_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumption_id": self.consumption_id,
            "package_id": self.package_id,
            "request_id": self.request_id,
            "consumer_id": self.consumer_id,
            "phase": self.phase,
            "block_ids": list(self.block_ids),
        }


__all__ = [
    "ContextBlock",
    "ContextBudget",
    "ContextCandidate",
    "ContextCompleteness",
    "ContextConsumer",
    "ContextConsumption",
    "ContextDiagnostic",
    "ContextOmission",
    "ContextPackage",
    "ContextPhase",
    "ContextReadIntent",
    "ContextRole",
    "ContextSourceBindingSnapshot",
    "ContextSourceChange",
    "ContextSourceChangeSet",
    "ContextSourceDescriptor",
    "ContextSourceDescriptorPage",
    "ContextSourceRead",
    "TaskContextEntrySnapshot",
    "TaskContextSnapshot",
]

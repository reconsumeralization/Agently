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

from collections.abc import Mapping, Sequence
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
    source_revision: str
    required: bool = False
    priority: int = 0
    scope: str = "task"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("binding_id", "source_id", "source_revision", "scope"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "source_id": self.source_id,
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
    "TaskContextEntrySnapshot",
    "TaskContextSnapshot",
]

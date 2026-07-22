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
from typing import Any, Literal


SkillResourceKind = Literal[
    "instruction",
    "reference",
    "example",
    "asset",
    "script",
    "resource",
]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_thaw(item) for item in value]
    return value


def _required(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty.")
    return normalized


@dataclass(frozen=True)
class SkillResourceDescriptor:
    path: str
    kind: SkillResourceKind
    sha256: str
    size: int
    media_type: str | None = None
    executable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("path", "kind", "sha256"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.size < 0:
            raise ValueError("size cannot be negative.")
        object.__setattr__(self, "metadata", _freeze(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "executable": self.executable,
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True)
class SkillPackageRevision:
    skill_id: str
    canonical_ref: str
    revision: str
    revision_ref: str
    name: str
    description: str
    version: str
    scope: str
    trust: str
    source: str
    installed_path: str
    instruction_body: str
    frontmatter: Mapping[str, Any]
    resources: tuple[SkillResourceDescriptor, ...]
    source_provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "skill_id",
            "canonical_ref",
            "revision",
            "revision_ref",
            "name",
            "version",
            "scope",
            "trust",
            "source",
            "installed_path",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "instruction_body", str(self.instruction_body or ""))
        object.__setattr__(self, "frontmatter", _freeze(dict(self.frontmatter)))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(
            self,
            "source_provenance",
            _freeze(dict(self.source_provenance)),
        )

    def resource(self, path: str) -> SkillResourceDescriptor:
        normalized = str(path)
        for resource in self.resources:
            if resource.path == normalized:
                return resource
        raise KeyError(normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "canonical_ref": self.canonical_ref,
            "revision": self.revision,
            "revision_ref": self.revision_ref,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "scope": self.scope,
            "trust": self.trust,
            "source": self.source,
            "installed_path": self.installed_path,
            "instruction_body": self.instruction_body,
            "frontmatter": _thaw(self.frontmatter),
            "resources": [item.to_dict() for item in self.resources],
            "source_provenance": _thaw(self.source_provenance),
        }


@dataclass(frozen=True)
class SkillPackRevision:
    """Library-owned grouping of exact installed Skill revisions."""

    skill_pack_id: str
    name: str
    source: str
    trust: str
    revision_refs: tuple[str, ...]
    installed_skills: tuple[str, ...]
    failed_skills: tuple[Mapping[str, Any], ...] = ()
    source_provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("skill_pack_id", "name", "source", "trust"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "revision_refs", tuple(self.revision_refs))
        object.__setattr__(self, "installed_skills", tuple(self.installed_skills))
        object.__setattr__(
            self,
            "failed_skills",
            tuple(_freeze(dict(item)) for item in self.failed_skills),
        )
        object.__setattr__(
            self,
            "source_provenance",
            _freeze(dict(self.source_provenance)),
        )

    @property
    def status(self) -> str:
        if self.installed_skills and not self.failed_skills:
            return "success"
        if self.installed_skills:
            return "partial"
        return "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_pack_id": self.skill_pack_id,
            "skills_pack_id": self.skill_pack_id,
            "name": self.name,
            "source": self.source,
            "source_type": str(self.source_provenance.get("source_type") or "local"),
            "source_provenance": _thaw(self.source_provenance),
            "trust_level": self.trust,
            "revision_refs": list(self.revision_refs),
            "installed_skills": list(self.installed_skills),
            "failed_skills": [_thaw(item) for item in self.failed_skills],
            "status": self.status,
        }


@dataclass(frozen=True)
class SkillResourceRead:
    revision_ref: str
    path: str
    data: bytes
    total_bytes: int
    offset: int
    truncated: bool
    sha256: str
    media_type: str | None

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


__all__ = [
    "SkillPackageRevision",
    "SkillPackRevision",
    "SkillResourceDescriptor",
    "SkillResourceKind",
    "SkillResourceRead",
]

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

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit


SkillMode = Literal["model_decision", "required"]
SkillRuntimeStreamItem = dict[str, Any]
SkillRuntimeStreamHandler = Callable[[SkillRuntimeStreamItem], Awaitable[None] | None]


@dataclass(frozen=True)
class SkillScriptAuthorization:
    auto_allow: bool = False
    expected_outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "auto_allow", bool(self.auto_allow))
        object.__setattr__(
            self,
            "expected_outputs",
            tuple(str(item) for item in self.expected_outputs),
        )


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType({str(key): item for key, item in dict(value or {}).items()})


def _required_text(value: Any, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty.")
    return normalized


def _safe_skill_source_subpath(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().replace("\\", "/")
    if not normalized:
        return None
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] == ".agently"
    ):
        raise ValueError(f"Skill source subpath is unsafe: {value!r}.")
    return path.as_posix()


def redact_skill_source(value: str) -> str:
    """Return public provenance without URL credentials or query secrets."""

    source = str(value or "")
    parsed = urlsplit(source)
    if not parsed.scheme or not parsed.netloc:
        return source
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


@dataclass(frozen=True)
class SkillSourceRequest:
    source: str = field(repr=False)
    source_type: str = "auto"
    ref: str | None = None
    subpath: str | None = None
    update: bool = False
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _required_text(self.source, name="source"))
        object.__setattr__(
            self,
            "source_type",
            _required_text(self.source_type, name="source_type").lower(),
        )
        object.__setattr__(self, "ref", str(self.ref).strip() if self.ref else None)
        object.__setattr__(self, "subpath", _safe_skill_source_subpath(self.subpath))
        object.__setattr__(self, "update", bool(self.update))
        object.__setattr__(self, "options", _freeze_mapping(self.options))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": redact_skill_source(self.source),
            "source_type": self.source_type,
            "ref": self.ref,
            "subpath": self.subpath,
            "update": self.update,
        }


@dataclass(frozen=True)
class SkillSourceSnapshot:
    provider_id: str
    source_type: str
    requested_source: str = field(repr=False)
    requested_ref: str | None
    resolved_revision: str
    subpath: str | None
    materialized_path: str = field(repr=False)
    source_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _required_text(self.provider_id, name="provider_id"))
        source_type = _required_text(self.source_type, name="source_type").lower()
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(
            self,
            "requested_source",
            _required_text(self.requested_source, name="requested_source"),
        )
        object.__setattr__(
            self,
            "requested_ref",
            str(self.requested_ref).strip() if self.requested_ref else None,
        )
        resolved = _required_text(self.resolved_revision, name="resolved_revision")
        if source_type == "git" and re.fullmatch(r"[0-9a-fA-F]{40}", resolved) is None:
            raise ValueError("Git Skill source resolved_revision must be an exact 40-character commit.")
        object.__setattr__(self, "resolved_revision", resolved.lower() if source_type == "git" else resolved)
        object.__setattr__(self, "subpath", _safe_skill_source_subpath(self.subpath))
        object.__setattr__(
            self,
            "materialized_path",
            _required_text(self.materialized_path, name="materialized_path"),
        )
        digest = _required_text(self.source_digest, name="source_digest")
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) is None:
            raise ValueError("Skill source source_digest must be a sha256 digest.")
        object.__setattr__(self, "source_digest", digest.lower())
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "source_type": self.source_type,
            "requested_source": redact_skill_source(self.requested_source),
            "requested_ref": self.requested_ref,
            "resolved_revision": self.resolved_revision,
            "subpath": self.subpath,
            "source_digest": self.source_digest,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "SkillMode",
    "SkillRuntimeStreamHandler",
    "SkillRuntimeStreamItem",
    "SkillScriptAuthorization",
    "SkillSourceRequest",
    "SkillSourceSnapshot",
    "redact_skill_source",
]

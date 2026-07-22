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

import uuid
from dataclasses import dataclass
from typing import Literal

from .Package import SkillPackageRevision


SkillBindingMode = Literal["required", "model_decision"]


class SkillBindingError(ValueError):
    """Raised when a Skill revision cannot be bound into an active task."""


@dataclass(frozen=True)
class SkillBinding:
    binding_id: str
    task_id: str
    canonical_ref: str
    revision: str
    revision_ref: str
    mode: SkillBindingMode
    scope: str = "task"

    @classmethod
    def create(
        cls,
        package: SkillPackageRevision,
        *,
        task_id: str,
        mode: SkillBindingMode = "model_decision",
        scope: str = "task",
        binding_id: str | None = None,
    ) -> "SkillBinding":
        if package.trust != "trusted":
            raise SkillBindingError(
                f"Active Skill instruction binding requires a trusted revision: {package.revision_ref}."
            )
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise SkillBindingError("task_id cannot be empty.")
        if mode not in {"required", "model_decision"}:
            raise SkillBindingError("Skill binding mode must be 'required' or 'model_decision'.")
        normalized_scope = str(scope or "").strip()
        if not normalized_scope:
            raise SkillBindingError("Skill binding scope cannot be empty.")
        return cls(
            binding_id=str(binding_id or f"skill_binding:{uuid.uuid4().hex}"),
            task_id=normalized_task_id,
            canonical_ref=package.canonical_ref,
            revision=package.revision,
            revision_ref=package.revision_ref,
            mode=mode,
            scope=normalized_scope,
        )


__all__ = ["SkillBinding", "SkillBindingError", "SkillBindingMode"]

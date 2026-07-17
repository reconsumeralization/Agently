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
from collections.abc import Mapping, Sequence
from typing import Any

from agently.types.data import ContextBlock, ContextCandidate, ContextReadIntent

from .Binding import SkillBinding, SkillBindingError
from .Package import SkillPackageRevision, SkillResourceDescriptor
from .SkillLibrary import SkillLibrary


class SkillContextSource:
    """ContextSource adapter for exact trusted Skill revision bindings."""

    def __init__(
        self,
        library: SkillLibrary,
        *,
        bindings: Sequence[SkillBinding],
    ) -> None:
        if not bindings:
            raise SkillBindingError("SkillContextSource requires at least one binding.")
        task_ids = {binding.task_id for binding in bindings}
        if len(task_ids) != 1:
            raise SkillBindingError("SkillContextSource bindings must belong to one task_id.")
        self.library = library
        self.bindings = tuple(bindings)
        self.packages = tuple(library.resolve(binding.revision_ref) for binding in self.bindings)
        for binding, package in zip(self.bindings, self.packages):
            if binding.revision_ref != package.revision_ref:
                raise SkillBindingError(
                    f"Skill binding revision could not be resolved exactly: {binding.revision_ref}."
                )
            if package.trust != "trusted":
                raise SkillBindingError(
                    f"Active Skill instruction binding requires trust: {binding.revision_ref}."
                )
        task_id = next(iter(task_ids))
        self.source_id = f"skill-context:{task_id}"
        self.source_revision = "skill-context:" + "|".join(
            package.revision for package in self.packages
        )
        self._package_by_revision_ref = {
            package.revision_ref: package for package in self.packages
        }
        self._binding_by_revision_ref = {
            binding.revision_ref: binding for binding in self.bindings
        }

    @staticmethod
    def _source_ref(package: SkillPackageRevision, path: str) -> str:
        return f"{package.revision_ref}/{path}"

    @staticmethod
    def _block_id(package: SkillPackageRevision, path: str) -> str:
        digest = hashlib.sha256(f"{package.revision_ref}\0{path}".encode("utf-8")).hexdigest()
        return f"skill_context_block:{digest}"

    @staticmethod
    def _resource_role(resource: SkillResourceDescriptor) -> str:
        return {
            "reference": "information",
            "example": "example",
            "asset": "artifact",
            "script": "capability",
        }.get(resource.kind, "information")

    @staticmethod
    def _resource_summary(resource: SkillResourceDescriptor) -> str:
        return f"{resource.kind} resource {resource.path} ({resource.size} bytes)"

    @staticmethod
    def _domain_metadata(
        binding: SkillBinding,
        package: SkillPackageRevision,
        path: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "revision_ref": package.revision_ref,
            "resource_path": path,
            "skill_id": package.skill_id,
            "skill_binding_id": binding.binding_id,
            "skill_mode": binding.mode,
            **extra,
        }

    def _candidate(
        self,
        *,
        binding: SkillBinding,
        package: SkillPackageRevision,
        path: str,
        role: str,
        summary: str,
        estimated_chars: int,
        required: bool,
        completeness: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextCandidate:
        return ContextCandidate(
            block_key=f"skill-source:{binding.binding_id}:{path}",
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=self._source_ref(package, path),
            binding_id=binding.binding_id,
            role=role,  # type: ignore[arg-type]
            summary=summary,
            estimated_chars=estimated_chars,
            required=required,
            completeness=completeness,  # type: ignore[arg-type]
            metadata=self._domain_metadata(
                binding,
                package,
                path,
                **dict(metadata or {}),
            ),
        )

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ContextCandidate]:
        del intent, filters
        candidates: list[ContextCandidate] = []
        for binding, package in zip(self.bindings, self.packages):
            candidates.append(
                self._candidate(
                    binding=binding,
                    package=package,
                    path="SKILL.md",
                    role="instruction",
                    summary=package.description or package.name,
                    estimated_chars=len(package.instruction_body),
                    required=True,
                    completeness="complete",
                )
            )
            index_items = [
                {
                    "path": resource.path,
                    "kind": resource.kind,
                    "size": resource.size,
                    "sha256": resource.sha256,
                }
                for resource in package.resources
                if resource.path != "SKILL.md"
            ]
            if index_items:
                candidates.append(
                    self._candidate(
                        binding=binding,
                        package=package,
                        path="resource-index",
                        role="index",
                        summary=f"Resource index for {package.name}",
                        estimated_chars=len(str(index_items)),
                        required=False,
                        completeness="complete",
                        metadata={"resource_index": index_items},
                    )
                )
            for resource in package.resources:
                if resource.path == "SKILL.md":
                    continue
                candidates.append(
                    self._candidate(
                        binding=binding,
                        package=package,
                        path=resource.path,
                        role=self._resource_role(resource),
                        summary=self._resource_summary(resource),
                        estimated_chars=resource.size,
                        required=False,
                        completeness="ref_only",
                        metadata={
                            "resource_kind": resource.kind,
                            "sha256": resource.sha256,
                            "size": resource.size,
                            "media_type": resource.media_type,
                        },
                    )
                )
        return tuple(candidates[: max(0, int(limit))])

    def _resolve_candidate(
        self,
        candidate: ContextCandidate,
    ) -> tuple[SkillBinding, SkillPackageRevision, str]:
        revision_ref = str(candidate.metadata.get("revision_ref") or "")
        path = str(candidate.metadata.get("resource_path") or "")
        package = self._package_by_revision_ref.get(revision_ref)
        binding = self._binding_by_revision_ref.get(revision_ref)
        if package is None or binding is None or not path:
            raise SkillBindingError("Skill Context candidate is not part of this exact binding set.")
        expected_ref = self._source_ref(package, path)
        if candidate.source_ref != expected_ref:
            raise SkillBindingError("Skill Context candidate source_ref does not match its binding.")
        return binding, package, path

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock:
        del representation
        binding, package, path = self._resolve_candidate(candidate)
        common = {
            "block_id": self._block_id(package, path),
            "block_key": candidate.block_key,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_ref": candidate.source_ref,
            "binding_id": binding.binding_id,
            "role": candidate.role,
            "required": candidate.required,
            "refs": (candidate.source_ref,),
        }
        if path == "SKILL.md":
            return ContextBlock(
                **common,
                content=package.instruction_body,
                completeness="complete",
                content_chars=len(package.instruction_body),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    trust=package.trust,
                ),
            )
        if path == "resource-index":
            index = candidate.metadata.get("resource_index", ())
            return ContextBlock(
                **common,
                content=index,
                completeness="complete",
                content_chars=len(str(index)),
                metadata=self._domain_metadata(binding, package, path),
            )
        resource = package.resource(path)
        if resource.kind == "script":
            descriptor = {
                "descriptor_kind": "skill_script",
                "revision_ref": package.revision_ref,
                "resource_path": resource.path,
                "sha256": resource.sha256,
                "size": resource.size,
            }
            return ContextBlock(
                **common,
                content=descriptor,
                completeness="ref_only",
                content_chars=len(str(descriptor)),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    resource_kind=resource.kind,
                ),
            )
        if resource.kind == "asset":
            descriptor = {
                "descriptor_kind": "skill_asset",
                "revision_ref": package.revision_ref,
                "resource_path": resource.path,
                "sha256": resource.sha256,
                "size": resource.size,
                "media_type": resource.media_type,
            }
            return ContextBlock(
                **common,
                content=descriptor,
                completeness="ref_only",
                content_chars=len(str(descriptor)),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    resource_kind=resource.kind,
                ),
            )
        readback = self.library.read_resource(
            package.revision_ref,
            path,
            max_bytes=max_chars,
        )
        return ContextBlock(
            **common,
            content=readback.text,
            completeness="truncated" if readback.truncated else "complete",
            content_chars=len(readback.text),
            metadata=self._domain_metadata(
                binding,
                package,
                path,
                resource_kind=resource.kind,
                sha256=resource.sha256,
                total_bytes=readback.total_bytes,
            ),
        )


__all__ = ["SkillContextSource"]

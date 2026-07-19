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
import re
from collections.abc import Mapping, Sequence
from typing import Any

from agently.core.context._Cursor import decode_source_cursor, encode_source_cursor
from agently.types.data import ContextBlock, ContextCandidate, ContextReadIntent
from agently.types.plugins import ContextSourceCandidateWindow

from .Binding import SkillBinding, SkillBindingError
from .Package import SkillPackageRevision, SkillResourceDescriptor
from .SkillLibrary import SkillLibrary


def _source_kind_enabled(filters: Mapping[str, Any], kind: str) -> bool:
    raw = filters.get("source_kinds")
    if raw is None:
        return True
    if isinstance(raw, str):
        offered = {raw.strip()}
    elif isinstance(raw, Sequence) and not isinstance(raw, bytes | bytearray):
        offered = {str(item).strip() for item in raw if str(item).strip()}
    else:
        return False
    return not offered or kind in offered


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

    @classmethod
    def _markdown_sections(
        cls,
        path: str,
        content: str,
    ) -> tuple[tuple[str, str, str], ...]:
        lines = content.splitlines(keepends=True)
        headings: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if match is not None:
                headings.append((index, match.group(1).strip()))
        sections: list[tuple[str, str, str]] = []
        if headings and headings[0][0] > 0:
            preamble = "".join(lines[: headings[0][0]]).strip()
            if preamble:
                sections.append((f"{path}#section-0", "Overview", preamble))
        for ordinal, (start, title) in enumerate(headings, start=1):
            end = headings[ordinal][0] if ordinal < len(headings) else len(lines)
            body = "".join(lines[start:end]).strip()
            if body:
                sections.append((f"{path}#section-{ordinal}", title, body))
        return tuple(sections)

    @classmethod
    def _instruction_sections(
        cls,
        package: SkillPackageRevision,
    ) -> tuple[tuple[str, str, str], ...]:
        return cls._markdown_sections("SKILL.md", package.instruction_body)

    @classmethod
    def _lossy_instruction_digest(
        cls,
        package: SkillPackageRevision,
        *,
        max_chars: int,
    ) -> tuple[str, tuple[str, ...], tuple[tuple[str, str, str], ...]]:
        sections = cls._instruction_sections(package)
        full_ref = cls._source_ref(package, "SKILL.md")
        section_refs = tuple(cls._source_ref(package, path) for path, _, _ in sections)
        lines = [
            f"# {package.name} — lossy task digest",
            "",
            (
                "This digest is an explicitly authorized lossy projection. "
                "The immutable full Skill remains authoritative at the original ref."
            ),
            "",
            f"Skill: `{package.skill_id}`",
            f"Revision: `{package.revision_ref}`",
            f"Description: {package.description or '(not provided)'}",
            f"Full instructions ref: `{full_ref}`",
        ]
        if sections:
            lines.extend(["", "## Section refs"])
            lines.extend(
                f"- {title}: `{cls._source_ref(package, path)}`"
                for path, title, _ in sections
            )
        content = "\n".join(lines).strip()
        if len(content) > max_chars:
            marker = "\n\n[outline truncated; use refs from block metadata]"
            content = content[: max(1, max_chars - len(marker))].rstrip() + marker
            content = content[:max_chars]
        return content, (full_ref, *section_refs), sections

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

    def _all_candidates(self) -> tuple[ContextCandidate, ...]:
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
            for section_path, section_title, section_body in self._instruction_sections(package):
                candidates.append(
                    self._candidate(
                        binding=binding,
                        package=package,
                        path=section_path,
                        role="instruction",
                        summary=f"Skill instruction section: {section_title}",
                        estimated_chars=len(section_body),
                        required=False,
                        completeness="complete",
                        metadata={"section_title": section_title},
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
                if resource.kind not in {"reference", "example"} or not resource.path.endswith(
                    ".md"
                ):
                    continue
                raw_sections = resource.metadata.get("markdown_sections", ())
                if not isinstance(raw_sections, Sequence) or isinstance(
                    raw_sections,
                    str | bytes | bytearray,
                ):
                    continue
                for raw_section in raw_sections:
                    if not isinstance(raw_section, Mapping):
                        continue
                    section_path = str(raw_section.get("section_path") or "")
                    section_title = str(raw_section.get("title") or "")
                    estimated_chars = int(raw_section.get("estimated_chars") or 0)
                    if not section_path or not section_title or estimated_chars <= 0:
                        continue
                    candidates.append(
                        self._candidate(
                            binding=binding,
                            package=package,
                            path=section_path,
                            role=self._resource_role(resource),
                            summary=(
                                f"{resource.kind} section: {section_title} "
                                f"({resource.path})"
                            ),
                            estimated_chars=estimated_chars,
                            required=False,
                            completeness="complete",
                            metadata={
                                "resource_kind": resource.kind,
                                "parent_resource_path": resource.path,
                                "section_title": section_title,
                                "byte_offset": int(raw_section.get("byte_offset") or 0),
                                "byte_size": int(raw_section.get("byte_size") or 0),
                                "sha256": resource.sha256,
                                "size": resource.size,
                                "media_type": resource.media_type,
                            },
                        )
                    )
        return tuple(candidates)

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow:
        page_size = int(limit)
        if page_size <= 0:
            raise ValueError("limit must be a positive integer.")
        resolved_filters = dict(filters or intent.filters)
        explicit_refs = set(intent.explicit_refs)
        scope = {
            "query": intent.query,
            "explicit_refs": sorted(explicit_refs),
            "roles": list(intent.roles),
            "skill_revision_refs": [
                package.revision_ref for package in self.packages
            ],
        }
        if not _source_kind_enabled(resolved_filters, "skill_library"):
            return ContextSourceCandidateWindow(
                source_id=self.source_id,
                source_revision=self.source_revision,
                scope={**scope, "enabled": False},
                candidates=(),
                returned_candidates=0,
                exhaustive=True,
                cursor=cursor,
            )
        offset = decode_source_cursor(
            cursor,
            source_id=self.source_id,
            source_revision=self.source_revision,
            scope=scope,
        )
        all_candidates = self._all_candidates()
        anchors = tuple(
            candidate
            for candidate in all_candidates
            if candidate.required or candidate.source_ref in explicit_refs
        )
        optional = tuple(
            candidate
            for candidate in all_candidates
            if not candidate.required and candidate.source_ref not in explicit_refs
        )
        page = optional[offset : offset + page_size]
        next_offset = offset + len(page)
        has_more = next_offset < len(optional)
        next_cursor = (
            encode_source_cursor(
                source_id=self.source_id,
                source_revision=self.source_revision,
                scope=scope,
                offset=next_offset,
            )
            if has_more
            else None
        )
        candidates = (*anchors, *page)
        return ContextSourceCandidateWindow(
            source_id=self.source_id,
            source_revision=self.source_revision,
            scope=scope,
            candidates=candidates,
            returned_candidates=len(candidates),
            exhaustive=not has_more,
            cursor=cursor,
            next_cursor=next_cursor,
        )

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
            if representation == "lossy_digest":
                content, refs, sections = self._lossy_instruction_digest(
                    package,
                    max_chars=max_chars,
                )
                return ContextBlock(
                    **{key: value for key, value in common.items() if key != "refs"},
                    content=content,
                    completeness="lossy",
                    content_chars=len(content),
                    refs=refs,
                    metadata=self._domain_metadata(
                        binding,
                        package,
                        path,
                        trust=package.trust,
                        representation="lossy_digest",
                        original_chars=len(package.instruction_body),
                        omitted_chars=max(0, len(package.instruction_body) - len(content)),
                        section_refs=[
                            {
                                "title": title,
                                "source_ref": self._source_ref(package, section_path),
                                "estimated_chars": len(body),
                            }
                            for section_path, title, body in sections
                        ],
                    ),
                )
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
        if path.startswith("SKILL.md#section-"):
            section = next(
                (
                    (section_title, section_body)
                    for section_path, section_title, section_body in self._instruction_sections(package)
                    if section_path == path
                ),
                None,
            )
            if section is None:
                raise SkillBindingError("Skill instruction section no longer matches the bound revision.")
            section_title, section_body = section
            content = section_body[:max_chars]
            return ContextBlock(
                **{key: value for key, value in common.items() if key != "refs"},
                content=content,
                completeness="truncated" if len(section_body) > max_chars else "complete",
                content_chars=len(content),
                refs=(candidate.source_ref, self._source_ref(package, "SKILL.md")),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    section_title=section_title,
                    total_chars=len(section_body),
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
        resource_section = re.match(r"^(.+\.md)#section-\d+$", path)
        if resource_section is not None:
            parent_path = resource_section.group(1)
            resource = package.resource(parent_path)
            raw_sections = resource.metadata.get("markdown_sections", ())
            section = next(
                (
                    raw_section
                    for raw_section in raw_sections
                    if isinstance(raw_section, Mapping)
                    and str(raw_section.get("section_path") or "") == path
                ),
                None,
            )
            if section is None:
                raise SkillBindingError(
                    "Skill resource section no longer matches the bound revision."
                )
            section_title = str(section.get("title") or "")
            byte_offset = int(section.get("byte_offset") or 0)
            byte_size = int(section.get("byte_size") or 0)
            readback = self.library.read_resource(
                package.revision_ref,
                parent_path,
                max_bytes=max(1, byte_size),
                offset=byte_offset,
            )
            section_body = readback.text.strip()
            content = section_body[:max_chars]
            return ContextBlock(
                **{key: value for key, value in common.items() if key != "refs"},
                content=content,
                completeness="truncated" if len(section_body) > max_chars else "complete",
                content_chars=len(content),
                refs=(candidate.source_ref, self._source_ref(package, parent_path)),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    resource_kind=resource.kind,
                    parent_resource_path=parent_path,
                    section_title=section_title,
                    total_chars=int(section.get("estimated_chars") or len(section_body)),
                    byte_offset=byte_offset,
                    byte_size=byte_size,
                    sha256=resource.sha256,
                ),
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

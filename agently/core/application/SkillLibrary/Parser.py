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
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .Package import SkillResourceDescriptor, SkillResourceKind


class SkillPackageError(ValueError):
    """Raised when a Skill package boundary or contract is invalid."""


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass(frozen=True)
class ParsedSkillPackage:
    root: Path
    skill_id: str
    name: str
    description: str
    version: str
    instruction_body: str
    frontmatter: dict[str, Any]
    revision: str
    resources: tuple[SkillResourceDescriptor, ...]


def _skill_id(value: str) -> str:
    normalized = re.sub(r"\s+", "-", value.strip().lower())
    normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("._-")
    normalized = re.sub(r"[-_.]{2,}", "-", normalized).strip("._-")
    if not normalized:
        raise SkillPackageError("SKILL.md frontmatter name is empty after normalization.")
    return normalized


def _resource_kind(path: str) -> SkillResourceKind:
    if path == "SKILL.md":
        return "instruction"
    root = path.split("/", 1)[0]
    return {
        "references": "reference",
        "examples": "example",
        "assets": "asset",
        "scripts": "script",
    }.get(root, "resource")  # type: ignore[return-value]


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".git", ".agently", "__pycache__"}:
            continue
        if path.is_symlink():
            raise SkillPackageError(
                f"Skill package cannot contain a symbolic link: {relative.as_posix()}"
            )
        if path.is_file():
            files.append(path)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    return files


def _markdown_section_index(path: str, data: bytes) -> list[dict[str, Any]]:
    lines = data.splitlines(keepends=True)
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        text = line.decode("utf-8", errors="replace")
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", text)
        if match is not None:
            headings.append((index, match.group(1).strip()))
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    sections: list[dict[str, Any]] = []

    def append_section(ordinal: int, title: str, start: int, end: int) -> None:
        section_data = b"".join(lines[start:end])
        content = section_data.decode("utf-8", errors="replace").strip()
        if not content:
            return
        sections.append(
            {
                "section_path": f"{path}#section-{ordinal}",
                "title": title,
                "byte_offset": offsets[start],
                "byte_size": offsets[end] - offsets[start],
                "estimated_chars": len(content),
            }
        )

    next_ordinal = 0
    if headings and headings[0][0] > 0:
        append_section(0, "Overview", 0, headings[0][0])
        next_ordinal = 1
    for heading_index, (start, title) in enumerate(headings):
        ordinal = heading_index + 1 if next_ordinal == 0 else heading_index + next_ordinal
        end = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(lines)
        append_section(ordinal, title, start, end)
    return sections


def parse_skill_package(source: str | Path) -> ParsedSkillPackage:
    root = Path(source).expanduser().resolve()
    skill_file = root / "SKILL.md"
    if not root.is_dir() or not skill_file.is_file():
        raise SkillPackageError(f"Skill package must contain SKILL.md: {root}")
    files = _package_files(root)
    text = skill_file.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillPackageError("SKILL.md frontmatter must include a non-empty name.")
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise SkillPackageError(f"Cannot parse SKILL.md frontmatter: {error}") from error
    if not isinstance(parsed, dict):
        raise SkillPackageError("SKILL.md frontmatter must be a mapping with non-empty name.")
    name = str(parsed.get("name") or "").strip()
    if not name:
        raise SkillPackageError("SKILL.md frontmatter must include a non-empty name.")

    package_digest = hashlib.sha256()
    resources: list[SkillResourceDescriptor] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        package_digest.update(relative.encode("utf-8"))
        package_digest.update(b"\0")
        package_digest.update(len(data).to_bytes(8, "big"))
        package_digest.update(data)
        kind = _resource_kind(relative)
        metadata: dict[str, Any] = {}
        if kind in {"reference", "example"} and relative.endswith(".md"):
            metadata["markdown_sections"] = _markdown_section_index(relative, data)
        resources.append(
            SkillResourceDescriptor(
                path=relative,
                kind=kind,
                sha256=_digest_file(path),
                size=len(data),
                media_type=mimetypes.guess_type(relative)[0],
                executable=kind == "script",
                metadata=metadata,
            )
        )
    return ParsedSkillPackage(
        root=root,
        skill_id=_skill_id(name),
        name=name,
        description=str(parsed.get("description") or "").strip(),
        version=str(parsed.get("version") or "0.1.0").strip() or "0.1.0",
        instruction_body=text[match.end() :].strip(),
        frontmatter=dict(parsed),
        revision=f"sha256:{package_digest.hexdigest()}",
        resources=tuple(resources),
    )


__all__ = ["ParsedSkillPackage", "SkillPackageError", "parse_skill_package"]

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

import json
import shutil
from pathlib import Path
from typing import Any

from .Package import SkillPackageRevision, SkillPackRevision, SkillResourceDescriptor
from .Parser import ParsedSkillPackage, SkillPackageError


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class SkillPackageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.packages_root = self.root / "packages"
        self.index_path = self.root / "index.json"

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {
                "schema_version": "agently.skill_library.index.v1",
                "skills": {},
                "packs": {},
            }
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SkillPackageError(f"Cannot read SkillLibrary index: {error}") from error
        if not isinstance(value, dict) or not isinstance(value.get("skills"), dict):
            raise SkillPackageError("SkillLibrary index is malformed.")
        value.setdefault("packs", {})
        if not isinstance(value["packs"], dict):
            raise SkillPackageError("SkillLibrary pack index is malformed.")
        return value

    def _write_index(self, index: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.index_path)

    @staticmethod
    def _revision_directory_name(revision: str) -> str:
        return revision.replace(":", "_")

    def save(
        self,
        parsed: ParsedSkillPackage,
        *,
        scope: str,
        trust: str,
        source: str,
    ) -> SkillPackageRevision:
        canonical_ref = f"skill:{parsed.skill_id}"
        revision_root = (
            self.packages_root
            / parsed.skill_id
            / self._revision_directory_name(parsed.revision)
        )
        metadata_path = revision_root / ".agently" / "package.json"
        if not revision_root.exists():
            revision_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                parsed.root,
                revision_root,
                ignore=shutil.ignore_patterns(".git", ".agently", "__pycache__"),
            )
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "schema_version": "agently.skill_library.package.v1",
                "skill_id": parsed.skill_id,
                "canonical_ref": canonical_ref,
                "revision": parsed.revision,
                "revision_ref": f"{canonical_ref}@{parsed.revision}",
                "name": parsed.name,
                "description": parsed.description,
                "version": parsed.version,
                "scope": scope,
                "trust": trust,
                "source": source,
                "installed_path": str(revision_root),
                "instruction_body": parsed.instruction_body,
                "frontmatter": parsed.frontmatter,
                "resources": [item.to_dict() for item in parsed.resources],
            }
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            keywords_value = parsed.frontmatter.get("keywords", ())
            keywords = (
                [str(item) for item in keywords_value if str(item).strip()]
                if isinstance(keywords_value, (list, tuple))
                else []
            )
            standard_resources = [
                item
                for item in parsed.resources
                if item.kind in {"reference", "example", "asset", "script"}
            ]
            (metadata_path.parent / "decision_card.json").write_text(
                json.dumps(
                    {
                        "skill_id": parsed.skill_id,
                        "name": parsed.name,
                        "description": parsed.description,
                        "keywords": keywords,
                        "guidance_excerpt": parsed.instruction_body[:1000],
                        "resource_summary": [
                            {
                                "path": item.path,
                                "kind": item.kind,
                                "size": item.size,
                            }
                            for item in standard_resources
                        ],
                        "checksum": parsed.revision.removeprefix("sha256:"),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

        index = self._read_index()
        skills = index.setdefault("skills", {})
        record = skills.setdefault(
            parsed.skill_id,
            {"canonical_ref": canonical_ref, "current": parsed.revision, "revisions": []},
        )
        revisions = record.setdefault("revisions", [])
        if parsed.revision not in revisions:
            revisions.append(parsed.revision)
        record["current"] = parsed.revision
        record["canonical_ref"] = canonical_ref
        self._write_index(index)
        return self.load(parsed.skill_id, parsed.revision)

    def _record(self, skill_ref: str) -> tuple[str, dict[str, Any]]:
        skill_id = str(skill_ref).removeprefix("skill:")
        index = self._read_index()
        record = index.get("skills", {}).get(skill_id)
        if not isinstance(record, dict):
            raise SkillPackageError(f"Skill is not installed: {skill_ref}")
        return skill_id, record

    def current_revision(self, skill_ref: str) -> str:
        _, record = self._record(skill_ref)
        revision = str(record.get("current") or "")
        if not revision:
            raise SkillPackageError(f"Skill has no current revision: {skill_ref}")
        return revision

    def revisions(self, skill_ref: str) -> list[str]:
        _, record = self._record(skill_ref)
        values = record.get("revisions")
        if not isinstance(values, list):
            raise SkillPackageError(f"Skill revision index is malformed: {skill_ref}")
        return [str(item) for item in values]

    def list_skill_ids(self) -> list[str]:
        return sorted(self._read_index().get("skills", {}))

    def save_pack(self, pack: SkillPackRevision) -> SkillPackRevision:
        index = self._read_index()
        index.setdefault("packs", {})[pack.skill_pack_id] = pack.to_dict()
        self._write_index(index)
        return self.load_pack(pack.skill_pack_id)

    def load_pack(self, skill_pack_id: str) -> SkillPackRevision:
        record = self._read_index().get("packs", {}).get(str(skill_pack_id))
        if not isinstance(record, dict):
            raise SkillPackageError(f"Skill pack is not installed: {skill_pack_id}")
        failures = record.get("failed_skills")
        return SkillPackRevision(
            skill_pack_id=str(record.get("skill_pack_id") or record.get("skills_pack_id") or ""),
            name=str(record.get("name") or ""),
            source=str(record.get("source") or ""),
            trust=str(record.get("trust_level") or record.get("trust") or "untrusted"),
            revision_refs=tuple(str(item) for item in record.get("revision_refs", ())),
            installed_skills=tuple(str(item) for item in record.get("installed_skills", ())),
            failed_skills=tuple(
                dict(item) for item in failures or () if isinstance(item, dict)
            ),
        )

    def list_pack_ids(self) -> list[str]:
        return sorted(self._read_index().get("packs", {}))

    def remove_pack(self, skill_pack_id: str) -> SkillPackRevision:
        pack = self.load_pack(skill_pack_id)
        index = self._read_index()
        del index["packs"][pack.skill_pack_id]
        self._write_index(index)
        return pack

    def load(self, skill_ref: str, revision: str | None = None) -> SkillPackageRevision:
        skill_id, _ = self._record(skill_ref)
        resolved_revision = revision or self.current_revision(skill_id)
        metadata_path = (
            self.packages_root
            / skill_id
            / self._revision_directory_name(resolved_revision)
            / ".agently"
            / "package.json"
        )
        if not metadata_path.is_file():
            raise SkillPackageError(
                f"Installed Skill revision metadata is missing: skill:{skill_id}@{resolved_revision}"
            )
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SkillPackageError(f"Cannot read installed Skill revision: {error}") from error
        if not isinstance(value, dict):
            raise SkillPackageError("Installed Skill revision metadata is malformed.")
        resources = tuple(
            SkillResourceDescriptor(
                path=str(item.get("path") or ""),
                kind=str(item.get("kind") or "resource"),  # type: ignore[arg-type]
                sha256=str(item.get("sha256") or ""),
                size=int(item.get("size") or 0),
                media_type=(str(item["media_type"]) if item.get("media_type") else None),
                executable=bool(item.get("executable", False)),
                metadata=_mapping_or_empty(item.get("metadata")),
            )
            for item in value.get("resources", [])
            if isinstance(item, dict)
        )
        return SkillPackageRevision(
            skill_id=str(value.get("skill_id") or ""),
            canonical_ref=str(value.get("canonical_ref") or ""),
            revision=str(value.get("revision") or ""),
            revision_ref=str(value.get("revision_ref") or ""),
            name=str(value.get("name") or ""),
            description=str(value.get("description") or ""),
            version=str(value.get("version") or ""),
            scope=str(value.get("scope") or ""),
            trust=str(value.get("trust") or ""),
            source=str(value.get("source") or ""),
            installed_path=str(value.get("installed_path") or ""),
            instruction_body=str(value.get("instruction_body") or ""),
            frontmatter=_mapping_or_empty(value.get("frontmatter")),
            resources=resources,
        )


__all__ = ["SkillPackageStore"]

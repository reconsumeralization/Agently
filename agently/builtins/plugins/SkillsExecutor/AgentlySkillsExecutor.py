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

import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlparse

import yaml

from agently.core import DynamicTaskContext, TaskDAGExecutor
from agently.types.data import (
    ActionResult,
    SkillCard,
    SkillContract,
    SkillExecutionDict,
    SkillExecutionPlan,
    SkillMode,
    SkillsPackRecord,
    SkillPlanRejection,
    SkillPlanSelection,
    SkillScope,
    SkillStage,
)
from agently.utils import FunctionShifter, Settings


_MANIFEST_NAMES = (
    "agently.skill.yaml",
    "agently.skill.yml",
    "agently.skill.json",
    "skill.yaml",
    "skill.yml",
    "skill.json",
)
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_SKILL_ID_PATTERN = re.compile(r"[^a-z0-9._-]+")
_TEMPLATE_PATTERN = re.compile(r"^\$\{([^}]+)\}$")
_DEFAULT_BASH_ACTION_ALIASES = {"bash", "shell", "sh", "cmd", "run_bash", "bash_sandbox"}


class SkillError(RuntimeError):
    pass


class SkillInstallError(SkillError):
    pass


class SkillNormalizationError(SkillError):
    pass


class SkillExecutionError(SkillError):
    pass


def _ensure_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in _ensure_list(value) if isinstance(item, dict)]


def _ensure_string_list(value: Any) -> list[str]:
    return [str(item) for item in _ensure_list(value) if str(item).strip()]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _sanitize_skill_id(value: str) -> str:
    skill_id = _SKILL_ID_PATTERN.sub("-", value.strip().lower()).strip("-")
    if not skill_id:
        raise SkillNormalizationError("Skill id is empty after normalization.")
    return skill_id


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return {}, text
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise SkillNormalizationError(f"Cannot parse SKILL.md frontmatter: { error }") from error
    return _ensure_dict(parsed), text[match.end():]


def _load_structured_file(path: Path) -> dict[str, Any]:
    text = _read_text(path)
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            parsed = yaml.safe_load(text)
        else:
            parsed = json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as error:
        raise SkillNormalizationError(f"Cannot parse skill manifest '{ path }': { error }") from error
    if not isinstance(parsed, dict):
        raise SkillNormalizationError(f"Skill manifest '{ path }' must parse to a dict.")
    return parsed


def _copy_public(value: Any) -> Any:
    return copy.deepcopy(value)


def _flatten_public_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_public_text(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return " ".join(_flatten_public_text(item) for item in value)
    return str(value)


# File-format type terms used in semantic output matching.
# Keys are canonical type names; values add common synonyms for that format.
# Skill-specific role aliases are NOT defined here — skills declare their own
# output schemas and should not rely on framework-level business domain terms.
_SEMANTIC_TYPE_ALIASES: dict[str, list[str]] = {
    "docx": ["docx", "word", "document"],
    "pdf": ["pdf", "printable", "handout"],
    "pptx": ["pptx", "powerpoint", "slides", "slide deck"],
    "xlsx": ["xlsx", "excel", "spreadsheet", "workbook"],
    "json": ["json", "structured"],
    "md": ["markdown", "md"],
    "directory": ["folder", "directory"],
    "zip": ["zip", "archive"],
}


def _semantic_role_and_type(name: str) -> tuple[str, str]:
    cleaned = str(name).strip().strip("/")
    if not cleaned:
        return "output", "artifact"
    leaf = cleaned.split("/")[-1]
    if "." not in leaf:
        return _sanitize_loose_id(leaf), "directory"
    role, suffix = leaf.rsplit(".", 1)
    return _sanitize_loose_id(role), suffix.lower()


def _sanitize_loose_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("_.-")
    return normalized or "output"


def _sanitize_skills_pack_storage_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("_.-")
    return normalized or "skill_pack"


def _normalize_skills_pack_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("skills_pack_id") or value.get("name") or value.get("id")
    return str(value or "").strip()


def _default_skills_pack_id(source: str | Path) -> str:
    raw = str(source).strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.netloc == "github.com":
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = [item for item in path.split("/") if item]
        if len(parts) >= 2:
            return f"{ parts[0] }/{ parts[1] }"
    if parsed.scheme and parsed.netloc:
        path = parsed.path.strip("/")
        name = path[:-4] if path.endswith(".git") else path
        return name or parsed.netloc
    return Path(raw).expanduser().name or raw


def _normalize_semantic_output_contract(value: Any) -> dict[str, Any]:
    if not value:
        return {"deliverables": []}
    if isinstance(value, dict) and isinstance(value.get("deliverables"), list):
        deliverables = [_normalize_deliverable(item) for item in value.get("deliverables", [])]
        return {"deliverables": [item for item in deliverables if item.get("role")]}
    if isinstance(value, dict):
        deliverables = []
        for role, spec in value.items():
            spec_data = _ensure_dict(spec)
            deliverables.append(
                _normalize_deliverable({
                    **spec_data,
                    "role": spec_data.get("role") or role,
                    "type": spec_data.get("type") or spec_data.get("artifact_type") or "artifact",
                    "aliases": spec_data.get("aliases") or [role],
                })
            )
        return {"deliverables": [item for item in deliverables if item.get("role")]}
    deliverables = [_normalize_deliverable(item) for item in _ensure_list(value)]
    return {"deliverables": [item for item in deliverables if item.get("role")]}


def _normalize_deliverable(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        role, output_type = _semantic_role_and_type(value)
        return {
            "role": role,
            "type": output_type,
            "required": True,
            "aliases": [value, role, role.replace("_", " ")],
        }
    data = _ensure_dict(value)
    role = str(data.get("role") or data.get("name") or data.get("id") or "")
    output_type = str(data.get("type") or data.get("artifact_type") or "")
    if not role and data.get("path"):
        role, inferred_type = _semantic_role_and_type(str(data.get("path")))
        output_type = output_type or inferred_type
    if not role:
        return {}
    role = _sanitize_loose_id(role)
    output_type = output_type or "artifact"
    aliases = _ensure_string_list(data.get("aliases")) or [role, role.replace("_", " ")]
    return {
        "role": role,
        "type": output_type,
        "required": bool(data.get("required", True)),
        "validation": _copy_public(data.get("validation", {})),
        "aliases": aliases,
    }


def _expected_output_names(contract: dict[str, Any]) -> list[str]:
    names = []
    for item in _ensure_list(contract.get("deliverables")):
        data = _ensure_dict(item)
        role = str(data.get("role") or "")
        output_type = str(data.get("type") or "artifact")
        if not role:
            continue
        if output_type == "directory":
            names.append(f"{ role }/")
        elif output_type == "artifact":
            names.append(role)
        else:
            names.append(f"{ role }.{ output_type }")
    return names


def _matches_selector(contract: SkillContract, selector: Any) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        return selector == contract.get("skill_id")
    if not isinstance(selector, dict):
        return False
    skill_id = selector.get("skill_id") or selector.get("id")
    if skill_id and str(skill_id) != contract.get("skill_id"):
        return False
    tags = selector.get("tags")
    if tags:
        contract_tags = set(str(tag) for tag in _ensure_list(contract.get("metadata", {}).get("tags")))
        if not set(str(tag) for tag in _ensure_list(tags)).issubset(contract_tags):
            return False
    return True


def _matches_skills_pack_selector(contract: SkillContract, selector: Any) -> bool:
    skills_pack_selector = _normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    source = _ensure_dict(contract.get("source"))
    metadata = _ensure_dict(contract.get("metadata"))
    candidates = {
        str(source.get("skills_pack_id") or ""),
        str(source.get("skills_pack_name") or ""),
        str(metadata.get("skills_pack_id") or ""),
        str(metadata.get("skills_pack_name") or ""),
    }
    return skills_pack_selector in candidates


@dataclass
class SkillSource:
    source: str
    source_type: str
    materialized_path: Path


class SkillRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def root(self) -> Path:
        return Path(str(self.settings.get("skills.registry.root", ".agently/skills"))).expanduser().resolve()

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            _write_json(self.index_path, {"skills": {}})

    def _read_index(self) -> dict[str, Any]:
        self._ensure_root()
        try:
            data = json.loads(_read_text(self.index_path))
        except json.JSONDecodeError as error:
            raise SkillInstallError(f"Cannot parse skills index '{ self.index_path }': { error }") from error
        if not isinstance(data, dict):
            raise SkillInstallError("Skills index must be a dict.")
        data.setdefault("skills", {})
        data.setdefault("packs", {})
        return data

    def _write_index(self, data: dict[str, Any]):
        data.setdefault("skills", {})
        data.setdefault("packs", {})
        _write_json(self.index_path, data)

    def install_skills(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
        skills_pack_id: str | None = None,
        skills_pack_name: str | None = None,
    ) -> SkillContract:
        source_info = self._materialize_source(source, source_type=source_type)
        contract = self._normalize_contract(source_info, trust_level=trust_level)
        skill_id = str(contract.get("skill_id", ""))
        skill_root = self.root / skill_id
        index = self._read_index()
        if skill_root.exists():
            if not update:
                raise SkillInstallError(f"Skill '{ skill_id }' is already installed. Pass update=True to replace it.")
            shutil.rmtree(skill_root)

        content_root = skill_root / "content"
        shutil.copytree(source_info.materialized_path, content_root)
        contract["source"] = {
            **_ensure_dict(contract.get("source")),
            "source": str(source),
            "source_type": source_info.source_type,
            "installed_path": str(content_root),
        }
        if skills_pack_id:
            contract["source"]["skills_pack_id"] = skills_pack_id
            contract["metadata"] = {**_ensure_dict(contract.get("metadata")), "skills_pack_id": skills_pack_id}
        if skills_pack_name:
            contract["source"]["skills_pack_name"] = skills_pack_name
            contract["metadata"] = {**_ensure_dict(contract.get("metadata")), "skills_pack_name": skills_pack_name}
        _write_json(skill_root / "canonical.skill.json", contract)
        index["skills"][skill_id] = {
            "skill_id": skill_id,
            "skills_pack_id": skills_pack_id or "",
            "skills_pack_name": skills_pack_name or "",
            "version": contract.get("version", "0.1.0"),
            "display_name": contract.get("card", {}).get("display_name", skill_id),
            "purpose": contract.get("card", {}).get("purpose", ""),
            "trust_level": contract.get("trust_level", "local"),
            "source_type": source_info.source_type,
            "manifest_path": str(skill_root / "canonical.skill.json"),
        }
        self._write_index(index)
        return _copy_public(contract)

    def install_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        fetch: bool = False,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = True,
        discover: str = "auto",
        resolver_mode: str = "deterministic",
        resolver_agent: Any = None,
    ) -> SkillsPackRecord:
        del discover, resolver_mode, resolver_agent
        resolved_skills_pack_id = str(skills_pack_id or name or _default_skills_pack_id(source)).strip()
        if name and skills_pack_id and name != skills_pack_id:
            raise SkillInstallError("install_skills_pack() received both name and skills_pack_id with different values.")
        if not resolved_skills_pack_id:
            raise SkillInstallError("install_skills_pack() requires a non-empty name or skills_pack_id.")
        skills_pack_name = str(name or skills_pack_id or resolved_skills_pack_id)
        source_root, resolved_source_type = self._materialize_skills_pack_source(
            source,
            skills_pack_id=resolved_skills_pack_id,
            source_type=source_type,
            fetch=fetch,
            update=update,
        )
        skill_dirs = self._discover_skills_pack_dirs(source_root)
        installed_skills: list[str] = []
        failed_skills: list[dict[str, Any]] = []
        for skill_dir in skill_dirs:
            try:
                contract = self.install_skills(
                    skill_dir,
                    trust_level=trust_level,
                    update=update,
                    skills_pack_id=resolved_skills_pack_id,
                    skills_pack_name=skills_pack_name,
                )
            except Exception as error:
                failed_skills.append({"path": str(skill_dir), "error": str(error)})
                continue
            installed_skills.append(str(contract.get("skill_id", "")))
        status = "success" if installed_skills and not failed_skills else "partial" if installed_skills else "error"
        record = SkillsPackRecord({
            "skills_pack_id": resolved_skills_pack_id,
            "name": skills_pack_name,
            "source": str(source),
            "source_type": resolved_source_type,
            "installed_skills": installed_skills,
            "failed_skills": failed_skills,
            "status": status,
        })
        index = self._read_index()
        packs = _ensure_dict(index.get("packs"))
        packs[resolved_skills_pack_id] = _copy_public(record)
        index["packs"] = packs
        self._write_index(index)
        return _copy_public(record)

    def list_skills(self) -> list[dict[str, Any]]:
        records = list(_ensure_dict(self._read_index().get("skills")).values())
        records.sort(key=lambda item: str(item.get("skill_id", "")))
        return _copy_public(records)

    def list_skills_packs(self) -> list[SkillsPackRecord]:
        records = list(_ensure_dict(self._read_index().get("packs")).values())
        records.sort(key=lambda item: str(item.get("skills_pack_id", "")))
        return _copy_public(records)

    def inspect_skills_pack(self, skills_pack_id: str) -> SkillsPackRecord:
        packs = _ensure_dict(self._read_index().get("packs"))
        if skills_pack_id not in packs:
            raise SkillInstallError(f"Skills pack '{ skills_pack_id }' is not installed.")
        record = packs[skills_pack_id]
        if not isinstance(record, dict):
            raise SkillInstallError(f"Installed skills pack record '{ skills_pack_id }' is malformed.")
        return _copy_public(record)

    def inspect_skills(self, skill_id: str) -> SkillContract:
        record = self._get_record(skill_id)
        manifest_path = Path(str(record["manifest_path"]))
        try:
            parsed = json.loads(_read_text(manifest_path))
        except json.JSONDecodeError as error:
            raise SkillInstallError(f"Cannot parse installed skill manifest '{ manifest_path }': { error }") from error
        if not isinstance(parsed, dict):
            raise SkillInstallError(f"Installed skill manifest '{ manifest_path }' must parse to a dict.")
        return _copy_public(parsed)

    def remove_skills(self, skill_id: str) -> dict[str, Any]:
        index = self._read_index()
        record = self._get_record(skill_id, index=index)
        skill_root = Path(str(record["manifest_path"])).parent
        if skill_root.exists():
            shutil.rmtree(skill_root)
        del index["skills"][skill_id]
        self._write_index(index)
        return {"removed": True, "skill_id": skill_id}

    def remove_skills_pack(self, skills_pack_id: str, *, remove_skills: bool = False) -> dict[str, Any]:
        index = self._read_index()
        packs = _ensure_dict(index.get("packs"))
        if skills_pack_id not in packs:
            raise SkillInstallError(f"Skills pack '{ skills_pack_id }' is not installed.")
        record = _ensure_dict(packs[skills_pack_id])
        removed_skills: list[str] = []
        if remove_skills:
            for skill_id in _ensure_string_list(record.get("installed_skills")):
                try:
                    self.remove_skills(skill_id)
                except SkillInstallError:
                    pass
                removed_skills.append(skill_id)
            index = self._read_index()
            packs = _ensure_dict(index.get("packs"))
        del packs[skills_pack_id]
        index["packs"] = packs
        self._write_index(index)
        return {"removed": True, "skills_pack_id": skills_pack_id, "removed_skills": removed_skills}

    def _get_record(self, skill_id: str, *, index: dict[str, Any] | None = None) -> dict[str, Any]:
        skills = _ensure_dict((index or self._read_index()).get("skills"))
        if skill_id not in skills:
            raise SkillInstallError(f"Skill '{ skill_id }' is not installed.")
        record = skills[skill_id]
        if not isinstance(record, dict):
            raise SkillInstallError(f"Installed skill record '{ skill_id }' is malformed.")
        return record

    def _materialize_source(self, source: str | Path, *, source_type: str | None = None) -> SkillSource:
        resolved_type = source_type or "local"
        if resolved_type != "local":
            raise SkillInstallError("V1 Skills install supports local directories only.")
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise SkillInstallError(f"Local skill source '{ source }' is not a directory.")
        return SkillSource(source=str(source), source_type="local", materialized_path=source_path)

    def _materialize_skills_pack_source(
        self,
        source: str | Path,
        *,
        skills_pack_id: str,
        source_type: str | None,
        fetch: bool,
        update: bool,
    ) -> tuple[Path, str]:
        raw = str(source)
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https", "ssh", "git"} or raw.startswith("git@"):
            if not fetch:
                raise SkillInstallError("Remote skills pack sources require fetch=True.")
            destination = self.root / "_pack_sources" / _sanitize_skills_pack_storage_id(skills_pack_id)
            if destination.exists() and update:
                shutil.rmtree(destination)
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                completed = subprocess.run(
                    ["git", "clone", "--depth", "1", raw, str(destination)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode != 0:
                    raise SkillInstallError(f"Cannot fetch skills pack '{ raw }': { completed.stderr[-1000:] }")
            return destination, "git"
        if source_type and source_type != "local":
            raise SkillInstallError("V1 install_skills_pack supports local directories and git URLs only.")
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise SkillInstallError(f"Local skills pack source '{ source }' is not a directory.")
        return source_path, "local"

    def _discover_skills_pack_dirs(self, root: Path) -> list[Path]:
        discovered: list[Path] = []
        seen: set[Path] = set()
        for candidate in [root, *root.rglob("*")]:
            if not candidate.is_dir():
                continue
            if not self._looks_like_skill_dir(candidate):
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(candidate)
        discovered.sort(key=lambda path: (len(path.relative_to(root).parts), str(path)))
        return discovered

    def _looks_like_skill_dir(self, path: Path) -> bool:
        if (path / "SKILL.md").is_file():
            return True
        return any((path / name).is_file() for name in _MANIFEST_NAMES)

    def _normalize_contract(self, source: SkillSource, *, trust_level: str | None) -> SkillContract:
        root = source.materialized_path
        manifest: dict[str, Any] = {}
        for name in _MANIFEST_NAMES:
            candidate = root / name
            if candidate.exists() and candidate.is_file():
                manifest = _load_structured_file(candidate)
                break
        frontmatter: dict[str, Any] = {}
        skill_body = ""
        skill_md = root / "SKILL.md"
        if skill_md.exists() and skill_md.is_file():
            frontmatter, skill_body = _parse_frontmatter(_read_text(skill_md))

        skill_id = _sanitize_skill_id(str(
            manifest.get("skill_id")
            or manifest.get("id")
            or frontmatter.get("name")
            or root.name
        ))
        version = str(manifest.get("version") or frontmatter.get("version") or "0.1.0")
        display_name = str(
            manifest.get("display_name")
            or manifest.get("name")
            or frontmatter.get("name")
            or skill_id
        )
        purpose = str(manifest.get("purpose") or manifest.get("description") or frontmatter.get("description") or "")
        stages = self._normalize_stages(manifest)
        action_requirements = [
            str(item)
            for item in _ensure_list(
                manifest.get("action_requirements")
                or manifest.get("requires", {}).get("actions")
            )
        ]
        declared_permissions = _ensure_dict(manifest.get("declared_permissions") or manifest.get("permissions"))
        card = self._normalize_card(
            manifest,
            skill_id=skill_id,
            version=version,
            display_name=display_name,
            purpose=purpose,
            frontmatter=frontmatter,
            action_requirements=action_requirements,
            has_primary_guidance=bool(skill_body.strip()),
        )
        assets = _ensure_dict(manifest.get("assets"))
        if skill_body.strip():
            guidance_assets = _ensure_list(assets.get("guidance_assets"))
            guidance_assets.insert(
                0,
                {
                    "asset_id": "primary-guidance",
                    "kind": "guidance",
                    "path": "SKILL.md",
                    "title": display_name,
                    "content": skill_body.strip(),
                },
            )
            assets["guidance_assets"] = guidance_assets

        return SkillContract({
            "skill_id": skill_id,
            "version": version,
            "source": {"source": source.source, "source_type": source.source_type},
            "trust_level": str(manifest.get("trust_level") or trust_level or source.source_type),
            "card": card,
            "kind": str(manifest.get("kind") or "guidance"),
            "declared_permissions": declared_permissions,
            "dependencies": [str(item) for item in _ensure_list(manifest.get("dependencies"))],
            "assets": assets,
            "declarative_stages": stages,
            "semantic_outputs": _ensure_dict(manifest.get("semantic_outputs") or manifest.get("outputs")),
            "action_requirements": action_requirements,
            "execution_environment_requirements": _ensure_list(
                manifest.get("execution_environment_requirements")
                or manifest.get("requires", {}).get("execution_environments")
            ),
            "validation_rules": _ensure_list(manifest.get("validation_rules")),
            "completion_rules": _ensure_dict(manifest.get("completion") or manifest.get("completion_rules")),
            "extension_slots": _ensure_dict(manifest.get("extension_slots")),
            "metadata": {
                "tags": [str(item) for item in _ensure_list(manifest.get("tags") or frontmatter.get("tags"))],
            },
        })

    def _normalize_card(
        self,
        manifest: dict[str, Any],
        *,
        skill_id: str,
        version: str,
        display_name: str,
        purpose: str,
        frontmatter: dict[str, Any],
        action_requirements: list[str],
        has_primary_guidance: bool,
    ) -> SkillCard:
        raw_card = _ensure_dict(manifest.get("card"))
        activation = _ensure_dict(manifest.get("activation") or manifest.get("activation_hints") or frontmatter.get("activation_hints"))
        keywords = [str(item).lower() for item in _ensure_list(activation.get("keywords") or frontmatter.get("keywords"))]
        inferred = self._infer_card_metadata(skill_id=skill_id, display_name=display_name, purpose=purpose)
        return SkillCard({
            "skill_id": skill_id,
            "version": version,
            "display_name": str(raw_card.get("display_name") or display_name),
            "purpose": str(raw_card.get("purpose") or purpose),
            "activation_hints": {
                "keywords": keywords,
                "invocation_names": [
                    str(item).lower()
                    for item in _ensure_list(activation.get("invocation_names") or [skill_id, display_name])
                    if str(item).strip()
                ],
            },
            "stage_roles": [
                str(item)
                for item in _ensure_list(raw_card.get("stage_roles") or manifest.get("stage_roles") or inferred.get("stage_roles"))
            ],
            "consumes": _ensure_dict_list(raw_card.get("consumes") or manifest.get("consumes") or inferred.get("consumes")),
            "produces": _ensure_dict_list(raw_card.get("produces") or manifest.get("produces") or inferred.get("produces")),
            "artifact_types": [
                str(item)
                for item in _ensure_list(raw_card.get("artifact_types") or manifest.get("artifact_types") or inferred.get("artifact_types"))
            ],
            "side_effects": _ensure_dict_list(raw_card.get("side_effects") or manifest.get("side_effects") or inferred.get("side_effects")),
            "required_capabilities": [
                str(item)
                for item in _ensure_list(raw_card.get("required_capabilities") or manifest.get("required_capabilities") or inferred.get("required_capabilities"))
            ] or action_requirements,
            "complements": [
                str(item)
                for item in _ensure_list(raw_card.get("complements") or manifest.get("complements") or inferred.get("complements"))
            ],
            "task_fit_examples": [str(item) for item in _ensure_list(raw_card.get("task_fit_examples"))],
            "input_expectations": str(raw_card.get("input_expectations") or ""),
            "output_expectations": str(raw_card.get("output_expectations") or ""),
            "available_action_summary": action_requirements,
            "required_permissions": _ensure_dict(raw_card.get("required_permissions")),
            "risk_profile": str(raw_card.get("risk_profile") or ""),
            "composition_hints": [str(item) for item in _ensure_list(raw_card.get("composition_hints"))],
            "failure_modes": [
                str(item)
                for item in _ensure_list(raw_card.get("failure_modes") or manifest.get("failure_modes") or inferred.get("failure_modes"))
            ],
            "content_refs": [
                str(item)
                for item in _ensure_list(
                    raw_card.get("content_refs")
                    or (["primary-guidance"] if has_primary_guidance else [])
                )
            ],
        })

    def _infer_card_metadata(self, *, skill_id: str, display_name: str, purpose: str) -> dict[str, Any]:
        text = f"{ skill_id } { display_name } { purpose }".lower()
        metadata: dict[str, Any] = {
            "stage_roles": [],
            "consumes": [{"role": "task_request", "type": "text"}],
            "produces": [],
            "artifact_types": [],
            "side_effects": [],
            "required_capabilities": [],
            "complements": [],
            "failure_modes": ["missing_dependency", "partial_output"],
        }
        artifact_map = {
            "docx": "docx",
            "pdf": "pdf",
            "pptx": "pptx",
            "xlsx": "xlsx",
        }
        if skill_id in artifact_map or any(word in text for word in ["document", "spreadsheet", "slides", "powerpoint"]):
            artifact_type = artifact_map.get(skill_id, "artifact")
            metadata["stage_roles"] = ["artifact_generation", "export"]
            metadata["artifact_types"] = [artifact_type]
            metadata["produces"] = [{"role": f"{ artifact_type }_artifact", "type": artifact_type}]
            metadata["side_effects"] = [{"kind": "local_file_write", "policy": "approval_or_workspace_policy"}]
            metadata["required_capabilities"] = [skill_id]
            return metadata
        if "webapp" in text or "playwright" in text or "browser" in text:
            metadata["stage_roles"] = ["coding_or_testing", "qa_validation", "evidence_capture"]
            metadata["artifact_types"] = ["screenshot", "trace", "json", "docx"]
            metadata["produces"] = [
                {"role": "screenshots", "type": "directory"},
                {"role": "playwright_trace", "type": "zip"},
                {"role": "console_errors", "type": "json"},
                {"role": "network_errors", "type": "json"},
            ]
            metadata["side_effects"] = [{"kind": "local_process_browser", "policy": "approval_required"}]
            metadata["required_capabilities"] = ["browser", "shell", "playwright"]
            metadata["complements"] = ["docx", "pptx"]
            return metadata
        if "triggerflow" in text or "workflow" in text:
            metadata["stage_roles"] = ["workflow", "orchestration", "dependency_planning"]
            metadata["produces"] = [{"role": "task_graph", "type": "json"}]
            return metadata
        if "runtime" in text or "action" in text or "environment" in text:
            metadata["stage_roles"] = ["tool_or_action_binding", "execution_environment"]
            metadata["side_effects"] = [{"kind": "local_or_external_tool", "policy": "capability_policy"}]
            return metadata
        metadata["stage_roles"] = ["guidance"]
        metadata["produces"] = [{"role": "guidance", "type": "text"}]
        return metadata

    def _normalize_stages(self, manifest: dict[str, Any]) -> list[SkillStage]:
        stages = []
        for index, raw in enumerate(_ensure_list(manifest.get("stages") or manifest.get("declarative_stages")), start=1):
            stage = _ensure_dict(raw)
            if not stage:
                continue
            stage_id = str(stage.get("stage_id") or stage.get("id") or f"stage_{ index }")
            kind = str(stage.get("kind") or "model")
            normalized = cast(SkillStage, {**stage, "stage_id": stage_id, "id": stage_id, "kind": kind})
            stages.append(normalized)
        return stages


class SkillPlanner:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def resolve(
        self,
        *,
        agent: Any,
        task: str | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
        decision_handler: Callable[..., Any] | None = None,
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecutionPlan:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        task_text = str(task or "")
        selectors = _ensure_list(skills)
        skills_pack_selectors = _ensure_list(skills_packs)
        installed = [self.registry.inspect_skills(str(item["skill_id"])) for item in self.registry.list_skills()]
        selected: list[SkillPlanSelection] = []
        selected_skills_packs: dict[str, SkillsPackRecord] = {}
        rejected: list[SkillPlanRejection] = []
        rejected_skills_packs: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        requirements: list[Any] = []
        stage_graph: list[dict[str, Any]] = []
        planned_semantic_outputs: dict[str, Any] = {}

        for contract in installed:
            matched_skill_selector = any(_matches_selector(contract, selector) for selector in selectors) if selectors else False
            matched_skills_pack_selector = any(_matches_skills_pack_selector(contract, selector) for selector in skills_pack_selectors) if skills_pack_selectors else False
            matched_selector = matched_skill_selector or matched_skills_pack_selector
            is_required = mode == "required" and matched_selector
            eligible, reason_code, reason = self._is_eligible(agent, contract)
            if not eligible:
                if matched_skill_selector:
                    rejected.append({"skill_id": str(contract.get("skill_id", "")), "reason_code": reason_code, "reason": reason})
                elif matched_skills_pack_selector:
                    diagnostics.append({
                        "level": "warning",
                        "code": reason_code,
                        "skill_id": str(contract.get("skill_id", "")),
                        "skills_pack_id": str(contract.get("source", {}).get("skills_pack_id", "")),
                        "message": reason,
                    })
                continue
            if not self._should_select(contract, task_text=task_text, matched_selector=matched_selector, mode=mode):
                continue

            selection = self._to_selection(contract, scope=scope, required=is_required, selected_by="required" if is_required else "model_planner")
            selected.append(selection)
            skills_pack_record = self._skills_pack_record_for_contract(contract)
            if skills_pack_record:
                selected_skills_packs[str(skills_pack_record.get("skills_pack_id", ""))] = skills_pack_record
            for requirement in _ensure_list(contract.get("execution_environment_requirements")):
                requirements.append(_copy_public(requirement))
            for stage in _ensure_list(selection.get("stages")):
                stage_key = str(stage.get("stage_id") or stage.get("id") or "")
                stage_graph.append(
                    {
                        "skill_id": str(selection.get("skill_id", "")),
                        "stage_id": stage_key,
                        "kind": stage.get("kind", "model"),
                    }
                )
                if stage_key:
                    planned_semantic_outputs[stage_key] = {
                        "role": stage_key,
                        "type": str(stage.get("kind", "model")),
                    }

        if mode == "required":
            required_ids = {str(selector) for selector in selectors if isinstance(selector, str)}
            selected_ids = {str(item.get("skill_id", "")) for item in selected}
            for missing in sorted(required_ids - selected_ids):
                if not any(item.get("skill_id") == missing for item in rejected):
                    rejected.append(
                        {
                            "skill_id": missing,
                            "reason_code": "required_not_selected",
                            "reason": f"Required skill '{ missing }' was not selected.",
                        }
                    )
            for selector in skills_pack_selectors:
                skills_pack_id = _normalize_skills_pack_identifier(selector)
                if not skills_pack_id:
                    continue
                selected_from_pack = any(
                    _matches_skills_pack_selector(self.registry.inspect_skills(str(item.get("skill_id"))), selector)
                    for item in selected
                    if item.get("skill_id")
                )
                if not selected_from_pack:
                    rejected_skills_packs.append({
                        "skills_pack_id": skills_pack_id,
                        "reason_code": "required_pack_not_selected",
                        "reason": f"Required skills pack '{ skills_pack_id }' had no eligible selected skills.",
                    })

        status = "resolved" if selected else "no_match"
        if mode == "required" and (rejected or rejected_skills_packs):
            status = "blocked"
        semantic_output_contract = _normalize_semantic_output_contract(semantic_outputs)
        if semantic_output_contract.get("deliverables"):
            planned_semantic_outputs.update({
                str(item.get("role")): _copy_public(item)
                for item in _ensure_list(semantic_output_contract.get("deliverables"))
                if item.get("role")
            })

        plan = SkillExecutionPlan({
            "plan_id": uuid.uuid4().hex,
            "mode": mode,
            "status": status,
            "task_summary": task_text,
            "selected_skills": selected,
            "selected_skills_packs": list(selected_skills_packs.values()),
            "rejected_skills": rejected,
            "rejected_skills_packs": rejected_skills_packs,
            "composed_stage_graph": stage_graph,
            "dynamic_task_graph": {},
            "prompt_bindings": [],
            "action_bindings": [
                {"skill_id": str(item.get("skill_id", "")), "actions": item.get("card", {}).get("available_action_summary", [])}
                for item in selected
            ],
            "resource_bindings": [],
            "execution_environment_requirements": requirements,
            "approval_requests": [],
            "state_keys": [str(stage.get("stage_id")) for item in selected for stage in _ensure_list(item.get("stages"))],
            "semantic_outputs": planned_semantic_outputs,
            "artifact_bindings": [],
            "expected_result_shape": semantic_output_contract,
            "stream_policy": {},
            "fallback_policy": {"normal_agent_response_allowed": mode == "model_decision"},
            "cleanup_policy": {"scope": scope},
            "diagnostics": diagnostics,
        })
        plan = await self._compose_plan_with_model(
            agent=agent,
            plan=plan,
            semantic_output_contract=semantic_output_contract,
            planner_mode=planner_mode,
            max_revisions=planner_max_revisions,
        )
        if decision_handler is not None:
            plan = await self._apply_decision_handler(decision_handler, plan=plan, context={"agent": agent, "task": task_text})
        return plan

    async def _compose_plan_with_model(
        self,
        *,
        agent: Any,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
        planner_mode: str,
        max_revisions: int,
    ) -> SkillExecutionPlan:
        selected = _ensure_list(plan.get("selected_skills"))
        if not selected:
            return plan
        deliverables = _ensure_list(semantic_output_contract.get("deliverables"))
        should_compose = planner_mode in {"model", "auto", "model_decision"} and (len(selected) > 1 or bool(deliverables))
        if planner_mode == "deterministic":
            should_compose = False
        if not should_compose and not deliverables:
            return plan

        model_result: dict[str, Any] = {}
        if should_compose:
            try:
                model_result = await self._request_model_plan(
                    agent=agent,
                    plan=plan,
                    semantic_output_contract=semantic_output_contract,
                    max_revisions=max_revisions,
                )
            except Exception as error:
                plan.setdefault("diagnostics", []).append({
                    "level": "warning",
                    "code": "model_planner_failed",
                    "message": str(error),
                })

        repaired = self._repair_planner_result(
            model_result,
            plan=plan,
            semantic_output_contract=semantic_output_contract,
        )
        evaluation = self._evaluate_planner_result(repaired, plan=plan, semantic_output_contract=semantic_output_contract)
        return self._apply_planner_result(plan, repaired, evaluation)

    async def _request_model_plan(
        self,
        *,
        agent: Any,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
        max_revisions: int,
    ) -> dict[str, Any]:
        cards = [
            _copy_public(_ensure_dict(item).get("card", {}))
            for item in _ensure_list(plan.get("selected_skills"))
        ]
        request = (
            agent.input(
                {
                    "task": plan.get("task_summary", ""),
                    "candidate_skill_cards": cards,
                    "semantic_output_contract": semantic_output_contract,
                    "planner_requirements": [
                        "Select entry and supporting skills from candidate_skill_cards only.",
                        "Switch skills by task stage when the case needs domain planning, tools, artifacts, QA, approval, or fallback.",
                        "Represent intermediate artifacts and which later stage consumes them.",
                        "Separate Skill guidance, Actions/tools, external APIs/MCP/SaaS, and final artifacts.",
                        "If side effects or credentials are involved, include explicit approval gates.",
                        "If dependencies, APIs, files, or environments may fail, include retry/fallback/degraded-mode behavior.",
                        "Cover every required semantic deliverable by role and type, not only by filename.",
                    ],
                }
            )
            .instruct(
                "Produce a Skills Executor orchestration plan. Do not claim that files or external writes are already complete; "
                "describe the executable plan and required boundaries."
            )
            .output(self._planner_output_schema())
        )
        result = await request.async_start(max_retries=max(1, max_revisions), raise_ensure_failure=False)
        return _ensure_dict(result)

    def _planner_output_schema(self) -> dict[str, Any]:
        return {
            "selected_skill_ids": [(str, "Skill ids selected from candidates.", True)],
            "entry_skill_id": (str, "Primary entry skill id, or none.", True),
            "stage_plan": [(str, "Ordered stage with skill handoff, dependency, and output notes.", True)],
            "skill_switches": [(str, "Where execution switches between skills or capability layers.", True)],
            "intermediate_artifacts": [(str, "Intermediate artifact role and producer/consumer.", True)],
            "external_side_effects": [(str, "External API/MCP/SaaS writes, local command effects, or file writes.", True)],
            "approval_gates": [(str, "Approval gates before side effects or credentialed actions.", True)],
            "fallbacks": [(str, "Retry, fallback, or degraded-mode behavior.", True)],
            "expected_outputs": [(str, "Final semantic deliverable role/type/path.", True)],
            "boundary_notes": [(str, "Skill vs Action/tool/API/artifact boundary.", True)],
            "risks": [(str, "Missing dependency, policy, environment, or data-quality risk.", True)],
        }

    def _repair_planner_result(
        self,
        result: dict[str, Any],
        *,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_ids = [str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills")) if item.get("skill_id")]
        selected_ids = [item for item in _ensure_string_list(result.get("selected_skill_ids")) if item in candidate_ids]
        output_names = _expected_output_names(semantic_output_contract)
        deliverables = _ensure_list(semantic_output_contract.get("deliverables"))
        selected_ids = self._ensure_supporting_skills(selected_ids, candidate_ids, deliverables)

        repaired = {
            "task_summary": str(plan.get("task_summary", "")),
            "selected_skill_ids": selected_ids,
            "entry_skill_id": str(result.get("entry_skill_id") or (selected_ids[0] if selected_ids else "none")),
            "stage_plan": _ensure_string_list(result.get("stage_plan")),
            "skill_switches": _ensure_string_list(result.get("skill_switches")),
            "intermediate_artifacts": _ensure_string_list(result.get("intermediate_artifacts")),
            "external_side_effects": _ensure_string_list(result.get("external_side_effects")),
            "approval_gates": _ensure_string_list(result.get("approval_gates")),
            "fallbacks": _ensure_string_list(result.get("fallbacks")),
            "expected_outputs": _ensure_string_list(result.get("expected_outputs")),
            "boundary_notes": _ensure_string_list(result.get("boundary_notes")),
            "risks": _ensure_string_list(result.get("risks")),
        }

        for output_name in output_names:
            if not self._text_covers_output(output_name, repaired):
                repaired["expected_outputs"].append(output_name)
                role, output_type = _semantic_role_and_type(output_name)
                repaired["intermediate_artifacts"].append(
                    f"{ role } ({ output_type }) is a required semantic deliverable and must be produced or marked partial."
                )

        flags = self._infer_plan_flags(plan=plan, selected_ids=selected_ids, deliverables=deliverables)
        if flags["external"] and not repaired["external_side_effects"]:
            repaired["external_side_effects"].append(
                "External API/MCP/browser/local process or local file writes may be required; keep them separate from Skill guidance."
            )
        if flags["approval"] and not repaired["approval_gates"]:
            repaired["approval_gates"].append(
                "Require human approval before credentialed external writes, SaaS mutation, browser/server actions, or final local file writes."
            )
        if flags["fallback"] and not repaired["fallbacks"]:
            repaired["fallbacks"].append(
                "If a dependency, API, tool, or artifact writer fails, retry once, then produce a degraded local package and mark partial outputs."
            )
        if flags["current_data"] and not any("source" in item.lower() or "citation" in item.lower() for item in repaired["boundary_notes"]):
            repaired["boundary_notes"].append(
                "Current or time-sensitive data must include source/citation records and stale-data warnings."
            )
        if flags["webapp"] and not self._contains_any(repaired, ["screenshot", "console", "network", "trace", "playwright"]):
            repaired["intermediate_artifacts"].append(
                "Web app evidence pack includes screenshots, console_errors.json, network_errors.json, and playwright_trace.zip."
            )

        if not repaired["boundary_notes"]:
            repaired["boundary_notes"].append(
                "Agent Skills packages provide behavior-loop guidance; Actions/tools/APIs execute atomic work; artifacts are explicit outputs."
            )
        if not repaired["skill_switches"] and selected_ids:
            for left, right in zip(selected_ids, selected_ids[1:]):
                repaired["skill_switches"].append(f"{ left } -> { right } after the previous semantic artifact is ready.")
        if not repaired["risks"]:
            repaired["risks"].append("Missing tool, credential, environment, or artifact writer can produce partial outputs.")

        repaired["stage_plan"] = self._ensure_stage_plan(
            stage_plan=repaired["stage_plan"],
            selected_ids=selected_ids,
            deliverables=deliverables,
            flags=flags,
        )
        return repaired

    def _ensure_supporting_skills(self, selected_ids: list[str], candidate_ids: list[str], deliverables: list[Any]) -> list[str]:
        result = [item for item in selected_ids if item in candidate_ids]
        if not result and candidate_ids:
            result.append(candidate_ids[0])
        required_types = {str(_ensure_dict(item).get("type") or "") for item in deliverables}
        type_to_skill = {"docx": "docx", "pdf": "pdf", "pptx": "pptx", "xlsx": "xlsx"}
        for output_type, skill_id in type_to_skill.items():
            if output_type in required_types and skill_id in candidate_ids and skill_id not in result:
                result.append(skill_id)
        for skill_id in candidate_ids:
            if len(result) >= min(3, len(candidate_ids)):
                break
            if skill_id not in result:
                result.append(skill_id)
        return result

    def _infer_plan_flags(self, *, plan: SkillExecutionPlan, selected_ids: list[str], deliverables: list[Any]) -> dict[str, bool]:
        text = " ".join([
            str(plan.get("task_summary", "")),
            " ".join(selected_ids),
            " ".join(str(item) for item in deliverables),
        ]).lower()
        return {
            "external": any(term in text for term in ["api", "mcp", "browser", "playwright", "webapp", "server"]),
            "approval": any(term in text for term in ["write", "external", "approval", "confirm", "browser", "server", "file", "pdf", "docx", "xlsx", "pptx"]),
            "fallback": True,
            "current_data": any(term in text for term in ["current", "recent", "research", "price", "weather", "source", "citation"]),
            "webapp": any(term in text for term in ["webapp", "web app", "playwright", "login", "sse", "console", "network"]),
        }

    def _ensure_stage_plan(
        self,
        *,
        stage_plan: list[str],
        selected_ids: list[str],
        deliverables: list[Any],
        flags: dict[str, bool],
    ) -> list[str]:
        stages = list(stage_plan)
        for index, skill_id in enumerate(selected_ids, start=1):
            if not any(skill_id in item for item in stages):
                stages.append(f"Stage { index }: use { skill_id } for its declared role and pass structured results to the next stage.")
        for item in deliverables:
            data = _ensure_dict(item)
            role = str(data.get("role") or "")
            output_type = str(data.get("type") or "artifact")
            if role and not any(role in stage for stage in stages):
                stages.append(f"Produce semantic deliverable { role } as { output_type } and attach validation/artifact refs.")
        if selected_ids and len(stages) < len(selected_ids):
            for skill_id in selected_ids:
                if len(stages) >= len(selected_ids):
                    break
                stages.append(
                    f"Dynamic Task node: run { skill_id } as an explicit skill stage and expose its produced artifacts to dependent stages."
                )
        if flags["approval"] and not any("approval" in stage.lower() or "confirm" in stage.lower() for stage in stages):
            stages.append("Approval gate: pause before external writes, credentialed tools, browser/server actions, or final artifact writes.")
        if flags["fallback"] and not any("fallback" in stage.lower() or "degraded" in stage.lower() for stage in stages):
            stages.append("Fallback stage: retry failed tools once, then produce degraded local outputs with partial status and diagnostics.")
        stages.append("QA/trace stage: validate semantic output coverage, source/compliance notes, boundary notes, and skill_trace/execution_log.")
        return stages

    def _evaluate_planner_result(
        self,
        result: dict[str, Any],
        *,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_ids = {str(item.get("skill_id")) for item in _ensure_list(plan.get("selected_skills")) if item.get("skill_id")}
        selected_ids = set(_ensure_string_list(result.get("selected_skill_ids")))
        output_coverage = {
            output_name: self._text_covers_output(output_name, result)
            for output_name in _expected_output_names(semantic_output_contract)
        }
        checks = {
            "selected_skills_are_candidates": selected_ids.issubset(candidate_ids),
            "has_stage_plan": bool(result.get("stage_plan")),
            "has_skill_switches": bool(result.get("skill_switches")) or len(selected_ids) <= 1,
            "has_intermediate_artifacts": bool(result.get("intermediate_artifacts")),
            "has_boundaries": self._contains_any(result, ["skill", "action", "tool", "api", "mcp", "artifact"]),
            "covers_semantic_outputs": all(output_coverage.values()),
        }
        return {
            "status": "pass" if all(checks.values()) else "needs_revision",
            "checks": checks,
            "output_coverage": output_coverage,
        }

    def _apply_planner_result(
        self,
        plan: SkillExecutionPlan,
        result: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> SkillExecutionPlan:
        selected_by_id = {
            str(item.get("skill_id")): item
            for item in _ensure_list(plan.get("selected_skills"))
            if item.get("skill_id")
        }
        ordered_selected = [
            selected_by_id[skill_id]
            for skill_id in _ensure_string_list(result.get("selected_skill_ids"))
            if skill_id in selected_by_id
        ]
        if ordered_selected:
            plan["selected_skills"] = _copy_public(ordered_selected)
        stages = []
        selected_ids = _ensure_string_list(result.get("selected_skill_ids"))
        for index, text in enumerate(_ensure_string_list(result.get("stage_plan")), start=1):
            skill_id = selected_ids[(index - 1) % len(selected_ids)] if selected_ids else ""
            stage_id = f"stage_{ index }"
            stages.append({
                "task_id": stage_id,
                "stage_id": stage_id,
                "skill_id": skill_id,
                "kind": "model_plan",
                "title": text[:120],
                "purpose": text,
                "depends_on": [f"stage_{ index - 1 }"] if index > 1 else [],
                "produces": self._stage_produces(index=index, text=text, plan=plan),
            })
        if stages:
            plan["composed_stage_graph"] = stages
        expected_outputs = _ensure_string_list(result.get("expected_outputs"))
        plan["planner_result"] = _copy_public(result)
        plan["planner_evaluation"] = _copy_public(evaluation)
        plan["stage_plan"] = _ensure_string_list(result.get("stage_plan"))
        plan["skill_switches"] = _ensure_string_list(result.get("skill_switches"))
        plan["intermediate_artifacts"] = _ensure_string_list(result.get("intermediate_artifacts"))
        plan["external_side_effects"] = _ensure_string_list(result.get("external_side_effects"))
        plan["approval_gates"] = _ensure_string_list(result.get("approval_gates"))
        plan["fallbacks"] = _ensure_string_list(result.get("fallbacks"))
        plan["expected_outputs"] = expected_outputs
        plan["boundary_notes"] = _ensure_string_list(result.get("boundary_notes"))
        plan["risks"] = _ensure_string_list(result.get("risks"))
        plan["approval_requests"] = [
            {"reason": item, "status": "required"}
            for item in _ensure_string_list(result.get("approval_gates"))
        ]
        plan["fallback_policy"] = {
            **_ensure_dict(plan.get("fallback_policy")),
            "planned_fallbacks": _ensure_string_list(result.get("fallbacks")),
            "semantic_evaluator": True,
        }
        plan["artifact_bindings"] = [
            {"role": role, "target": output_name}
            for output_name in expected_outputs
            for role, _ in [_semantic_role_and_type(output_name)]
        ]
        plan.setdefault("diagnostics", []).append({
            "level": "info",
            "code": "model_composed_plan",
            "message": f"Planner evaluation { evaluation.get('status') }.",
        })
        return plan

    def _stage_produces(self, *, index: int, text: str, plan: SkillExecutionPlan) -> list[dict[str, Any]]:
        produced: list[dict[str, Any]] = []
        lower = text.lower()
        for item in _ensure_list(_ensure_dict(plan.get("expected_result_shape")).get("deliverables")):
            data = _ensure_dict(item)
            role = str(data.get("role") or "")
            if role and (role.lower() in lower or role.replace("_", " ").lower() in lower):
                produced.append({"role": role, "type": str(data.get("type") or "artifact")})
        return produced or [{"role": f"stage_{ index }", "type": "plan"}]

    def _text_covers_output(self, output_name: str, result: dict[str, Any]) -> bool:
        role, output_type = _semantic_role_and_type(output_name)
        searchable = _flatten_public_text(result).lower()
        if output_name.lower().strip("/") in searchable:
            return True
        role_terms = [role, role.replace("_", " "), role.replace("_", "-")]
        type_aliases = _SEMANTIC_TYPE_ALIASES.get(output_type, [output_type])
        return any(term.lower() in searchable for term in role_terms if term) and any(
            alias.lower() in searchable for alias in type_aliases
        )

    def _contains_any(self, value: Any, terms: list[str]) -> bool:
        text = _flatten_public_text(value).lower()
        return any(term.lower() in text for term in terms)

    def _is_eligible(self, agent: Any, contract: SkillContract) -> tuple[bool, str, str]:
        allowed_trust = {str(item) for item in _ensure_list(agent.settings.get("skills.allowed_trust_levels", []))}
        trust_level = str(contract.get("trust_level", "local"))
        if allowed_trust and trust_level not in allowed_trust:
            return False, "trust_denied", f"Trust level '{ trust_level }' is not allowed."
        for action_id in _ensure_list(contract.get("action_requirements")):
            resolved, reason = self._ensure_action_available_or_resolvable(agent, str(action_id))
            if not resolved:
                return False, "missing_action", reason
        return True, "", ""

    def _ensure_action_available_or_resolvable(self, agent: Any, action_id: str) -> tuple[bool, str]:
        if self._action_available(agent, action_id):
            return True, "available"
        if self._can_auto_bind_bash_action(agent, action_id):
            try:
                self._auto_bind_bash_action(agent, action_id)
            except Exception as error:
                return False, f"Required action '{ action_id }' could not be auto-bound to Bash sandbox: { error }"
            if self._action_available(agent, action_id):
                return True, "auto_bound_bash"
        return False, (
            f"Required action '{ action_id }' is not available, and Skills Executor could not find a "
            "controlled built-in substitute. Bind an Action, enable an execution environment, or approve a "
            "trusted replacement before running this Skill."
        )

    def _action_available(self, agent: Any, action_id: str) -> bool:
        action = getattr(agent, "action", None)
        registry = getattr(action, "action_registry", None)
        if registry is not None and registry.has(action_id):
            return True
        from agently.base import action_registry

        return bool(action_registry.has(action_id))

    def _can_auto_bind_bash_action(self, agent: Any, action_id: str) -> bool:
        if agent.settings.get("skills.action_resolution.auto_enable_bash", True) is False:
            return False
        configured_aliases = _ensure_string_list(agent.settings.get("skills.action_resolution.bash_action_aliases"))
        aliases = {item.strip().lower() for item in configured_aliases if item.strip()} or _DEFAULT_BASH_ACTION_ALIASES
        return action_id.strip().lower() in aliases

    def _auto_bind_bash_action(self, agent: Any, action_id: str):
        action = getattr(agent, "action", None)
        register = getattr(action, "register_bash_sandbox_action", None)
        if not callable(register):
            raise SkillExecutionError("agent.action.register_bash_sandbox_action is not available.")
        allowed_prefixes_setting = agent.settings.get("skills.action_resolution.bash_allowed_cmd_prefixes", None)
        allowed_prefixes = None
        if allowed_prefixes_setting is not None:
            allowed_prefixes = _ensure_string_list(allowed_prefixes_setting)
        timeout = int(agent.settings.get("skills.action_resolution.bash_timeout", 20) or 20)
        register(
            action_id=action_id,
            desc=(
                "Auto-bound by Skills Executor as a controlled Bash substitute for a Skill action. "
                "Runs allowlisted shell commands inside the current workspace boundary."
            ),
            expose_to_model=False,
            allowed_cmd_prefixes=allowed_prefixes,
            allowed_workdir_roots=[str(Path.cwd().resolve())],
            timeout=timeout,
        )

    def _skills_pack_record_for_contract(self, contract: SkillContract) -> SkillsPackRecord | None:
        source = _ensure_dict(contract.get("source"))
        skills_pack_id = str(source.get("skills_pack_id") or "")
        if not skills_pack_id:
            return None
        try:
            return self.registry.inspect_skills_pack(skills_pack_id)
        except SkillInstallError:
            return SkillsPackRecord({
                "skills_pack_id": skills_pack_id,
                "name": str(source.get("skills_pack_name") or skills_pack_id),
                "source": str(source.get("source") or ""),
                "source_type": str(source.get("source_type") or ""),
                "installed_skills": [str(contract.get("skill_id", ""))],
                "failed_skills": [],
                "status": "unknown",
            })

    def _should_select(
        self,
        contract: SkillContract,
        *,
        task_text: str,
        matched_selector: bool,
        mode: SkillMode,
    ) -> bool:
        if matched_selector:
            return True
        if mode == "required":
            return False
        task_lower = task_text.lower()
        hints = _ensure_dict(contract.get("card", {}).get("activation_hints"))
        keywords = [str(item).lower() for item in _ensure_list(hints.get("keywords"))]
        names = [str(item).lower() for item in _ensure_list(hints.get("invocation_names"))]
        return any(keyword and keyword in task_lower for keyword in keywords) or any(
            name and (name in task_lower or f"${ name }" in task_lower) for name in names
        )

    def _to_selection(
        self,
        contract: SkillContract,
        *,
        scope: SkillScope,
        required: bool,
        selected_by: str,
    ) -> SkillPlanSelection:
        skill_id = str(contract.get("skill_id", ""))
        return SkillPlanSelection({
            "skill_id": skill_id,
            "skills_pack_id": str(contract.get("source", {}).get("skills_pack_id", "")),
            "skills_pack_name": str(contract.get("source", {}).get("skills_pack_name", "")),
            "version": str(contract.get("version", "")),
            "display_name": str(contract.get("card", {}).get("display_name", skill_id)),
            "scope": scope,
            "reason": "matched selector" if required else "matched skill card",
            "selected_by": selected_by,
            "required": required,
            "card": _copy_public(contract.get("card", {})),
            "stages": _copy_public(contract.get("declarative_stages", [])),
        })

    async def _apply_decision_handler(
        self,
        decision_handler: Callable[..., Any],
        *,
        plan: SkillExecutionPlan,
        context: dict[str, Any],
    ) -> SkillExecutionPlan:
        if asyncio.iscoroutinefunction(decision_handler):
            result = await decision_handler(_copy_public(plan), context)
        else:
            result = decision_handler(_copy_public(plan), context)
            if asyncio.iscoroutine(result):
                result = await result
        if result is False:
            plan["status"] = "rejected"
            plan["selected_skills"] = []
            return plan
        if isinstance(result, dict):
            merged = _copy_public(plan)
            merged.update(result)
            return SkillExecutionPlan(merged)
        return plan


class SkillExecution:
    def __init__(self, data: SkillExecutionDict):
        self.data = data
        self.execution_id = str(data.get("execution_id", ""))
        self.plan = data.get("plan", {})
        self.status = data.get("status", "created")
        self.output = data.get("output")
        self.result = data.get("result")
        self.runtime_stream = data.get("runtime_stream", [])
        self.skill_logs = data.get("skill_logs", [])
        self.action_logs = data.get("action_logs", [])
        self.approval_records = data.get("approval_records", [])
        self.intervention_records = data.get("intervention_records", [])
        self.close_snapshot = data.get("close_snapshot", {})

    def to_dict(self) -> SkillExecutionDict:
        return _copy_public(self.data)


class SkillExecutor:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def execute(
        self,
        *,
        agent: Any,
        task: str,
        plan: SkillExecutionPlan,
    ) -> SkillExecution:
        execution_id = uuid.uuid4().hex
        action_logs: list[ActionResult] = []
        skill_logs: list[dict[str, Any]] = []
        runtime_stream: list[dict[str, Any]] = []
        state: dict[str, Any] = {"task": task}
        status = str(plan.get("status", "no_match"))
        if status in {"blocked", "rejected"}:
            user_message = self._blocked_user_message(plan)
            return self._build_execution(
                execution_id=execution_id,
                status="blocked",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output={
                    "error": "Skill execution plan is blocked.",
                    "user_message": user_message,
                    "rejected_skills": plan.get("rejected_skills", []),
                    "rejected_skills_packs": plan.get("rejected_skills_packs", []),
                    "resolution_suggestions": self._blocked_resolution_suggestions(plan),
                },
            )
        if not plan.get("selected_skills"):
            return self._build_execution(
                execution_id=execution_id,
                status="no_match",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output=None,
            )

        graph = self._build_dynamic_task_graph(plan)
        plan["dynamic_task_graph"] = graph
        if not graph.get("tasks"):
            return self._build_execution(
                execution_id=execution_id,
                status="success",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output=_copy_public(state),
                task_dag_close_snapshot={},
            )

        try:
            close_snapshot, dag_stream = await self._run_dynamic_task_graph(
                graph=graph,
                agent=agent,
                task=task,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
            )
        except Exception as error:
            return self._build_execution(
                execution_id=execution_id,
                status="error",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output={"error": str(error), "state": _copy_public(state)},
                task_dag_close_snapshot={"error": str(error)},
            )

        runtime_stream.extend(dag_stream)

        failed_stages = [log for log in skill_logs if log.get("status") in {"error", "blocked"}]
        dag_status = "error" if failed_stages else "success"
        return self._build_execution(
            execution_id=execution_id,
            status=dag_status,
            plan=plan,
            state=state,
            skill_logs=skill_logs,
            action_logs=action_logs,
            runtime_stream=runtime_stream,
            output=_copy_public(state),
            task_dag_close_snapshot=close_snapshot,
        )

    def _build_dynamic_task_graph(self, plan: SkillExecutionPlan) -> dict[str, Any]:
        tasks: list[dict[str, Any]] = []
        semantic_outputs: dict[str, Any] = {}
        composed = [_ensure_dict(item) for item in _ensure_list(plan.get("composed_stage_graph")) if _ensure_dict(item)]
        if composed and any(item.get("task_id") for item in composed):
            for index, stage_data in enumerate(composed, start=1):
                task_id = str(stage_data.get("task_id") or stage_data.get("stage_id") or f"stage_{ index }")
                stage_id = str(stage_data.get("stage_id") or task_id)
                tasks.append({
                    "id": task_id,
                    "kind": "skill_stage_handler",
                    "title": str(stage_data.get("title") or f"{ stage_data.get('skill_id', 'skill') }:{ stage_id }"),
                    "purpose": str(stage_data.get("purpose") or stage_data.get("title") or f"Execute composed skill stage { stage_id }."),
                    "depends_on": _ensure_string_list(stage_data.get("depends_on")),
                    "inputs": {
                        "selection": {
                            "skill_id": str(stage_data.get("skill_id") or ""),
                            "card": {},
                        },
                        "stage": {
                            **stage_data,
                            "stage_id": stage_id,
                            "kind": str(stage_data.get("kind") or "model_plan"),
                        },
                        "stage_id": stage_id,
                    },
                    "produces": _ensure_dict_list(stage_data.get("produces")) or [{"role": stage_id, "type": "plan"}],
                })
                for produced in _ensure_dict_list(stage_data.get("produces")):
                    role = str(produced.get("role") or "")
                    if role:
                        semantic_outputs[role] = {"task_id": task_id}
                if stage_id not in semantic_outputs:
                    semantic_outputs[stage_id] = {"task_id": task_id}
            for output_name in _ensure_string_list(plan.get("expected_outputs")):
                role, _ = _semantic_role_and_type(output_name)
                if role and role not in semantic_outputs and tasks:
                    semantic_outputs[role] = {"task_id": tasks[-1]["id"]}
            return {
                "graph_id": f"skill-execution-{ uuid.uuid4().hex[:12] }",
                "task_schema_version": "task_dag/v1",
                "tasks": tasks,
                "semantic_outputs": semantic_outputs,
                "policies": {"source": "skills_executor", "composed": True},
                "diagnostics": [],
            }
        previous_task_id = ""
        index = 0
        for selection in _ensure_list(plan.get("selected_skills")):
            selection_data = _ensure_dict(selection)
            skill_id = str(selection_data.get("skill_id", "skill"))
            for stage in _ensure_list(selection_data.get("stages")):
                stage_data = _ensure_dict(stage)
                if not stage_data:
                    continue
                index += 1
                stage_id = str(stage_data.get("stage_id") or stage_data.get("id") or f"stage_{ index }")
                task_id = self._task_id_for_stage(index=index, skill_id=skill_id, stage_id=stage_id)
                task_entry = {
                    "id": task_id,
                    "kind": "skill_stage_handler",
                    "title": f"{ skill_id }:{ stage_id }",
                    "purpose": f"Execute Skill '{ skill_id }' stage '{ stage_id }'.",
                    "depends_on": [previous_task_id] if previous_task_id else [],
                    "inputs": {
                        "selection": selection_data,
                        "stage": stage_data,
                        "stage_id": stage_id,
                    },
                    "produces": [{"role": stage_id, "type": str(stage_data.get("kind", "model"))}],
                }
                tasks.append(task_entry)
                semantic_outputs[stage_id] = {"task_id": task_id}
                previous_task_id = task_id
        return {
            "graph_id": f"skill-execution-{ uuid.uuid4().hex[:12] }",
            "task_schema_version": "task_dag/v1",
            "tasks": tasks,
            "semantic_outputs": semantic_outputs,
            "policies": {"source": "skills_executor"},
            "diagnostics": [],
        }

    def _task_id_for_stage(self, *, index: int, skill_id: str, stage_id: str) -> str:
        raw = f"s{ index }_{ skill_id }_{ stage_id }"
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_.-")
        if not normalized or not re.match(r"^[A-Za-z_]", normalized):
            normalized = f"s{ index }"
        return normalized

    def _blocked_user_message(self, plan: SkillExecutionPlan) -> str:
        rejected = [_ensure_dict(item) for item in _ensure_list(plan.get("rejected_skills"))]
        missing_actions = [
            str(item.get("reason") or item.get("skill_id") or "")
            for item in rejected
            if item.get("reason_code") == "missing_action"
        ]
        if missing_actions:
            return (
                "Skills Executor could not complete the requested Skill because one or more required "
                "capabilities are unavailable and no controlled substitute could be found. Third-party "
                "Skill scripts are installed as assets and are not executed directly. Please bind the "
                "missing capability as an Action, enable an appropriate execution environment, or choose "
                "another trusted provider."
            )
        return "Skills Executor could not complete the requested Skill. Review the rejected skills and required policies."

    def _blocked_resolution_suggestions(self, plan: SkillExecutionPlan) -> list[str]:
        suggestions = [
            "Bind a framework Action that provides the missing capability.",
            "Use a declarative stage backed by a sandboxed Bash/Python/Node action when that safely replaces the helper script.",
            "Install or configure an external provider/API key when the Skill depends on an external service.",
            "If no controlled substitute exists, ask the user to resolve the missing dependency before retrying.",
        ]
        rejected = [_ensure_dict(item) for item in _ensure_list(plan.get("rejected_skills"))]
        if any(item.get("reason_code") == "missing_action" for item in rejected):
            suggestions.insert(
                0,
                "For simple shell work, declare a Bash/shell action stage; Skills Executor can auto-bind a controlled Bash sandbox.",
            )
        return suggestions

    async def _run_dynamic_task_graph(
        self,
        *,
        graph: dict[str, Any],
        agent: Any,
        task: str,
        state: dict[str, Any],
        skill_logs: list[dict[str, Any]],
        action_logs: list[ActionResult],
        runtime_stream: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async def run_skill_stage(context: DynamicTaskContext):
            inputs = _ensure_dict(context.task.inputs)
            selection = _ensure_dict(inputs.get("selection"))
            stage = _ensure_dict(inputs.get("stage"))
            stage_log = await self._execute_stage(
                agent=agent,
                task=task,
                selection=selection,
                stage=stage,
                state=state,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
            )
            skill_logs.append(stage_log)
            stage_id = str(stage_log.get("stage_id") or inputs.get("stage_id") or context.task.id)
            # Return the stage result even on failure so the TaskDAG chunk completes
            # normally and downstream stages trigger rather than remaining pending.
            # Overall skills execution status is derived from skill_logs afterward.
            return {
                "skill_id": stage_log.get("skill_id"),
                "stage_id": stage_id,
                "kind": stage_log.get("kind"),
                "status": stage_log.get("status"),
                "value": _copy_public(state.get(stage_id)),
            }

        stage_timeout = float(agent.settings.get("skills.stage_execution_timeout", 600) or 600)
        executor = TaskDAGExecutor({"skill_stage_handler": run_skill_stage}, name="skill-execution")
        compiled = executor.compile(graph)
        execution = compiled.create_execution(auto_close=False)
        stream = execution.get_async_runtime_stream(timeout=0.1)
        await execution.async_start({"task": task, "plan": _copy_public(graph)})
        close_snapshot = await execution.async_close(timeout=stage_timeout)
        dag_stream = []
        async for item in stream:
            dag_stream.append(item)
        return close_snapshot, dag_stream

    async def _execute_stage(
        self,
        *,
        agent: Any,
        task: str,
        selection: dict[str, Any],
        stage: dict[str, Any],
        state: dict[str, Any],
        action_logs: list[ActionResult],
        runtime_stream: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage_id = str(stage.get("stage_id") or stage.get("id") or uuid.uuid4().hex)
        kind = str(stage.get("kind") or "model")
        log = {"skill_id": selection.get("skill_id"), "stage_id": stage_id, "kind": kind, "status": "success"}
        try:
            if kind == "action":
                action_id = str(stage.get("action") or "")
                if not action_id:
                    raise SkillExecutionError(f"Skill stage '{ stage_id }' is missing action.")
                if not self._action_available(agent, action_id):
                    log["status"] = "blocked"
                    log["action_id"] = action_id
                    log["error"] = (
                        f"Action '{ action_id }' is not available and could not be resolved to a controlled substitute. "
                        "Third-party Skill scripts are not executed directly."
                    )
                    state[stage_id] = {
                        "blocked": True,
                        "user_message": log["error"],
                        "resolution_suggestions": [
                            "Bind the missing action before running the Skill.",
                            "Declare a Bash/shell stage when a controlled shell command can safely replace the helper.",
                            "Ask the user to install or configure the required provider when no substitute is available.",
                        ],
                    }
                    return log
                action_input = self._resolve_templates(stage.get("input", {}), task=task, state=state)
                result = await agent.action.async_execute_action(
                    action_id,
                    action_input if isinstance(action_input, dict) else {},
                    purpose=f"Skill { selection.get('skill_id') } stage { stage_id }",
                    source_protocol="skill",
                )
                action_logs.append(result)
                state[stage_id] = result.get("data", result.get("result"))
                log["action_id"] = action_id
                log["action_status"] = result.get("status")
                if result.get("status") != "success":
                    log["status"] = result.get("status", "error")
                    log["error"] = result.get("error", "")
            elif kind == "model":
                prompt = str(stage.get("prompt") or "")
                state[stage_id] = {"prompt": self._resolve_templates(prompt, task=task, state=state)}
                log["status"] = "prepared"
            elif kind == "validate":
                self._validate_stage(stage, state)
                state[stage_id] = {"validated": True}
            elif kind == "emit":
                item = {
                    "skill_id": selection.get("skill_id"),
                    "stage_id": stage_id,
                    "data": self._resolve_templates(stage.get("data", stage.get("emits", {})), task=task, state=state),
                }
                runtime_stream.append(item)
                state[stage_id] = item
            elif kind in {"model_plan", "artifact_plan", "approval", "fallback", "qa_validation"}:
                state[stage_id] = {
                    "skill_id": selection.get("skill_id"),
                    "stage_id": stage_id,
                    "kind": kind,
                    "purpose": stage.get("purpose") or stage.get("title") or "",
                    "produces": _copy_public(stage.get("produces", [])),
                    "status": "planned",
                }
                log["status"] = "planned"
            else:
                state[stage_id] = {"skipped": True, "reason": f"Stage kind '{ kind }' is not implemented in V1."}
                log["status"] = "skipped"
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            log["status"] = "error"
            log["error"] = str(error)
        return log

    def _action_available(self, agent: Any, action_id: str) -> bool:
        action = getattr(agent, "action", None)
        registry = getattr(action, "action_registry", None)
        if registry is not None and registry.has(action_id):
            return True
        from agently.base import action_registry

        return bool(action_registry.has(action_id))

    def _validate_stage(self, stage: dict[str, Any], state: dict[str, Any]):
        validation = _ensure_dict(stage.get("validation") or stage)
        required_state = [str(item) for item in _ensure_list(validation.get("required_state"))]
        missing = [key for key in required_state if key not in state]
        if missing:
            raise SkillExecutionError(f"Validation failed. Missing state keys: { ', '.join(missing) }")

    def _resolve_templates(self, value: Any, *, task: str, state: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve_templates(item, task=task, state=state) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_templates(item, task=task, state=state) for item in value]
        if not isinstance(value, str):
            return value
        match = _TEMPLATE_PATTERN.match(value.strip())
        if match is None:
            return value.replace("${task}", task)
        path = match.group(1)
        if path == "task":
            return task
        if path.startswith("state."):
            return self._read_path(state, path[len("state."):])
        return value

    def _read_path(self, source: Any, path: str):
        current = source
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
        return current

    def _build_execution(
        self,
        *,
        execution_id: str,
        status: str,
        plan: SkillExecutionPlan,
        state: dict[str, Any],
        skill_logs: list[dict[str, Any]],
        action_logs: list[ActionResult],
        runtime_stream: list[dict[str, Any]],
        output: Any,
        task_dag_close_snapshot: dict[str, Any] | None = None,
    ) -> SkillExecution:
        data = cast(SkillExecutionDict, {
            "execution_id": execution_id,
            "plan_id": str(plan.get("plan_id", "")),
            "status": status,
            "output": output,
            "result": output,
            "plan": _copy_public(plan),
            "runtime_stream": _copy_public(runtime_stream),
            "skill_logs": _copy_public(skill_logs),
            "action_logs": _copy_public(action_logs),
            "approval_records": _copy_public(plan.get("approval_requests", [])),
            "intervention_records": [],
            "close_snapshot": {
                "state": _copy_public(state),
                "status": status,
                "task_dag": _copy_public(task_dag_close_snapshot or {}),
            },
        })
        return SkillExecution(data)

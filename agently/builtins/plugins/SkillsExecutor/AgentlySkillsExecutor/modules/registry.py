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
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from agently.types.data import SkillCard, SkillContract, SkillStage, SkillsPackRecord
from agently.utils import Settings
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_dict_list, _ensure_list, _ensure_string_list

from .errors import SkillInstallError, SkillNormalizationError
from .helpers import _SEMANTIC_TYPE_ALIASES

# ── Manifest discovery ──────────────────────────────────────────────────────

_MANIFEST_NAMES = (
    "agently.skill.yaml",
    "agently.skill.yml",
    "agently.skill.json",
    "skill.yaml",
    "skill.yml",
    "skill.json",
)

# ── Manifest / frontmatter parsing ──────────────────────────────────────────

_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_SKILL_ID_PATTERN = re.compile(r"[^a-z0-9._-]+")


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


def _sanitize_skills_pack_storage_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("_.-")
    return normalized or "skill_pack"


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
        artifact_type = self._infer_artifact_type(text)
        if artifact_type:
            metadata["stage_roles"] = ["artifact_generation", "export"]
            metadata["artifact_types"] = [artifact_type]
            metadata["produces"] = [{"role": f"{ artifact_type }_artifact", "type": artifact_type}]
            metadata["side_effects"] = [{"kind": "local_file_write", "policy": "approval_or_workspace_policy"}]
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

    def _infer_artifact_type(self, text: str) -> str:
        for artifact_type, aliases in _SEMANTIC_TYPE_ALIASES.items():
            if artifact_type in {"json", "md", "directory", "zip"}:
                continue
            terms = [artifact_type, *aliases]
            if any(term in text for term in terms):
                return artifact_type
        return ""

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

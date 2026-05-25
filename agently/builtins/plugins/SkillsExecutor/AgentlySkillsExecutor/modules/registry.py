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
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from agently.types.data import SkillContract, SkillDecisionCard, SkillsPackRecord
from agently.utils import Settings
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list, _ensure_string_list

from .errors import SkillInstallError, SkillNormalizationError

_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_ROOT_MANIFEST_NAMES = (
    "agently.skill.yaml",
    "agently.skill.yml",
    "agently.skill.json",
    "skill.yaml",
    "skill.yml",
    "skill.json",
)
_STANDARD_RESOURCE_DIRS = ("scripts", "references", "assets")
_DECISION_CARD_FORBIDDEN_KEYS = {"only_when", "exclude_when", "not_for", "required_context", "availability"}


@dataclass
class SkillSource:
    source: str
    source_type: str
    materialized_path: Path
    metadata: dict[str, Any] | None = None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError as error:
        raise SkillInstallError(f"Cannot parse '{ path }': { error }") from error
    if not isinstance(data, dict):
        raise SkillInstallError(f"'{ path }' must parse to a dict.")
    return data


def _sanitize_skill_id(value: str) -> str:
    normalized = re.sub(r"\s+", "-", value.strip().lower())
    normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("._-")
    normalized = re.sub(r"[-_.]{2,}", "-", normalized).strip("._-")
    if not normalized:
        raise SkillNormalizationError("Skill id is empty after normalizing SKILL.md frontmatter 'name'.")
    return normalized


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return {}, text
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise SkillNormalizationError(f"Cannot parse SKILL.md frontmatter: { error }") from error
    return _ensure_dict(parsed), text[match.end():]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_excerpt(text: str, *, limit: int = 1200) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit]


def _default_skills_pack_id(source: str | Path) -> str:
    raw = str(source).strip().rstrip("/")
    if _is_github_shorthand(raw):
        return raw[:-4] if raw.endswith(".git") else raw
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


def _sanitize_skills_pack_storage_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("_.-")
    return normalized or "skill_pack"


def _is_github_shorthand(value: str) -> bool:
    if "://" in value or value.startswith("git@") or value.startswith("/") or value.startswith("."):
        return False
    parts = [item for item in value.removesuffix(".git").split("/") if item]
    if len(parts) != 2:
        return False
    return all(re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in parts)


def _normalize_git_source(value: str) -> tuple[str, bool]:
    raw = value.strip()
    if _is_github_shorthand(raw):
        return f"https://github.com/{ raw.removesuffix('.git') }.git", True
    return raw, False


class SkillRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def root(self) -> Path:
        return Path(str(self.settings.get("skills.registry.root", ".agently/skills"))).expanduser().resolve()

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def install_skills(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
        skills_pack_id: str | None = None,
        skills_pack_name: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> SkillContract:
        source_info = self._materialize_source(source, source_type=source_type)
        if source_metadata:
            source_info.metadata = _copy_public(source_metadata)
        frontmatter, body = self._read_standard_skill(source_info.materialized_path)
        name = str(frontmatter.get("name") or "").strip()
        if not name:
            raise SkillNormalizationError("SKILL.md frontmatter must include non-empty 'name'.")
        skill_id = _sanitize_skill_id(name)
        skill_root = self.root / skill_id
        index = self._read_index()
        if skill_root.exists():
            if not update:
                raise SkillInstallError(f"Skill '{ skill_id }' is already installed. Pass update=True to replace it.")
            shutil.rmtree(skill_root)

        shutil.copytree(source_info.materialized_path, skill_root, ignore=shutil.ignore_patterns(".agently"))
        agently_root = skill_root / ".agently"
        if agently_root.exists():
            shutil.rmtree(agently_root)
        agently_root.mkdir(parents=True, exist_ok=True)

        contract = self._build_installed_contract(
            skill_root=skill_root,
            source=source_info,
            frontmatter=frontmatter,
            body=body,
            trust_level=trust_level,
            skills_pack_id=skills_pack_id,
            skills_pack_name=skills_pack_name,
        )
        index["skills"][skill_id] = {
            "skill_id": skill_id,
            "skills_pack_id": skills_pack_id or "",
            "skills_pack_name": skills_pack_name or "",
            "display_name": contract.get("card", {}).get("display_name", skill_id),
            "description": contract.get("card", {}).get("description", ""),
            "trust_level": contract.get("trust_level", "local"),
            "source_type": source_info.source_type,
            "source": source_info.source,
            "source_url": str(_ensure_dict(contract.get("source")).get("source_url") or ""),
            "source_ref": str(_ensure_dict(contract.get("source")).get("source_ref") or ""),
            "source_commit": str(_ensure_dict(contract.get("source")).get("source_commit") or ""),
            "source_subpath": str(_ensure_dict(contract.get("source")).get("source_subpath") or ""),
            "source_package": str(_ensure_dict(contract.get("source")).get("source_package") or ""),
            "installed_path": str(skill_root),
            "metadata_path": str(agently_root / "install.json"),
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
        ref: str | None = None,
        subpath: str | None = None,
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
        source_root, resolved_source_type, source_metadata = self._materialize_skills_pack_source(
            source,
            skills_pack_id=resolved_skills_pack_id,
            source_type=source_type,
            fetch=fetch,
            ref=ref,
            subpath=subpath,
            update=update,
        )
        installed_skills: list[str] = []
        failed_skills: list[dict[str, Any]] = []
        for skill_dir in self._discover_skills_pack_dirs(source_root):
            try:
                contract = self.install_skills(
                    skill_dir,
                    trust_level=trust_level,
                    update=update,
                    skills_pack_id=resolved_skills_pack_id,
                    skills_pack_name=skills_pack_name,
                    source_metadata=source_metadata,
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
            "source_url": str(source_metadata.get("source_url") or ""),
            "source_ref": str(source_metadata.get("source_ref") or ""),
            "source_commit": str(source_metadata.get("source_commit") or ""),
            "source_subpath": str(source_metadata.get("source_subpath") or ""),
            "source_package": str(source_metadata.get("source_package") or ""),
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

    def discover_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        fetch: bool = True,
        ref: str | None = None,
        subpath: str | None = None,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> dict[str, Any]:
        """Lightly materialize a skills source and build cards without installing.

        Discovery may fetch/cache a remote repository so the planner can read
        `SKILL.md`, but it does not copy Skills into the registry or execute any
        bundled resources.
        """
        resolved_skills_pack_id = str(skills_pack_id or name or _default_skills_pack_id(source)).strip()
        if not resolved_skills_pack_id:
            raise SkillInstallError("discover_skills_pack() requires a non-empty source or skills_pack_id.")
        source_root, resolved_source_type, source_metadata = self._materialize_skills_pack_source(
            source,
            skills_pack_id=resolved_skills_pack_id,
            source_type=source_type,
            fetch=fetch,
            ref=ref,
            subpath=subpath,
            update=update,
        )
        contracts: list[SkillContract] = []
        failed_skills: list[dict[str, Any]] = []
        for skill_dir in self._discover_skills_pack_dirs(source_root):
            try:
                frontmatter, body = self._read_standard_skill(skill_dir)
                contract = self._contract_from_files(skill_root=skill_dir, frontmatter=frontmatter, body=body)
                contract_source = _ensure_dict(contract.get("source"))
                contract_source.update({
                    "source": str(skill_dir),
                    "source_type": resolved_source_type,
                    "installed_path": "",
                    "skills_pack_id": resolved_skills_pack_id,
                    "skills_pack_name": str(name or skills_pack_id or resolved_skills_pack_id),
                    "source_url": str(source_metadata.get("source_url") or ""),
                    "source_ref": str(source_metadata.get("source_ref") or ""),
                    "source_commit": str(source_metadata.get("source_commit") or ""),
                    "source_subpath": str(source_metadata.get("source_subpath") or ""),
                    "source_package": str(source_metadata.get("source_package") or ""),
                })
                contract["source"] = contract_source
                contract["trust_level"] = str(trust_level or ("remote" if resolved_source_type == "git" else resolved_source_type))
                contract["install_metadata"] = {
                    "schema_version": "agently.skills.discovery.v1",
                    "source": str(skill_dir),
                    "source_type": resolved_source_type,
                    "trust_level": contract["trust_level"],
                    "skills_pack_id": resolved_skills_pack_id,
                    "skills_pack_name": str(name or skills_pack_id or resolved_skills_pack_id),
                    "source_url": str(source_metadata.get("source_url") or ""),
                    "source_ref": str(source_metadata.get("source_ref") or ""),
                    "source_commit": str(source_metadata.get("source_commit") or ""),
                    "source_subpath": str(source_metadata.get("source_subpath") or ""),
                    "source_package": str(source_metadata.get("source_package") or ""),
                }
                contracts.append(contract)
            except Exception as error:
                failed_skills.append({"path": str(skill_dir), "error": str(error)})
        return {
            "skills_pack_id": resolved_skills_pack_id,
            "name": str(name or skills_pack_id or resolved_skills_pack_id),
            "source": str(source),
            "source_type": resolved_source_type,
            "source_url": str(source_metadata.get("source_url") or ""),
            "source_ref": str(source_metadata.get("source_ref") or ""),
            "source_commit": str(source_metadata.get("source_commit") or ""),
            "source_subpath": str(source_metadata.get("source_subpath") or ""),
            "source_package": str(source_metadata.get("source_package") or ""),
            "contracts": _copy_public(contracts),
            "failed_skills": failed_skills,
            "status": "success" if contracts and not failed_skills else "partial" if contracts else "error",
        }

    def source_selector_options(self, selector: Any) -> dict[str, Any] | None:
        if isinstance(selector, dict):
            source = selector.get("source") or selector.get("url") or selector.get("package")
            if not source:
                return None
            return {
                "source": str(source),
                "name": selector.get("name"),
                "skills_pack_id": selector.get("skills_pack_id") or selector.get("pack_id"),
                "fetch": bool(selector.get("fetch", True)),
                "ref": selector.get("ref"),
                "subpath": selector.get("subpath"),
                "source_type": selector.get("source_type"),
                "trust_level": selector.get("trust_level") or "remote",
                "auto_allow": bool(selector.get("auto_allow", False)),
            }
        if isinstance(selector, str):
            raw = selector.strip()
            parsed = urlparse(raw)
            if (
                parsed.scheme in {"http", "https", "ssh", "git", "file"}
                or raw.startswith("git@")
                or _is_github_shorthand(raw)
                or Path(raw).expanduser().exists()
            ):
                return {
                    "source": raw,
                    "name": None,
                    "skills_pack_id": None,
                    "fetch": True,
                    "ref": None,
                    "subpath": None,
                    "source_type": None,
                    "trust_level": "remote",
                    "auto_allow": False,
                }
        return None

    def list_skills(self) -> list[dict[str, Any]]:
        records = list(_ensure_dict(self._read_index().get("skills")).values())
        records.sort(key=lambda item: str(item.get("skill_id", "")))
        return _copy_public(records)

    def list_skills_packs(self) -> list[SkillsPackRecord]:
        records = list(_ensure_dict(self._read_index().get("packs")).values())
        records.sort(key=lambda item: str(item.get("skills_pack_id", "")))
        return _copy_public(records)

    def inspect_skills(self, skill_id: str) -> SkillContract:
        record = self._get_record(skill_id)
        installed_path = Path(str(record.get("installed_path") or Path(str(record.get("manifest_path", ""))).parent))
        return _copy_public(self._load_installed_contract(installed_path))

    def inspect_skills_pack(self, skills_pack_id: str) -> SkillsPackRecord:
        packs = _ensure_dict(self._read_index().get("packs"))
        if skills_pack_id not in packs:
            raise SkillInstallError(f"Skills pack '{ skills_pack_id }' is not installed.")
        record = packs[skills_pack_id]
        if not isinstance(record, dict):
            raise SkillInstallError(f"Installed skills pack record '{ skills_pack_id }' is malformed.")
        return _copy_public(record)

    def remove_skills(self, skill_id: str) -> dict[str, Any]:
        index = self._read_index()
        record = self._get_record(skill_id, index=index)
        skill_root = Path(str(record.get("installed_path") or Path(str(record.get("metadata_path", ""))).parent.parent))
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

    def rebuild_agently_metadata(self, skill_id: str) -> SkillContract:
        record = self._get_record(skill_id)
        return self._rebuild_agently_metadata(Path(str(record["installed_path"])))

    def _ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            _write_json(self.index_path, {"skills": {}, "packs": {}})

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
            raise SkillInstallError("Skills install supports local directories only.")
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
        ref: str | None,
        subpath: str | None,
        update: bool,
    ) -> tuple[Path, str, dict[str, Any]]:
        raw = str(source).strip()
        normalized_raw, shorthand = _normalize_git_source(raw)
        metadata: dict[str, Any] = {
            "source_url": "",
            "source_ref": str(ref or ""),
            "source_commit": "",
            "source_subpath": str(subpath or ""),
            "source_package": raw if shorthand else "",
        }
        parsed = urlparse(normalized_raw)
        if parsed.scheme in {"http", "https", "ssh", "git", "file"} or normalized_raw.startswith("git@") or shorthand:
            if not fetch:
                raise SkillInstallError("Remote skills pack sources require fetch=True.")
            metadata["source_url"] = normalized_raw
            destination = self.root / "_pack_sources" / _sanitize_skills_pack_storage_id(skills_pack_id)
            if destination.exists() and update:
                shutil.rmtree(destination)
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                clone_cmd = ["git", "clone", "--depth", "1"]
                if ref:
                    clone_cmd.extend(["--branch", ref])
                clone_cmd.extend([normalized_raw, str(destination)])
                completed = subprocess.run(clone_cmd, check=False, capture_output=True, text=True)
                if completed.returncode != 0:
                    raise SkillInstallError(f"Cannot fetch skills pack '{ normalized_raw }': { completed.stderr[-1000:] }")
            try:
                completed = subprocess.run(
                    ["git", "-C", str(destination), "rev-parse", "HEAD"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode == 0:
                    metadata["source_commit"] = completed.stdout.strip()
            except Exception:
                metadata["source_commit"] = ""
            root = self._resolve_source_subpath(destination, subpath)
            return root, "git", metadata
        if source_type and source_type != "local":
            raise SkillInstallError("install_skills_pack supports local directories and git URLs only.")
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise SkillInstallError(f"Local skills pack source '{ source }' is not a directory.")
        root = self._resolve_source_subpath(source_path, subpath)
        return root, "local", metadata

    @staticmethod
    def _resolve_source_subpath(root: Path, subpath: str | None) -> Path:
        if not subpath:
            return root
        resolved_root = root.resolve()
        resolved = (resolved_root / subpath).resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise SkillInstallError(f"Skills pack subpath '{ subpath }' escapes source root.")
        if not resolved.exists() or not resolved.is_dir():
            raise SkillInstallError(f"Skills pack subpath '{ subpath }' is not a directory.")
        return resolved

    def _discover_skills_pack_dirs(self, root: Path) -> list[Path]:
        discovered: list[Path] = []
        seen: set[Path] = set()
        for candidate in [root, *root.rglob("*")]:
            if not candidate.is_dir() or not (candidate / "SKILL.md").is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(candidate)
        discovered.sort(key=lambda path: (len(path.relative_to(root).parts), str(path)))
        return discovered

    def _read_standard_skill(self, root: Path) -> tuple[dict[str, Any], str]:
        for name in _ROOT_MANIFEST_NAMES:
            if (root / name).is_file():
                raise SkillInstallError(
                    f"Non-standard Skill manifest '{ name }' is not supported. "
                    "Agently Skills use SKILL.md as the only capability definition."
                )
        skill_md = root / "SKILL.md"
        if not skill_md.is_file():
            raise SkillInstallError("Standard Skill directories must contain SKILL.md.")
        return _parse_frontmatter(_read_text(skill_md))

    def _load_installed_contract(self, skill_root: Path) -> SkillContract:
        try:
            frontmatter, body = self._read_standard_skill(skill_root)
        except SkillInstallError:
            raise
        except Exception as error:
            raise SkillInstallError(f"Cannot inspect installed Skill '{ skill_root }': { error }") from error
        contract = self._contract_from_files(skill_root=skill_root, frontmatter=frontmatter, body=body)
        self._apply_install_metadata(skill_root, contract)
        decision_card = self._load_valid_decision_card(skill_root, contract)
        if decision_card is None:
            contract = self._rebuild_agently_metadata(skill_root)
        elif decision_card:
            contract["decision_card"] = decision_card
        return contract

    def _rebuild_agently_metadata(self, skill_root: Path) -> SkillContract:
        frontmatter, body = self._read_standard_skill(skill_root)
        contract = self._contract_from_files(skill_root=skill_root, frontmatter=frontmatter, body=body)
        self._apply_install_metadata(skill_root, contract)
        self._write_agently_metadata(skill_root=skill_root, contract=contract)
        return contract

    def _build_installed_contract(
        self,
        *,
        skill_root: Path,
        source: SkillSource,
        frontmatter: dict[str, Any],
        body: str,
        trust_level: str | None,
        skills_pack_id: str | None,
        skills_pack_name: str | None,
    ) -> SkillContract:
        contract = self._contract_from_files(skill_root=skill_root, frontmatter=frontmatter, body=body)
        source_metadata = _ensure_dict(source.metadata)
        install_metadata = {
            "schema_version": "agently.skills.install.v1",
            "source": source.source,
            "source_type": source.source_type,
            "trust_level": str(trust_level or source.source_type),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "skills_pack_id": skills_pack_id or "",
            "skills_pack_name": skills_pack_name or "",
            "source_url": str(source_metadata.get("source_url") or ""),
            "source_ref": str(source_metadata.get("source_ref") or ""),
            "source_commit": str(source_metadata.get("source_commit") or ""),
            "source_subpath": str(source_metadata.get("source_subpath") or ""),
            "source_package": str(source_metadata.get("source_package") or ""),
        }
        contract["source"] = {
            "source": source.source,
            "source_type": source.source_type,
            "installed_path": str(skill_root),
            "skills_pack_id": skills_pack_id or "",
            "skills_pack_name": skills_pack_name or "",
            "source_url": install_metadata["source_url"],
            "source_ref": install_metadata["source_ref"],
            "source_commit": install_metadata["source_commit"],
            "source_subpath": install_metadata["source_subpath"],
            "source_package": install_metadata["source_package"],
        }
        contract["trust_level"] = install_metadata["trust_level"]
        contract["install_metadata"] = install_metadata
        self._write_agently_metadata(skill_root=skill_root, contract=contract)
        return self._load_installed_contract(skill_root)

    def _apply_install_metadata(self, skill_root: Path, contract: SkillContract) -> None:
        metadata_path = skill_root / ".agently" / "install.json"
        if not metadata_path.exists():
            return
        try:
            install_metadata = _read_json(metadata_path)
        except SkillInstallError:
            return
        contract["install_metadata"] = install_metadata
        source = _ensure_dict(contract.get("source"))
        source.update({
            "source": str(install_metadata.get("source") or source.get("source") or ""),
            "source_type": str(install_metadata.get("source_type") or source.get("source_type") or "local"),
            "installed_path": str(skill_root),
            "skills_pack_id": str(install_metadata.get("skills_pack_id") or ""),
            "skills_pack_name": str(install_metadata.get("skills_pack_name") or ""),
            "source_url": str(install_metadata.get("source_url") or ""),
            "source_ref": str(install_metadata.get("source_ref") or ""),
            "source_commit": str(install_metadata.get("source_commit") or ""),
            "source_subpath": str(install_metadata.get("source_subpath") or ""),
            "source_package": str(install_metadata.get("source_package") or ""),
        })
        contract["source"] = source
        contract["trust_level"] = str(install_metadata.get("trust_level") or contract.get("trust_level") or "local")

    def _contract_from_files(self, *, skill_root: Path, frontmatter: dict[str, Any], body: str) -> SkillContract:
        name = str(frontmatter.get("name") or "").strip()
        if not name:
            raise SkillNormalizationError("SKILL.md frontmatter must include non-empty 'name'.")
        skill_id = _sanitize_skill_id(name)
        description = str(frontmatter.get("description") or "")
        diagnostics = []
        if not description.strip():
            diagnostics.append({
                "level": "warning",
                "code": "missing_description",
                "message": "SKILL.md frontmatter does not include a description.",
            })
        checksums = self._build_checksums(skill_root)
        resource_index = self._build_resource_index(skill_root)
        decision_card = self._build_decision_card(
            skill_id=skill_id,
            name=name,
            description=description,
            frontmatter=frontmatter,
            body=body,
            resource_index=resource_index,
            checksum=str(checksums.get("root_checksum", "")),
        )
        return SkillContract({
            "skill_id": skill_id,
            "version": str(frontmatter.get("version") or "0.1.0"),
            "source": {"installed_path": str(skill_root)},
            "trust_level": "local",
            "card": {
                "skill_id": skill_id,
                "name": name,
                "display_name": name,
                "description": description,
                "purpose": description,
                "activation_hints": {"keywords": _ensure_string_list(frontmatter.get("keywords"))},
                "content_refs": ["SKILL.md"],
            },
            "guidance": {"path": "SKILL.md", "content": body.strip()},
            "assets": {"skill_root": str(skill_root)},
            "decision_card": decision_card,
            "resource_index": resource_index,
            "checksums": checksums,
            "diagnostics": diagnostics,
            "metadata": {"skill_format": "anthropic-skill", "frontmatter": _copy_public(frontmatter)},
        })

    def _write_agently_metadata(self, *, skill_root: Path, contract: SkillContract) -> None:
        agently_root = skill_root / ".agently"
        agently_root.mkdir(parents=True, exist_ok=True)
        install_metadata = _ensure_dict(contract.get("install_metadata"))
        if not install_metadata:
            install_metadata = {
                "schema_version": "agently.skills.install.v1",
                "source": _ensure_dict(contract.get("source")).get("source", ""),
                "source_type": _ensure_dict(contract.get("source")).get("source_type", "local"),
                "trust_level": contract.get("trust_level", "local"),
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "skills_pack_id": _ensure_dict(contract.get("source")).get("skills_pack_id", ""),
                "skills_pack_name": _ensure_dict(contract.get("source")).get("skills_pack_name", ""),
            }
            contract["install_metadata"] = install_metadata
        _write_json(agently_root / "install.json", install_metadata)
        _write_json(agently_root / "checksums.json", contract.get("checksums", {}))
        _write_json(agently_root / "resource_index.json", contract.get("resource_index", {}))
        _write_json(agently_root / "decision_card.json", contract.get("decision_card", {}))

    def _build_checksums(self, skill_root: Path) -> dict[str, Any]:
        files = []
        for path in self._iter_standard_files(skill_root):
            rel = path.relative_to(skill_root).as_posix()
            files.append({"path": rel, "sha256": _sha256_file(path), "size": path.stat().st_size})
        root_digest = hashlib.sha256()
        for item in sorted(files, key=lambda value: str(value["path"])):
            root_digest.update(str(item["path"]).encode("utf-8"))
            root_digest.update(str(item["sha256"]).encode("utf-8"))
        return {"schema_version": "agently.skills.checksums.v1", "root_checksum": root_digest.hexdigest(), "files": files}

    def _build_resource_index(self, skill_root: Path) -> dict[str, Any]:
        resources = []
        for dirname in _STANDARD_RESOURCE_DIRS:
            root = skill_root / dirname
            if not root.exists():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                resources.append({
                    "path": path.relative_to(skill_root).as_posix(),
                    "kind": dirname.rstrip("s"),
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "summary": self._resource_summary(path),
                })
        return {"schema_version": "agently.skills.resources.v1", "resources": resources}

    def read_resource(self, skill_id: str, path: str, *, max_bytes: int = 262144) -> str:
        """Read a bundled resource file content, byte-budgeted.

        Returns the full file content if size ≤ max_bytes, otherwise truncated
        content with a trailing truncation marker.
        """
        record = self._get_record(skill_id)
        skill_root = Path(str(record.get("installed_path") or ""))
        if not skill_root.is_dir():
            raise SkillInstallError(f"Skill '{ skill_id }' installed path is not a directory.")
        resource_path = (skill_root / path).resolve()
        if skill_root not in resource_path.parents and resource_path != skill_root:
            raise SkillInstallError(
                f"Resource path '{ path }' escapes skill root for '{ skill_id }'."
            )
        if not resource_path.is_file():
            raise SkillInstallError(
                f"Resource '{ path }' not found in skill '{ skill_id }'."
            )
        file_size = resource_path.stat().st_size
        content = _read_text(resource_path)
        if len(content) <= max_bytes:
            return content
        truncated = content[:max_bytes]
        marker = (
            f"\n\n... [truncated at { max_bytes }/{ file_size } bytes; "
            f"use max_bytes=N to increase]"
        )
        return truncated + marker

    def _resource_summary(self, path: Path) -> str:
        if path.suffix.lower() in {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml"}:
            try:
                return _safe_excerpt(_read_text(path), limit=300)
            except UnicodeDecodeError:
                return ""
        return ""

    def _build_decision_card(
        self,
        *,
        skill_id: str,
        name: str,
        description: str,
        frontmatter: dict[str, Any],
        body: str,
        resource_index: dict[str, Any],
        checksum: str,
    ) -> SkillDecisionCard:
        return SkillDecisionCard({
            "skill_id": skill_id,
            "name": name,
            "description": description,
            "keywords": _ensure_string_list(frontmatter.get("keywords")),
            "guidance_excerpt": _safe_excerpt(body),
            "resource_summary": [
                {"path": item.get("path"), "kind": item.get("kind"), "summary": item.get("summary", "")}
                for item in _ensure_list(resource_index.get("resources"))[:20]
                if isinstance(item, dict)
            ],
            "checksum": checksum,
        })

    def _load_valid_decision_card(self, skill_root: Path, contract: SkillContract) -> SkillDecisionCard | None:
        path = skill_root / ".agently" / "decision_card.json"
        if not path.exists():
            return None
        try:
            card = _read_json(path)
        except SkillInstallError:
            return None
        if _DECISION_CARD_FORBIDDEN_KEYS.intersection(card.keys()):
            return None
        if str(card.get("checksum") or "") != str(_ensure_dict(contract.get("checksums")).get("root_checksum") or ""):
            return None
        return SkillDecisionCard({
            "skill_id": str(card.get("skill_id") or contract.get("skill_id") or ""),
            "name": str(card.get("name") or _ensure_dict(contract.get("card")).get("display_name") or ""),
            "description": str(card.get("description") or _ensure_dict(contract.get("card")).get("description") or ""),
            "keywords": _ensure_string_list(card.get("keywords")),
            "guidance_excerpt": str(card.get("guidance_excerpt") or ""),
            "resource_summary": [item for item in _ensure_list(card.get("resource_summary")) if isinstance(item, dict)],
            "checksum": str(card.get("checksum") or ""),
        })

    def _iter_standard_files(self, skill_root: Path):
        skill_md = skill_root / "SKILL.md"
        if skill_md.is_file():
            yield skill_md
        for dirname in _STANDARD_RESOURCE_DIRS:
            root = skill_root / dirname
            if not root.exists():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                yield path

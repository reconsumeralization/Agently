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

from typing import Any

from agently.types.data import SkillContract
from agently.utils.DataGuardian import _ensure_dict


def matches_selector(contract: SkillContract, selector: Any) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        card = _ensure_dict(contract.get("card"))
        return selector in {
            str(contract.get("skill_id") or ""),
            str(card.get("display_name") or ""),
            str(card.get("name") or ""),
        }
    if not isinstance(selector, dict):
        return False
    source_options = selector.get("source") or selector.get("url") or selector.get("package")
    if source_options:
        return matches_source_selector(contract, selector)
    skill_id = selector.get("skill_id") or selector.get("id") or selector.get("name")
    if skill_id and str(skill_id) != contract.get("skill_id") and str(skill_id) != contract.get("card", {}).get("display_name"):
        return False
    return True


def matches_source_selector(contract: SkillContract, selector: dict[str, Any]) -> bool:
    source = _ensure_dict(contract.get("source"))
    install = _ensure_dict(contract.get("install_metadata"))
    raw_source = str(selector.get("source") or selector.get("url") or selector.get("package") or "").strip()
    subpath = str(selector.get("subpath") or "").strip()
    pack_id = str(selector.get("skills_pack_id") or selector.get("pack_id") or selector.get("name") or "").strip()
    source_candidates = {
        str(source.get("source") or ""),
        str(source.get("source_url") or ""),
        str(source.get("source_package") or ""),
        str(install.get("source") or ""),
        str(install.get("source_url") or ""),
        str(install.get("source_package") or ""),
    }
    if raw_source and raw_source not in source_candidates:
        try:
            from pathlib import Path

            raw_path = Path(raw_source).expanduser().resolve()
            contract_path = Path(str(source.get("source") or "")).expanduser().resolve()
            if raw_path != contract_path and raw_path not in contract_path.parents:
                return False
        except Exception:
            return False
    if subpath and subpath not in {
        str(source.get("source_subpath") or ""),
        str(install.get("source_subpath") or ""),
    }:
        return False
    if pack_id and pack_id not in {
        str(source.get("skills_pack_id") or ""),
        str(source.get("skills_pack_name") or ""),
        str(install.get("skills_pack_id") or ""),
        str(install.get("skills_pack_name") or ""),
    }:
        return False
    return True


def normalize_skills_pack_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("skills_pack_id") or value.get("name") or value.get("id")
    return str(value or "").strip()


def matches_skills_pack_selector(contract: SkillContract, selector: Any) -> bool:
    skills_pack_selector = normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    source = _ensure_dict(contract.get("source"))
    install = _ensure_dict(contract.get("install_metadata"))
    candidates = {
        str(source.get("skills_pack_id") or ""),
        str(source.get("skills_pack_name") or ""),
        str(install.get("skills_pack_id") or ""),
        str(install.get("skills_pack_name") or ""),
    }
    return skills_pack_selector in candidates


def matches_record_selector(record: dict[str, Any], selector: Any) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        return selector in {
            str(record.get("skill_id") or ""),
            str(record.get("display_name") or ""),
            str(record.get("name") or ""),
        }
    if not isinstance(selector, dict):
        return False
    if selector.get("source") or selector.get("url") or selector.get("package"):
        raw_source = str(selector.get("source") or selector.get("url") or selector.get("package") or "")
        subpath = str(selector.get("subpath") or "")
        source_ok = raw_source in {
            str(record.get("source") or ""),
            str(record.get("source_url") or ""),
            str(record.get("source_package") or ""),
        }
        subpath_ok = not subpath or subpath == str(record.get("source_subpath") or "")
        return source_ok and subpath_ok
    skill_id = selector.get("skill_id") or selector.get("id") or selector.get("name")
    if skill_id:
        return str(skill_id) in {
            str(record.get("skill_id") or ""),
            str(record.get("display_name") or ""),
            str(record.get("name") or ""),
        }
    return True


def matches_record_pack_selector(record: dict[str, Any], selector: Any) -> bool:
    skills_pack_selector = normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    return skills_pack_selector in {
        str(record.get("skills_pack_id") or ""),
        str(record.get("skills_pack_name") or ""),
    }


__all__ = [
    "matches_record_pack_selector",
    "matches_record_selector",
    "matches_selector",
    "matches_skills_pack_selector",
    "matches_source_selector",
    "normalize_skills_pack_identifier",
]

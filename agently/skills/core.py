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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import yaml

from agently.types.data import (
    ActionResult,
    SkillCard,
    SkillContract,
    SkillExecutionDict,
    SkillExecutionPlan,
    SkillMode,
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
        return data

    def _write_index(self, data: dict[str, Any]):
        _write_json(self.index_path, data)

    def install(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
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
        _write_json(skill_root / "canonical.skill.json", contract)
        index["skills"][skill_id] = {
            "skill_id": skill_id,
            "version": contract.get("version", "0.1.0"),
            "display_name": contract.get("card", {}).get("display_name", skill_id),
            "purpose": contract.get("card", {}).get("purpose", ""),
            "trust_level": contract.get("trust_level", "local"),
            "source_type": source_info.source_type,
            "manifest_path": str(skill_root / "canonical.skill.json"),
        }
        self._write_index(index)
        return _copy_public(contract)

    def list(self) -> list[dict[str, Any]]:
        records = list(_ensure_dict(self._read_index().get("skills")).values())
        records.sort(key=lambda item: str(item.get("skill_id", "")))
        return _copy_public(records)

    def inspect(self, skill_id: str) -> SkillContract:
        record = self._get_record(skill_id)
        manifest_path = Path(str(record["manifest_path"]))
        try:
            parsed = json.loads(_read_text(manifest_path))
        except json.JSONDecodeError as error:
            raise SkillInstallError(f"Cannot parse installed skill manifest '{ manifest_path }': { error }") from error
        if not isinstance(parsed, dict):
            raise SkillInstallError(f"Installed skill manifest '{ manifest_path }' must parse to a dict.")
        return _copy_public(parsed)

    def remove(self, skill_id: str) -> dict[str, Any]:
        index = self._read_index()
        record = self._get_record(skill_id, index=index)
        skill_root = Path(str(record["manifest_path"])).parent
        if skill_root.exists():
            shutil.rmtree(skill_root)
        del index["skills"][skill_id]
        self._write_index(index)
        return {"removed": True, "skill_id": skill_id}

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
            "declared_permissions": declared_permissions,
            "dependencies": [str(item) for item in _ensure_list(manifest.get("dependencies"))],
            "assets": assets,
            "declarative_stages": stages,
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
            "task_fit_examples": [str(item) for item in _ensure_list(raw_card.get("task_fit_examples"))],
            "input_expectations": str(raw_card.get("input_expectations") or ""),
            "output_expectations": str(raw_card.get("output_expectations") or ""),
            "available_action_summary": action_requirements,
            "required_permissions": _ensure_dict(raw_card.get("required_permissions")),
            "risk_profile": str(raw_card.get("risk_profile") or ""),
            "composition_hints": [str(item) for item in _ensure_list(raw_card.get("composition_hints"))],
            "content_refs": [
                str(item)
                for item in _ensure_list(
                    raw_card.get("content_refs")
                    or (["primary-guidance"] if has_primary_guidance else [])
                )
            ],
        })

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
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
        decision_handler: Callable[..., Any] | None = None,
    ) -> SkillExecutionPlan:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        task_text = str(task or "")
        selectors = _ensure_list(skills)
        installed = [self.registry.inspect(str(item["skill_id"])) for item in self.registry.list()]
        selected: list[SkillPlanSelection] = []
        rejected: list[SkillPlanRejection] = []
        requirements: list[Any] = []
        stage_graph: list[dict[str, Any]] = []

        for contract in installed:
            matched_selector = any(_matches_selector(contract, selector) for selector in selectors) if selectors else False
            is_required = mode == "required" and matched_selector
            eligible, reason_code, reason = self._is_eligible(agent, contract)
            if not eligible:
                if matched_selector or is_required:
                    rejected.append({"skill_id": str(contract.get("skill_id", "")), "reason_code": reason_code, "reason": reason})
                continue
            if not self._should_select(contract, task_text=task_text, matched_selector=matched_selector, mode=mode):
                continue

            selection = self._to_selection(contract, scope=scope, required=is_required, selected_by="required" if is_required else "model_planner")
            selected.append(selection)
            for requirement in _ensure_list(contract.get("execution_environment_requirements")):
                requirements.append(_copy_public(requirement))
            for stage in _ensure_list(selection.get("stages")):
                stage_graph.append(
                    {
                        "skill_id": str(selection.get("skill_id", "")),
                        "stage_id": stage.get("stage_id") or stage.get("id"),
                        "kind": stage.get("kind", "model"),
                    }
                )

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

        status = "resolved" if selected else "no_match"
        if mode == "required" and rejected:
            status = "blocked"
        plan = SkillExecutionPlan({
            "plan_id": uuid.uuid4().hex,
            "mode": mode,
            "status": status,
            "task_summary": task_text,
            "selected_skills": selected,
            "rejected_skills": rejected,
            "composed_stage_graph": stage_graph,
            "prompt_bindings": [],
            "action_bindings": [
                {"skill_id": str(item.get("skill_id", "")), "actions": item.get("card", {}).get("available_action_summary", [])}
                for item in selected
            ],
            "resource_bindings": [],
            "execution_environment_requirements": requirements,
            "approval_requests": [],
            "state_keys": [str(stage.get("stage_id")) for item in selected for stage in _ensure_list(item.get("stages"))],
            "expected_result_shape": {},
            "stream_policy": {},
            "fallback_policy": {"normal_agent_response_allowed": mode == "model_decision"},
            "cleanup_policy": {"scope": scope},
            "diagnostics": [],
        })
        if decision_handler is not None:
            plan = await self._apply_decision_handler(decision_handler, plan=plan, context={"agent": agent, "task": task_text})
        return plan

    def _is_eligible(self, agent: Any, contract: SkillContract) -> tuple[bool, str, str]:
        allowed_trust = {str(item) for item in _ensure_list(agent.settings.get("skills.allowed_trust_levels", []))}
        trust_level = str(contract.get("trust_level", "local"))
        if allowed_trust and trust_level not in allowed_trust:
            return False, "trust_denied", f"Trust level '{ trust_level }' is not allowed."
        for action_id in _ensure_list(contract.get("action_requirements")):
            if not self._action_available(agent, str(action_id)):
                return False, "missing_action", f"Required action '{ action_id }' is not available."
        return True, "", ""

    def _action_available(self, agent: Any, action_id: str) -> bool:
        action = getattr(agent, "action", None)
        registry = getattr(action, "action_registry", None)
        if registry is not None and registry.has(action_id):
            return True
        from agently.base import action_registry

        return bool(action_registry.has(action_id))

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
            return self._build_execution(
                execution_id=execution_id,
                status="blocked",
                plan=plan,
                state=state,
                skill_logs=skill_logs,
                action_logs=action_logs,
                runtime_stream=runtime_stream,
                output={"error": "Skill execution plan is blocked.", "rejected_skills": plan.get("rejected_skills", [])},
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

        status = "success"
        for selection in _ensure_list(plan.get("selected_skills")):
            for stage in _ensure_list(_ensure_dict(selection).get("stages")):
                stage_log = await self._execute_stage(
                    agent=agent,
                    task=task,
                    selection=_ensure_dict(selection),
                    stage=_ensure_dict(stage),
                    state=state,
                    action_logs=action_logs,
                    runtime_stream=runtime_stream,
                )
                skill_logs.append(stage_log)
                if stage_log.get("status") in {"error", "approval_required", "blocked"}:
                    status = str(stage_log["status"])
                    return self._build_execution(
                        execution_id=execution_id,
                        status=status,
                        plan=plan,
                        state=state,
                        skill_logs=skill_logs,
                        action_logs=action_logs,
                        runtime_stream=runtime_stream,
                        output={"error": stage_log.get("error", ""), "state": _copy_public(state)},
                    )

        return self._build_execution(
            execution_id=execution_id,
            status=status,
            plan=plan,
            state=state,
            skill_logs=skill_logs,
            action_logs=action_logs,
            runtime_stream=runtime_stream,
            output=_copy_public(state),
        )

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
            else:
                state[stage_id] = {"skipped": True, "reason": f"Stage kind '{ kind }' is not implemented in V1."}
                log["status"] = "skipped"
        except Exception as error:
            log["status"] = "error"
            log["error"] = str(error)
        return log

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
            "close_snapshot": {"state": _copy_public(state), "status": status},
        })
        return SkillExecution(data)


class GlobalSkillsFacade:
    def __init__(self, settings: Settings):
        self.registry = SkillRegistry(settings)

    def install(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> SkillContract:
        return self.registry.install(source, source_type=source_type, trust_level=trust_level, update=update)

    def list(self) -> list[dict[str, Any]]:
        return self.registry.list()

    def inspect(self, skill_id: str) -> SkillContract:
        return self.registry.inspect(skill_id)

    def remove(self, skill_id: str) -> dict[str, Any]:
        return self.registry.remove(skill_id)


class AgentSkillsMixin:
    def _init_skills(self, registry: SkillRegistry):
        self.skills_registry = registry
        self.__session_skill_selectors: list[Any] = []
        self.__request_skill_selectors: list[Any] = []
        self.__skill_decision_handler: Callable[..., Any] | None = None
        self.__skill_execution_logs: list[Any] = []

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
    ):
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        target = self.__request_skill_selectors if scope == "request" else self.__session_skill_selectors
        for item in _ensure_list(skills):
            target.append({"selector": _copy_public(item), "mode": mode, "scope": scope})
        return self

    async def async_resolve_skill_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
    ) -> SkillExecutionPlan:
        selectors = self._collect_skill_selectors(skills=skills, mode=mode)
        planner = SkillPlanner(self.skills_registry)
        return await planner.resolve(
            agent=self,
            task=task,
            skills=selectors,
            mode=mode,
            scope=scope,
            decision_handler=self.__skill_decision_handler,
        )

    def resolve_skill_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
    ) -> SkillExecutionPlan:
        return FunctionShifter.syncify(self.async_resolve_skill_plan)(task, skills=skills, mode=mode, scope=scope)

    async def async_run_skill_task(
        self,
        task: str,
        *,
        skills: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "execution",
    ) -> SkillExecution:
        plan = await self.async_resolve_skill_plan(task, skills=skills, mode=mode, scope=scope)
        execution = await SkillExecutor(self.skills_registry).execute(agent=self, task=task, plan=plan)
        self.__skill_execution_logs.append(execution.to_dict())
        return execution

    def run_skill_task(
        self,
        task: str,
        *,
        skills: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "execution",
    ) -> SkillExecution:
        return FunctionShifter.syncify(self.async_run_skill_task)(task, skills=skills, mode=mode, scope=scope)

    def set_skill_decision_handler(self, handler: Callable[..., Any] | None):
        self.__skill_decision_handler = handler
        return self

    def get_skill_execution_logs(self) -> list[dict[str, Any]]:
        return _copy_public(self.__skill_execution_logs)

    def _collect_skill_selectors(self, *, skills: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills is not None:
            selectors.extend(_ensure_list(skills))
        for item in [*self.__session_skill_selectors, *self.__request_skill_selectors]:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    async def _apply_skill_cards_to_prompt(self, prompt: Any):
        selectors = self._collect_skill_selectors(skills=None, mode="model_decision")
        if not selectors:
            return
        cards = []
        guidance = []
        settings = getattr(self, "settings")
        include_guidance = bool(settings.get("skills.prompt.include_primary_guidance", True))
        max_guidance_chars = int(settings.get("skills.prompt.max_guidance_chars_per_skill", 6000) or 6000)
        for record in self.skills_registry.list():
            contract = self.skills_registry.inspect(str(record["skill_id"]))
            if any(_matches_selector(contract, selector) for selector in selectors):
                cards.append(contract.get("card", {}))
                if include_guidance:
                    guidance.extend(self._collect_prompt_guidance(contract, max_chars=max_guidance_chars))
        if not cards:
            return
        payload = {
            "skill_cards": cards,
            "skill_instruction": (
                "These skills are optional behavior-loop candidates. "
                "Use them only when they fit the task; otherwise answer normally."
            ),
        }
        if guidance:
            payload["skill_guidance"] = guidance
        prompt.append("info", payload)

    def _clear_request_skill_selectors(self):
        self.__request_skill_selectors = []

    def _collect_prompt_guidance(self, contract: SkillContract, *, max_chars: int) -> list[dict[str, Any]]:
        assets = _ensure_dict(contract.get("assets"))
        guidance_assets = []
        for asset in _ensure_list(assets.get("guidance_assets")):
            asset_data = _ensure_dict(asset)
            content = str(asset_data.get("content") or "")
            if not content.strip():
                continue
            trimmed = content[:max_chars]
            guidance_assets.append(
                {
                    "skill_id": str(contract.get("skill_id", "")),
                    "asset_id": str(asset_data.get("asset_id") or "guidance"),
                    "title": str(asset_data.get("title") or contract.get("card", {}).get("display_name", "")),
                    "content": trimmed,
                    "truncated": len(content) > len(trimmed),
                }
            )
            break
        return guidance_assets

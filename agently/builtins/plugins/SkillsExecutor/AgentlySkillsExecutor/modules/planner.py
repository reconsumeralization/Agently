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
from typing import Any, Literal, cast

from agently.types.data import (
    ExecutionStrategy,
    SkillCapabilityNeed,
    SkillContract,
    SkillExecutionPlan,
    SkillMode,
    SkillPlanRejection,
    SkillPlanSelection,
    SkillsPackRecord,
)
from agently.types.plugins import SkillsPlanningContext
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list, _ensure_string_list

from .registry import SkillRegistry


def _matches_selector(contract: SkillContract, selector: Any) -> bool:
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
        return _matches_source_selector(contract, selector)
    skill_id = selector.get("skill_id") or selector.get("id") or selector.get("name")
    if skill_id and str(skill_id) != contract.get("skill_id") and str(skill_id) != contract.get("card", {}).get("display_name"):
        return False
    return True


def _matches_source_selector(contract: SkillContract, selector: dict[str, Any]) -> bool:
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


def _normalize_skills_pack_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("skills_pack_id") or value.get("name") or value.get("id")
    return str(value or "").strip()


def _matches_skills_pack_selector(contract: SkillContract, selector: Any) -> bool:
    skills_pack_selector = _normalize_skills_pack_identifier(selector)
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


def _matches_record_selector(record: dict[str, Any], selector: Any) -> bool:
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


def _matches_record_pack_selector(record: dict[str, Any], selector: Any) -> bool:
    skills_pack_selector = _normalize_skills_pack_identifier(selector)
    if not skills_pack_selector:
        return False
    return skills_pack_selector in {
        str(record.get("skills_pack_id") or ""),
        str(record.get("skills_pack_name") or ""),
    }


class SkillPlanner:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    async def resolve(
        self,
        *,
        context: SkillsPlanningContext,
        task: str | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> SkillExecutionPlan:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        task_text = str(task or "")
        selectors = _ensure_list(skills)
        pack_selectors = _ensure_list(skills_packs)
        discovered, source_diagnostics = self._discover_source_selectors(selectors)
        records = self._records_for_scope(
            self.registry.list_skills(),
            selectors=selectors,
            pack_selectors=pack_selectors,
        )
        installed, diagnostics = self._inspect_records(records)
        diagnostics.extend(source_diagnostics)
        candidate_pool = [*installed, *discovered]
        rejected: list[SkillPlanRejection] = []
        rejected_packs: list[dict[str, Any]] = []
        selected_pack_records: dict[str, SkillsPackRecord] = {}

        if mode == "required":
            selected, rejected, rejected_packs = self._select_required(candidate_pool, selectors=selectors, pack_selectors=pack_selectors)
        else:
            candidates = self._model_decision_candidates(candidate_pool, task_text=task_text, selectors=selectors, pack_selectors=pack_selectors)
            selected = await self._select_model_ordered(context=context, task_text=task_text, candidates=candidates)

        selected, install_diagnostics = self._materialize_selected_sources(selected, selectors)
        selected = self._dedupe_contracts(selected)
        diagnostics.extend(install_diagnostics)

        for contract in selected:
            pack_record = self._skills_pack_record_for_contract(contract)
            if pack_record:
                selected_pack_records[str(pack_record.get("skills_pack_id", ""))] = pack_record

        selections = [
            self._to_selection(contract, required=mode == "required", selected_by="required" if mode == "required" else "model_decision")
            for contract in selected
        ]
        prompt_bindings = [self._prompt_binding_for_selection(selection) for selection in selections]
        prompt_bindings = [item for item in prompt_bindings if item]
        status = "resolved" if selections else "no_match"
        if mode == "required" and (rejected or rejected_packs):
            status = "blocked"

        execution_strategy = self._resolve_execution_strategy(selected)
        execution_stages = self._resolve_execution_stages(selected, execution_strategy)
        capability_needs = self._discover_capability_needs(selections)
        capability_needs = await self._discover_model_capability_needs(
            context=context,
            task_text=task_text,
            selections=selections,
            current_needs=capability_needs,
        )

        return SkillExecutionPlan({
            "plan_id": uuid.uuid4().hex,
            "mode": mode,
            "status": status,
            "task_summary": task_text,
            "selected_skills": selections,
            "selected_skills_packs": list(selected_pack_records.values()),
            "rejected_skills": rejected,
            "rejected_skills_packs": rejected_packs,
            "decision_cards": [_copy_public(selection.get("decision_card", {})) for selection in selections],
            "prompt_bindings": prompt_bindings,
            "resource_bindings": [_copy_public(selection.get("resource_index", {})) for selection in selections],
            "capability_needs": capability_needs,
            "expected_result_shape": _ensure_dict(semantic_outputs),
            "expected_result_format": output_format or context.get_setting("prompt.default_output_format", "json"),
            "capability_policy": {
                "auto_allow": any(bool(_ensure_dict(selector).get("auto_allow")) for selector in selectors if isinstance(selector, dict)),
            },
            "stage_model_keys": self._stage_model_keys(context),
            "execution_strategy": execution_strategy,
            "execution_stages": execution_stages,
            "diagnostics": diagnostics,
        })

    def _discover_source_selectors(self, selectors: list[Any]) -> tuple[list[SkillContract], list[dict[str, Any]]]:
        discovered: list[SkillContract] = []
        diagnostics: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, str, str]] = set()
        for selector in selectors:
            options = self.registry.source_selector_options(selector)
            if not options:
                continue
            key = (str(options.get("source") or ""), str(options.get("subpath") or ""), str(options.get("ref") or ""))
            if key in seen_sources:
                continue
            seen_sources.add(key)
            try:
                report = self.registry.discover_skills_pack(
                    options["source"],
                    name=options.get("name"),
                    skills_pack_id=options.get("skills_pack_id"),
                    fetch=bool(options.get("fetch", True)),
                    ref=options.get("ref"),
                    subpath=options.get("subpath"),
                    source_type=options.get("source_type"),
                    trust_level=options.get("trust_level"),
                    update=False,
                )
                contracts = [
                    cast(SkillContract, contract)
                    for contract in _ensure_list(report.get("contracts"))
                    if isinstance(contract, dict)
                ]
                discovered.extend(contracts)
                diagnostics.append({
                    "level": "info",
                    "code": "source_discovered",
                    "source": options["source"],
                    "source_subpath": str(options.get("subpath") or ""),
                    "skills_pack_id": str(report.get("skills_pack_id") or ""),
                    "skill_count": len(contracts),
                    "status": str(report.get("status") or ""),
                })
            except Exception as error:
                diagnostics.append({
                    "level": "warning",
                    "code": "source_discovery_failed",
                    "source": options["source"],
                    "source_subpath": str(options.get("subpath") or ""),
                    "message": str(error),
                })
        return discovered, diagnostics

    def _materialize_selected_sources(
        self,
        selected: list[SkillContract],
        selectors: list[Any],
    ) -> tuple[list[SkillContract], list[dict[str, Any]]]:
        diagnostics: list[dict[str, Any]] = []
        materialized: list[SkillContract] = []
        installed_ids = {str(record.get("skill_id") or "") for record in self.registry.list_skills()}
        source_selectors = [selector for selector in selectors if self.registry.source_selector_options(selector)]
        installed_sources: set[tuple[str, str, str]] = set()

        for contract in selected:
            skill_id = str(contract.get("skill_id") or "")
            if skill_id in installed_ids:
                materialized.append(contract)
                continue
            matched_selector = next(
                (selector for selector in source_selectors if isinstance(selector, dict) and _matches_source_selector(contract, selector)),
                None,
            )
            if matched_selector is None:
                matched_selector = next((selector for selector in source_selectors if not isinstance(selector, dict)), None)
            options = self.registry.source_selector_options(matched_selector)
            if not options:
                materialized.append(contract)
                continue
            key = (str(options.get("source") or ""), str(options.get("subpath") or ""), str(options.get("ref") or ""))
            if key not in installed_sources:
                try:
                    # Planning installs a not-yet-installed source-selected Skill so
                    # it can be used, but must not silently replace/upgrade Skills
                    # the host already installed (reproducibility). update=False
                    # installs the missing target and leaves installed Skills as-is.
                    report = self.registry.install_skills_pack(
                        options["source"],
                        name=options.get("name"),
                        skills_pack_id=options.get("skills_pack_id"),
                        fetch=bool(options.get("fetch", True)),
                        ref=options.get("ref"),
                        subpath=options.get("subpath"),
                        source_type=options.get("source_type"),
                        trust_level=options.get("trust_level"),
                        update=False,
                    )
                    diagnostics.append({
                        "level": "info",
                        "code": "source_installed",
                        "source": options["source"],
                        "source_subpath": str(options.get("subpath") or ""),
                        "skills_pack_id": str(report.get("skills_pack_id") or ""),
                        "installed_skills": list(report.get("installed_skills") or []),
                    })
                    installed_sources.add(key)
                    installed_ids.update(str(item) for item in report.get("installed_skills", []))
                except Exception as error:
                    diagnostics.append({
                        "level": "warning",
                        "code": "source_install_failed",
                        "source": options["source"],
                        "source_subpath": str(options.get("subpath") or ""),
                        "skill_id": skill_id,
                        "message": str(error),
                    })
                    materialized.append(contract)
                    continue
            try:
                materialized.append(self.registry.inspect_skills(skill_id))
            except Exception:
                materialized.append(contract)
        return materialized, diagnostics

    def _dedupe_contracts(self, contracts: list[SkillContract]) -> list[SkillContract]:
        """Keep selector order while avoiding installed+discovered duplicates."""
        deduped: list[SkillContract] = []
        seen: set[str] = set()
        for contract in contracts:
            skill_id = str(contract.get("skill_id") or "").strip()
            if not skill_id:
                deduped.append(contract)
                continue
            if skill_id in seen:
                continue
            seen.add(skill_id)
            deduped.append(contract)
        return deduped

    def _resolve_execution_strategy(
        self,
        selected: list[SkillContract],
    ) -> ExecutionStrategy:
        """Determine execution strategy without private Skill authoring fields."""
        del selected
        return "single_shot"

    def _resolve_execution_stages(
        self,
        selected: list[SkillContract],
        execution_strategy: str,
    ) -> list[dict[str, Any]]:
        """Standard Skills do not declare Agently execution stages."""
        del selected, execution_strategy
        return []

    def _discover_capability_needs(
        self,
        selections: list[SkillPlanSelection],
    ) -> list[SkillCapabilityNeed]:
        needs: list[SkillCapabilityNeed] = []
        seen: set[tuple[str, str, str, str]] = set()

        def add(
            *,
            skill_id: str,
            need: str,
            source: str,
            evidence: str,
            risk: str,
            confidence: float,
            resource_path: str = "",
            capability_config: dict[str, Any] | None = None,
        ) -> None:
            if not skill_id or not need:
                return
            clipped = " ".join(str(evidence or "").split())[:240]
            key = (skill_id, need, source, resource_path or clipped)
            if key in seen:
                return
            seen.add(key)
            needs.append(SkillCapabilityNeed({
                "skill_id": skill_id,
                "need": cast(Any, need),
                "source": cast(Any, source),
                "evidence": clipped,
                "risk": cast(Any, risk),
                "confidence": float(confidence),
                "resource_path": resource_path,
                "capability_config": _copy_public(capability_config or {}),
            }))

        for selection in selections:
            skill_id = str(selection.get("skill_id") or "")
            guidance = _ensure_dict(selection.get("guidance"))
            body = str(guidance.get("content") or "")
            resource_index = _ensure_dict(selection.get("resource_index"))
            metadata = _ensure_dict(selection.get("metadata"))
            frontmatter = _ensure_dict(metadata.get("frontmatter"))

            self._scan_capability_text(
                body,
                add=lambda **kwargs: add(skill_id=skill_id, source="body", **kwargs),
            )
            compatibility = frontmatter.get("compatibility")
            if compatibility:
                self._scan_capability_text(
                    compatibility,
                    add=lambda **kwargs: add(skill_id=skill_id, source="compatibility", **kwargs),
                    confidence=0.8,
                )
            public_metadata = frontmatter.get("metadata")
            if public_metadata:
                self._scan_capability_text(
                    public_metadata,
                    add=lambda **kwargs: add(skill_id=skill_id, source="metadata", **kwargs),
                    confidence=0.65,
                )
            for resource in _ensure_list(resource_index.get("resources")):
                if not isinstance(resource, dict):
                    continue
                resource_path = str(resource.get("path") or "")
                kind = str(resource.get("kind") or "")
                summary = str(resource.get("summary") or "")
                if kind == "script":
                    suffix = resource_path.rsplit(".", 1)[-1].lower() if "." in resource_path else ""
                    script_need = "python" if suffix == "py" else "script_run"
                    add(
                        skill_id=skill_id,
                        need=script_need,
                        source="resource_index",
                        evidence=f"Bundled script resource: { resource_path }",
                        risk="local_exec",
                        confidence=0.95,
                        resource_path=resource_path,
                    )
                    add(
                        skill_id=skill_id,
                        need="script_run",
                        source="resource_index",
                        evidence=f"Bundled script resource: { resource_path }",
                        risk="local_exec",
                        confidence=0.9,
                        resource_path=resource_path,
                    )
                elif kind in {"reference", "asset"} and summary:
                    self._scan_capability_text(
                        summary,
                        add=lambda resource_path=resource_path, **kwargs: add(
                            skill_id=skill_id,
                            source="resource_index",
                            resource_path=resource_path,
                            **kwargs,
                        ),
                        confidence=0.6,
                    )

        return needs

    async def _discover_model_capability_needs(
        self,
        *,
        context: SkillsPlanningContext,
        task_text: str,
        selections: list[SkillPlanSelection],
        current_needs: list[SkillCapabilityNeed],
    ) -> list[SkillCapabilityNeed]:
        enabled = bool(context.get_setting("skills.capability_discovery.model_assisted", False))
        if not enabled or not selections:
            return current_needs

        try:
            result = await context.async_request_model(
                prompt={
                    "task": task_text,
                    "capability_need_discovery": {
                        "purpose": "Infer host capabilities needed by selected standard Skills without granting permission.",
                        "selected_skills": [
                            {
                                "skill_id": selection.get("skill_id"),
                                "display_name": selection.get("display_name"),
                                "guidance": _copy_public(selection.get("guidance", {})),
                                "resource_index": _copy_public(selection.get("resource_index", {})),
                                "public_metadata": _copy_public(selection.get("metadata", {})),
                            }
                            for selection in selections
                        ],
                        "already_detected": [_copy_public(item) for item in current_needs],
                        "allowed_need_names": [
                            "web_search",
                            "web_browse",
                            "workspace_write",
                            "workspace_read",
                            "script_run",
                            "mcp",
                            "http_request",
                            "shell",
                            "python",
                            "unknown",
                        ],
                    },
                    "policy": [
                        "Return capability needs only; do not grant or authorize capabilities.",
                        "Use source=model_inference for every returned item.",
                        "Prefer unknown when the Skill implies an environment need that does not match a known need.",
                    ],
                },
                model_key=self._stage_model_key(context, "planner"),
                output_schema={
                    "capability_needs": [
                        (
                            {
                                "skill_id": (str, "Selected skill id.", True),
                                "need": (str, "One allowed need name.", True),
                                "evidence": (str, "Short evidence from the Skill or task.", True),
                                "risk": (str, "read_only, local_exec, filesystem_write, network, or external_side_effect.", True),
                                "confidence": (float, "Confidence from 0 to 1.", True),
                                "resource_path": (str, "Related resource path when applicable.", False),
                                "capability_config": (dict, "Public capability hints only, never authorization.", False),
                            },
                            "Model-inferred capability need.",
                            True,
                        )
                    ],
                },
                output_format="json",
                ensure_keys=["capability_needs"],
                max_retries=2,
            )
        except Exception:
            return current_needs

        model_needs: list[SkillCapabilityNeed] = []
        valid_needs = {
            "web_search",
            "web_browse",
            "workspace_write",
            "workspace_read",
            "script_run",
            "mcp",
            "http_request",
            "shell",
            "python",
            "unknown",
        }
        valid_risks = {
            "read_only",
            "local_exec",
            "filesystem_write",
            "network",
            "external_side_effect",
        }
        selected_ids = {str(selection.get("skill_id") or "") for selection in selections}
        for item in _ensure_list(_ensure_dict(result).get("capability_needs")):
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "")
            need = str(item.get("need") or "")
            if skill_id not in selected_ids or need not in valid_needs:
                continue
            risk = str(item.get("risk") or "")
            if risk not in valid_risks:
                risk = "external_side_effect" if need == "mcp" else "read_only"
            confidence_raw = item.get("confidence", 0.5)
            try:
                confidence = max(0.0, min(float(confidence_raw), 1.0))
            except Exception:
                confidence = 0.5
            model_needs.append(SkillCapabilityNeed({
                "skill_id": skill_id,
                "need": cast(Any, need),
                "source": "model_inference",
                "evidence": " ".join(str(item.get("evidence") or "").split())[:240],
                "risk": cast(Any, risk),
                "confidence": confidence,
                "resource_path": str(item.get("resource_path") or ""),
                "capability_config": _copy_public(_ensure_dict(item.get("capability_config"))),
            }))

        return self._merge_capability_needs([*current_needs, *model_needs])

    def _merge_capability_needs(
        self,
        needs: list[SkillCapabilityNeed],
    ) -> list[SkillCapabilityNeed]:
        merged: list[SkillCapabilityNeed] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in needs:
            key = (
                str(item.get("skill_id") or ""),
                str(item.get("need") or ""),
                str(item.get("source") or ""),
                str(item.get("resource_path") or item.get("evidence") or ""),
            )
            if not key[0] or not key[1] or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _scan_capability_text(
        self,
        value: Any,
        *,
        add: Any,
        confidence: float = 0.75,
    ) -> None:
        if isinstance(value, dict):
            text = " ".join(f"{ key } { val }" for key, val in value.items())
        elif isinstance(value, (list, tuple, set)):
            text = " ".join(str(item) for item in value)
        else:
            text = str(value or "")
        normalized = text.lower()
        if not normalized.strip():
            return

        def has_any(*terms: str) -> bool:
            return any(term in normalized for term in terms)

        if has_any("search", "web search", "public sources", "public context", "搜索", "公开资料", "公开信息"):
            add(need="web_search", evidence=self._capability_excerpt(text, ["search", "public", "搜索", "公开"]), risk="network", confidence=confidence)
        if has_any("browse", "open page", "web page", "relevant pages", "浏览", "网页"):
            add(need="web_browse", evidence=self._capability_excerpt(text, ["browse", "page", "浏览", "网页"]), risk="network", confidence=confidence)
        if has_any("http", "api request", "rest api", "fetch url", "network request"):
            add(need="http_request", evidence=self._capability_excerpt(text, ["http", "api", "fetch", "request"]), risk="network", confidence=confidence)
        if has_any("write file", "write the final", "save file", "markdown deliverable", "输出到文件", "写入文件", "保存文件"):
            add(need="workspace_write", evidence=self._capability_excerpt(text, ["write", "save", "file", "写入", "保存"]), risk="filesystem_write", confidence=confidence)
        if has_any("read file", "list files", "读取文件", "列出文件"):
            add(need="workspace_read", evidence=self._capability_excerpt(text, ["read", "file", "workspace", "读取"]), risk="read_only", confidence=confidence)
        if has_any("run script", "scripts/", "execute script", "运行脚本", "执行脚本"):
            add(need="script_run", evidence=self._capability_excerpt(text, ["script", "scripts/", "脚本"]), risk="local_exec", confidence=confidence)
        if has_any("mcp", "model context protocol"):
            add(need="mcp", evidence=self._capability_excerpt(text, ["mcp", "model context protocol"]), risk="external_side_effect", confidence=confidence)
        if has_any("bash", "shell", "command line", "cli command", "命令行", "终端命令"):
            add(need="shell", evidence=self._capability_excerpt(text, ["bash", "shell", "command", "命令"]), risk="local_exec", confidence=confidence)
        if has_any("python", ".py", "python script"):
            add(need="python", evidence=self._capability_excerpt(text, ["python", ".py"]), risk="local_exec", confidence=confidence)

    def _capability_excerpt(self, text: str, terms: list[str]) -> str:
        collapsed = " ".join(str(text or "").split())
        lowered = collapsed.lower()
        positions = [lowered.find(term.lower()) for term in terms if lowered.find(term.lower()) >= 0]
        if not positions:
            return collapsed[:180]
        start = max(min(positions) - 60, 0)
        return collapsed[start:start + 180]

    def _records_for_scope(
        self,
        records: list[dict[str, Any]],
        *,
        selectors: list[Any],
        pack_selectors: list[Any],
    ) -> list[dict[str, Any]]:
        if not selectors and not pack_selectors:
            return records
        return [
            record
            for record in records
            if any(_matches_record_selector(record, selector) for selector in selectors)
            or any(_matches_record_pack_selector(record, selector) for selector in pack_selectors)
        ]

    def _inspect_records(self, records: list[dict[str, Any]]) -> tuple[list[SkillContract], list[dict[str, Any]]]:
        installed: list[SkillContract] = []
        diagnostics: list[dict[str, Any]] = []
        for record in records:
            skill_id = str(record.get("skill_id") or "")
            if not skill_id:
                diagnostics.append({
                    "level": "warning",
                    "code": "skill_unreadable",
                    "skill_id": "",
                    "message": "Installed skill index entry is missing skill_id.",
                    "record": _copy_public(record),
                })
                continue
            try:
                installed.append(self.registry.inspect_skills(skill_id))
            except Exception as error:
                diagnostics.append({
                    "level": "warning",
                    "code": "skill_unreadable",
                    "skill_id": skill_id,
                    "message": str(error),
                    "record": _copy_public(record),
                })
        return installed, diagnostics

    def _select_required(
        self,
        installed: list[SkillContract],
        *,
        selectors: list[Any],
        pack_selectors: list[Any],
    ) -> tuple[list[SkillContract], list[SkillPlanRejection], list[dict[str, Any]]]:
        selected: list[SkillContract] = []
        rejected: list[SkillPlanRejection] = []
        rejected_packs: list[dict[str, Any]] = []
        for selector in selectors:
            matches = [contract for contract in installed if _matches_selector(contract, selector)]
            if not matches:
                rejected.append({
                    "skill_id": str(selector),
                    "reason_code": "required_not_found",
                    "reason": f"Required skill '{ selector }' is not installed.",
                })
                continue
            for contract in matches:
                if contract not in selected:
                    selected.append(contract)
        for selector in pack_selectors:
            matches = [contract for contract in installed if _matches_skills_pack_selector(contract, selector)]
            if not matches:
                pack_id = _normalize_skills_pack_identifier(selector)
                rejected_packs.append({
                    "skills_pack_id": pack_id,
                    "reason_code": "required_pack_not_found",
                    "reason": f"Required skills pack '{ pack_id }' had no installed standard Skills.",
                })
                continue
            for contract in matches:
                if contract not in selected:
                    selected.append(contract)
        return selected, rejected, rejected_packs

    def _model_decision_candidates(
        self,
        installed: list[SkillContract],
        *,
        task_text: str,
        selectors: list[Any],
        pack_selectors: list[Any],
    ) -> list[SkillContract]:
        if selectors or pack_selectors:
            candidates = [
                contract
                for contract in installed
                if any(_matches_selector(contract, selector) for selector in selectors)
                or any(_matches_skills_pack_selector(contract, selector) for selector in pack_selectors)
            ]
            return candidates
        task_lower = task_text.lower()
        activation_matches: list[SkillContract] = []
        for contract in installed:
            card = _ensure_dict(contract.get("card"))
            decision_card = _ensure_dict(contract.get("decision_card"))
            names = [str(contract.get("skill_id", "")), str(card.get("display_name", "")), str(decision_card.get("name", ""))]
            keywords = _ensure_string_list(_ensure_dict(card.get("activation_hints")).get("keywords"))
            keywords.extend(_ensure_string_list(decision_card.get("keywords")))
            if any(term and term.lower() in task_lower for term in [*names, *keywords]):
                activation_matches.append(contract)
        # Match only on author-declared names/keywords (the intended, language-
        # neutral activation signal). Description-word overlap is intentionally
        # not used: it matched on any common word in the Skill metadata language,
        # selecting every Skill for in-language tasks and none for others.
        return activation_matches

    async def _select_model_ordered(
        self,
        *,
        context: SkillsPlanningContext,
        task_text: str,
        candidates: list[SkillContract],
    ) -> list[SkillContract]:
        if len(candidates) <= 1:
            return candidates
        candidate_by_id = {str(contract.get("skill_id")): contract for contract in candidates}
        try:
            result = await context.async_request_model(
                prompt={
                    "task": task_text,
                    "candidate_skill_cards": [_copy_public(contract.get("decision_card", {})) for contract in candidates],
                    "routing_policy": [
                        "Select and order the Skills that should be used for this task.",
                        "Only choose from candidate_skill_cards.",
                        "Do not exclude a selected Skill because of Agently metadata; base relevance on SKILL.md name, description, and summary.",
                    ],
                },
                model_key=self._stage_model_key(context, "planner"),
                output_schema={
                    "selected_skill_ids": [(str, "Selected skill ids in execution order.", True)],
                    "reason": (str, "Concise route choice reason."),
                },
                output_format="json",
                ensure_keys=["selected_skill_ids"],
                max_retries=2,
            )
        except Exception:
            return candidates
        ordered = []
        for skill_id in _ensure_string_list(_ensure_dict(result).get("selected_skill_ids")):
            if skill_id in candidate_by_id and candidate_by_id[skill_id] not in ordered:
                ordered.append(candidate_by_id[skill_id])
        return ordered or candidates

    def _stage_model_key(self, context: SkillsPlanningContext, stage: str) -> str:
        configured = context.get_setting("skills.runtime.stage_model_keys", {}) or {}
        if isinstance(configured, dict):
            value = configured.get(stage)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if stage in {"planner", "research", "reason", "executor", "verifier", "reflector", "finalizer"}:
            return stage
        return "reason"

    def _stage_model_keys(self, context: SkillsPlanningContext) -> dict[str, str]:
        stages = ["planner", "research", "reason", "executor", "verifier", "reflector", "finalizer"]
        return {stage: self._stage_model_key(context, stage) for stage in stages}

    def _to_selection(self, contract: SkillContract, *, required: bool, selected_by: str) -> SkillPlanSelection:
        skill_id = str(contract.get("skill_id", ""))
        source = _ensure_dict(contract.get("source"))
        card = _ensure_dict(contract.get("card"))
        return SkillPlanSelection({
            "skill_id": skill_id,
            "skills_pack_id": str(source.get("skills_pack_id") or ""),
            "skills_pack_name": str(source.get("skills_pack_name") or ""),
            "version": str(contract.get("version", "")),
            "display_name": str(card.get("display_name") or skill_id),
            "reason": "required skill" if required else "selected by model decision",
            "selected_by": selected_by,
            "required": required,
            "card": _copy_public(card),
            "decision_card": _copy_public(contract.get("decision_card", {})),
            "guidance": _copy_public(contract.get("guidance", {})),
            "resource_index": _copy_public(contract.get("resource_index", {})),
            "source": _copy_public(contract.get("source", {})),
            "metadata": _copy_public(contract.get("metadata", {})),
        })

    def _prompt_binding_for_selection(self, selection: SkillPlanSelection) -> dict[str, Any]:
        guidance = _ensure_dict(selection.get("guidance"))
        content = str(guidance.get("content") or "").strip()
        if not content:
            return {}
        return {
            "skill_id": str(selection.get("skill_id", "")),
            "display_name": str(selection.get("display_name") or selection.get("skill_id", "")),
            "path": str(guidance.get("path") or "SKILL.md"),
            "content": content,
            "format": "markdown",
        }

    def _skills_pack_record_for_contract(self, contract: SkillContract) -> SkillsPackRecord | None:
        source = _ensure_dict(contract.get("source"))
        pack_id = str(source.get("skills_pack_id") or "")
        if not pack_id:
            return None
        try:
            return self.registry.inspect_skills_pack(pack_id)
        except Exception:
            return SkillsPackRecord({
                "skills_pack_id": pack_id,
                "name": str(source.get("skills_pack_name") or pack_id),
                "source": str(source.get("source") or ""),
                "source_type": str(source.get("source_type") or ""),
                "installed_skills": [str(contract.get("skill_id", ""))],
                "failed_skills": [],
                "status": "unknown",
            })

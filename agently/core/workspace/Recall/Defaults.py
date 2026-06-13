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
from typing import Any

from agently.types.data.workspace import (
    WorkspaceContextItem,
    WorkspaceContextOmission,
    WorkspaceContextPack,
    WorkspaceRecallPlan,
    WorkspaceRecordRef,
)


class RuleRecallPlanner:
    name = "rule"

    async def plan(
        self,
        *,
        workspace,
        goal: str,
        scope: dict[str, Any],
        budget: dict[str, Any],
        profile: str,
    ) -> WorkspaceRecallPlan:
        _ = workspace
        filters = {f"scope.{key}": value for key, value in scope.items()}
        return {
            "goal": goal,
            "profile": profile,
            "queries": [goal] if goal else [],
            "filters": filters,
            "scope": scope,
            "budget": budget,
            "diagnostics": {"planner": self.name, "model_assisted": False},
        }


class WorkspaceRetriever:
    name = "workspace"

    async def retrieve(
        self,
        *,
        workspace,
        plan: WorkspaceRecallPlan,
    ) -> list[WorkspaceRecordRef]:
        seen: set[str] = set()
        records: list[WorkspaceRecordRef] = []
        queries = plan.get("queries") or [None]
        for query in queries:
            for record in await workspace.search(query, filters=plan.get("filters") or {}):
                record_id = record["id"]
                if record_id not in seen:
                    seen.add(record_id)
                    records.append(record)
        if not records:
            for record in await workspace.search(None, filters=plan.get("filters") or {}):
                record_id = record["id"]
                if record_id not in seen:
                    seen.add(record_id)
                    records.append(record)
        policy = getattr(workspace.backend, "policy", None)
        if policy is not None:
            records = await policy.filter_records(records, purpose="prompt")
        return records


class DefaultContextBuilder:
    name = "default"

    async def build(
        self,
        *,
        workspace,
        goal: str,
        profile: str,
        records: list[WorkspaceRecordRef],
        budget: dict[str, Any],
        diagnostics: dict[str, Any],
    ) -> WorkspaceContextPack:
        char_budget = _char_budget(budget)
        used_chars = 0
        omitted_count = 0
        items: list[WorkspaceContextItem] = []
        item_budget = _item_budget(budget, char_budget)
        for record in records:
            value = await _safe_read(workspace, record)
            content = _context_content(record, value)
            excerpt = _excerpt(content, max_chars=min(item_budget, max(200, char_budget - used_chars)))
            item_chars = len(record.get("summary") or "") + len(excerpt or "")
            if used_chars + item_chars > char_budget and items:
                omitted_count += 1
                continue
            used_chars += item_chars
            items.append(
                {
                    "ref": record,
                    "kind": record.get("kind"),
                    "summary": record.get("summary") or "",
                    "content": excerpt,
                    "use": "evidence",
                }
            )
        omitted: list[WorkspaceContextOmission] = (
            [{"reason": "budget", "count": omitted_count}] if omitted_count else []
        )
        return {
            "goal": goal,
            "profile": profile,
            "items": items,
            "omitted": omitted,
            "diagnostics": {
                **diagnostics,
                "builder": self.name,
                "candidate_count": len(records),
                "used_chars": used_chars,
                "char_budget": char_budget,
            },
        }


def _char_budget(budget: dict[str, Any]) -> int:
    if isinstance(budget.get("chars"), int):
        return max(1, int(budget["chars"]))
    if isinstance(budget.get("tokens"), int):
        return max(1, int(budget["tokens"]) * 4)
    return 12000


def _item_budget(budget: dict[str, Any], char_budget: int) -> int:
    configured = budget.get("item_chars")
    if isinstance(configured, int):
        return max(200, min(char_budget, configured))
    return max(1200, min(char_budget, 2400))


async def _safe_read(workspace, record: WorkspaceRecordRef) -> Any:
    try:
        return await workspace.get(record)
    except Exception:
        return None


def _context_content(record: WorkspaceRecordRef, value: Any) -> str | None:
    if value is None:
        return None
    parsed = _parse_json_like(value)
    compacted = _compact_record_content(record, parsed)
    if isinstance(compacted, (dict, list)):
        return json.dumps(compacted, ensure_ascii=False, default=str)
    return str(compacted)


def _parse_json_like(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def _compact_record_content(record: WorkspaceRecordRef, value: Any) -> Any:
    kind = str(record.get("kind") or "")
    if kind == "agent_task_observation" and isinstance(value, dict):
        execution_meta = value.get("execution_meta", {})
        logs = execution_meta.get("logs", {}) if isinstance(execution_meta, dict) else {}
        return {
            "iteration": value.get("iteration"),
            "action_evidence": _compact_action_logs(logs.get("action_logs", []) if isinstance(logs, dict) else []),
            "execution_result": value.get("execution_result"),
            "plan": _compact_plan(value.get("plan")),
            "execution_summary": _compact_execution_meta(execution_meta),
        }
    if kind == "agent_task_verification" and isinstance(value, dict):
        return {
            "iteration": value.get("iteration"),
            "verification": value.get("verification"),
        }
    if kind == "agent_task_decision" and isinstance(value, dict):
        return {
            "iteration": value.get("iteration"),
            "plan": _compact_plan(value.get("plan")),
            "context_item_count": value.get("context_item_count"),
        }
    return value


def _compact_plan(plan: Any) -> Any:
    if not isinstance(plan, dict):
        return plan
    return {
        key: plan.get(key)
        for key in (
            "execution_shape",
            "effective_execution_shape",
            "step_instruction",
            "expected_evidence",
            "rationale",
            "step_scope",
        )
        if key in plan
    }


def _compact_execution_meta(execution_meta: Any) -> dict[str, Any]:
    if not isinstance(execution_meta, dict):
        return {}
    return {
        "status": execution_meta.get("status"),
        "route": execution_meta.get("route"),
        "close_snapshot": execution_meta.get("close_snapshot"),
    }


def _compact_action_logs(action_logs: Any) -> list[dict[str, Any]]:
    entries: list[Any]
    if isinstance(action_logs, dict):
        entries = [
            {"action_id": action_id, **record} if isinstance(record, dict) else {"action_id": action_id, "status": record}
            for action_id, record in action_logs.items()
        ]
    elif isinstance(action_logs, list):
        entries = action_logs
    else:
        entries = []
    compacted: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or item.get("id") or item.get("name") or "").strip()
        data = item.get("data", item.get("result"))
        compacted.append(
            {
                "action_id": action_id,
                "status": item.get("status"),
                "success": item.get("success"),
                "data": _compact_action_data(data),
            }
        )
    return compacted


def _compact_action_data(data: Any) -> Any:
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        sources = [source for source in data["sources"] if isinstance(source, dict)]
        return {
            **{key: value for key, value in data.items() if key != "sources"},
            "source_index": [
                {
                    "path": source.get("path"),
                    "status": source.get("status"),
                }
                for source in sources
            ],
            "sources": [
                {
                    "path": source.get("path"),
                    "status": source.get("status"),
                    "excerpt": _truncate_text(source.get("excerpt"), 220),
                }
                for source in sources
            ],
        }
    return data


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 12)].rstrip() + "\n[truncated]"


def _excerpt(content: str | None, *, max_chars: int) -> str | None:
    if content is None:
        return None
    if len(content) <= max_chars:
        return content
    return content[: max(0, max_chars - 12)].rstrip() + "\n[truncated]"

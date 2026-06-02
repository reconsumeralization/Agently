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
        for record in records:
            content = await _safe_read(workspace, record)
            excerpt = _excerpt(content, max_chars=min(1200, max(200, char_budget - used_chars)))
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


async def _safe_read(workspace, record: WorkspaceRecordRef) -> str | None:
    try:
        value = await workspace.get(record)
    except Exception:
        return None
    return str(value)


def _excerpt(content: str | None, *, max_chars: int) -> str | None:
    if content is None:
        return None
    if len(content) <= max_chars:
        return content
    return content[: max(0, max_chars - 12)].rstrip() + "\n[truncated]"

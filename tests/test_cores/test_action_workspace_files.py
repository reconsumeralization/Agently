# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid

import pytest

from agently import Agently
from agently.types.data import PolicyApprovalRequest


def _register_handler(result: Any, requests: list[PolicyApprovalRequest]) -> str:
    name = f"workspace-test-{uuid.uuid4().hex}"

    async def handler(request: PolicyApprovalRequest) -> Any:
        requests.append(request)
        return result

    Agently.policy_approval.register_handler(name, handler)
    return name


@pytest.mark.asyncio
async def test_default_workspace_actions_create_new_product_in_fallback_without_approval(
    tmp_path: Path,
) -> None:
    requests: list[PolicyApprovalRequest] = []
    handler = _register_handler(False, requests)
    agent = Agently.create_agent("workspace-fallback-action").use_workspace(tmp_path)
    agent.configure_policy_approval(handler=handler)
    agent.enable_workspace_file_actions()

    result = await agent.action.async_execute_action(
        "write_file",
        {"path": "deliverables/report.md", "content": "approved product"},
    )

    assert result.get("status") == "success"
    actual_path = str(result.get("data", {}).get("path"))
    assert actual_path.startswith(".agently/files/")
    assert actual_path.endswith("/deliverables/report.md")
    assert (tmp_path / actual_path).read_text(encoding="utf-8") == "approved product"
    assert requests == []
    assert not (tmp_path / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        ({"status": "pending", "reason": "await host"}, "approval_required"),
        ({"status": "denied", "reason": "host denied"}, "blocked"),
    ],
)
async def test_external_workspace_edit_waits_for_or_obeys_policy_approval(
    tmp_path: Path,
    decision: dict[str, Any],
    expected_status: str,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("before\n", encoding="utf-8")
    requests: list[PolicyApprovalRequest] = []
    handler = _register_handler(decision, requests)
    agent = Agently.create_agent(f"workspace-{expected_status}").use_workspace(tmp_path)
    agent.configure_policy_approval(handler=handler)
    agent.enable_workspace_file_actions()

    result = await agent.action.async_execute_action(
        "write_file",
        {
            "path": "src/app.py",
            "content": "after\n",
            "external_write_granted": True,
        },
        source_protocol="native_tool_calls",
    )

    assert result.get("status") == expected_status
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / ".agently").exists()
    assert len(requests) == 1
    request = requests[0]
    assert request.get("source") == "action"
    assert request.get("risk") == "filesystem_write"
    payload = request.get("payload")
    assert payload is not None
    facts = payload["workspace_mutation"]
    assert facts["operation"] == "write_file"
    assert facts["path"] == "src/app.py"
    assert facts["canonical_path"] == str(target.resolve())
    assert facts["workspace_id"] == agent.workspace.workspace_id
    assert "external_write_granted" not in payload["action_call"]["action_input"]


@pytest.mark.asyncio
async def test_approved_external_workspace_edit_uses_external_root_without_fallback(
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("before\n", encoding="utf-8")
    requests: list[PolicyApprovalRequest] = []
    handler = _register_handler(True, requests)
    agent = Agently.create_agent("workspace-approved").use_workspace(tmp_path)
    agent.configure_policy_approval(handler=handler)
    agent.enable_workspace_file_actions()

    result = await agent.action.async_execute_action(
        "write_file",
        {"path": "src/app.py", "content": "after\n"},
    )

    assert result.get("status") == "success"
    assert result.get("data", {}).get("path") == "src/app.py"
    assert target.read_text(encoding="utf-8") == "after\n"
    assert len(requests) == 1
    assert not (tmp_path / ".agently").exists()

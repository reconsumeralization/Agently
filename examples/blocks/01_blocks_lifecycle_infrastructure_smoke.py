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

"""Blocks lifecycle infrastructure smoke.

This example is intentionally scoped to the Blocks compiler/runtime substrate,
not to model-owned planning or final business acceptance. The mocked ticket
lookup is the external business-system boundary; Skill activation, Blocks
compilation, TriggerFlow execution, Workspace evidence, ResultAdapter, and
EvidenceEnvelope are real framework paths.

Run this as a quick substrate check before business examples. The script prints
the observed evidence fields from the actual run; those values are process
evidence, not a canned natural-language answer.
"""

from __future__ import annotations

import asyncio

from agently import Agently
from agently.core.application.SkillsExecutor import DictSkillSource, SkillCapabilityAdapter


def build_skill_adapter() -> SkillCapabilityAdapter:
    return SkillCapabilityAdapter(
        DictSkillSource(
            {
                "incident-review": {
                    "skill_id": "incident-review",
                    "card": {
                        "name": "Incident Review",
                        "description": "Guidance for incident ticket evidence review.",
                    },
                    "guidance": {
                        "body": (
                            "Use ticket lookup records, write Workspace evidence, "
                            "and validate readback before reporting status."
                        )
                    },
                    "resource_index": {
                        "references/evidence.md": {
                            "kind": "reference",
                            "summary": "Evidence and readback checklist",
                            "size": 64,
                        }
                    },
                }
            }
        )
    )


async def main() -> None:
    workspace = Agently.create_workspace()
    graph = Agently.blocks.compile(
        {
            "plan_id": "blocks-incident-review",
            "plan_blocks": [
                {
                    "id": "activate",
                    "plan_block_id": "skill_activation",
                    "kind": "skill_activation",
                    "bound_inputs": {
                        "skill_id": "incident-review",
                        "task": "review ticket INC-42 evidence",
                    },
                },
                {
                    "id": "lookup_ticket",
                    "plan_block_id": "action_call",
                    "kind": "action_call",
                    "runtime_preferences": {"handler": "ticket_lookup"},
                },
                {
                    "id": "store_evidence",
                    "plan_block_id": "workspace_operation",
                    "kind": "workspace_operation",
                    "bound_inputs": {
                        "operation": "ingest",
                        "collection": "observations",
                        "kind": "incident_ticket_evidence",
                        "summary": "Incident ticket lookup evidence",
                    },
                },
                {
                    "id": "validate",
                    "plan_block_id": "validation",
                    "kind": "validation",
                    "evidence_contract": {"requires_workspace_ref": True},
                },
            ],
            "edges": [
                {"from": "activate", "to": "lookup_ticket"},
                {"from": "lookup_ticket", "to": "store_evidence"},
                {"from": "store_evidence", "to": "validate"},
            ],
        }
    )

    async def ticket_lookup(context):
        return {
            "system": "mock_ticket_system",
            "ticket_id": "INC-42",
            "status": "open",
            "customer": "ACME",
            "readback": "lookup confirmed by mock_ticket_system",
            "input_seen": bool(context["input"]),
        }

    flow = Agently.blocks.bind_runtime(graph)
    execution = flow.create_execution(
        auto_close=False,
        workspace=workspace,
        runtime_resources={
            "skills.capability_adapter": build_skill_adapter(),
            "blocks.handlers": {"ticket_lookup": ticket_lookup},
        },
    )
    await execution.async_start({"ticket_id": "INC-42"})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    result = Agently.blocks.map_result(graph, snapshot)
    action_output = evidence.action_evidence[0]["output"]
    validation_output = result["semantic_outputs"]["validate:validation"]

    print(f"plan_id={evidence.plan_id}")
    print(f"skill_evidence_kind={evidence.skill_evidence[0]['evidence_kind']}")
    print(f"skill_proves_side_effect={evidence.skill_evidence[0]['proves_side_effect']}")
    print(f"ticket_status={action_output['status']}")
    print(f"workspace_ref_count={len(evidence.workspace_refs)}")
    print(f"validation_ok={validation_output['ok']}")


if __name__ == "__main__":
    asyncio.run(main())

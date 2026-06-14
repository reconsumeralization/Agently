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

from agently.core.application.SkillsExecutor import (
    DictSkillSource,
    RegistrySkillSource,
    SkillCapabilityAdapter,
)


class _Registry:
    def __init__(self):
        self.inspect_calls: list[str] = []

    def list_skills(self):
        return [
            {
                "skill_id": "diagram",
                "card": {"name": "Diagram", "description": "Draw diagrams."},
                "guidance": {"body": "This must not be loaded during discovery."},
            }
        ]

    def inspect_skills(self, skill_id: str):
        self.inspect_calls.append(skill_id)
        return {
            "skill_id": skill_id,
            "card": {"name": "Diagram", "description": "Draw diagrams."},
            "guidance": {"body": "Read files and write file outputs when approved."},
            "resource_index": {"references/style.md": {"kind": "reference", "size": 24}},
        }


def test_registry_skill_source_uses_inspect_only_for_activation():
    registry = _Registry()
    adapter = SkillCapabilityAdapter(RegistrySkillSource(registry))

    cards = adapter.discover()

    assert registry.inspect_calls == []
    assert cards[0]["skill_id"] == "diagram"

    activation = adapter.activate("diagram", task="write diagram file")

    assert registry.inspect_calls == ["diagram"]
    assert activation.loaded_guidance_refs == ("diagram:SKILL.md",)
    assert any(need["need"] == "workspace_write" for need in activation.capability_needs)


def test_skill_discovery_is_metadata_only():
    adapter = SkillCapabilityAdapter(
        DictSkillSource(
            {
                "webapp-testing": {
                    "skill_id": "webapp-testing",
                    "card": {
                        "skill_id": "webapp-testing",
                        "name": "Web App Testing",
                        "description": "Browser QA guidance",
                    },
                    "guidance": {"body": "Use browser screenshots."},
                    "resource_index": {
                        "scripts/visual_check.py": {"kind": "script", "summary": "Visual check", "size": 32}
                    },
                    "source": {"path": "/skills/webapp-testing"},
                }
            }
        )
    )

    cards = adapter.discover()

    assert cards == [
        {
            "skill_id": "webapp-testing",
            "name": "Web App Testing",
            "description": "Browser QA guidance",
            "path": "/skills/webapp-testing",
            "source": {"path": "/skills/webapp-testing"},
            "trust_level": None,
        }
    ]
    assert "guidance" not in cards[0]
    assert "resource_index" not in cards[0]


def test_skill_activation_recommends_plan_blocks_without_granting_capability():
    adapter = SkillCapabilityAdapter(
        DictSkillSource(
            {
                "webapp-testing": {
                    "skill_id": "webapp-testing",
                    "card": {
                        "skill_id": "webapp-testing",
                        "name": "Web App Testing",
                        "description": "Browser QA guidance",
                    },
                    "guidance": {
                        "body": "Use browser screenshots and run scripts only after host approval."
                    },
                    "resource_index": {
                        "scripts/visual_check.py": {"kind": "script", "summary": "Visual check", "size": 32}
                    },
                    "source": {"path": "/skills/webapp-testing"},
                }
            }
        )
    )

    activation = adapter.activate("webapp-testing", task="capture browser screenshot")

    assert activation.loaded_guidance_refs == ("webapp-testing:SKILL.md",)
    assert any(need["need"] == "web_browse" for need in activation.capability_needs)
    assert any(need["need"] == "script_run" for need in activation.capability_needs)
    assert activation.selected_resource_refs == ("scripts/visual_check.py",)
    assert all(candidate["grants"] is False for candidate in activation.action_candidate_specs)
    assert {
        recommendation["plan_block_id"] for recommendation in activation.plan_block_recommendations
    } >= {"action_call", "script_action", "validation"}

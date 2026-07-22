from __future__ import annotations

from pathlib import Path

import pytest

from agently import Agently
from agently.builtins.agent_extensions.SkillsExtension.SkillActionBinder import (
    SkillActionBinder,
)
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.application.SkillLibrary import SkillBinding, SkillLibrary
from agently.types.data import SkillScriptAuthorization


def _write_skill(root: Path, *, trust_marker: str = "ok") -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: Script Bridge\ndescription: Validate an artifact.\n---\n\n"
        "Use the validation script.",
        encoding="utf-8",
    )
    (root / "scripts" / "check.py").write_text(
        "from pathlib import Path\n"
        f"Path('output/validated.txt').write_text('{trust_marker}')\n",
        encoding="utf-8",
    )
    return root


class _ActionRegistryRecorder:
    def __init__(self) -> None:
        self.registered: list[dict[str, object]] = []

    def register_action(self, **kwargs: object) -> None:
        self.registered.append(dict(kwargs))


class _Execution:
    def __init__(self, workspace: TaskWorkspace) -> None:
        self.id = "execution-1"
        self.task_workspace = workspace
        self.action = _ActionRegistryRecorder()
        self.local_action_ids: list[str] = []


def test_binder_registers_exact_revision_script_as_ordinary_code_action(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    binding = SkillBinding.create(package, task_id="execution-1", mode="required")
    execution = _Execution(
        TaskWorkspace(tmp_path / "workspace", mode="read_only", execution_id="execution-1")
    )

    bound = SkillActionBinder(library).bind(
        execution=execution,
        skill_binding=binding,
        resource_path="scripts/check.py",
        authorization=SkillScriptAuthorization(auto_allow=True),
    )

    assert bound.action_id in execution.local_action_ids
    spec = execution.action.registered[0]
    assert spec["side_effect_level"] == "exec"
    assert spec["sandbox_required"] is True
    assert spec["execution_resources"] == [
        {
            "kind": "code_execution",
            "resource_key": bound.action_id,
            "scope": "action_call",
            "provider_candidates": ["docker"],
                "required_capabilities": {
                    "language": "python",
                    "isolation": {
                        "process_contained": True,
                        "host_filesystem_restricted": True,
                        "privilege_escalation_blocked": True,
                        "syscalls_restricted": True,
                    },
                    "workspace_access_mode": "snapshot",
                },
            "workspace_access": {
                "mode": "snapshot",
                "expected_outputs": [],
            },
        }
    ]
    meta = spec["meta"]
    assert isinstance(meta, dict)
    assert meta["skill_revision_ref"] == package.revision_ref
    assert meta["skill_resource_path"] == "scripts/check.py"
    assert meta["skill_resource_sha256"] == package.resource("scripts/check.py").sha256
    assert "installed_path" not in meta


def test_binder_rejects_untrusted_or_unauthorized_script(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="untrusted")
    binding = SkillBinding(
        binding_id="forged-untrusted-binding",
        task_id="execution-1",
        canonical_ref=package.canonical_ref,
        revision=package.revision,
        revision_ref=package.revision_ref,
        mode="required",
    )
    execution = _Execution(
        TaskWorkspace(tmp_path / "workspace", mode="read_only", execution_id="execution-1")
    )
    binder = SkillActionBinder(library)

    with pytest.raises(PermissionError, match="trusted"):
        binder.bind(
            execution=execution,
            skill_binding=binding,
            resource_path="scripts/check.py",
            authorization=SkillScriptAuthorization(auto_allow=True),
        )

    trusted = library.install(
        _write_skill(tmp_path / "trusted", trust_marker="trusted"),
        trust="trusted",
    )
    trusted_binding = SkillBinding.create(
        trusted,
        task_id="execution-1",
        mode="required",
    )
    with pytest.raises(PermissionError, match="authorization|auto_allow"):
        binder.bind(
            execution=execution,
            skill_binding=trusted_binding,
            resource_path="scripts/check.py",
            authorization=SkillScriptAuthorization(auto_allow=False),
        )


@pytest.mark.asyncio
async def test_agent_exposes_explicit_script_binding_after_exact_skill_binding(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-application-api").use_task_workspace(
        tmp_path / "workspace",
        mode="read_only",
    )
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    execution = agent.create_execution().require_skills([package.revision_ref])
    await execution.async_prepare_task_context()

    bound = agent.bind_skill_script_action(
        execution,
        binding_id=execution.skill_bindings[0].binding_id,
        resource_path="scripts/check.py",
        authorization=SkillScriptAuthorization(
            auto_allow=True,
            expected_outputs=("output/validated.txt",),
        ),
    )

    assert bound.action_id in execution.local_action_ids
    executor = execution.action.action_registry.get_executor(bound.action_id)
    assert executor is not None
    assert executor.skill_library is agent.skill_library


@pytest.mark.asyncio
async def test_bound_skill_script_executes_through_workspace_and_docker(
    tmp_path: Path,
) -> None:
    availability = DockerExecutionResource().inspect_availability()
    if not availability["available"]:
        pytest.skip(f"Docker is unavailable: {availability}")

    skill_root = _write_skill(tmp_path / "skill", trust_marker="actual-docker")
    (skill_root / "scripts" / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('../output/validated.txt').write_text('actual-docker')\n"
        "print('skill-bridge-actual')\n",
        encoding="utf-8",
    )
    agent = Agently.create_agent("skill-script-actual").use_task_workspace(
        tmp_path / "workspace",
        mode="read_only",
    )
    package = agent.skill_library.install(skill_root, trust="trusted")
    execution = agent.create_execution().require_skills([package.revision_ref])
    await execution.async_prepare_task_context()
    bound = agent.bind_skill_script_action(
        execution,
        binding_id=execution.skill_bindings[0].binding_id,
        resource_path="scripts/check.py",
        authorization=SkillScriptAuthorization(
            auto_allow=True,
            expected_outputs=("output/validated.txt",),
        ),
    )

    result = await agent.action.async_execute_action(bound.action_id, {})

    assert result.get("status") == "success"
    result_data = result.get("data")
    artifacts = result.get("artifacts")
    assert isinstance(result_data, dict)
    assert isinstance(artifacts, list) and len(artifacts) == 1
    assert result_data["stdout"] == "skill-bridge-actual\n"
    published_path = artifacts[0].get("path", "")
    assert published_path.endswith("/output/validated.txt")
    assert published_path.startswith(".agently/files/")
    readback = await execution.task_workspace.read_file(published_path)
    assert readback.content == "actual-docker"
    assert result_data["meta"]["provider_contract"] == (
        "workspace_code_execution_v1"
    )

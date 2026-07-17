from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.application.SkillLibrary import (
    SkillBinding,
    SkillBindingError,
    SkillContextSource,
    SkillLibrary,
)
from agently.core.context import ContextSelection, TaskContext
from agently.types.data import ContextCandidate, ContextConsumer, ContextReadIntent


def _write_skill(root: Path, *, body: str = "Always verify the report before delivery.") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("references", "examples", "assets", "scripts"):
        (root / name).mkdir(exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: Report Verification\n"
        "description: Produce verified reports.\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    (root / "references" / "criteria.md").write_text(
        "Check accuracy, citations, and completeness.",
        encoding="utf-8",
    )
    (root / "examples" / "accepted.md").write_text("# Accepted", encoding="utf-8")
    (root / "assets" / "report.txt").write_text("REPORT TEMPLATE", encoding="utf-8")
    (root / "scripts" / "validate.py").write_text("print('validate')", encoding="utf-8")
    return root


class SelectRefs:
    async def async_select(
        self,
        *,
        intent: ContextReadIntent,
        candidates: list[ContextCandidate] | tuple[ContextCandidate, ...],
        consumer: ContextConsumer,
        phase: str,
    ) -> ContextSelection:
        return ContextSelection(
            selected_keys=tuple(
                item.block_key
                for item in candidates
                if item.source_ref.endswith("references/criteria.md")
            )
        )


class FailIfSelected:
    async def async_select(self, **_kwargs) -> ContextSelection:
        raise AssertionError("A Skill without optional resources needs no semantic selection.")


@pytest.mark.asyncio
async def test_skill_context_source_exposes_typed_progressive_candidates(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    binding = SkillBinding.create(package, task_id="task-1", mode="required")
    source = SkillContextSource(library, bindings=(binding,))

    candidates = await source.async_list_candidates(
        ContextReadIntent(query="Create the report"),
        limit=100,
    )
    by_path = {item.metadata.get("resource_path", "core"): item for item in candidates}

    assert source.source_id == "skill-context:task-1"
    assert package.revision in source.source_revision
    assert by_path["SKILL.md"].role == "instruction"
    assert by_path["SKILL.md"].required is True
    assert by_path["SKILL.md"].completeness == "complete"
    assert by_path["resource-index"].role == "index"
    assert by_path["references/criteria.md"].role == "information"
    assert by_path["references/criteria.md"].completeness == "ref_only"
    assert by_path["examples/accepted.md"].role == "example"
    assert by_path["assets/report.txt"].role == "artifact"
    assert by_path["scripts/validate.py"].role == "capability"
    assert by_path["scripts/validate.py"].completeness == "ref_only"


@pytest.mark.asyncio
async def test_skill_without_resources_does_not_offer_an_empty_optional_index(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "minimal-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: Minimal Skill\n"
        "description: One complete instruction with no extra resources.\n"
        "---\n\n"
        "Apply the minimal procedure.",
        encoding="utf-8",
    )
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(skill_root, trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(
            SkillBinding.create(installed, task_id="minimal-task", mode="required"),
        ),
    )
    candidates = await source.async_list_candidates(
        ContextReadIntent(query="Apply the procedure"),
        limit=100,
    )
    context = TaskContext("minimal-task")
    context.attach(source, required=True)
    package = await context.reader(
        consumer="direct-model",
        semantic_selector=FailIfSelected(),
    ).async_read("Apply the procedure")

    assert [item.metadata["resource_path"] for item in candidates] == ["SKILL.md"]
    assert [block.content for block in package.blocks] == ["Apply the minimal procedure."]
    assert package.diagnostics == ()


@pytest.mark.asyncio
async def test_skill_core_is_always_delivered_and_reference_body_is_progressive(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="task-1", mode="required"),),
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skills", required=True)
    reader = context.reader(
        consumer="worker",
        phase="execution",
        semantic_selector=SelectRefs(),
    )

    package_result = await reader.async_read("Create and verify the report")

    assert [block.role for block in package_result.blocks[:2]] == ["instruction", "information"]
    assert package_result.blocks[0].content == "Always verify the report before delivery."
    reference = next(
        block for block in package_result.blocks if block.source_ref.endswith("references/criteria.md")
    )
    assert reference.content == "Check accuracy, citations, and completeness."
    assert reference.completeness == "complete"


@pytest.mark.asyncio
async def test_skill_domain_identity_survives_task_context_source_binding(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    skill_binding = SkillBinding.create(
        package,
        task_id="task-1",
        mode="model_decision",
        binding_id="skill_binding:task-1:1",
    )
    context = TaskContext("task-1")
    context.attach(
        SkillContextSource(library, bindings=(skill_binding,)),
        binding_id="task_context_source_binding:skills",
    )

    result = await context.reader(consumer="planner").async_read("Use the Skill")
    instruction = next(block for block in result.blocks if block.role == "instruction")

    # Generic Context identity remains the TaskContext attachment identity.
    assert instruction.binding_id == "task_context_source_binding:skills"
    # Skill-domain identity and activation policy are explicit metadata; callers
    # must never infer either value from the generic binding or required flag.
    assert instruction.metadata["skill_binding_id"] == "skill_binding:task-1:1"
    assert instruction.metadata["skill_mode"] == "model_decision"
    assert instruction.required is True


@pytest.mark.asyncio
async def test_skill_resources_remain_cold_until_selected(tmp_path: Path, monkeypatch) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="task-1", mode="required"),),
    )
    reads: list[str] = []
    original = library.read_resource

    def recording_read(skill, path, **kwargs):
        reads.append(path)
        return original(skill, path, **kwargs)

    monkeypatch.setattr(library, "read_resource", recording_read)
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skills")
    reader = context.reader(consumer="planner")

    result = await reader.async_read("Inspect available capabilities")

    assert [block.role for block in result.blocks] == ["instruction"]
    assert reads == []
    assert any(item.code == "context.semantic_selector_unavailable" for item in result.diagnostics)


@pytest.mark.asyncio
async def test_skill_script_read_returns_descriptor_not_executable_object(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="task-1", mode="required"),),
    )
    candidates = await source.async_list_candidates(ContextReadIntent(query="Validate"), limit=100)
    script = next(item for item in candidates if item.role == "capability")

    block = await source.async_read(script, max_chars=1000)

    assert block.role == "capability"
    assert block.completeness == "ref_only"
    assert block.content == {
        "descriptor_kind": "skill_script",
        "revision_ref": package.revision_ref,
        "resource_path": "scripts/validate.py",
        "sha256": package.resource("scripts/validate.py").sha256,
        "size": package.resource("scripts/validate.py").size,
    }
    assert not callable(block.content)
    assert "action" not in block.metadata
    assert "permission" not in block.metadata


def test_untrusted_skill_cannot_create_active_instruction_binding(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "skill"),
        scope="project",
        trust="untrusted",
    )

    with pytest.raises(SkillBindingError, match="trusted"):
        SkillBinding.create(package, task_id="task-1", mode="required")


def test_skill_binding_pins_exact_revision_across_library_updates(tmp_path: Path) -> None:
    source_root = _write_skill(tmp_path / "skill")
    library = SkillLibrary(tmp_path / "library")
    first = library.install(source_root, trust="trusted")
    binding = SkillBinding.create(first, task_id="task-1", mode="required")
    _write_skill(source_root, body="Revised instructions")
    latest = library.install(source_root, trust="trusted")
    context_source = SkillContextSource(library, bindings=(binding,))

    assert latest.revision != first.revision
    assert context_source.bindings[0].revision_ref == first.revision_ref
    assert context_source.packages[0].instruction_body == first.instruction_body

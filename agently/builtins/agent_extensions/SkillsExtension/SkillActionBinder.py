# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from agently.builtins.plugins.ActionExecutor import CodeExecutionActionExecutor
from agently.core.application.SkillLibrary import SkillBinding, SkillLibrary
from agently.types.data import (
    SkillScriptAuthorization,
    required_code_execution_isolation,
)


@dataclass(frozen=True)
class BoundSkillAction:
    action_id: str
    skill_binding_id: str
    skill_revision_ref: str
    resource_path: str


class SkillActionBinder:
    """Projects an authorized exact Skill script revision as an ordinary Action."""

    _LANGUAGES = {
        ".py": "python",
        ".js": "nodejs",
        ".mjs": "nodejs",
        ".cjs": "nodejs",
        ".go": "go",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
    }

    def __init__(self, library: SkillLibrary) -> None:
        self.library = library

    @classmethod
    def _language(cls, resource_path: str) -> str:
        suffix = PurePosixPath(resource_path).suffix.casefold()
        language = cls._LANGUAGES.get(suffix)
        if language is None:
            raise ValueError(f"Skill script language is unsupported: {suffix or resource_path!r}")
        return language

    @staticmethod
    def _action_id(binding: SkillBinding, resource_path: str) -> str:
        stem = re.sub(
            r"[^a-z0-9]+",
            "_",
            PurePosixPath(resource_path).stem.casefold(),
        ).strip("_") or "script"
        digest = hashlib.sha256(
            f"{binding.revision_ref}\0{resource_path}".encode("utf-8")
        ).hexdigest()[:12]
        return f"skill_{stem}_{digest}"

    def bind(
        self,
        *,
        execution: Any,
        skill_binding: SkillBinding,
        resource_path: str,
        authorization: SkillScriptAuthorization,
    ) -> BoundSkillAction:
        if str(getattr(execution, "id", "")) != skill_binding.task_id:
            raise PermissionError("Skill binding belongs to another task execution.")
        if not isinstance(authorization, SkillScriptAuthorization) or not authorization.auto_allow:
            raise PermissionError("Skill script authorization requires explicit auto_allow=True.")
        package = self.library.resolve(skill_binding.revision_ref)
        if package.revision_ref != skill_binding.revision_ref or package.trust != "trusted":
            raise PermissionError("Skill script execution requires a trusted exact revision.")
        descriptor = package.resource(resource_path)
        if descriptor.kind != "script" or not descriptor.executable:
            raise PermissionError("Skill resource is not an executable script.")
        language = self._language(resource_path)
        action_id = self._action_id(skill_binding, resource_path)
        settings = getattr(execution, "settings", None)
        configured_providers = (
            settings.get("code_execution.providers", ["docker"])
            if settings is not None and hasattr(settings, "get")
            else ["docker"]
        )
        if not isinstance(configured_providers, list) or not configured_providers:
            raise ValueError("code_execution.providers must be a non-empty ordered list.")
        execution.action.register_action(
            action_id=action_id,
            desc=f"Run the authorized {package.name} Skill script {resource_path}.",
            kwargs={
                "args": ("list[str]", "Optional bounded script arguments."),
            },
            executor=CodeExecutionActionExecutor(
                language=language,
                skill_library=self.library,
            ),
            default_policy={"auto_allow": True},
            side_effect_level="exec",
            approval_required=False,
            sandbox_required=True,
            replay_safe=False,
            expose_to_model=True,
            execution_resources=[
                {
                    "kind": "code_execution",
                    "resource_key": action_id,
                    "scope": "action_call",
                    "provider_candidates": list(configured_providers),
                    "required_capabilities": {
                        "language": language,
                        "isolation": required_code_execution_isolation(),
                        "workspace_access_mode": "snapshot",
                    },
                    "workspace_access": {
                        "mode": "snapshot",
                        "expected_outputs": list(authorization.expected_outputs),
                    },
                }
            ],
            meta={
                "skill_revision_ref": package.revision_ref,
                "skill_resource_path": descriptor.path,
                "skill_resource_sha256": descriptor.sha256,
                "expected_outputs": list(authorization.expected_outputs),
            },
        )
        local_action_ids = getattr(execution, "local_action_ids", None)
        if not isinstance(local_action_ids, list):
            raise TypeError("AgentExecution.local_action_ids must be a list.")
        if action_id not in local_action_ids:
            local_action_ids.append(action_id)
        return BoundSkillAction(
            action_id=action_id,
            skill_binding_id=skill_binding.binding_id,
            skill_revision_ref=package.revision_ref,
            resource_path=descriptor.path,
        )


__all__ = ["BoundSkillAction", "SkillActionBinder"]

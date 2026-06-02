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

from pathlib import Path
from typing import Any, Callable, cast

from agently.types.data.workspace import WorkspaceContextPack
from agently.types.plugins import ContextBuilder, IngestionProfile, RecallPlanner, Retriever, WorkspaceBackend

from ..Recall import DefaultContextBuilder, RecallProfile, RuleRecallPlanner, WorkspaceRetriever
from .Errors import WorkspaceConfigurationError
from .Workspace import Workspace
from .LocalBackend import LocalWorkspaceBackend
from .Profiles import CheckpointIngestionProfile, FastIngestionProfile


class WorkspaceManager:
    """Factory and registry for Workspace foundation capabilities."""

    def __init__(self):
        self._profiles: dict[str, IngestionProfile] = {}
        self._recall_profiles: dict[str, RecallProfile] = {}
        self.register_profile("fast", FastIngestionProfile())
        self.register_profile("checkpoint", CheckpointIngestionProfile())
        self.register_recall_profile(
            "auto",
            profile=RecallProfile(
                name="auto",
                planner=RuleRecallPlanner(),
                retriever=WorkspaceRetriever(),
                context_builder=DefaultContextBuilder(),
            ),
        )
        self.register_recall_profile(
            "software_dev",
            profile=RecallProfile(
                name="software_dev",
                planner=RuleRecallPlanner(),
                retriever=WorkspaceRetriever(),
                context_builder=DefaultContextBuilder(),
            ),
        )

    def create(
        self,
        path_or_backend: str | Path | WorkspaceBackend,
        *,
        create: bool = True,
        mode: str = "read_write",
    ) -> Workspace:
        if hasattr(path_or_backend, "put") and hasattr(path_or_backend, "search"):
            backend = cast(WorkspaceBackend, path_or_backend)
        else:
            backend = LocalWorkspaceBackend(path_or_backend, create=create, mode=mode)  # type: ignore[arg-type]
        return Workspace(backend, self)

    def register_profile(self, name: str, handler: IngestionProfile | Callable[..., Any]):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace profile name must be non-empty.")
        if not hasattr(handler, "ingest"):
            raise TypeError("Workspace profile handler must provide async ingest(...).")
        self._profiles[normalized] = handler  # type: ignore[assignment]
        return self

    def get_profile(self, name: str) -> IngestionProfile:
        normalized = str(name or "fast").strip() or "fast"
        if normalized not in self._profiles:
            raise WorkspaceConfigurationError(f"Workspace ingestion profile is not registered: { normalized }")
        return self._profiles[normalized]

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())

    def register_recall_profile(
        self,
        name: str,
        *,
        profile: RecallProfile | None = None,
        planner: RecallPlanner | None = None,
        retriever: Retriever | None = None,
        context_builder: ContextBuilder | None = None,
    ):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace recall profile name must be non-empty.")
        if profile is None:
            default = self.get_recall_profile("auto") if "auto" in self._recall_profiles else None
            profile = RecallProfile(
                name=normalized,
                planner=planner or (default.planner if default else RuleRecallPlanner()),
                retriever=retriever or (default.retriever if default else WorkspaceRetriever()),
                context_builder=context_builder or (default.context_builder if default else DefaultContextBuilder()),
            )
        self._recall_profiles[normalized] = profile
        return self

    def get_recall_profile(self, name: str) -> RecallProfile:
        normalized = str(name or "auto").strip() or "auto"
        if normalized not in self._recall_profiles:
            raise WorkspaceConfigurationError(f"Workspace recall profile is not registered: { normalized }")
        return self._recall_profiles[normalized]

    def list_recall_profiles(self) -> list[str]:
        return sorted(self._recall_profiles.keys())

    async def build_context(
        self,
        workspace: Workspace,
        *,
        goal: str,
        scope: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        profile: str = "auto",
    ) -> WorkspaceContextPack:
        recall_profile = self.get_recall_profile(profile)
        scope = scope or {}
        budget = budget or {}
        plan = await recall_profile.planner.plan(
            workspace=workspace,
            goal=goal,
            scope=scope,
            budget=budget,
            profile=profile,
        )
        records = await recall_profile.retriever.retrieve(workspace=workspace, plan=plan)
        return await recall_profile.context_builder.build(
            workspace=workspace,
            goal=goal,
            profile=profile,
            records=records,
            budget=budget,
            diagnostics=plan.get("diagnostics", {}),
        )

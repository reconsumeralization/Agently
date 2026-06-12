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
from agently.types.plugins import (
    ContextBuilder,
    IngestionProfile,
    RecallPlanner,
    Retriever,
    WorkspaceBackend,
    WorkspaceBackendProvider,
)

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
        self._backend_providers: dict[str, WorkspaceBackendProvider] = {}
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

    def create(
        self,
        path_or_backend: str | Path | WorkspaceBackend | None = None,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
    ) -> Workspace:
        if provider is not None:
            backend = self._create_backend_from_provider(
                provider,
                root=path_or_backend,
                create=create,
                mode=mode,
                provider_options=provider_options,
            )
        elif hasattr(path_or_backend, "put") and hasattr(path_or_backend, "search"):
            backend = cast(WorkspaceBackend, path_or_backend)
        else:
            if path_or_backend is None:
                path_or_backend = Path(".agently") / "workspaces" / "default"
            backend = LocalWorkspaceBackend(path_or_backend, create=create, mode=mode)  # type: ignore[arg-type]
        return Workspace(backend, self)

    def _validate_backend(self, backend: Any, *, provider: str | None = None) -> WorkspaceBackend:
        required = ("put", "search", "get_data", "capabilities")
        missing = [name for name in required if not hasattr(backend, name)]
        if missing:
            detail = f" from provider '{provider}'" if provider else ""
            raise TypeError(
                f"Workspace backend{detail} must implement WorkspaceBackend; "
                f"missing: {', '.join(missing)}."
            )
        return cast(WorkspaceBackend, backend)

    def _create_backend_from_provider(
        self,
        provider: str,
        *,
        root: str | Path | WorkspaceBackend | None = None,
        create: bool = True,
        mode: str = "read_write",
        provider_options: dict[str, Any] | None = None,
    ) -> WorkspaceBackend:
        normalized = str(provider).strip()
        if not normalized:
            raise ValueError("Workspace backend provider name must be non-empty.")
        if normalized not in self._backend_providers:
            raise WorkspaceConfigurationError(f"Workspace backend provider is not registered: { normalized }")
        options = dict(provider_options or {})
        if root is not None:
            options.setdefault("root", root)
        backend = self._backend_providers[normalized](
            create=create,
            mode=mode,
            **options,
        )
        return self._validate_backend(backend, provider=normalized)

    def register_backend_provider(self, name: str, provider: WorkspaceBackendProvider):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace backend provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("Workspace backend provider must be callable.")
        self._backend_providers[normalized] = provider
        return self

    def unregister_backend_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace backend provider name must be non-empty.")
        self._backend_providers.pop(normalized, None)
        return self

    def list_backend_providers(self) -> list[str]:
        return sorted(self._backend_providers.keys())

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

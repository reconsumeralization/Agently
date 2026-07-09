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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, cast

from agently.types.data.workspace import (
    WorkspaceContextPackage,
    WorkspaceFileExportResult,
    WorkspaceFileInfo,
    WorkspaceFileOperation,
    WorkspaceFileReadResult,
    WorkspaceFileWriteResult,
)
from agently.types.plugins import (
    ContextBuilder,
    DBStoreProvider,
    EmbeddingProvider,
    IngestionProfile,
    ContextPlanner,
    Retriever,
    VectorStoreProvider,
    WorkspaceBackend,
    WorkspaceBackendProvider,
    WorkspaceProviderFactory,
    WorkspaceFileIOHandler,
)

from .ContextBuilder import DefaultContextBuilder, ContextProfile, RuleContextPlanner, WorkspaceRetriever
from .Errors import WorkspaceConfigurationError
from .FileIO import (
    DefaultTextWorkspaceFileIOHandler,
    HtmlExportWorkspaceFileIOHandler,
    ImageVLMWorkspaceFileIOHandler,
    OfficeWorkspaceFileIOHandler,
    PdfWorkspaceFileIOHandler,
    inspect_workspace_file,
    unsupported_export_result,
    unsupported_read_result,
    unsupported_write_result,
)
from .Workspace import Workspace
from .LocalBackend import LocalWorkspaceBackend
from .Profiles import CheckpointIngestionProfile, FastIngestionProfile
from .Stores import AgentEmbeddingProvider, CallableEmbeddingProvider, ChromaVectorStoreProvider, EmbeddingFunction, SQLiteVectorStoreProvider


class WorkspaceManager:
    """Factory and registry for Workspace foundation capabilities."""

    _DB_STORE_REQUIRED_METHODS = (
        "put_record",
        "get_record",
        "index_record",
        "search",
        "link",
        "link_evidence",
        "links",
        "checkpoint",
        "put_checkpoint",
        "get_checkpoint",
        "put_artifact_ref",
        "claim_lease",
        "heartbeat_lease",
        "release_lease",
        "put_snapshot",
        "get_snapshot",
        "latest_snapshot",
        "latest_checkpoint",
        "checkpoint_history",
        "append_runtime_event",
        "query_runtime_events",
        "record_file_policy",
        "get_file_policy",
        "add_retention_anchor",
        "retention_anchors",
        "prune_scope",
        "register_scratch_lease",
        "get_scratch_lease",
        "list_scratch_leases",
        "close_scratch_lease",
    )
    _VECTOR_STORE_REQUIRED_METHODS = ("index_record", "search_by_embedding")

    def __init__(self):
        self._profiles: dict[str, IngestionProfile] = {}
        self._context_profiles: dict[str, ContextProfile] = {}
        self._backend_providers: dict[str, WorkspaceBackendProvider] = {}
        self._db_store_providers: dict[str, WorkspaceProviderFactory] = {}
        self._embedding_providers: dict[str, WorkspaceProviderFactory] = {}
        self._vector_store_providers: dict[str, WorkspaceProviderFactory] = {}
        self._file_io_handlers: dict[str, WorkspaceFileIOHandler] = {}
        self.register_db_store_provider("sqlite", lambda **options: None)
        self.register_embedding_provider("callable", self._create_callable_embedding_provider)
        self.register_embedding_provider("agent", self._create_agent_embedding_provider)
        self.register_vector_store_provider(
            "sqlite",
            lambda **options: SQLiteVectorStoreProvider(
                Path(options["root"]) / "workspace.db",
                read_only=options.get("mode") in {"read", "read_only", "readonly"},
                create=bool(options.get("create", True)),
                similarity=options.get("similarity", "cosine"),
            ),
        )
        self.register_vector_store_provider(
            "chroma",
            lambda **options: ChromaVectorStoreProvider(
                Path(options["root"]) / "vectors" / "chroma",
                create=bool(options.get("create", True)),
                mode=str(options.get("mode", "read_write")),
                similarity=options.get("similarity", "cosine"),
                collection_name=str(options.get("collection_name", "workspace_records")),
            ),
        )
        self.register_profile("fast", FastIngestionProfile())
        self.register_profile("checkpoint", CheckpointIngestionProfile())
        self.register_context_profile(
            "auto",
            profile=ContextProfile(
                name="auto",
                planner=RuleContextPlanner(),
                retriever=WorkspaceRetriever(),
                context_builder=DefaultContextBuilder(),
            ),
        )
        self.register_file_io_handler(DefaultTextWorkspaceFileIOHandler())
        self.register_file_io_handler(PdfWorkspaceFileIOHandler())
        self.register_file_io_handler(OfficeWorkspaceFileIOHandler())
        self.register_file_io_handler(ImageVLMWorkspaceFileIOHandler())
        self.register_file_io_handler(HtmlExportWorkspaceFileIOHandler())

    @staticmethod
    def _create_callable_embedding_provider(**options: Any) -> CallableEmbeddingProvider:
        embedding_function = options.get("embedding_function") or options.get("embedder") or options.get("callable")
        if not callable(embedding_function):
            raise WorkspaceConfigurationError(
                "Workspace callable embedding provider requires embedding_options={'embedding_function': callable}."
            )
        return CallableEmbeddingProvider(cast(EmbeddingFunction, embedding_function))

    @staticmethod
    def _create_agent_embedding_provider(**options: Any) -> AgentEmbeddingProvider:
        agent = options.get("agent") or options.get("embedding_agent")
        if agent is None:
            raise WorkspaceConfigurationError(
                "Workspace agent embedding provider requires embedding_options={'agent': embedding_agent}."
            )
        return AgentEmbeddingProvider(agent)

    def create(
        self,
        path_or_backend: str | Path | WorkspaceBackend | None = None,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
        files_root: str | Path | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
        scope_lineage: "Sequence[Mapping[str, Any]] | None" = None,
    ) -> Workspace:
        component_options_used = any(
            value is not None
            for value in (
                db_store_provider,
                db_store_options,
                embedding_provider,
                embedding_options,
                vector_store_provider,
                vector_store_options,
            )
        )
        if provider is not None:
            if component_options_used:
                raise WorkspaceConfigurationError(
                    "Workspace provider=... replaces the full backend and cannot be combined with "
                    "db_store_provider, embedding_provider, or vector_store_provider options."
                )
            backend = self._create_backend_from_provider(
                provider,
                root=path_or_backend,
                create=create,
                mode=mode,
                provider_options=provider_options,
            )
        elif hasattr(path_or_backend, "put") and hasattr(path_or_backend, "search"):
            if component_options_used:
                raise WorkspaceConfigurationError(
                    "A concrete WorkspaceBackend cannot be combined with component provider options."
                )
            backend = cast(WorkspaceBackend, path_or_backend)
        else:
            if path_or_backend is None:
                path_or_backend = Path(".agently") / "workspaces" / "default"
            backend_root = cast(str | Path, path_or_backend)
            backend = LocalWorkspaceBackend(
                backend_root,
                create=create,
                mode=mode,
                initialize_default_vector_store_provider=False,
            )
            self._configure_local_backend_components(
                backend,
                root=backend_root,
                create=create,
                mode=mode,
                db_store_provider=db_store_provider,
                db_store_options=db_store_options,
                embedding_provider=embedding_provider,
                embedding_options=embedding_options,
                vector_store_provider=vector_store_provider,
                vector_store_options=vector_store_options,
            )
        return Workspace(
            backend,
            self,
            files_root=files_root,
            default_scope=default_scope,
            default_search_scope=default_search_scope,
            scope_lineage=scope_lineage,
        )

    def _configure_local_backend_components(
        self,
        backend: LocalWorkspaceBackend,
        *,
        root: str | Path | WorkspaceBackend,
        create: bool,
        mode: str,
        db_store_provider: Any | None,
        db_store_options: dict[str, Any] | None,
        embedding_provider: Any | None,
        embedding_options: dict[str, Any] | None,
        vector_store_provider: Any | None,
        vector_store_options: dict[str, Any] | None,
    ) -> None:
        db_store, db_store_name = self._resolve_db_store_provider(
            db_store_provider,
            root=root,
            create=create,
            mode=mode,
            options=db_store_options,
        )
        embedder = self._resolve_embedding_provider(embedding_provider, embedding_options)
        vector_store_provider_instance, vector_store_name, fallback_reason = self._resolve_vector_store_provider(
            vector_store_provider,
            root=root,
            create=create,
            mode=mode,
            options=vector_store_options,
        )
        backend.configure_components(
            db_store_provider=db_store,
            db_store_provider_name=db_store_name,
            embedding_provider=embedder,
            vector_store_provider=vector_store_provider_instance,
            vector_store_provider_name=vector_store_name,
            vector_store_fallback_reason=fallback_reason,
        )

    def _resolve_db_store_provider(
        self,
        provider: Any | None,
        *,
        root: str | Path | WorkspaceBackend,
        create: bool,
        mode: str,
        options: dict[str, Any] | None,
    ) -> tuple[DBStoreProvider | None, str]:
        if provider is not None and not isinstance(provider, str):
            resolved_provider = self._validate_db_store_provider(provider, label=getattr(provider, "name", None))
            return resolved_provider, str(getattr(resolved_provider, "name", type(resolved_provider).__name__))
        normalized = str(provider or "sqlite").strip() or "sqlite"
        if normalized not in self._db_store_providers:
            raise WorkspaceConfigurationError(f"Workspace DB store provider is not registered: { normalized }")
        resolved = self._db_store_providers[normalized](root=root, create=create, mode=mode, **dict(options or {}))
        if resolved is None or resolved == "sqlite":
            return None, normalized
        return self._validate_db_store_provider(resolved, label=normalized), normalized

    def _validate_db_store_provider(self, candidate: Any, *, label: str | None = None) -> DBStoreProvider:
        provider_label = label or getattr(candidate, "name", None) or type(candidate).__name__
        missing = [name for name in self._DB_STORE_REQUIRED_METHODS if not hasattr(candidate, name)]
        if missing:
            raise TypeError(
                f"Workspace DB store provider '{ provider_label }' must implement DBStoreProvider; "
                f"missing: {', '.join(missing)}."
            )
        return cast(DBStoreProvider, candidate)

    def _resolve_embedding_provider(
        self,
        provider: Any | None,
        options: dict[str, Any] | None,
    ) -> EmbeddingProvider | None:
        if provider is None:
            return None
        if hasattr(provider, "embed_texts"):
            return cast(EmbeddingProvider, provider)
        if callable(provider) and not isinstance(provider, str):
            return CallableEmbeddingProvider(cast(EmbeddingFunction, provider))
        normalized = str(provider).strip()
        if normalized not in self._embedding_providers:
            raise WorkspaceConfigurationError(f"Workspace embedding provider is not registered: { normalized }")
        resolved = self._embedding_providers[normalized](**dict(options or {}))
        if not hasattr(resolved, "embed_texts"):
            raise TypeError(f"Workspace embedding provider '{ normalized }' must implement embed_texts(...).")
        return cast(EmbeddingProvider, resolved)

    def _resolve_vector_store_provider(
        self,
        provider: Any | None,
        *,
        root: str | Path | WorkspaceBackend,
        create: bool,
        mode: str,
        options: dict[str, Any] | None,
    ) -> tuple[VectorStoreProvider | None, str | None, str | None]:
        if provider is not None and not isinstance(provider, str):
            vector_store_provider_object = self._validate_vector_store_provider(
                provider,
                label=getattr(provider, "name", None),
            )
            return (
                vector_store_provider_object,
                getattr(vector_store_provider_object, "name", type(vector_store_provider_object).__name__),
                None,
            )
        normalized = str(provider or "auto").strip() or "auto"
        if normalized == "auto":
            try:
                return self._create_vector_store_provider(
                    "chroma",
                    root=root,
                    create=create,
                    mode=mode,
                    options=options,
                ), "chroma", None
            except Exception as error:
                fallback = self._create_vector_store_provider(
                    "sqlite",
                    root=root,
                    create=create,
                    mode=mode,
                    options=options,
                )
                return fallback, "sqlite", f"chroma_unavailable:{type(error).__name__}"
        return self._create_vector_store_provider(
            normalized,
            root=root,
            create=create,
            mode=mode,
            options=options,
        ), normalized, None

    def _create_vector_store_provider(
        self,
        name: str,
        *,
        root: str | Path | WorkspaceBackend,
        create: bool,
        mode: str,
        options: dict[str, Any] | None,
    ) -> VectorStoreProvider:
        normalized = str(name).strip()
        if normalized not in self._vector_store_providers:
            raise WorkspaceConfigurationError(f"Workspace vector store provider is not registered: { normalized }")
        resolved = self._vector_store_providers[normalized](
            root=root,
            create=create,
            mode=mode,
            **dict(options or {}),
        )
        return self._validate_vector_store_provider(resolved, label=normalized)

    def _validate_vector_store_provider(self, candidate: Any, *, label: str | None = None) -> VectorStoreProvider:
        provider_label = label or getattr(candidate, "name", None) or type(candidate).__name__
        missing = [name for name in self._VECTOR_STORE_REQUIRED_METHODS if not hasattr(candidate, name)]
        if missing:
            raise TypeError(
                f"Workspace vector store provider '{ provider_label }' must implement VectorStoreProvider; "
                f"missing: {', '.join(missing)}."
            )
        return cast(VectorStoreProvider, candidate)

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

    def register_db_store_provider(self, name: str, provider: WorkspaceProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace DB store provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("Workspace DB store provider must be callable.")
        self._db_store_providers[normalized] = provider
        return self

    def unregister_db_store_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace DB store provider name must be non-empty.")
        self._db_store_providers.pop(normalized, None)
        return self

    def list_db_store_providers(self) -> list[str]:
        return sorted(self._db_store_providers.keys())

    def register_embedding_provider(self, name: str, provider: WorkspaceProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace embedding provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("Workspace embedding provider must be callable.")
        self._embedding_providers[normalized] = provider
        return self

    def unregister_embedding_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace embedding provider name must be non-empty.")
        self._embedding_providers.pop(normalized, None)
        return self

    def list_embedding_providers(self) -> list[str]:
        return sorted(self._embedding_providers.keys())

    def register_vector_store_provider(self, name: str, provider: WorkspaceProviderFactory):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace vector store provider name must be non-empty.")
        if not callable(provider):
            raise TypeError("Workspace vector store provider must be callable.")
        self._vector_store_providers[normalized] = provider
        return self

    def unregister_vector_store_provider(self, name: str):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace vector store provider name must be non-empty.")
        self._vector_store_providers.pop(normalized, None)
        return self

    def list_vector_store_providers(self) -> list[str]:
        return sorted(self._vector_store_providers.keys())

    def register_profile(self, name: str, handler: IngestionProfile | Callable[..., Any]):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace profile name must be non-empty.")
        if not hasattr(handler, "ingest"):
            raise TypeError("Workspace put profile handler is invalid.")
        self._profiles[normalized] = handler  # type: ignore[assignment]
        return self

    def get_profile(self, name: str) -> IngestionProfile:
        normalized = str(name or "fast").strip() or "fast"
        if normalized not in self._profiles:
            raise WorkspaceConfigurationError(f"Workspace put profile is not registered: { normalized }")
        return self._profiles[normalized]

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())

    def register_context_profile(
        self,
        name: str,
        *,
        profile: ContextProfile | None = None,
        planner: ContextPlanner | None = None,
        retriever: Retriever | None = None,
        context_builder: ContextBuilder | None = None,
    ):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace context profile name must be non-empty.")
        if profile is None:
            default = self.get_context_profile("auto") if "auto" in self._context_profiles else None
            profile = ContextProfile(
                name=normalized,
                planner=planner or (default.planner if default else RuleContextPlanner()),
                retriever=retriever or (default.retriever if default else WorkspaceRetriever()),
                context_builder=context_builder or (default.context_builder if default else DefaultContextBuilder()),
            )
        self._context_profiles[normalized] = profile
        return self

    def get_context_profile(self, name: str) -> ContextProfile:
        normalized = str(name or "auto").strip() or "auto"
        if normalized not in self._context_profiles:
            raise WorkspaceConfigurationError(f"Workspace context profile is not registered: { normalized }")
        return self._context_profiles[normalized]

    def list_context_profiles(self) -> list[str]:
        return sorted(self._context_profiles.keys())

    def register_file_io_handler(
        self,
        handler: WorkspaceFileIOHandler,
        *,
        replace: bool = False,
    ):
        name = str(getattr(handler, "name", "")).strip()
        if not name:
            raise ValueError("Workspace file IO handler name must be non-empty.")
        for method_name in ("supports", "read", "write", "export"):
            if not callable(getattr(handler, method_name, None)):
                raise TypeError(f"Workspace file IO handler must provide { method_name }(...).")
        if name in self._file_io_handlers and not replace:
            raise WorkspaceConfigurationError(f"Workspace file IO handler is already registered: { name }")
        register_hook = getattr(handler, "_on_register", None)
        if callable(register_hook):
            register_hook()
        if name in self._file_io_handlers and replace:
            unregister_hook = getattr(self._file_io_handlers[name], "_on_unregister", None)
            if callable(unregister_hook):
                unregister_hook()
        self._file_io_handlers[name] = handler
        return self

    def unregister_file_io_handler(self, handler_id: str):
        normalized = str(handler_id).strip()
        if not normalized:
            raise ValueError("Workspace file IO handler name must be non-empty.")
        handler = self._file_io_handlers.pop(normalized, None)
        if handler is not None:
            unregister_hook = getattr(handler, "_on_unregister", None)
            if callable(unregister_hook):
                unregister_hook()
        return self

    def list_file_io_handlers(self) -> list[str]:
        return sorted(self._file_io_handlers.keys())

    def inspect_file_path(self, path: Path, *, relative_path: str) -> WorkspaceFileInfo:
        return inspect_workspace_file(path, relative_path=relative_path)

    def _select_file_io_handler(
        self,
        *,
        operation: WorkspaceFileOperation,
        file_info: WorkspaceFileInfo,
        handler: str | None = None,
        export_kind: str | None = None,
    ) -> WorkspaceFileIOHandler | None:
        if handler is not None:
            normalized = str(handler).strip()
            if not normalized:
                raise ValueError("Workspace file IO handler name must be non-empty.")
            if normalized not in self._file_io_handlers:
                raise WorkspaceConfigurationError(f"Workspace file IO handler is not registered: { normalized }")
            selected = self._file_io_handlers[normalized]
            if selected.supports(operation=operation, file_info=file_info, export_kind=export_kind):
                return selected
            return None
        for candidate in sorted(
            self._file_io_handlers.values(),
            key=lambda item: (int(getattr(item, "priority", 1000)), str(getattr(item, "name", ""))),
        ):
            if candidate.supports(operation=operation, file_info=file_info, export_kind=export_kind):
                return candidate
        return None

    async def read_file_path(
        self,
        path: Path,
        *,
        relative_path: str,
        max_bytes: int = 20000,
        offset: int = 0,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileReadResult:
        file_info = self.inspect_file_path(path, relative_path=relative_path)
        selected = self._select_file_io_handler(operation="read", file_info=file_info, handler=handler)
        if selected is None:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=handler or "none",
                code="workspace.file.no_read_handler",
                message="No registered Workspace file IO handler can read this file type.",
            )
        return await selected.read(
            path=path,
            file_info=file_info,
            max_bytes=max_bytes,
            offset=offset,
            options=options,
        )

    async def write_file_path(
        self,
        path: Path,
        *,
        relative_path: str,
        content: str,
        append: bool = False,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileWriteResult:
        file_info = self.inspect_file_path(path, relative_path=relative_path)
        selected = self._select_file_io_handler(operation="write", file_info=file_info, handler=handler)
        if selected is None:
            return unsupported_write_result(
                file_info=file_info,
                handler_id=handler or "none",
                code="workspace.file.no_write_handler",
                message="No registered Workspace file IO handler can write this file type.",
            )
        return await selected.write(
            path=path,
            file_info=file_info,
            content=content,
            append=append,
            options=options,
        )

    async def export_file_path(
        self,
        source_path: Path,
        output_path: Path,
        *,
        source_relative_path: str,
        output_relative_path: str,
        export_kind: str,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileExportResult:
        source_info = self.inspect_file_path(source_path, relative_path=source_relative_path)
        output_info = self.inspect_file_path(output_path, relative_path=output_relative_path)
        selected = self._select_file_io_handler(
            operation="export",
            file_info=source_info,
            handler=handler,
            export_kind=export_kind,
        )
        if selected is None:
            return unsupported_export_result(
                source_info=source_info,
                output_info=output_info,
                export_kind=export_kind,
                handler_id=handler or "none",
                code="workspace.file.no_export_handler",
                message="No registered Workspace file IO handler can export this source file to the requested kind.",
            )
        return await selected.export(
            source_path=source_path,
            output_path=output_path,
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            options=options,
        )

    async def build_context(
        self,
        workspace: Workspace,
        *,
        goal: str,
        scope: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        profile: str = "auto",
    ) -> WorkspaceContextPackage:
        context_profile = self.get_context_profile(profile)
        scope = scope or {}
        budget = budget or {}
        plan = await context_profile.planner.plan(
            workspace=workspace,
            goal=goal,
            scope=scope,
            budget=budget,
            profile=profile,
        )
        records = await context_profile.retriever.retrieve(workspace=workspace, plan=plan)
        return await context_profile.context_builder.build(
            workspace=workspace,
            goal=goal,
            profile=profile,
            records=records,
            budget=budget,
            diagnostics=plan.get("diagnostics", {}),
        )

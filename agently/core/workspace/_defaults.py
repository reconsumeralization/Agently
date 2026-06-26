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

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._utils import slug

ScopeNode = dict[str, str]
WORKSPACE_GUIDE_FILENAME = "AGENTLY_WORKSPACE.md"

# Run-lineage scope kinds carry an indexable ancestor membership field so record
# pruning by a broader scope (e.g. ``prune_scope({"task_id": T})``) can match
# records written under any nested descendant. Session/project/agent membership
# already lives in the workspace ``default_scope`` and is not re-derived here.
RUN_LINEAGE_KINDS: dict[str, str] = {
    "tasks": "task_id",
    "executions": "execution_id",
    "actions": "action_call_id",
    "dynamic_tasks": "dynamic_task_id",
    "trigger_flows": "trigger_flow_id",
}

# Full kind -> scope-key map, including the broader scope roots that may anchor a
# physical lineage subtree.
SCOPE_LINEAGE_KINDS: dict[str, str] = {
    "projects": "project_id",
    "sessions": "session_id",
    "agents": "agent_id",
    **RUN_LINEAGE_KINDS,
}

# Inverse map used to translate a prune-scope filter back to the lineage path
# node(s) that must be located and removed.
SCOPE_KEY_KINDS: dict[str, str] = {value: key for key, value in SCOPE_LINEAGE_KINDS.items()}


def default_workspace_base_root() -> Path:
    return Path(".agently") / "workspaces"


def script_scope(settings: Any = None) -> str:
    configured = _settings_get(settings, "workspace.script_scope")
    if configured is not None:
        return slug(str(configured), "script")
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        name = Path(argv0).stem
        if name:
            return slug(name, "script")
    return slug(Path.cwd().name, "script")


def default_physical_root(settings: Any = None, *, session_id: str | None = None) -> Path:
    configured = _settings_get(settings, "workspace.default_root")
    if configured is not None:
        return Path(str(configured))
    configured_project = _settings_get(settings, "workspace.project_id")
    if configured_project is not None:
        return default_workspace_base_root() / "projects" / slug(str(configured_project), "project")
    resolved_session_id = session_id or _settings_get(settings, "runtime.session_id")
    if resolved_session_id:
        return default_workspace_base_root() / "sessions" / slug(str(resolved_session_id), "session")
    return default_workspace_base_root() / "scripts" / script_scope(settings)


def scope_node(kind: str, node_id: str | None) -> ScopeNode:
    """Build one lineage node. ``kind`` is canonical (plural); ``id`` stays raw.

    Raw ids are preserved so scope membership fields keep matching the values
    other code filters on. Path segments are slugged at path-construction time.
    """

    return {"kind": str(kind), "id": str(node_id) if node_id not in (None, "") else "default"}


def normalize_lineage(scope_lineage: Sequence[Mapping[str, Any]] | None) -> list[ScopeNode]:
    nodes: list[ScopeNode] = []
    for node in scope_lineage or []:
        if not isinstance(node, Mapping):
            continue
        kind = node.get("kind")
        if not kind:
            continue
        nodes.append(scope_node(str(kind), node.get("id")))
    return nodes


def extend_lineage(
    parent_lineage: Sequence[Mapping[str, Any]] | None,
    kind: str,
    node_id: str | None = None,
) -> list[ScopeNode]:
    return [*normalize_lineage(parent_lineage), scope_node(kind, node_id)]


def extend_lineage_nodes(
    parent_lineage: Sequence[Mapping[str, Any]] | None,
    nodes: Sequence[Mapping[str, Any]],
) -> list[ScopeNode]:
    return [*normalize_lineage(parent_lineage), *normalize_lineage(nodes)]


def _lineage_area_root(
    physical_root: str | Path,
    scope_lineage: Sequence[Mapping[str, Any]] | None,
    area: str,
) -> Path:
    nodes = normalize_lineage(scope_lineage)
    base = Path(physical_root) / area
    if not nodes:
        return base
    base = base / "lineage"
    for node in nodes:
        base = base / slug(node["kind"], "scope") / slug(node["id"], "default")
    return base / area


def lineage_files_root(
    physical_root: str | Path,
    scope_lineage: Sequence[Mapping[str, Any]] | None,
) -> Path:
    """Recursive lineage-contained durable file root for a resolved scope chain.

    Produces ``<root>/files/lineage/<kind>/<id>/.../<leaf-kind>/<leaf-id>/files``
    so pruning a broader ancestor scope can delete the matching subtree without
    touching unrelated siblings (spec section 8.2).
    """

    return _lineage_area_root(physical_root, scope_lineage, "files")


def lineage_scratch_root(
    physical_root: str | Path,
    scope_lineage: Sequence[Mapping[str, Any]] | None,
) -> Path:
    """Recursive lineage-contained scratch root mirroring ``lineage_files_root``."""

    return _lineage_area_root(physical_root, scope_lineage, "scratch")


def scope_from_lineage(scope_lineage: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Derive record scope fields from a resolved lineage chain.

    Emits indexable ancestor membership fields for prunable run-lineage kinds and
    the ordered ``scope_lineage`` list, so physical subtree cleanup and
    record-index cleanup agree on one lineage tree (spec section 8.2).
    """

    nodes = normalize_lineage(scope_lineage)
    scope: dict[str, Any] = {}
    for node in nodes:
        key = RUN_LINEAGE_KINDS.get(node["kind"])
        if key:
            scope[key] = node["id"]
    if nodes:
        scope["scope_lineage"] = [dict(node) for node in nodes]
        scope["lineage_leaf_kind"] = nodes[-1]["kind"]
        scope["lineage_leaf_id"] = nodes[-1]["id"]
    return scope


def scope_filter_path_nodes(scope: Mapping[str, Any] | None) -> list[ScopeNode]:
    """Translate a prune-scope filter into lineage path nodes to locate/remove."""

    nodes: list[ScopeNode] = []
    for key, value in dict(scope or {}).items():
        kind = SCOPE_KEY_KINDS.get(key)
        if kind and value is not None:
            nodes.append(scope_node(kind, str(value)))
    return nodes


def merge_scope(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    merged = {key: value for key, value in dict(base or {}).items() if value is not None}
    merged.update({key: value for key, value in dict(override or {}).items() if value is not None})
    return merged


def _settings_get(settings: Any, key: str) -> Any:
    if settings is None:
        return None
    getter = getattr(settings, "get", None)
    if callable(getter):
        return getter(key, None)
    if isinstance(settings, dict):
        return settings.get(key)
    return None

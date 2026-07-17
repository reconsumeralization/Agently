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

from collections.abc import Mapping
from typing import Any


def identity_reference_graph(
    manifests: Mapping[str, Mapping[str, Any]],
    locator_indexes: Mapping[str, Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Build host-owned reachability edges from bounded identity facts."""

    graph: dict[str, set[str]] = {entity_id: set() for entity_id in manifests}

    def connect(source: str, target: str) -> None:
        if source in graph and target in graph:
            graph[source].add(target)

    for entity_id, manifest in manifests.items():
        kind = str(manifest.get("kind") or "")
        if kind == "content_version":
            locator_id = str(manifest.get("locator_id") or "")
            connect(entity_id, locator_id)
        elif kind == "segment":
            content_version_id = str(manifest.get("content_version_id") or "")
            connect(content_version_id, entity_id)
            connect(entity_id, content_version_id)
        elif kind == "link":
            source_id = str(manifest.get("source_id") or "")
            target_id = str(manifest.get("target_id") or "")
            connect(source_id, entity_id)
            connect(entity_id, source_id)
            connect(entity_id, target_id)

    for locator_id, index in locator_indexes.items():
        current_version = str(index.get("current_content_version_id") or "")
        connect(locator_id, current_version)
    return graph


def retained_identity_closure(
    graph: Mapping[str, set[str]],
    roots: set[str],
) -> set[str]:
    retained: set[str] = set()
    pending = list(roots)
    while pending:
        entity_id = pending.pop()
        if entity_id in retained or entity_id not in graph:
            continue
        retained.add(entity_id)
        pending.extend(graph[entity_id] - retained)
    return retained

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


class FastIngestionProfile:
    name = "fast"

    async def ingest(self, *, workspace, content, collection, kind, scope, source, summary=None, meta=None):
        return await workspace.put(
            content,
            collection=collection,
            kind=kind,
            summary=summary,
            scope=scope,
            source=source,
            meta=meta,
        )


class CheckpointIngestionProfile:
    name = "checkpoint"

    async def ingest(self, *, workspace, content, collection, kind, scope, source, summary=None, meta=None):
        run_id = str(scope.get("run_id") or source.get("run_id") or "default")
        step_id = scope.get("step_id")
        state = content if isinstance(content, dict) else {"value": content}
        return await workspace.checkpoint(run_id, state, step_id=str(step_id) if step_id is not None else None)

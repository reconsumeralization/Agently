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

"""ReadBlock — async_read_resource → file body."""

from __future__ import annotations

import uuid
from typing import Any

from agently.builtins.blocks._protocol import FlowBlock


class ReadBlock(FlowBlock):
    """Resource read block wrapping ``async_read_resource``."""

    name = "ReadBlock"

    def __init__(self, *, max_bytes: int = 65536):
        self._max_bytes = max_bytes

    # ── Direct execution ──

    async def execute(
        self,
        *,
        skill_id: str,
        path: str,
        context: Any,
        max_bytes: int | None = None,
    ) -> str:
        """Read a bundled resource on demand."""
        mb = max_bytes if max_bytes is not None else self._max_bytes
        content = await context.async_read_resource(
            skill_id=skill_id, path=path, max_bytes=mb
        )
        await context.async_emit_runtime_stream(
            {
                "type": "block.resource.read",
                "action": "done",
                "payload": {
                    "skill_id": skill_id,
                    "path": path,
                    "size": len(content),
                },
            }
        )
        return content

    # ── TriggerFlow operator builder ──

    def build_operators(
        self,
        *,
        blueprint: Any,
        context: Any,
        settings: dict[str, Any] | None = None,
    ) -> list[str]:
        cfg = settings or {}
        max_bytes = cfg.get("max_bytes", self._max_bytes)

        block_id = f"read-block-{uuid.uuid4().hex[:8]}"
        event_start = f"{block_id}.start"
        event_done = f"{block_id}.done"

        block = self

        async def handler(data: Any) -> Any:
            spec = data.value
            skill_id = spec.get("skill_id", "")
            path = spec.get("path", "")
            mb = spec.get("max_bytes", max_bytes)
            return await block.execute(
                skill_id=skill_id, path=path, context=context, max_bytes=mb
            )

        blueprint.add_handler("event", event_start, handler, id=block_id)
        blueprint.definition.add_operator(
            id=block_id,
            kind="chunk",
            name=self.name,
            listen_signals=[
                {
                    "id": f"{block_id}.listen",
                    "trigger_type": "event",
                    "trigger_event": event_start,
                }
            ],
            emit_signals=[
                {
                    "id": f"{block_id}.emit",
                    "trigger_type": "event",
                    "trigger_event": event_done,
                }
            ],
            handler_ref={"source": "block", "block": self.name},
            options={"max_bytes": max_bytes},
        )

        return [block_id]

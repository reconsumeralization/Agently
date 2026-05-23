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

"""FinalizeBlock — semantic output assembly + terminal event."""

from __future__ import annotations

import uuid
from typing import Any

from agently.builtins.blocks._protocol import FlowBlock


class FinalizeBlock(FlowBlock):
    """Terminal block that assembles outputs and emits the final event.

    Collects execution state from prior blocks, optionally runs a final
    model call to structure the output against ``semantic_outputs``, and
    emits a close event.
    """

    name = "FinalizeBlock"

    def __init__(
        self,
        *,
        model_key: str | None = None,
        semantic_outputs: dict[str, Any] | None = None,
    ):
        self._model_key = model_key
        self._semantic_outputs = semantic_outputs

    # ── Direct execution ──

    async def execute(
        self,
        *,
        context: Any,
        collected_outputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble final output and emit terminal event."""
        outputs = collected_outputs or {}

        # If semantic_outputs defined and a model_key is available,
        # run a final structuring call
        if self._semantic_outputs and self._model_key:
            result = await context.async_request_model(
                prompt={
                    "task": "Structure the following collected outputs into the required format.",
                    "collected_outputs": outputs,
                    "required_structure": self._semantic_outputs,
                },
                model_key=self._model_key,
                output_schema=self._semantic_outputs,
                output_format="json",
            )
            outputs = result if isinstance(result, dict) else outputs

        await context.async_emit_runtime_stream(
            {
                "type": "block.finalize",
                "action": "done",
                "payload": {
                    "output_keys": list(outputs.keys()) if isinstance(outputs, dict) else [],
                },
            }
        )

        return outputs

    # ── TriggerFlow operator builder ──

    def build_operators(
        self,
        *,
        blueprint: Any,
        context: Any,
        settings: dict[str, Any] | None = None,
    ) -> list[str]:
        cfg = settings or {}
        model_key = cfg.get("model_key", self._model_key)
        semantic_outputs = cfg.get("semantic_outputs", self._semantic_outputs)

        block_id = f"finalize-block-{uuid.uuid4().hex[:8]}"
        event_start = f"{block_id}.start"

        block = self

        async def handler(data: Any) -> Any:
            collected = data.value if isinstance(data.value, dict) else {}
            return await block.execute(
                context=context,
                collected_outputs=collected,
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
            emit_signals=[],
            handler_ref={"source": "block", "block": self.name},
            options={
                "model_key": model_key,
                "semantic_outputs": semantic_outputs,
            },
        )

        return [block_id]

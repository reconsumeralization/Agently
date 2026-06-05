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

"""ReasonBlock — prompt → async_request_model → parsed result."""

from __future__ import annotations

import uuid
from typing import Any, Callable

from agently.builtins.blocks._protocol import FlowBlock


class ReasonBlock(FlowBlock):
    """Model reasoning block wrapping ``async_request_model``.

    Can be used directly via :meth:`execute` or as a TriggerFlow operator
    builder via :meth:`build_operators`.
    """

    name = "ReasonBlock"

    def __init__(
        self,
        *,
        model_key: str = "reason",
        output_format: str | None = None,
        stream_bridge: bool = True,
        max_retries: int = 3,
    ):
        self._model_key = model_key
        self._output_format = output_format
        self._stream_bridge = stream_bridge
        self._max_retries = max_retries

    # ── Direct execution (no TriggerFlow required) ──

    async def execute(
        self,
        *,
        prompt: Any,
        context: Any,
        output_schema: Any = None,
        output_format: str | None = None,
        ensure_keys: list[str] | None = None,
    ) -> Any:
        """Run a single reasoning call directly."""
        fmt = output_format if output_format is not None else self._output_format

        stream_handler: Callable | None = None
        if self._stream_bridge:
            async def _bridge(item: dict[str, Any]) -> None:
                await context.async_emit_runtime_stream(
                    {
                        "type": "block.reason",
                        "action": "delta",
                        "payload": item,
                    }
                )

            stream_handler = _bridge

        await context.async_emit_runtime_stream(
            {
                "type": "block.reason",
                "action": "start",
                "payload": {
                    "model_key": self._model_key,
                    "prompt_length": len(str(prompt)),
                },
            }
        )

        result = await context.async_request_model(
            prompt=prompt,
            model_key=self._model_key,
            output_schema=output_schema,
            output_format=fmt,
            ensure_keys=ensure_keys,
            max_retries=self._max_retries,
            stream_handler=stream_handler,
        )

        await context.async_emit_runtime_stream(
            {
                "type": "block.reason",
                "action": "done",
                "payload": {
                    "model_key": self._model_key,
                    "output_length": len(str(result)),
                },
            }
        )

        return result

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
        output_format = cfg.get("output_format", self._output_format)
        stream_bridge = cfg.get("stream_bridge", self._stream_bridge)
        max_retries = cfg.get("max_retries", self._max_retries)

        block_id = f"reason-block-{uuid.uuid4().hex[:8]}"
        event_start = f"{block_id}.start"
        event_done = f"{block_id}.done"

        block = self

        async def handler(data: Any) -> Any:
            prompt = data.value
            return await block.execute(
                prompt=prompt,
                context=context,
                output_format=output_format,
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
            handler_ref={
                "source": "block",
                "block": self.name,
                "model_key": model_key,
            },
            options={
                "model_key": model_key,
                "output_format": output_format,
                "stream_bridge": stream_bridge,
                "max_retries": max_retries,
            },
        )

        return [block_id]

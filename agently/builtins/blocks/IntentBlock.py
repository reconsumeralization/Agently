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

"""IntentBlock — ReasonBlock specialization for classification output."""

from __future__ import annotations

import uuid
from typing import Any

from agently.builtins.blocks.ReasonBlock import ReasonBlock
from agently.builtins.blocks._protocol import FlowBlock

_DEFAULT_INTENT_SCHEMA = {
    "intent": (str, "classified intent category"),
    "confidence": (float, "confidence score 0.0–1.0"),
}


class IntentBlock(FlowBlock):
    """Classification block — always emits structured intent + confidence.

    Uses a fast/cheap model (default ``model_key="reason_fast"``) with a
    fixed output schema for deterministic classification.
    """

    name = "IntentBlock"

    def __init__(
        self,
        *,
        model_key: str = "reason_fast",
        intent_schema: dict[str, tuple[type, str]] | None = None,
        max_retries: int = 2,
    ):
        self._reason = ReasonBlock(
            model_key=model_key,
            output_format="json",
            stream_bridge=False,
            max_retries=max_retries,
        )
        self._intent_schema = intent_schema

    # ── Direct execution ──

    async def execute(
        self,
        *,
        prompt: Any,
        context: Any,
        intent_schema: dict[str, tuple[type, str]] | None = None,
    ) -> dict[str, Any]:
        """Classify *prompt* into {intent, confidence}."""
        schema = intent_schema or self._intent_schema or _DEFAULT_INTENT_SCHEMA
        result = await self._reason.execute(
            prompt=prompt,
            context=context,
            output_schema=schema,
            output_format="json",
        )
        await context.async_emit_runtime_stream(
            {
                "type": "block.intent",
                "action": "done",
                "payload": {
                    "intent": result.get("intent"),
                    "confidence": result.get("confidence"),
                    "model_key": self._reason._model_key,
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
        schema = cfg.get("intent_schema") or self._intent_schema or _DEFAULT_INTENT_SCHEMA
        model_key = cfg.get("model_key", self._reason._model_key)

        block_id = f"intent-block-{uuid.uuid4().hex[:8]}"
        event_start = f"{block_id}.start"
        event_done = f"{block_id}.done"

        block = self

        async def handler(data: Any) -> Any:
            return await block.execute(
                prompt=data.value,
                context=context,
                intent_schema=schema,
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
            handler_ref={"source": "block", "block": self.name, "model_key": model_key},
            options={
                "model_key": model_key,
                "max_retries": cfg.get("max_retries", self._reason._max_retries),
            },
        )

        return [block_id]

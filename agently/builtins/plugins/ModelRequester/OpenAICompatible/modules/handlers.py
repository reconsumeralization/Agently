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

from typing import TYPE_CHECKING, Any, AsyncGenerator

from httpx import HTTPStatusError, RequestError
from httpx_sse import SSEError

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.model.AttemptRunner import AttemptRunner, core_attempt_runner_entrypoint
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState


class OpenAICompatibleHandlersMixin:
    plugin_settings: Any

    if TYPE_CHECKING:
        def _request_model_legacy(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]: ...

    def _get_request_retry_max_attempts(self) -> int:
        retry_config = self.plugin_settings.get("request_retry", None)
        if retry_config is False:
            return 1
        if isinstance(retry_config, dict):
            raw_attempts = retry_config.get("max_attempts", 2)
        else:
            raw_attempts = self.plugin_settings.get("request_retry_max_attempts", 2)
        try:
            attempts = int(raw_attempts)
        except (TypeError, ValueError):
            attempts = 2
        return max(1, attempts)

    def _get_request_retry_after_output(self) -> bool:
        retry_config = self.plugin_settings.get("request_retry", None)
        if retry_config is False:
            return False
        if isinstance(retry_config, dict):
            return bool(retry_config.get("after_output", True))
        return True

    @staticmethod
    def _is_retryable_provider_error(error: BaseException) -> bool:
        if isinstance(error, HTTPStatusError):
            return False
        if isinstance(error, RuntimeStageStallError):
            return error.stage == "response_first_event"
        if isinstance(error, (RequestError, TimeoutError, SSEError)):
            # Locally synthesized HTTP status failures use RequestError too,
            # but those are model/provider decisions rather than transport
            # disconnects. Do not replay them unless key failover handled them.
            return not str(error).lstrip().startswith("Status Code:")
        return False

    def build_request_handlers(self, request_data: "AgentlyRequestData") -> AttemptHandlers:
        max_attempts = self._get_request_retry_max_attempts()
        retry_after_output = self._get_request_retry_after_output()

        async def execute(state: AttemptState) -> AsyncGenerator[tuple[str, Any], None]:
            async for item in self._request_model_legacy(request_data):
                event, payload = item
                if (
                    event == "error"
                    and isinstance(payload, BaseException)
                    and self._is_retryable_provider_error(payload)
                    and state.attempt_index < max_attempts
                    and (not state.output_started or retry_after_output)
                ):
                    raise payload
                yield item

        async def handle_error(error: BaseException, state: AttemptState) -> AttemptDecision:
            if (
                self._is_retryable_provider_error(error)
                and state.attempt_index < max_attempts
                and (not state.output_started or retry_after_output)
            ):
                return AttemptDecision.retry(
                    reason="provider_transient_error",
                    allow_after_output_started=retry_after_output,
                )
            return AttemptDecision.yield_error(error)

        return AttemptHandlers(execute=execute, handle_error=handle_error)

    @core_attempt_runner_entrypoint
    async def request_model(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]:
        runner = AttemptRunner(self.build_request_handlers(request_data))
        async for item in runner.run_stream():
            yield item

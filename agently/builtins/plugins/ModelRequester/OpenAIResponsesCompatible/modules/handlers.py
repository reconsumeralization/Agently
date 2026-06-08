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

from agently.core.model.AttemptRunner import AttemptRunner, core_attempt_runner_entrypoint
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState

if TYPE_CHECKING:
    pass


class OpenAIResponsesCompatibleHandlersMixin:
    if TYPE_CHECKING:
        def _request_model_legacy(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]: ...

    def build_request_handlers(self, request_data: "AgentlyRequestData") -> AttemptHandlers:
        async def execute(_state: AttemptState) -> AsyncGenerator[tuple[str, Any], None]:
            async for item in self._request_model_legacy(request_data):
                yield item

        async def handle_error(error: BaseException, _state: AttemptState) -> AttemptDecision:
            return AttemptDecision.yield_error(error)

        return AttemptHandlers(execute=execute, handle_error=handle_error)

    @core_attempt_runner_entrypoint
    async def request_model(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]:
        runner = AttemptRunner(self.build_request_handlers(request_data))
        async for item in runner.run_stream():
            yield item

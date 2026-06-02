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

import asyncio
import sys
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Literal, cast

from httpx import AsyncClient, HTTPStatusError, ReadError, RequestError, Timeout
from httpx_sse import SSEError, aconnect_sse
from stamina import retry

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.types.data import AgentlyRequestData, SerializableValue
from agently.utils import DataFormatter


class AnthropicCompatibleTransportMixin:
    name: str
    plugin_settings: Any

    if TYPE_CHECKING:
        def _build_headers_with_auth(self, request_data: "AgentlyRequestData") -> dict[str, Any]: ...
        def _build_failover_headers(
            self,
            request_data: "AgentlyRequestData",
            *,
            error: Any,
            status_code: int | None,
            response_text: str | None,
            full_request_data: dict[str, Any],
            stream_started: bool,
        ) -> dict[str, Any] | None: ...
        def _build_full_request_data(self, request_data: "AgentlyRequestData") -> dict[str, Any]: ...

    def _create_async_client(self, **client_options: Any):
        from .. import plugin as plugin_module

        package_module = sys.modules.get(plugin_module.__package__ or "")
        package_client = getattr(package_module, "AsyncClient", AsyncClient)
        plugin_client = getattr(plugin_module, "AsyncClient", AsyncClient)
        client_factory = package_client if package_client is not AsyncClient else plugin_client
        return client_factory(**client_options)

    def _get_timeout_mode(self) -> Literal["http", "first_token"]:
        timeout_mode = self.plugin_settings.get("timeout_mode", "first_token")
        if timeout_mode == "http":
            return "http"
        return "first_token"

    def _get_timeout_configs(self) -> dict[str, Any]:
        return DataFormatter.to_str_key_dict(
            self.plugin_settings.get(
                "timeout",
                {
                    "connect": 30.0,
                    "read": 120.0,
                    "write": 30.0,
                    "pool": 30.0,
                },
            ),
            default_value={},
        )

    def _get_http_timeout(self, *, disable_read: bool = False) -> Timeout:
        timeout_configs = self._get_timeout_configs().copy()
        if disable_read:
            timeout_configs["read"] = None
        return Timeout(**timeout_configs)

    def _get_first_token_timeout_seconds(self) -> float | None:
        read_timeout = self._get_timeout_configs().get("read")
        if isinstance(read_timeout, (int, float)) and read_timeout > 0:
            return float(read_timeout)
        return None

    def _should_use_first_token_timeout(self, request_data: "AgentlyRequestData") -> bool:
        return self._get_timeout_mode() == "first_token" and bool(request_data.stream)

    def _build_stream_stall_error(
        self,
        *,
        timeout_seconds: float,
        message: str,
    ) -> RuntimeStageStallError:
        return RuntimeStageStallError(
            message,
            stage="response_first_event",
            status="stalled",
            idle_seconds=timeout_seconds,
            timeout_seconds=timeout_seconds,
            provider=self.name,
            model=cast(str | None, self.plugin_settings.get("model", None)),
        )

    async def _aiter_with_first_token_timeout(
        self,
        generator: AsyncGenerator[Any, None],
        *,
        timeout_seconds: float | None,
    ) -> AsyncGenerator[Any, None]:
        if timeout_seconds is None:
            async for item in generator:
                yield item
            return

        try:
            first_item = await asyncio.wait_for(anext(generator), timeout=timeout_seconds)
        except asyncio.TimeoutError as e:
            await generator.aclose()
            raise self._build_stream_stall_error(
                timeout_seconds=timeout_seconds,
                message=f"First token timeout after { timeout_seconds } seconds.",
            ) from e

        yield first_item
        async for item in generator:
            yield item

    async def _aiter_sse_with_retry(
        self,
        client: AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, Any],
        json: "SerializableValue",
    ):
        last_event_id = ""
        reconnection_delay = 0.0

        @retry(on=ReadError)
        async def _aiter_sse():
            nonlocal last_event_id, reconnection_delay
            time.sleep(reconnection_delay)
            headers.update({"Accept": "text/event-stream"})
            if last_event_id:
                headers.update({"Last-Event-ID": last_event_id})

            async with aconnect_sse(client, method, url, headers=headers, json=json) as event_source:
                try:
                    async for sse in event_source.aiter_sse():
                        last_event_id = sse.id
                        if sse.retry is not None:
                            reconnection_delay = sse.retry / 1000
                        yield sse
                except GeneratorExit:
                    pass

        return _aiter_sse()
    async def _request_model_legacy(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]:
        headers_with_auth = self._build_headers_with_auth(request_data)
        full_request_data = self._build_full_request_data(request_data)

        if request_data.stream:
            client_options = request_data.client_options.copy()
            if self._should_use_first_token_timeout(request_data):
                client_options.update({"timeout": self._get_http_timeout(disable_read=True)})

            async with self._create_async_client(**client_options) as client:
                client.headers.update(headers_with_auth)
                stream_started = False
                while True:
                    try:
                        sse_generator = await self._aiter_sse_with_retry(
                            client,
                            "POST",
                            request_data.request_url,
                            json=full_request_data,
                            headers=headers_with_auth,
                        )
                        if self._should_use_first_token_timeout(request_data):
                            sse_generator = self._aiter_with_first_token_timeout(
                                sse_generator,
                                timeout_seconds=self._get_first_token_timeout_seconds(),
                            )
                        async for sse in sse_generator:
                            stream_started = True
                            yield sse.event, sse.data
                        break
                    except SSEError:
                        response = await client.post(
                            request_data.request_url,
                            json=full_request_data,
                            headers=headers_with_auth,
                        )
                        if response.status_code >= 400:
                            error = RequestError(
                                f"Status Code: { response.status_code }\n"
                                f"Detail: { response.text }\n"
                                f"Request Data: { full_request_data }"
                            )
                            failover_headers = self._build_failover_headers(
                                request_data,
                                error=error,
                                status_code=response.status_code,
                                response_text=response.text,
                                full_request_data=full_request_data,
                                stream_started=stream_started,
                            )
                            if failover_headers is not None:
                                headers_with_auth = failover_headers
                                client.headers.update(headers_with_auth)
                                continue
                            yield "error", error
                        else:
                            yield "message", response.content.decode()
                        break
                    except HTTPStatusError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=e.response.status_code,
                            response_text=e.response.text,
                            full_request_data=full_request_data,
                            stream_started=stream_started,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except TimeoutError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=None,
                            response_text=None,
                            full_request_data=full_request_data,
                            stream_started=stream_started,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except RequestError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=None,
                            response_text=None,
                            full_request_data=full_request_data,
                            stream_started=stream_started,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except Exception as e:
                        yield "error", e
                        break
            return

        async with self._create_async_client(**request_data.client_options) as client:
            client.headers.update(headers_with_auth)
            while True:
                try:
                    response = await client.post(
                        request_data.request_url,
                        json=full_request_data,
                        headers=headers_with_auth,
                    )
                    if response.status_code >= 400:
                        error = RequestError(
                            f"Status Code: { response.status_code }\n"
                            f"Detail: { response.text }\n"
                            f"Request Data: { full_request_data }"
                        )
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=error,
                            status_code=response.status_code,
                            response_text=response.text,
                            full_request_data=full_request_data,
                            stream_started=False,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", error
                    else:
                        yield "message", response.content.decode()
                    break
                except HTTPStatusError as e:
                    failover_headers = self._build_failover_headers(
                        request_data,
                        error=e,
                        status_code=e.response.status_code,
                        response_text=e.response.text,
                        full_request_data=full_request_data,
                        stream_started=False,
                    )
                    if failover_headers is not None:
                        headers_with_auth = failover_headers
                        client.headers.update(headers_with_auth)
                        continue
                    yield "error", e
                    break
                except RequestError as e:
                    failover_headers = self._build_failover_headers(
                        request_data,
                        error=e,
                        status_code=None,
                        response_text=None,
                        full_request_data=full_request_data,
                        stream_started=False,
                    )
                    if failover_headers is not None:
                        headers_with_auth = failover_headers
                        client.headers.update(headers_with_auth)
                        continue
                    yield "error", e
                    break
                except Exception as e:
                    yield "error", e
                    break

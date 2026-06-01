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

from typing import Any

from agently.types.data import AgentlyRequestData
from agently.utils import DataFormatter
from agently.utils.ModelPool import resolve_api_key_failover


class OpenAIResponsesCompatibleCredentialMixin:
    name: str
    plugin_settings: Any

    def _build_headers_with_auth(self, request_data: "AgentlyRequestData") -> dict[str, Any]:
        auth = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("auth", "None"),
            value_format="serializable",
            default_key="api_key",
        )
        api_key = self.plugin_settings.get("api_key", None)
        auth_api_key = auth.get("api_key", "None")
        if api_key is not None and auth_api_key == "None":
            auth["api_key"] = str(api_key)
            auth_api_key = auth["api_key"]

        headers_with_auth = request_data.headers.copy()
        if "headers" in auth and isinstance(auth["headers"], dict):
            headers_with_auth.update(
                DataFormatter.to_str_key_dict(
                    auth["headers"],
                    value_format="str",
                    default_value={},
                )
            )
        if "body" in auth and isinstance(auth["body"], dict):
            request_data.data.update(**auth["body"])
        if auth_api_key != "None" and "Authorization" not in headers_with_auth:
            headers_with_auth["Authorization"] = f"Bearer { auth_api_key }"
        return headers_with_auth

    def _build_failover_headers(
        self,
        request_data: "AgentlyRequestData",
        *,
        error: Any,
        status_code: int | None,
        response_text: str | None,
        full_request_data: dict[str, Any],
        stream_started: bool,
    ) -> dict[str, Any] | None:
        decision = resolve_api_key_failover(
            self.plugin_settings,
            error=error,
            status_code=status_code,
            response_text=response_text,
            request_data=full_request_data,
            provider=self.name,
            stream_started=stream_started,
        )
        if not decision.retry:
            return None
        return self._build_headers_with_auth(request_data)

    def _build_full_request_data(self, request_data: "AgentlyRequestData") -> dict[str, Any]:
        full_request_data = DataFormatter.to_str_key_dict(
            request_data.data,
            value_format="serializable",
            default_value={},
        )
        full_request_data.update(request_data.request_options)
        return full_request_data

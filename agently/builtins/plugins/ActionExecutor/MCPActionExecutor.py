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

import json
from typing import Any

from agently.utils import LazyImport


class MCPActionExecutor:
    name = "MCPActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "mcp"
    sandboxed = False

    def __init__(self, action_id: str, transport: Any):
        self.action_id = action_id
        self.transport = transport

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    @staticmethod
    def _dump_content_block(block: Any) -> dict[str, Any]:
        if hasattr(block, "model_dump"):
            dumped = block.model_dump()
            return dumped if isinstance(dumped, dict) else {"value": dumped}
        if isinstance(block, dict):
            return dict(block)
        return {"value": block}

    @classmethod
    def _artifact_from_content_block(cls, block: Any) -> dict[str, Any] | None:
        dumped = cls._dump_content_block(block)
        block_type = str(dumped.get("type") or type(block).__name__)
        if block_type.lower() in {"text", "textcontent"}:
            return None
        path = dumped.get("uri") or dumped.get("url") or dumped.get("path")
        media_type = str(
            dumped.get("mimeType")
            or dumped.get("mime_type")
            or dumped.get("media_type")
            or "application/json"
        )
        label = str(
            dumped.get("name")
            or dumped.get("title")
            or path
            or f"MCP {block_type} output"
        )
        artifact: dict[str, Any] = {
            "artifact_type": f"mcp_{block_type.lower()}",
            "label": label,
            "media_type": media_type,
            "value": dumped,
            "meta": {
                "source": "mcp",
                "mcp_content_type": block_type,
            },
        }
        if path is not None:
            artifact["path"] = str(path)
        return artifact

    @staticmethod
    def _has_explicit_artifacts(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and (
                isinstance(value.get("artifact_refs"), list)
                or isinstance(value.get("artifacts"), list)
                or isinstance(value.get("file_refs"), list)
            )
        )

    @classmethod
    def _result_with_artifacts(cls, data: Any, artifacts: list[dict[str, Any]]) -> Any:
        if not artifacts and not cls._has_explicit_artifacts(data):
            return data
        result: dict[str, Any] = {
            "ok": True,
            "status": "success",
            "data": data,
            "result": data,
            "meta": {"source": "mcp"},
        }
        if artifacts:
            result["artifacts"] = artifacts
        if isinstance(data, dict):
            for key in ("artifact_refs", "file_refs"):
                if isinstance(data.get(key), list):
                    result[key] = data[key]
            if isinstance(data.get("artifacts"), list):
                result["artifacts"] = [*artifacts, *data["artifacts"]]
        return result

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = (spec, policy, settings)
        LazyImport.import_package("fastmcp", version_constraint=">=3", auto_install=False)
        LazyImport.import_package("mcp", auto_install=False)
        from fastmcp import Client
        from mcp.types import AudioContent, EmbeddedResource, ImageContent, ResourceLink, TextContent

        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        environment_resources = action_call.get("execution_resource_resources", {})
        transport = self.transport
        if isinstance(environment_resources, dict) and self.action_id in environment_resources:
            transport = environment_resources[self.action_id]

        async with Client(transport) as client:  # type: ignore[arg-type]
            mcp_result = await client.call_tool(
                name=self.action_id,
                arguments=action_input,
                raise_on_error=False,
            )
            if mcp_result.is_error:
                return {"error": mcp_result.content[0].text}  # type: ignore[index]
            artifacts = [
                artifact
                for artifact in (
                    self._artifact_from_content_block(block)
                    for block in list(mcp_result.content or [])
                )
                if artifact is not None
            ]
            if mcp_result.structured_content:
                return self._result_with_artifacts(mcp_result.structured_content, artifacts)
            try:
                content = list(mcp_result.content or [])
                if not content:
                    return self._result_with_artifacts(None, artifacts)
                result = content[0]
                if isinstance(result, TextContent):
                    try:
                        parsed = json.loads(result.text)
                        return self._result_with_artifacts(parsed, artifacts)
                    except json.JSONDecodeError:
                        return self._result_with_artifacts(result.text, artifacts)
                if isinstance(result, (ImageContent, AudioContent, ResourceLink, EmbeddedResource)):
                    data = result.model_dump()
                    return self._result_with_artifacts(data, artifacts)
            except Exception:
                return None

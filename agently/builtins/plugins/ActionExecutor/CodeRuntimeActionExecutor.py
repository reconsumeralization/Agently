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

from typing import Any


class CodeRuntimeActionExecutor:
    name = "CodeRuntimeActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "code_runtime"
    sandboxed = True

    def __init__(self, *, language: str, timeout: int = 60):
        self.language = language
        self.timeout = timeout

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = settings
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        raw_code = action_input.get("source_code", action_input.get("code", ""))
        source_code = "\n".join(str(line) for line in raw_code) if isinstance(raw_code, list) else str(raw_code)
        raw_files = action_input.get("files", None)
        files = {str(key): str(value) for key, value in raw_files.items()} if isinstance(raw_files, dict) else None
        args = action_input.get("args", [])
        if not isinstance(args, list):
            args = [str(args)]
        action_id = str(spec.get("action_id", "run_code"))
        timeout = int(policy.get("timeout_seconds", self.timeout))
        environment_resources = action_call.get("execution_resource_resources", {})
        if isinstance(environment_resources, dict):
            code_resource = environment_resources.get(action_id)
            if code_resource is not None and hasattr(code_resource, "run_code"):
                return await code_resource.run_code(
                    language=self.language,
                    source_code=source_code,
                    files=files,
                    args=[str(arg) for arg in args],
                    timeout=timeout,
                )
        return {
            "ok": False,
            "error": "Code runtime execution resource is not available.",
        }

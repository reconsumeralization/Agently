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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agently import Agently

ACTION_ID = "run_python_code_runtime"
SUPPORTED_ADAPTERS = ("python", "nodejs", "go", "cpp")


def main():
    agent = Agently.create_agent()
    agent.enable_code_runtime(
        language="python",
        action_id=ACTION_ID,
        expose_to_model=False,
        providers=["docker"],
        unsafe_fallback=True,
        isolation="preferred",
        provisioning_profile="strict",
    )

    result = agent.action.execute_action(
        ACTION_ID,
        {
            "source_code": (
                "print('runtime:python')\n"
                "value = 21 * 2\n"
                "print(f'result:{value}')\n"
            ),
        },
    )

    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
    meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
    capabilities = (
        meta.get("provider_capabilities", {})
        if isinstance(meta.get("provider_capabilities"), dict)
        else {}
    )
    supported = ", ".join(SUPPORTED_ADAPTERS)
    print("status:", result.get("status"))
    print("stdout:", str(data.get("stdout", "")).strip().replace("\n", " | "))
    print("provider safety:", capabilities.get("safety_class", "unavailable"))
    print("adapters:", supported)


if __name__ == "__main__":
    main()


# Expected key output with Docker or the explicitly enabled local fallback:
# status: success
# stdout: runtime:python | result:42
# adapters: python, nodejs, go, cpp
#
# Working principle:
# agent.enable_code_runtime(...) registers a provider-neutral Action. It binds a
# TaskWorkspace grant, tries Docker first, materializes an immutable adapter
# bundle, and uses the explicitly authorized unsafe local provider only when no
# eligible Docker provider is available. The built-in adapters are Python,
# Node.js, Go, and C++.

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
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    CODE_RUNTIME_PROFILES,
)

ACTION_ID = "run_python_code_runtime"


def main():
    agent = Agently.create_agent()
    agent.enable_code_runtime(
        language="python",
        action_id=ACTION_ID,
        expose_to_model=False,
        provisioning_profile="developer",
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
    supported = ", ".join(sorted(CODE_RUNTIME_PROFILES))
    print("status:", result.get("status"))
    print("stdout:", str(data.get("stdout", "")).strip().replace("\n", " | "))
    print("catalog:", supported)


if __name__ == "__main__":
    main()


# Expected key output with a local Docker service:
# status: success
# stdout: runtime:python | result:42
# catalog: bash, c, cpp, csharp, go, java, lua, nodejs, perl, php, python, r, ruby, rust, typescript
#
# Working principle:
# agent.enable_code_runtime(language="python", provisioning_profile="developer")
# registers a Docker-backed Action whose ExecutionResource profile may pull the
# missing runtime image, then runs fixed provider-owned entrypoint code. The
# common-language catalog also includes JavaScript/Node.js, TypeScript, C, C++,
# Go, Rust, Java, C#/.NET, PHP, Ruby, Perl, R, Lua, and Bash profiles.

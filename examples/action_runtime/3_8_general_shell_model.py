"""A real model runs an existing Skill script through one general Shell Action.

Set MODEL_BASE_URL, MODEL_API_KEY, MODEL_NAME. Docker with python:3.12-slim
is required by the default offline environment. SHELL_ENVIRONMENT=host is an
explicit unisolated alternative, never an automatic fallback. Windows host
users should set SHELL_BINARY to a real PowerShell executable.

The Host supplies the complete existing Skill guidance and a read-only resource
directory. This example demonstrates pre-supplied context, not automatic Skill
selection. The model chooses the command and reports the fresh token actually
returned by the bundled script. No per-script Action or canned model reply.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from agently import Agently
from agently.types.data import ShellEnvironment
from typing import cast


async def run_example() -> dict[str, object]:
    skill = Path(__file__).resolve().parents[1] / "skills_executor/skills/script-release-probe"
    with tempfile.TemporaryDirectory(prefix="agently-general-shell-") as directory:
        agent = Agently.create_agent().use_task_workspace(directory, mode="read_write")
        agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
            "base_url": os.environ["MODEL_BASE_URL"], "auth": os.environ["MODEL_API_KEY"],
            "model": os.environ["MODEL_NAME"], "request_retry": {"max_attempts": 1},
            "request_options": {"temperature": 0.3, **json.loads(os.getenv("MODEL_REQUEST_OPTIONS", "{}"))},
        })
        environment = cast(ShellEnvironment, os.getenv("SHELL_ENVIRONMENT", "offline"))
        agent.enable_shell(
            environment=environment,
            approval="none",  # Explicit demo authorization; isolation is unchanged.
            binary=os.getenv("SHELL_BINARY"),
            read_paths={"probe": skill}, timeout=30,
        )
        agent.set_action_loop(max_rounds=4)
        execution = (
            agent.create_execution()
            .input({
                "task": "Use the release-probe Skill for release 4.1.4.8 and component ShellRuntime. Run its script once with run_shell and report the observed token and status.",
                "skill_guidance": (skill / "SKILL.md").read_text(encoding="utf-8"),
                "runtime": {"python": sys.executable if environment == "host" else "python", "skill_root_alias": "probe"},
            })
            .output({"probe_token": str, "status": str, "summary": str})
        )
        result = await execution.async_get_data()
        return {"result": result, "environment": environment, "meta": await execution.async_get_meta()}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), ensure_ascii=False, indent=2))

# Expected: status reflects the successful Action and probe_token matches the
# fresh script stdout. Missing providers/dependencies or failed calls must be
# reported honestly. Token and wording change between runs.
# Observed local 27B output: status="success", probe_token="b68b5963";
# two model requests consumed one actual script call with that same token.

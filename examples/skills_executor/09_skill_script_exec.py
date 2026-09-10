"""Select and run a Skill script when a later Session message needs it.

Run:
    DYNAMIC_TASK_MODEL_PROVIDER=ollama \
      python examples/skills_executor/09_skill_script_exec.py

Environment:
    A local Ollama OpenAI-compatible endpoint with ``qwen:latest``, or
    ``DEEPSEEK_API_KEY`` in the shell or .env file.
    Docker is required by the default isolated code-execution provider.

Expected key output shape:
    first selected Skill count: 0
    first action count: 0
    later selected Skill count: 1
    later action count: 1
    script path: scripts/release_probe.py
    execution status: success
    probe token: <fresh hexadecimal value>

Working principle:
    Each user request -> fresh AgentExecution -> current-task Skill selection
    -> exact Skill scope -> one stable Python Action with execution-local
    authorization -> host path/revision/digest resolution -> Docker +
    TaskWorkspace -> Action evidence -> grounded model response
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from agently.types.data import SkillScriptAuthorization  # noqa: E402
from examples.dynamic_task._shared import configure_model  # noqa: E402


SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "script-release-probe"


async def main() -> None:
    configure_model(temperature=0)
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skill_script_"))
    Agently.skills_executor.configure(
        registry_root=str(runtime_dir / "registry"),
        allowed_trust_levels=["trusted"],
    )
    contract = Agently.skills_executor.install_skills(
        SKILL_SOURCE,
        trust_level="trusted",
        update=True,
    )

    agent = Agently.create_agent("skill-script-release-probe").use_task_workspace(
        runtime_dir / "workspace",
        mode="read_only",
    )
    agent.activate_session(session_id="skill-script-late-request")
    agent.use_skills(contract["skill_id"], always=True)

    first = (
        agent.create_execution()
        .input("Greet the user in one concise sentence.")
        .strategy("direct")
    )
    await first.async_prepare_task_context()
    first_result = await first.async_get_text()

    later = (
        agent.create_execution()
        .input(
            "Now use the installed release-probe Skill to run its bundled script for "
            "release 4.1.4.8 and component SkillRuntime. Invoke the available "
            "Action, then report only observed execution evidence."
        )
        .strategy("direct")
        .output(
            {
                "script_path": (str, "The Skill script path actually invoked.", True),
                "probe_token": (str, "Fresh token observed in Action stdout.", True),
                "execution_status": (str, "Observed Action status.", True),
                "summary": (str, "Concise evidence-grounded result.", True),
            },
            format="json",
        )
    )
    await later.async_prepare_task_context()
    if not later.skill_bindings:
        raise RuntimeError("The current request did not select the release-probe Skill.")
    action_id = agent.enable_skill_script_exec(
        later,
        authorization=SkillScriptAuthorization(auto_allow=True),
        language="python",
    )
    result = await later.async_get_data()

    print("first result:", first_result)
    print("first selected Skill count:", len(first.skill_bindings))
    print("first action count:", len(first.local_action_ids))
    print("later selected Skill count:", len(later.skill_bindings))
    print("later action count:", len(later.local_action_ids))
    print("action id:", action_id)
    print("script path:", result["script_path"])
    print("execution status:", result["execution_status"])
    print("probe token:", result["probe_token"])
    print("summary:", result["summary"])


if __name__ == "__main__":
    asyncio.run(main())

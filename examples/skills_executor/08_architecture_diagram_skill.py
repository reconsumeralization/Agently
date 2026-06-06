"""Standard SKILL.md skill that generates an architecture diagram (single_shot).

Run:
    python examples/skills_executor/08_architecture_diagram_skill.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

What this demonstrates
----------------------
A real task driven by the default single_shot SkillsExecutor path:

    * The capability is a single standard ``SKILL.md`` (frontmatter ``name`` /
      ``description`` / ``keywords`` + a Markdown design system). This example
      intentionally declares no ``execution`` metadata, so it stays single_shot.
    * ``agent.use_skills(["architecture-diagram"], mode="required")`` selects it.
      (``mode`` defaults to ``"model_decision"``; we force it here because we know
      exactly which skill must run.)
    * ``run_skills_task(...)`` issues one structured model request that injects the
      full SKILL.md body as instructions and returns a structured result shaped by
      ``output``.
    * The HOST owns the side effect: it writes the returned HTML to disk. The skill
      never touches the filesystem.

The task: draw a real architecture diagram for the current Agently development line. The
architecture brief below is grounded in this repository (agently/core +
agently/builtins), so the diagram describes the actual framework, not a guess.

Expected key output (shape; exact bytes vary by model):
    selected skill: architecture-diagram (required)
    skill status: success
    html bytes: ~6,000-20,000
    diagram saved: .../agently_architecture_generated.html
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

ARTIFACTS_DIR = Path(__file__).resolve().parent / "_artifacts"


# ── The skill: a standard SKILL.md, guidance only ───────────────────────────
SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "architecture-diagram"


# ── The task: a repo-grounded brief of the current Agently architecture ──────
AGENTLY_ARCHITECTURE_BRIEF = """\
Draw the architecture of the current Agently framework development line. Agently is
layered execution & development infrastructure. Show these layers as bands,
top to bottom, with the listed components:

1. Business Application (external/generic): user assistants, internal tools,
   automations, evaluators, workflows.
2. Developer Surface / Agent Components (facade colour): Agently (main facade),
   Agent, TriggerFlow, plus Agent Extensions (Skills, Action, Session/ChatSession,
   ConfigurePrompt, AutoFunc, KeyWaiter, StreamingPrint).
3. Core Contracts (core colour, from agently/core): Agent, Session, Prompt,
   ModelRequest, ModelResponseResult, DynamicTask, TaskDAGExecutor, TriggerFlow,
   ExecutionEnvironment, Action, Tool, SkillsExecutor, PluginManager, EventCenter.
4. Plugins / Providers (plugin colour, from agently/builtins/plugins):
   ModelRequester (OpenAICompatible), PromptGenerator, ResponseParser,
   AgentOrchestrator, SkillsExecutor (SKILL.md single_shot / staged / react),
   TaskDAGPlanner,
   ActionExecutor, ActionFlow, ActionRuntime, ExecutionEnvironmentProvider,
   ToolManager.
5. Execution Environment Manager & Capabilities (capability colour): Sandbox,
   Process, Filesystem, Network, Credentials, MCP, Resources.
6. External Dependencies & Integrations (external colour): Model Providers
   (DeepSeek, Ollama, any OpenAI-compatible endpoint), ChromaDB, FastAPI.
7. Runtime Events, Diagnostics & DevTools (event-bus colour): core EventCenter
   dispatches RuntimeEvent records; the main framework DevTools bridge projects
   them to ObservationEvent payloads consumed by the agently-devtools companion
   (observe / evaluate / playground). Core never imports the companion eagerly.

Key principle to convey: the separation rule
"core contract -> plugin/provider impl -> built-in capability Action ->
agent extension -> business application".

Title it "Agently Framework Architecture" with subtitle noting the current
development line.
"""


def install_skill(runtime_dir: Path) -> str:
    skill_src = SKILL_SOURCE
    Agently.skills_executor.configure(registry_root=str(runtime_dir / "registry"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


async def main() -> None:
    configure_model(temperature=0.2)
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_archdiagram_"))
    skill_id = install_skill(runtime_dir)

    agent = Agently.create_agent("architecture-diagrammer")

    execution = await agent.async_run_skills_task(
        AGENTLY_ARCHITECTURE_BRIEF,
        skills=[skill_id],
        mode="required",
        output={
            "html": (str, "The complete self-contained HTML document for the diagram."),
            "notes": (str, "One-line summary of the layers represented."),
        },
    )

    print("=" * 64)
    print("selected skill:", skill_id, "(required)")
    print("skill status:", execution.status)
    if execution.status != "success":
        print("output:", execution.output)
        return

    html = str((execution.output or {}).get("html", ""))
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ARTIFACTS_DIR / "agently_architecture_generated.html"
    out_path.write_text(html, encoding="utf-8")

    print("notes:", (execution.output or {}).get("notes", ""))
    print(f"html bytes: {len(html):,}")
    print("diagram saved:", out_path)
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())

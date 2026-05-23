"""Standard SKILL.md skill that generates an architecture diagram (prompt-only).

Run:
    python examples/skills_executor/08_architecture_diagram_skill.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

What this demonstrates
----------------------
A real task driven entirely by the rewritten, prompt-first SkillsExecutor:

    * The capability is a single standard ``SKILL.md`` (frontmatter ``name`` /
      ``description`` / ``keywords`` + a Markdown design system). No ``skill.yaml``,
      no stages, no embedded actions.
    * ``agent.use_skills(["architecture-diagram"], mode="required")`` selects it.
      (``mode`` defaults to ``"model_decision"``; we force it here because we know
      exactly which skill must run.)
    * ``run_skills_task(...)`` issues ONE structured model request that injects the
      full SKILL.md body as instructions and returns a structured result shaped by
      ``semantic_outputs``.
    * The HOST owns the side effect: it writes the returned HTML to disk. The skill
      never touches the filesystem.

The task: draw a real architecture diagram for the latest Agently version. The
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
SKILL_MD = """\
---
name: architecture-diagram
description: >-
  Create a professional, dark-themed software/system architecture diagram as a
  single self-contained HTML file with inline SVG. Use when asked to visualize
  system components, layers, services, or how parts of a codebase fit together.
keywords: [architecture diagram, system design, svg, html, components, layers]
---

# Architecture Diagram

Produce ONE self-contained HTML document (embedded CSS + inline SVG, no
JavaScript, no external images; Google Fonts link is allowed). It must render
correctly when opened directly in a browser.

## Visual design system
- Background `#020617` with a subtle 40px grid pattern.
- Font: JetBrains Mono (monospace) via Google Fonts.
- Component boxes: rounded rects (`rx="6"`), 1.5px stroke, semi-transparent
  fills. Colour components by role:
  - facade / developer surface: fill `rgba(8,51,68,0.4)`, stroke `#22d3ee`
  - core contracts: fill `rgba(6,78,59,0.4)`, stroke `#34d399`
  - plugins / providers: fill `rgba(120,53,15,0.3)`, stroke `#fbbf24`
  - execution environment / capabilities: fill `rgba(76,29,149,0.4)`, stroke `#a78bfa`
  - observation / event bus: fill `rgba(251,146,60,0.3)`, stroke `#fb923c`
  - external / generic: fill `rgba(30,41,59,0.5)`, stroke `#94a3b8`
- Component name at 11px white bold; sublabel at 8-9px `#94a3b8`.
- Arrows via an SVG `marker` arrowhead; draw connecting arrows before boxes so
  they sit behind them.

## Layout rules
- Lay the system out as labelled horizontal bands stacked top-to-bottom, one band
  per architectural layer, with each layer's components inside its band.
- Keep ≥40px vertical gaps between stacked rows; never overlap boxes.
- Add a short header (title + subtitle) and a small legend mapping each colour to
  a layer. Place the legend outside every band.

## Output
Return the complete HTML document as a single string. Do not include commentary
outside the HTML.
"""


# ── The task: a repo-grounded brief of the latest Agently architecture ──────
AGENTLY_ARCHITECTURE_BRIEF = """\
Draw the architecture of the Agently framework (version 4.1.2.x). Agently is
layered execution & development infrastructure. Show these layers as bands,
top to bottom, with the listed components:

1. Business Application (external/generic): user assistants, internal tools,
   automations, evaluators, workflows.
2. Developer Surface / Agent Components (facade colour): Agently (main facade),
   Agent, TriggerFlow, plus Agent Extensions (Skills, Action, Session/ChatSession,
   ConfigurePrompt, AutoFunc, KeyWaiter, StreamingPrint).
3. Core Contracts (core colour, from agently/core): Agent, Session, Prompt,
   ModelRequest, ModelResponse, DynamicTask, TaskDAGExecutor, TriggerFlow,
   ExecutionEnvironment, Action, Tool, SkillsExecutor, PluginManager, EventCenter.
4. Plugins / Providers (plugin colour, from agently/builtins/plugins):
   ModelRequester (OpenAICompatible), PromptGenerator, ResponseParser,
   AgentOrchestrator, SkillsExecutor (SKILL.md prompt-only), TaskDAGPlanner,
   ActionExecutor, ActionFlow, ActionRuntime, ExecutionEnvironmentProvider,
   ToolManager.
5. Execution Environment Manager & Capabilities (capability colour): Sandbox,
   Process, Filesystem, Network, Credentials, MCP, Resources.
6. External Dependencies & Integrations (external colour): Model Providers
   (DeepSeek, Ollama, any OpenAI-compatible endpoint), ChromaDB, FastAPI.
7. Observation, Diagnostics & DevTools (event-bus colour): core EventCenter emits
   serialized ObservationEvent objects consumed by the agently-devtools companion
   (observe / evaluate / playground). Core never imports the companion.

Key principle to convey: the separation rule
"core contract -> plugin/provider impl -> built-in capability Action ->
agent extension -> business application".

Title it "Agently Framework Architecture" with subtitle noting v4.1.2.x.
"""


def install_skill(runtime_dir: Path) -> str:
    skill_src = runtime_dir / "architecture-diagram"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
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
        semantic_outputs={
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

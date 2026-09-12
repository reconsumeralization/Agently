"""Explicit long-form field production, followed by a summary of the actual body.

Set AGENT_EXECUTION_OLLAMA_MODEL to an installed Qwen model (prefer local 27B).
No small output window is imposed to manufacture continuation.

Expected key output (local qwen3.8:27b-mlx, 2026-09-10):
    model_request_count=5
    body: Chinese prose preserving explicit confirmation and original-file retention.
    summary: A summary of the actual body; returned object remains Handoff.
The observed chain is plan -> body -> chapter summary -> body -> final summary.
Three runs completed normally without continuation. This does not demonstrate
natural truncation recovery or full lifecycle acceptance.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently, LongContent  # noqa: E402 - runnable from a source checkout
from examples.agent_auto_orchestration._ollama_qwen import configure_ollama_qwen  # noqa: E402


class Handoff(BaseModel):
    body: LongContent = Field(
        description="中文交接说明，分别说明责任确认和交接操作。使用连续自然段，不添加全文标题；不重复论述。",
    )
    summary: str = Field(
        description="基于已完成 body 的实际内容，给出简短中文摘要；不得补入正文没有的事实。"
    )


async def main() -> None:
    model = configure_ollama_qwen(max_tokens=None)
    parent = (
        ROOT / ".example_runtime" / "agent_auto_orchestration" / "field_long_content"
    )
    parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    agent = Agently.create_agent("field-long-content").use_task_workspace(workspace)
    agent.set_settings("plugins.AgentExecution.long_content.max_sections", 2)
    execution = (
        agent.input(
            {
                "任务": "为维护交接写一份简洁的说明，分为责任确认、交接操作两个互补部分，随后给出实际正文摘要。",
                "已确认规则": [
                    "责任人必须明确确认后才能锁定责任，未回复不能视为默认接受。",
                    "交接时保留配置文件原件，另存工作副本，操作后核对修改记录。",
                ],
                "要求": "全文使用中文；只使用给定信息，必要建议应标明为建议，不虚构执行结果。",
            }
        )
        .output(Handoff)
        .auto_continue()
    )
    result = await execution.async_get_data()
    typed = await execution.async_get_data_object()
    assert isinstance(typed, Handoff)
    assert typed.model_dump() == result
    meta = await execution.async_get_meta()
    print(f"model={model}")
    print(f"runtime_root={workspace}")
    run_info = meta["diagnostics"].get("execution_run")
    if run_info is None:
        raise RuntimeError("Expected execution request-count diagnostics.")
    print(f"model_request_count={run_info['model_request_count']}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

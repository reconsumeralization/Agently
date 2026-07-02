from __future__ import annotations

from _shared import create_task_agent, run_and_print, stream_options


SOURCE_PACKET = """
资料边界：
- [L1] 本示例只使用脚本内提供的 LMCC 青少年组学习范围摘要，不代表完整官方大纲。
- [L2] 允许考察：大模型基础概念、提示词设计、工具调用、模型评测、AI 安全与负责任使用。
- [L3] 不允许考察：训练大型模型的底层工程细节、GPU 集群调度、未给出的数学公式推导。
- [L4] 题目需要区分题干、选项或作答要求、答案、解析、考察点。
- [L5] 教师需要看到覆盖说明和是否超出资料边界的自查。
"""


def main() -> None:
    agent, provider, workspace = create_task_agent(
        "agent-task-example-lmcc-exam",
        workspace_prefix="lmcc-mock-exam",
        language="zh-CN",
    )
    execution = agent.create_task(
        goal=(
            "请基于下面的资料边界，为 LMCC 青少年组方向生成一套 3 题短版迷你模拟题。"
            "只能使用资料包中的信息，不要声称已经读取完整官方大纲。输出中文，包含题目、答案、"
            "简明解析、考察点、覆盖说明和超纲自查。每题解析不超过 80 个汉字；覆盖说明和超纲自查"
            "使用短列表，不写长表格，整体保持紧凑，方便 delta 流和 Workspace 回读展示。\n\n"
            f"{SOURCE_PACKET}"
        ),
        success_criteria=[
            "生成 3 道题，题型可以混合但结构必须清楚。",
            "每道题都有答案、解析和考察点。",
            "覆盖说明引用资料 id，例如 [L2] 或 [L3]。",
            "明确披露本示例资料边界，不能伪称完整官方大纲。",
        ],
        options=stream_options(),
    )
    run_and_print(execution, provider=provider, workspace=workspace)


if __name__ == "__main__":
    main()

# Expected key output:
# prints a [DELTA_STREAM] section from get_async_generator(type="delta");
# status is completed, accepted is true, execution_strategy is selected by
# AgentTask auto, and the delta stream is Chinese with 3 questions plus boundary
# citations such as [L1], [L2], and [L3].

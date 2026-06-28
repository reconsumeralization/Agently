# Agent 自动编排

Agently 4.1.3 将 `agent.start()` 作为 Agent turn 的默认用户层入口。它仍然返回
业务结果，但 Agent 可以在显式注入候选能力后，路由到普通模型响应、Actions 或
Skills Executor。

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

候选注入是边界。如果没有注册 Actions、Skills 或 Skills Packs，`agent.start()`
仍然是普通模型请求。

`TaskDAG` 是 DAG 基石能力。`DynamicTask` 保留为 DAG planning/execution 之上的
兼容与便利 facade，不再是第二套推荐任务生命周期，也不是 AgentTaskLoop 的自动策略
route。当应用或可视化自动化界面拥有图形结构并需要显式运行该图时，使用
TaskDAG / DynamicTask。

quick prompt 链会创建 execution-scoped draft。Agent 可以作为服务单例保存共享
settings、模型激活、Actions、Skills、Workspace 和 `define(...)` / `always=True`
prompt；同一条链里的 `.input(...)`、`.system(...)`、`.output(...)`、附件和本次
execution options 会写入隔离的 `AgentExecution` draft：

```python
results = await asyncio.gather(
    agent.input("Summarize request A").async_start(),
    agent.input("Summarize request B").async_start(),
    agent.input("Summarize request C").async_start(),
)
```

多语句 setup 应显式拿住 execution draft：

```python
execution = agent.create_execution()
execution.input("Review this renewal risk.")
execution.output({"answer": (str, "final answer", True)})
result = await execution.async_start()
```

不要再依赖 `agent.input(...); agent.output(...); await agent.async_start()`
来累计本轮 execution prompt。Agent 生命周期状态使用 `always=True`、
`set_agent_prompt(...)` 或 `agent.define(...)`。只有明确需要低层 request-builder
兼容面时才使用 `agent.create_request(...)` / `agent.request`。

已验收开发线的路由是候选驱动、确定性优先：required Skills 候选进入 Skills
route。当同时存在多个可选候选，例如 model-decision Skills 和普通 Actions 时，
默认由模型选择 route；如果只有一个可选候选，则直接选择该 route。

公开 Agent API 仍由 core 持有，但路线规划和执行由 active
`AgentOrchestrator` plugin 通过 `AgentOrchestrator` protocol 承担。这样
Skills、DAG substrate 和后续 route 实现都可以替换，而不需要 core 知道内置
plugin 的内部实现。

## Goal Pursuit

当业务目标需要有边界的 planning、execution、evidence、verification 和 replan
闭环时，使用 `agent.goal(goal_or_goals, success_criteria=None)`。
`agent.goals(...)` 只是同一个入口的复数 alias。

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .require_actions("write_file")
    .goals(
        [
            "构建一个小型产品官网。",
            "准备上线检查清单。",
        ],
        success_criteria=[
            "最终产物是一个可运行页面文件。",
            "页面内容覆盖所有输入的业务事实。",
            "执行证据包含文件写入、读回和内容检查。",
        ],
    )
    .effort(
        "high",
        budget={
            "iteration_limit": 4,
            "model_call_limit": 10,
            "wall_time_seconds": 300,
        },
        planning={"depth": "expanded", "max_plan_items": 8},
        verification={"strictness": "strict"},
        replan={"policy": "on_verification_failure", "limit": 2},
        progress={"detail": "phase"},
    )
    .start()
)
```

简单代码仍然可以只写 `.effort("low" | "medium" | "high")`。展开形式仍然属于
同一个入口：effort 只控制策略和资源强度，不决定 execution 是否进入目标追求。
`budget.iteration_limit`、`model_call_limit` 和 `wall_time_seconds` 是软策略元数据：
它们可以影响 planning、reflection、repair 倾向和 evidence 深度，但不会静默设置
task-loop `max_iterations` 或 AgentExecution hard limits。宿主需要硬资源控制时，
应显式使用 task options 或 `limits={...}`。默认情况下，AgentTask 不施加模型请求数、
迭代数、TaskBoard tick 数或 Action round 数配额；no-progress 和 idle timeout 仍作为
卡死执行的活性保护，而不是策略效果证据。完成仍然必须同时通过 model verification 和
host guards。
对于 task-strategy execution，effort 还控制 reflection 密度：`low` 总是记录最终
reflection，只在 planner 标记的重要过程节点记录过程 reflection；`medium` 在每个
大任务节点或 TaskBoard card/tick 后记录 reflection；`high` 在每个框架可观测的
bounded step、Action/ACP call、TaskBoard card 和最终结果后记录 reflection。
Reflection 会作为 Workspace evidence 进入 verifier/replan 输入，但它本身不是完成证据。

`execution.step_plan` 只作为兼容指导保留，普通用户不需要显式写出来。AgentTaskLoop
不再把 TaskDAG / DynamicTask 作为内部 bounded step 策略；旧模型输出
`dynamic_task` / `execution_dag` 或旧配置 `execution={"step_plan": "dag"}` 会降级为
direct bounded execution，并留下 diagnostics。当宿主拥有提交式 DAG 或可视化自动化图
时，单独使用 TaskDAG / DynamicTask。

## AgentTask Loop

当业务目标需要一个有边界的多轮闭环，而不是一次 direct AgentExecution 时，使用
`agent.create_task(...)`。它返回一个 task-strategy `AgentExecution` draft；
内部保留的 `AgentTask` record 运行一个由单个 Agent 持有的任务：计划、执行一个
bounded step、写入 Workspace 证据、验证、必要时 replan，最后以 complete 或
blocked 结束。

内部实现上，`flat` 和 `taskboard` 是协调策略，不是两套独立 execution carrier。
两者都会把 strategy 拥有的 work unit 下沉到内部 Block carrier，再进入
`ExecutionPlan` / Blocks / TriggerFlow evidence 路径。TaskBoard primitive 仍然
负责 board schedule、dependency state 和 patch validation；AgentTask 只把 bounded
card execution evidence 交给 carrier 承载。

在当前 4.1.3 线里，这是一个加固后的有边界公开 task-loop strategy，
不是完整未来版 AgentTask 系统。`agent.create_task_loop(...)` 是同一个长任务
strategy 的显式写法，适合代码需要把 strategy 选择说清楚的场景。两个 API 仍然
返回 `AgentExecution`；新代码应通过 `execution.get_result()` 或 execution 的
stream/meta facade 消费 data、text、stream、metadata、status 和 task refs，而不是
把 `AgentTask` 当成第二套 public lifecycle。

`execution="auto"` 是默认 task execution strategy。`auto` 会让 AgentTaskLoop
先请求模型做自然语言 task-shape analysis，再输出很薄的结构化 `execution_hint`；
随后由策略层把实际执行形态解析为 `flat` 或 `taskboard`。这个 hint 只是策略证据；
TaskBoard 不负责判断任务复杂度，verifier 也不能把 hint 当成完成证据。需要强制线性
loop 时使用 `execution="flat"` 或 `.strategy("flat")`；只有 host 明确想用 TaskBoard
时才使用 `execution="taskboard"` 或 `.strategy("taskboard")`。嵌套的 AgentExecution
默认继承父执行的 strategy context，除非子执行显式调用 `.strategy(...)` 覆盖。

```python
agent.language("zh-CN")

execution = agent.create_task(
    goal="将旧版 Agently 脚本迁移到当前 4.1.x API，并确保它可以运行。",
    success_criteria=[
        "原始失败已被记录。",
        "脚本不再使用不兼容的旧 API。",
        "修复后的脚本可以运行，并产出预期结构化结果。",
    ],
    workspace="./.agently/tasks/legacy-script-upgrade",
    max_iterations=4,
    verify="before_done",
    options={
        "agent_task": {
            "stream_progress": True,
            "stream_progress_background": True,
            "stream_snapshots": True,
            # 可选：用单独 model key 基于 snapshot 生成自然语言进展。
            # 不设置时使用模板 progress，不增加模型请求。
            # "progress_model_key": "cheap-progress-model",
            # 可选兼容别名：只影响 progress narration。
            # "progress_language": "zh-CN",
        },
    },
)

result = execution.get_result()

async for item in result.get_async_generator():
    if (item.meta or {}).get("stream_kind") == "progress_delta":
        print(item.delta or "", end="", flush=True)
    elif (item.meta or {}).get("stream_kind") == "progress":
        print("[PROGRESS]", item.value["message"])
    elif (item.meta or {}).get("stream_kind") == "snapshot":
        print("[SNAPSHOT]", item.path, item.value["snapshot"])

data = await result.async_get_data()
meta = await result.async_get_meta()
task_refs = result.task_refs
```

每轮会把 planning decision、execution observation、verification evidence、
evidence links 和 checkpoint 写入 Workspace。checkpoint 通过 Workspace
checkpoint-store port 写入，task evidence 关系通过 `workspace.link_evidence(...)`
记录。下一轮通过 `workspace.build_context(...)` 取得 ContextPackage，因此 loop
可以把证据带入下一轮，但 Workspace 不会变成自主规划器。

AgentTask 的验证仍由模型判断拥有，但最终验收采用保守 guard。loop 会规范化
verifier 输出；当仍有 missing criteria、必需 action evidence 失败或被 blocked、
仍需 approval，或必需 final deliverable 缺失时，不会把任务标记为 complete。
这些 guard 决策会记录在 task diagnostics 中，让下一轮基于具体证据 replan，而不是
接受一个证据不足的完成声明。

task-strategy AgentExecution stream 会发出结构化结果事件，并默认发出紧凑中间状态
`snapshot` item。
自然语言 `progress` item 需要通过
`options={"agent_task": {"stream_progress": True}}` 显式打开；内置描述是模板文本，
未配置 `progress_model_key` 时不会增加模型请求或 token 消耗。设置
`progress_model_key` 后，AgentTask 会用这个单独 model key 在后台基于已产生的
snapshot 和任务元数据总结进展。模型生成的进度会先以
`stream_kind="progress_delta"` 的 delta 事件边生成边输出，然后再以完整的
`stream_kind="progress"` item 发出，便于日志和 UI 收敛状态。推荐用
`agent.language("zh-CN")` 设置 Agent 级语言策略，它会影响最终输出、关键过程文本、
progress 文本，以及 Search/Browse 的 locale hint；也可以用
`execution.language("zh-CN")` 只作用于单次 AgentExecution draft。单次执行仍可用
`options={"agent_task": {"progress_language": "zh-CN"}}` 作为兼容别名只控制 progress
语言，也可以用 `Agently.set_settings("agent_task.progress.language", "zh-CN")`
设置 progress 全局默认；`auto` 保持框架默认。主循环不会为了 progress 多产出字段，也不会等待 progress
总结完成。progress narrator 失败属于 side-channel diagnostics 和
warning 级 runtime event，不会把主 execution 标记成 `model.request_failed`。
progress model 只接收 operator-safe snapshot；底层 Workspace/SQLite fallback
等 developer diagnostics 仍保留在 snapshot 和 `task.meta()["diagnostics"]`，
但不会进入 progress model 输入。

对于文本消费方，`get_async_generator(type="delta")` 仍然是公开文本流。task-strategy
execution 中，它既包含模型生成的文本增量，也会把部分过程事件投影成段落文本：
模板 progress、snapshot、heartbeat 状态、phase 状态、retry marker 和任务终态结果。UI 如果需要
原始结构化事件载荷，包括 `path`、`value`、`delta`、`is_complete` 和 `meta`，应使用
`type="instant"`。

长时间静默等待时，如果超过
`agent_task.heartbeat_interval_seconds` 秒没有任何其他 stream item，
AgentTask 可以发出 `agent_task.heartbeat`。默认间隔是 10 秒。heartbeat
只是一条观测状态：它帮助 UI 和日志消费者知道当前阶段，但不满足证据要求，
不掩盖卡死，也不替代 request/no-progress/task deadline timeout。任何正常的
progress、snapshot、child-execution、delta 或 phase 事件都会重置静默计时，
因此活跃流不会被 heartbeat 污染。

任务终态和 artifact 验收是两件事。`completed` 表示 verifier 已验收结果
（`accepted=True`、`artifact_status="accepted"`）。`max_iterations` 仍可能留下
有用的 Workspace 文件或 checkpoint，但它只是 partial artifact
（`accepted=False`、`artifact_status="partial"`），不是已完成的业务结果。

当某个 bounded step 或 TaskBoard card 返回短小 `artifact_markdown` 正文或分段
`artifact_manifest` 时，AgentTask 会通过绑定的 Workspace 写入交付物，并立刻
readback `path`、`bytes`、`sha256`、有界 preview 和 `file_refs`。对于长篇、
分段或重自然语言交付物，应先选择合适的内容载体：单一自由正文可以直接生成自然
Markdown / plain text，不必为了携带正文而声明 `.output()`；如果调用方需要可独立寻址
的字段，可在适合目标模型和消费方的情况下使用
`.output(..., format=...)` 的 `xml_field`、`hybrid` 或 `yaml_literal`；状态、
证据和校验保持为单独的紧凑 judgment/readback contract。若 AgentTask 必须交付可信文件
artifact，再使用 `artifact_manifest.sections` 加 Workspace readback。模型声明的
`file_refs` 只作为 diagnostics，只有框架完成 Workspace 写入和读回后才是可信证据，
同时仍保留真实 `final.md` 或其他成品文件供 host 复核。

同一套 ref-backed 路径也可以用于中间过程。某个步骤可以下载文件、保存网页快照、
写入生成代码、沉淀搜索笔记或类似 memory 的任务笔记，或把大段抽取文本持久化为 Workspace / Action artifact refs。
热路径 prompt 应只携带紧凑 refs 和有界 preview；后续 block 真的需要正文时，再通过
`read_file(max_bytes=..., offset=...)` 或 artifact readback 打开 scoped snippet。
这些中间 refs 是执行证据，不是最终交付物存在的证明。发现了某个 URL、路径、下载或
快照 ref，也不代表已经读过其内容；在有界 readback 或 content preview 可见之前，
它仍是 `ref_only`。source-grounded 交付物要么用结构化 `target_refs` 请求读取这些
未读 refs，要么把它们标为 discovered-only，不能声称事实来自未读内容。如果 Action
artifact readback 暴露了已物化下载文件的 Workspace `file_refs`，TaskBoard readback
会把这些嵌套 refs 提升为 card-level `file_refs`，让后续工作可以继续用 Workspace
readback，而不是依赖埋在 JSON preview 里的路径字符串。若非最终 TaskBoard card 提议写入 `final.md` 这类
required final path，AgentTask 会把该中间 artifact 重定位到
`working/taskboard/<card-id>/...`，并把声明的最终路径留给最终 synthesis/finalization card。
由框架生成且显式标记了 required final deliverable path 的 final repair / continuation
card 可以写入该最终路径，避免 repair 只反复产出 working evidence 文件而无法满足 host guard。
Flat source refs 也遵守同一边界：repository clone/list manifest 中发现的文件路径在文件读取、
artifact readback 或有界 content preview 出现前都是 `ref_only`。verifier 或 repair
planner 可以复用这些精确路径作为检索目标，但不能把它们当成文件内容事实的证明。

Flat 和 TaskBoard 的 work unit 也会收到同一份 task context contract，其中包含
紧凑的 `current_time` 事实：`utc`，以及本地时区可识别时的 `local` 和
`timezone`。对于 current、latest、recent 或 as-of 任务，除非调用方明确给了更具体
日期，否则应使用这些时间上下文。该 contract 只是 model decision、planning、evidence
selection 和 source-boundary handling 的上下文，不会设置模型调用、工具调用、节点数、
迭代数或 wall-clock 硬上限。

TaskBoard readback card 可以用有界冷读回读取 Action artifact refs 和可信的
Workspace file refs。框架生成的 readback card 会把 evidence scope 扩展到直接依赖和
上游 evidence card，所以 control-card readback 仍能读取更早 evidence-gathering card
产出的 Action refs。若框架生成的 continuation card 仍报告同一批 readback 不足，框架不会
继续递归合成新的 readback/continuation 链；该 card 必须提出其他可执行工作，或者带
diagnostics 保持 blocked。
当缺失证据是新的具体 URL、路径或 ref，而不是已有 Action / Workspace ref 时，control card
应返回 `next_board_action="readback"` 加结构化 `target_refs`。AgentTask 会把这个紧凑意图
转成可执行 action 的 evidence card，由它负责下载、保存快照或物化目标，再运行 continuation
card。只写在 `gaps` 自然语言里的 URL 属于 diagnostics，不会被解析成可执行目标。
如果 control card 返回的是 `next_board_action="patch"` 加 Workspace 文本 patch
proposal，AgentTask 会把补丁应用到绑定的 Workspace 文件，写回后再读回并返回可信
`file_refs`。这只负责物化修补事实；最终是否完成仍由终局验收和 host guards 判断。
对于 `completed` 且 `sufficient=True` 的 control 输出，非致命 `gaps` 不会阻止 Workspace
artifact 物化；`remaining_work`、blocked 状态、repair 或 readback 仍会阻止写入。写入
artifact 只是为后续 readback 和 verification 创建证据，不代表最终任务已经被接受。
Flat 和 TaskBoard 都不需要在每个中间 work unit 后额外调用独立 verifier。Flat step
可以返回 `ready_for_final_verification=false` 并携带 `remaining_work`；下一轮 iteration
消费这些新事实并决定下一步行动。TaskBoard 中真正消费 dependency evidence 的下游
card 判断这些信息是否足够完成自己的目标。独立 verifier 应保留给终局验收、fan-in/control
合流验收、证据/artifact 边界审计、矛盾或高风险复核。

AgentTask observation 也会在结构化 stream 上发布归一化 action 事实：
`agent_task.action.started`、`agent_task.action.completed` 和
`agent_task.action.failed`。这些事件从已有 Action records 汇总安全的 input summary、
result preview、refs、耗时、diagnostics 和 work-unit 归属。它们只是给 DevTools、UI 和
实验日志使用的 observation facts；是否有用、质量如何、任务是否完成仍由下游 consumer、
终局 verifier/final control 和 strategy 判断。

写入成功且读回可信时，verifier 输入会包含这些读回字段和
`capability_evidence.artifacts.readback`；在 `max_iterations=1` 下，真实已写入且可读回的
artifact 不应只因为 evidence 链缺失而变成 partial。如果读回失败，或缺少可信的
`path` / `bytes` / `sha256` 证据，diagnostics 会使用
`agent_task.workspace_artifact.readback_failed` 或
`agent_task.workspace_artifact.readback_insufficient`，明确报告 Workspace artifact
readback missing/insufficient，而不是泛化为预算或迭代不足。

如果结构化 task input 或 output contract 声明了必需交付物，AgentTask 的 host
guard 会要求这些 Workspace 文件真实存在并可读回，才允许验收完成。verifier
声称文件已存在并不够，必须以声明的最终路径 Workspace readback 为准。

对于框架介绍、API 指南这类公开参考材料，task verifier 的 accepted 仍不等于来源质量
保证。应把当前 docs/spec/source references 喂入任务，或增加 Agently model-judge /
source-reference 校验，避免旧 API、泛化 API 只因为 task-level verifier 接受草稿而通过。

带强结构合同的中间处理步骤必须在所属的 `ModelRequest` 或 `AgentExecution` 上使用
Agently `.output(..., format=...)`。不要只为了控制长篇自然语言正文而给纯正文生成请求
添加限制性的 JSON `.output()` contract。紧凑控制 payload 可以使用 JSON；当内容较重的
payload 确实需要结构化合同，可按场景使用 `xml_field` 表达 XML-like 字段边界，使用
`hybrid` 表达正文加类型化控制字段，或在适合目标模型和消费方时使用 `yaml_literal`。
如果声明的非 JSON 格式解析失败，Agently 会尝试用 JSON 解析兜底，并且只有解析结果是
dict 时才接受。execution output contract 存在时，task `final_result` 也执行同一守卫。

`examples/agent_task/goal_effort_public_stream.py` 是这个合同的公开链式 API
流式证明。它运行
`.goal(...).effort(...).input(...).output(...).strategy("flat")`，消费
`get_async_generator()`，流式输出模型生成的进度 delta，并检查 execution prompt
snapshot 是否进入 AgentTaskLoop 的 planning、execution 和 verification。
`examples/agent_task/goal_pursuit_acceptance_matrix.py` 仍保留为 accepted 与
non-accepted 终态矩阵脚本。

`examples/agent_task/real_complex_bundle_goal_stream.py` 是同一路径下的高封装
真实复杂任务证明。它通过公开 Agent capability API 挂载 Search、AMap MCP、
Workspace 文件 Actions 和 CocoonAI `architecture-diagram` Skill，然后让任务循环
生成 operator 日报、杭州商务游记和 HTML/SVG 架构图，并流式输出自然语言进度
delta。它使用多轮 bounded direct steps，以证明当前公开 AgentTask 生命周期，而不
依赖混合 DynamicTask/DAG 执行。较底层的
`examples/blocks/07_real_complex_bundle_stream.py` 只保留为 Blocks 外部能力
substrate 探针，不作为推荐业务入口。

`examples/agent_task/agently_architecture_diagram_task.py` 是同一路径下更长的
设计文档实验。它使用 `.goal(...).effort(...).strategy("task")` 作为
AgentTaskLoop draft 的兼容写法，同时使用仓库源码资料 Action、Workspace 文件
Actions，以及独立 Agently model judge，生成并复查一份层次清晰的 Agently 架构图。
除非 host 显式选择 `flat` 或 `taskboard`，实际执行形态仍由 task strategy 层解析。

第一版公开 slice 有明确边界：单任务、单 Agent owner、约 2-5 次迭代，并通过
`AgentExecution` 执行 bounded step。这些 step 可以使用调用方已经在 Agent 上启用的
Actions、Skills 或 DAG 候选，也可以使用当前 execution 上临时挂载的 DAG 候选。
AgentTask 不提供多任务协同、后台自治、分布式租约、step 内 pause/resume 或
长期记忆管理。这个 slice 的崩溃恢复通过 `agent.resume(...)` /
`agent.async_resume(...)` 暴露，它会重建 task-strategy `AgentExecution`，而不是把
AgentTask 暴露成第二套公开生命周期。

### 崩溃后恢复任务

AgentTaskLoop 在每次迭代完成后都会持久化一份可恢复快照。若进程崩溃，可以恢复成一个
新的 `AgentExecution` 并从下一次迭代继续。已完成的迭代不会重复执行：

```python
execution = await agent.async_resume("issue-123")   # 或 agent.resume("issue-123")
result = await execution.async_start()              # 从第 N+1 次迭代继续
meta = await execution.async_get_meta()
```

恢复会从 Workspace 读取该任务最新快照，还原迭代历史与累计的 required 能力进度；若不存在
可恢复快照则抛出 `ValueError`。崩溃时正在执行中的那次迭代会被重新规划，因此非
replay-safe 的 step 副作用由宿主负责。当 result 带有可恢复的 `task_refs` 时，
`AgentExecutionResult.resume()` 会委托同一个 Agent resume facade；否则返回不支持恢复的
响应。`resume_task(...)` 只保留为 `resume(...)` 的兼容别名。

当示例需要验证模型生成内容的语义质量时，应组合 deterministic smoke check 和第二个
Agently model-judge request。文件存在、问题数量、source label 可见等结构检查只能作为
smoke gate；语义验收应使用带每条规则 evidence 和 boolean 结果的 judge schema。

示例里的业务系统 fixture 可以 mock，但只能返回业务事实、记录、政策或有缺陷/不完整的
source data。不要让 mock 返回 pass/fail、隐藏标准答案或本地质量 verdict。若场景需要判断
artifact 是否正确处理了缺陷数据或冲突事实，应由 AgentTask verifier 或独立 Agently
model-judge request 基于明确规则和证据做判断。

## Execution 对象

当调用方需要路线诊断、多种结果视图或过程流式输出时，使用
`agent.create_execution()`：

```python
execution = agent.create_execution()
execution.input("Summarize the reviewed DAG snapshot for the operator.")
execution.info({"dag_snapshot": snapshot})

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

execution 对象沿用模型 response 的消费风格：`get_data`、`get_text`、
`get_meta`、`get_generator` 以及对应 async 方法。
默认 stream 是 `type="delta"`，产出纯文本字符串；模型流式请求重放时会产出保留的
`"<$retry>{reason}</$retry>"` 边界标记。需要结构化执行事件时使用
`type="instant"`：`AgentExecutionStreamData` 保留熟悉的 `path`、`value`、
`delta`、`is_complete` 字段，并增加过程级事件需要的 route metadata。

`create_execution()` 创建一个 AgentExecution draft。只有 prompt 的 draft 会作为
直接模型请求执行。DynamicTask/TaskDAG workflow 先通过
`Agently.create_dynamic_task(...)` 或 `TaskDAGExecutor(...)` 运行，再把 snapshot
作为 evidence 传给后续 AgentExecution。当开发者自己编写循环，或 task strategy
需要一个有边界的单步 AgentExecution 时，用 `lineage` 和 `limits` 表达边界：

```python
execution = agent.input("Try one bounded fix step.").create_execution(
    lineage={
        "task_id": "issue-123",
        "iteration_id": "iter-2",
        "step_id": "execute-fix",
        "parent_execution_id": "exec-prev",
    },
    limits={
        "max_model_requests": 3,
        "max_seconds": 180,
        "max_no_progress_seconds": 60,
    },
)
```

这仍然只是一次 AgentExecution，不是多轮循环本身。`lineage` 提供稳定关联，
`limits` 提供跨普通模型 route 和 Skills model stage 共享的模型请求预算计数。
无限预算用 `None` 表达。

如果有边界的 execution 超出模型请求预算，Agently 会抛出
`AgentExecutionLimitExceeded`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。execution meta 仍然可以检查，
并会记录 `status="blocked"`，以及 `diagnostics` 里的 limit event。

对于卡住的执行，`limits.max_seconds` 是整个 AgentExecution 的硬截止时间。在
Goal Pursuit / task strategy 运行中，这个 wall-clock budget 由 AgentTaskLoop
拥有，并返回带 task metadata 的 `timed_out` 任务结果；其它 route 会把硬截止
时间暴露为 `RuntimeStageStallError`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。`limits.max_no_progress_seconds`
是 idle stall 边界：route selection、模型流、Skills、ActionRuntime
任何被接受的运行进展都会刷新计时。`async_get_meta()` 仍然可检查，并记录
`status="timed_out"` 或 `status="stalled"`，以及 `diagnostics["timeouts"]` /
`diagnostics["stalls"]` 和最后一次进展事件。

Provider 与 response materialization 等待有独立配置：

```python
Agently.set_settings("OpenAICompatible.stream_idle_timeout", 60.0)
Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 60.0)
Agently.set_settings("response.materialization_idle_timeout", 60.0)
```

`stream_idle_timeout` 限制首个 provider stream event 之后相邻事件之间的空闲间隔。
首事件超时和 stream idle timeout 都会抛出 `RuntimeStageStallError`，在 requester
能够识别时带上 provider/model 字段。
`response.materialization_idle_timeout` 限制最终 text、data、object 或 meta 从
response parser materialize 出来的等待时间。`None` 表示无限制；`-1` 作为兼容写法可用。
如果 provider 或最终响应构造在 materialization 完成前发出显式 stream error，
`get_text()` / `get_data()` / `get_meta()` 会传播该原始错误，而不是继续等到
materialization timeout。

高频 RuntimeEvent 出口应该通过 Event Center 请求摘要投递，而不是让
AgentExecution 在信号源降频：

```python
Agently.event_center.register_hook(
    handler,
    event_types="model.response.delta",
    hook_name="app.delta_summary",
    delivery_policy={"mode": "summary", "emit_interval": 0.1, "max_items": 20},
)
```

AgentExecution stream API 保持 raw。某个 hook 主动选择 summary delivery 时，
Event Center 摘要事件会包含 `meta["coalesced"]`、`coalesced_count` 和源事件 id。

`async_get_meta()` 会包含 `lineage`、`limits`、
`route`、`route_plan`、`logs`、`diagnostics` 和 `workspace_refs`。`logs` 是跨
route 稳定检查运行事实的位置，例如模型响应 id、
ActionRuntime action records 和 artifact refs：

```python
meta = await execution.async_get_meta()
meta["route"]["selected_route"]
meta["logs"]["model_response_ids"]
meta["logs"]["action_logs"]
meta["logs"]["artifact_refs"]
```

当 `model_request` route 使用 Actions 时，execution 会通过 meta 和
`actions.<action_id>` 这类 stream event 暴露 action records。需要持久化业务证据时，
host 应读取框架 action record 或 artifact，再显式写入 Workspace；不要为了让 host
能存储结果而要求模型把 raw action stdout 再复制一遍。

每条过程流 item 也会带上关联 metadata：

```python
item.meta["execution_id"]
item.meta["lineage"]["task_id"]
```

默认 Agent 带有 lazy Workspace binding；也可以在 `create_execution()` 之前用
`agent.use_workspace(...)` 覆盖为显式 root 或 provider。AgentExecution 仍然不会自动
决定什么应该进入记忆；调用方应从 execution 侧显式持久化：

```python
workspace_record = await execution.async_record_workspace(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
    checkpoint=True,
)
```

这个 helper 会通过 execution 绑定的 Workspace provider surface 写入。请求
checkpoint 时，它会使用 checkpoint-store port，并在 AgentExecution record 与
checkpoint 之间写入 evidence link。record id、checkpoint id 和 evidence link id
都可以从 `meta["workspace_refs"]` 读取。Workspace 保持 durable substrate，不需要
理解 AgentExecution 策略语义。下一步再由调用方调用 `workspace.build_context(...)`。

开发排障时，可以挂 EventCenter observation hook，或临时打开控制台明细：

```python
Agently.event_center.register_hook(print, event_types=None, hook_name="debug")
agent.set_settings("debug", "detail")
```

这只用于调试 route selection、model request、ActionRuntime 或 Workspace 持久化。
问题定位后，应从示例和生产代码中移除 debug hook/settings。

## 提交式 DAG 输入

通过独立 DynamicTask facade 运行的提交式 DAG，其 task `inputs` 继续使用 DAG 运行时
占位符，例如 `${INIT.ticket}` 和 `${DEPS.lookup}`。graph input 按以下顺序解析：

```text
async_run(graph_input=...)
> {"target": task_target}
```

这让 DAG 输入显式独立于 AgentExecution prompt 路由：

```python
task = Agently.create_dynamic_task(
    target="review ticket",
    plan=graph,
    handlers=handlers,
)
snapshot = await task.async_run(graph_input={"ticket": "TICKET-OK"})
```

如果 AgentExecution 需要使用结果，把 snapshot 作为普通 evidence 传入
`input(...)`、`info(...)` 或 Workspace record。DAG snapshot 本身不等于更大业务目标
已经完成。

## Skills 语义

`agent.use_skills(...)` 和 `agent.use_skills_packs(...)` 注册 route candidates。
它们默认不再表示“把完整 Skill guidance 注入普通模型请求”。完整 Skill guidance
属于真正规划或执行该 Skill 的 Skills route。如果路由没有选中 Skills，普通请求
只接收安全的能力摘要。

如果调用方必须强制执行 Skills，使用 `agent.run_skills_task(...)`。

## 过程流

Agent execution stream item 保留熟悉的 instant stream 形态：

```python
item.path
item.value
item.delta
item.event_type
item.is_complete
item.route
item.stage_id
item.task_id
item.action_id
item.graph_id
```

Executor route 会桥接 route 与 ModelRequest instant checkpoint，让服务能流式输出
route decision、task/action 进度、选定模型字段 delta 和最终 semantic outputs。
如果 TriggerFlow-backed route 失败，Agent execution stream 会关闭，并把原始
错误抛给消费者，而不是让 `get_async_generator(...)` 一直等待后续 item。

独立 TaskDAG model 节点如果需要字段级展示，应消费 TriggerFlow runtime stream 并
归一化 `task_dag.model_field` item：

```python
task = Agently.create_dynamic_task(target="reply", plan=graph, handlers=handlers)
execution = task.compile(graph).create_execution(auto_close=False)
async for item in execution.get_async_runtime_stream({"ticket": ticket}, timeout=None):
    if item.get("type") == "task_dag.model_field" and item.get("field_path") == "reply":
        print(item.get("delta") or "", end="", flush=True)
```

这样可以把 AgentExecution stream 语义和独立 DAG runtime stream 语义分开。

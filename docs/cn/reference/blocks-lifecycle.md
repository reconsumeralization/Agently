---
title: Blocks lifecycle
description: 当前 Blocks plan、execution、context-read、result 与 evidence contract。
keywords: Agently, Blocks, ExecutionPlan, ExecutionBlockGraph, context_read
---

# Blocks lifecycle

Blocks 把已校验的 `ExecutionPlan` lowering 为 `ExecutionBlockGraph`，绑定到
TriggerFlow，再把终态映射成 result/evidence view。它是执行 substrate，不是
Skill engine，也不是持久化 manager。

## 当前 block 所有权

- `model_request`：一次模型语义请求；
- `action_call`、`mcp_tool_call`、`script_action`：显式 capability call；
- `context_read`：从调用方绑定的 `ContextReader` 做一次 bounded read；
- `validation`：显式边界上的 host 或 model validation；
- `flow_segment`：可信 developer-owned subflow；
- 当前 plan contract 定义的 join/control blocks。

不存在 `skill_activation` block。AgentExecution 在 Blocks 介入前，把
SkillLibrary revision 绑定到 TaskContext。也不存在 `workspace_operation`
block：文件写入属于 TaskWorkspace Actions，record 写入属于 RecordStore，审批
属于对应 policy/runtime owner。

## Context read

```python
task_context = TaskContext("refund-review")
task_context.put(
    role="instruction",
    content="超过 USD 1000 的退款需要财务审批。",
    source_ref="policy/refunds",
    required=True,
)
reader = task_context.reader(
    consumer="blocks:refund-review",
    phase="execution",
)

graph = Agently.blocks.compile({
    "plan_id": "refund-context",
    "plan_blocks": [{
        "id": "policy",
        "plan_block_id": "context_read",
        "kind": "context_read",
        "intent": "读取退款政策",
        "bound_inputs": {
            "operation": "read",
            "query": "退款审批",
            "explicit_refs": ["policy/refunds"],
        },
    }],
})
execution = Agently.blocks.bind_runtime(graph).create_execution(
    record_store=False,
    runtime_resources={"context_reader": reader},
)
```

`context_read` 只接受 `read`、`search`、`scoped_search`。它返回
ContextPackage projection、locator refs 与 bounded evidence snippets。写入、
links、checkpoints 和其他副作用 fail closed。

`source_kinds`、`path`、`pattern`、`method`、`top_n` 等 source-specific
filters 会转发给绑定的 reader。它们约束 candidate collection，不代替语义
相关性选择。

## Result 与 evidence

`Agently.blocks.map_result(...)` 投影语义终态输出。
`Agently.blocks.map_evidence(...)` 保留 block results、action evidence、context
refs/snippets、diagnostics 与 graph lineage。宿主应基于这些事实做分析，不能
把 runner boolean 当成最终语义判断。

可运行示例见
`examples/blocks/01_blocks_lifecycle_infrastructure_smoke.py`。

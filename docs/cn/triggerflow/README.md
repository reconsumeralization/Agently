# TriggerFlow

建议阅读顺序：

1. [概览](overview.md)：TriggerFlow 的边界、心智模型和最小 flow。
2. [Lifecycle](lifecycle.md)：open / sealed / closed，以及 start / close 入口。
3. [State 与 Resources](state-and-resources.md)：state、flow_data、runtime_resources 怎么选。
4. [事件与流](events-and-streams.md)：`emit` / `when` 与 runtime stream。
5. [模式](patterns.md)：分支、match、batch、for_each、事件驱动循环。
6. [Sub-Flow](sub-flow.md)：父子 flow 组合。
7. [持久化与 Blueprint](persistence-and-blueprint.md)：save/load 与定义导出。
8. [Pause 与 Resume](pause-and-resume.md)：人工介入和外部恢复。
9. [模型集成](model-integration.md)：在 chunk 内调用 agent / request。
10. [兼容](compatibility.md)：迁移旧 `.end()`、`set_result()`、`runtime_data`。

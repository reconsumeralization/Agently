---
title: Agently 4.1.4.3 Release Notes
description: 修复 ModelRequest 与 AgentExecution 直接使用 Pydantic v2 输出模型的兼容性。
keywords: Agently, 4.1.4.3, Pydantic, 结构化输出, ModelRequest, AgentExecution
---

# Agently 4.1.4.3 Release Notes

Agently 4.1.4.3 是一个聚焦结构化输出的兼容性补丁，保留 4.1.4.2 的公开 API
与所有权边界。

## Pydantic 输出模型

把 Pydantic v2 `BaseModel` 类型直接传给 `.output(...)` 时，请求可能在 provider
调用前的初始化阶段失败。Prompt generator 把这个类型按普通 Python type 处理，并在
缺少必填字段的情况下尝试实例化它。

4.1.4.3 会先识别 `BaseModel` 类型，递归把嵌套字段投影为 Prompt schema，同时保留
原始模型类型作为最终结果模型。

```python
class MeetingMinutes(BaseModel):
    topic: str
    decisions: list[Decision]

minutes = (
    agent
    .input(meeting_text)
    .output(MeetingMinutes, format="json")
    .get_result()
    .get_data_object()
)

assert isinstance(minutes, MeetingMinutes)
```

direct `ModelRequest` 与 `AgentExecution.output(...)` 都支持这份契约。原有字典、
tuple、JSON Schema 和显式嵌套 output 描述保持可用。

## 验证

- 回归覆盖 direct request、嵌套 Pydantic 模型和 AgentExecution direct strategy。
- 发布前把候选 wheel 安装到隔离的 Python 3.10 环境中验证。
- 底层修正在进入发布准备前已通过完整仓库测试与静态类型门禁。

关联 issue：[#329](https://github.com/AgentEra/Agently/issues/329)。

## 兼容性

- Package version：`4.1.4.3`。
- Release manifest：`compatibility/releases/4.1.4.3.json`。
- Python：`>=3.10`。
- 推荐 DevTools 版本仍为 `agently-devtools >=0.1.10,<0.2.0`。

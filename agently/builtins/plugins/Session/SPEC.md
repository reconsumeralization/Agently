# Session Plugin 规格草案（AgentlyMemoSession）

## 概览
AgentlyMemoSession 是会话容器，负责承接 Agent 的对话历史与记忆数据，并通过可配置策略控制上下文规模。支持同步/异步调用、可插拔策略/处理器与 JSON/YAML 结构化导入导出。

## 功能目标（先定方向，后细化）
1) Agent 集成接管
   - Session 可嵌入 Agent；Agent 默认可不装载 Session。
   - 一旦装载，Agent 的 chat_history/prompt 相关读写由 Session 接管。
   - add_chat_history 等写入操作，改为写入 Session 内部数据结构（full/current/memo）。

2) 两种会话管理模式
   - 轻量模式（ChatHistory 管理）
     - 以“多会话 chat_history 隔离”为核心。
     - 通过最大消息长度/数量上限直接截取会话消息列表。
     - 不强制使用 memo/总结。
   - 记忆模式（Memo 管理）
     - 通过不同策略总结关键信息，保证上下文长度可控。
     - 允许使用 LLM 做压缩与更新。
     - 外层可扩展存储/检索能力，但不属于本插件范围。

3) 可扩展与可替换
   - 策略、resize 处理、附件摘要均可替换。
   - 同步/异步使用体验一致。

4) 结构化导入/导出
   - 支持 Session 信息的 JSON/YAML 导出与读入。
   - 不提供 Storage 层的持久化实现。

5) Session 与 Agent 的职责边界
   - Session 在 core 层可独立使用，负责会话数据管理（full/current/memo/resize），不关心最终接入位置。
   - Agent 集成通过 agent_extensions 完成（胶水层），避免让 Session 插件反向依赖 Agent。

## SessionExtension（Agent 集成胶水层）
### 职责
- 把 Session 插件能力接入 Agent：绑定/解绑、chat_history 代理、请求前后钩子。
- 不承载 Session 的数据结构与策略逻辑。

### 建议接口
- `attach_session(session: Session | None = None, *, mode: str | None = None)`：绑定或自动创建 Session。
- `detach_session()`：解除绑定。
- `session` / `get_session()`：获取当前绑定 Session（只读）。
- `set_chat_history(...)` / `add_chat_history(...)` / `reset_chat_history(...)`：已绑定则代理到 Session。

### 钩子行为
- `request_prefixes`：请求前将 `prompt.chat_history` 设为 `session.current_chat_history`。
- `finally`：请求完成后将 user/assistant 消息写回 Session。

## 非目标（暂不覆盖）
- 追求 token 级精确计量或特定模型厂商的截断策略。
- 强制 memo schema（memo 内容的强校验由上层负责）。
- 存储后端与持久化实现。

## 数据模型
- id: unique session id (uuid hex string)
- full_chat_history: list of ChatMessage
- current_chat_history: list of ChatMessage (pruned view)
- memo: SerializableData (usually a dict)
- _turns: count of assistant responses
- _last_resize_turn: last turn index when resize ran
- _memo_cursor: index into full history used for incremental memo updates

## Settings
默认值：
- session.resize.every_n_turns: 8
- session.resize.max_messages_text_length: 12000
- session.resize.max_keep_messages_count: None
- session.memo.instruct: list of instructions for memo update
- session.memo.enabled: False

不再兼容旧配置键：
- session.resize.max_current_chars (legacy of max_messages_text_length)
- session.resize.keep_last_messages (legacy of max_keep_messages_count)

### Settings 减负（推荐入口）
**目标**：降低用户配置成本，优先使用更少更清晰的入口参数。

推荐入口（面向大多数用户）：
- session.mode: "lite" | "memo"（默认 lite）
- session.limit: {chars?: int, messages?: int}
- session.resize.every_n_turns: int（可选）

高级入口（面向高级用户）：
- session.resize.max_messages_text_length
- session.resize.max_keep_messages_count
> 不再支持 legacy keys。

优先级规则（必须保持一致）：
1) 显式 handler（policy/resize/attachment/memo）优先于任何 settings。
2) session.limit 覆盖 session.resize.*
3) session.memo.enabled 若显式设置，覆盖 session.mode 推导

## 配置与 Handler 优先级（分层入口）
- 高级开发者可直接传入 `policy_handler` / `resize_handlers` / `attachment_summary_handler`。
- 当显式提供 handler 时，其优先级高于配置值，默认策略与默认处理器不再生效。
- 未提供 handler 时，才使用配置驱动的默认策略与默认处理器。

## 生命周期
1) append_message(message)
   - Adds message to full_chat_history and current_chat_history.
   - If role == "assistant", increments _turns.

2) async_judge_resize(force=False)
   - If force is provided, returns a decision with reason "force".
   - Otherwise consults policy handler (default policy described below).

3) async_resize(force=False)
   - Uses decision from async_judge_resize.
   - Invokes resize handler by type ("lite" or "deep").
   - Updates full_chat_history, current_chat_history, memo.
   - Stores last resize reason in memo["last_resize"].

同步包装：
- judge_resize and resize are syncified wrappers over async_* functions.

## 默认 Resize 策略
触发优先级：
1) If current history approx chars >= max_messages_text_length => deep resize.
2) If current history length > max_keep_messages_count => lite resize.
3) If turns since last resize >= every_n_turns => lite resize.
4) Otherwise no resize.

文本长度为近似值：role 长度 + content 估算长度，对 string/list/结构化内容做尽力处理。

## 默认 Lite Resize 处理器
- Incremental memo update:
  - Uses full_chat_history[_memo_cursor:] as delta.
  - If memo is dict and memo enabled, updates via model.
  - Advances _memo_cursor to end of full history.
- Pruning:
  - If max_keep_messages_count is None: prune by max_messages_text_length.
  - Else keep last N messages, then also prune by max_messages_text_length.
- Records memo["last_resize"] = {type: "lite", turn: _turns, reason: "lite_resize"}.

## 默认 Deep Resize 处理器
- Full memo update:
  - Chunk full history by max_messages_text_length.
  - Update memo for each chunk.
  - Advances _memo_cursor to end of full history.
- Pruning:
  - If max_keep_messages_count is None: prune by max_messages_text_length.
  - Else keep last N messages, then also prune by max_messages_text_length.
- Records memo["last_resize"] = {type: "deep", turn: _turns, reason: "deep_resize"}.

## 通过模型更新 Memo
- Enabled only when session.memo.enabled is True and messages are non-empty.
- Input schema:
  - current_memo: dict
  - messages: serialized chat history
  - attachments: summarized attachments
- Output schema expects a "memo" dict, otherwise falls back to raw dict.

## 附件摘要
- Scans message content parts when content is a list.
- For non-text parts, emits:
  - type: part type
  - ref: first found of file/url/path/id/name
  - meta: subset of name, mime_type, size, width, height, duration

## 结构化导入/导出
- 支持导出完整 Session 状态为 JSON/YAML。
- 支持从 JSON/YAML 读入 Session 状态。
- Storage 持久化不在本插件范围内。

## 扩展点
- set_policy_handler(handler)
- set_resize_handlers(type, handler)
- set_attachment_summary_handler(handler)

Handlers can be sync or async; they are wrapped to async internally.

## 错误处理
- Invalid policy result type raises TypeError.
- Missing resize handler raises KeyError.
- load_* expects a dict; non-dict raises TypeError.

## 已知限制/待讨论
- 文本长度估算是启发式的，不是 token 级精度。
- memo schema 未强约束，上层使用需防御性处理。

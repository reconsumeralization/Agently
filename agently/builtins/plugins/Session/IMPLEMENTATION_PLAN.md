# Session 实现方案设计（草案）

## 现状盘点（基于当前代码）
- Session 插件已注册：默认加载 `AgentlyMemoSession`（见 `_default_init.py`）。
- core `Session` 只是插件的薄封装（`agently/core/Session.py`），选择 active 插件并转发接口。
- Agent 当前直接操作 `Prompt`：`set_chat_history/add_chat_history/reset_chat_history` 直接改 `agent_prompt`。
- ModelRequest 支持 `extension_handlers`（request_prefixes/finally 等），可用于在请求前后注入逻辑。
- `AgentlyMemoSession` 已实现 full/current history、memo、resize 策略、JSON/YAML 导入导出。
- `ChatSessionExtension` 通过运行时变量存储 chat_history，但与 Session 插件尚未打通。

## 目标体验（来自需求）
1) 轻量管理：用户只需设置关键参数（长度上限等）即可使用。
   - Session 主要职责：存储多会话 chat_history（最好保留全量），以及为 prompt 提供 current chat_history。
2) Memo 管理：用户可自定义压缩策略（可用 LLM），压缩结果可 JSON/YAML 导出与读入。
3) Agent 集成：Agent 装载 Session 后，chat_history 的读写由 Session 接管。

## Settings 减负（必须落地）
**目标**：普通用户只需要 1~2 个入口设置即可启用会话管理，不必理解一堆 resize 细节。

### 推荐入口（面向用户）
- `session.mode`: "lite" | "memo"（默认 lite）
- `session.limit`: `{chars?: int, messages?: int}`（可选）
- `session.resize.every_n_turns`: int（可选）

### 高级入口（面向高级用户）
- `session.resize.max_messages_text_length`
- `session.resize.max_keep_messages_count`
> 不再兼容旧配置键（`session.resize.max_current_chars` / `session.resize.keep_last_messages`）。

### 优先级与合并规则（必须写入实现与测试）
1) 显式 handler（policy/resize/attachment/memo）优先于任何 settings。
2) `session.limit` 覆盖 `session.resize.*`。
3) `session.memo.enabled` 若显式设置，覆盖 `session.mode` 的推导。

### 必须同步到文档与测试
- 文档中标注“减负入口”为首选配置方式。
- 测试覆盖 `session.limit` 与 `session.mode` 的语义和优先级。

## 最终策略（推荐架构）
**一个核心 Session 插件 + 策略扩展**，并将“Agent 集成”放在 core 层完成：
- core `Session` 保持稳定 API（创建/设置/代理插件方法）。
- `AgentlyMemoSession` 作为默认实现，提供 lite + memo 两种模式。
- 模式切换使用设置/策略，不拆成多个 Session 插件，避免 API 与状态重复。
- Agent 集成逻辑放在 `agent_extensions` 内完成（SessionExtension），避免业务插件反向依赖 Agent。

## 模式设计
### 轻量模式（Lite）
- 配置原则：只需设置长度上限即可用。
- 建议配置：
  - `session.mode = "lite"`
  - `session.limit`（chars/messages 二选一或同时提供）
  - `session.resize.every_n_turns` 可选
- 行为：
  - full_chat_history 作为全量记录。
  - current_chat_history 为裁剪后内容，用于 prompt。

### 记忆模式（Memo）
- 配置原则：允许用户自定义策略与处理器。
- 建议配置：
  - `session.mode = "memo"`
  - `session.limit`（可选）
  - 自定义 policy/resize/attachment handlers（或使用默认实现）。
- 行为：
  - full_chat_history 用于增量或分块总结。
  - memo 作为“稳定信息摘要”。
  - memo 支持 JSON/YAML 导出/读入（不等于持久化）。

## 配置与 Handler 优先级（分层入口）
- **简单入口**：只用配置值即可（适合默认/轻量用户）。
- **高级入口**：直接传入 `policy_handler` / `resize_handlers` / `attachment_summary_handler`。
- 当显式提供 handler 时，**优先级高于配置**，默认策略与默认处理器不再生效。
- 未提供 handler 时，才使用配置驱动的默认策略与默认处理器。

## JSON/YAML 导入导出
- Session 支持结构化导出与读入（JSON/YAML）。
- 不提供 Storage 级别持久化；持久化由外层插件或用户决定。

## Agent 集成策略（关键）
### 目标
- Agent 装载 Session 后，`chat_history` 读写由 Session 接管。
- prompt 中的 `chat_history` 内容由 Session 的 `current_chat_history` 动态填充。

### 建议方案
1) **新增 SessionExtension（agent_extensions 层）**
   - 负责将 Session 插件能力“接入” Agent（胶水层）。
   - Session 本体仍由 core `Session` + Session 插件实现。

2) **在 Agent/Extension 增加 Session 绑定接口**
   - `Agent.attach_session(session: Session | None = None, mode: str | None = None)`
   - `Agent.detach_session()`
   - `Agent.session` 只读属性（或可替换）。

3) **改造 Agent 的 chat_history API**（仅在 Session 已绑定时）
   - `set_chat_history`/`add_chat_history`/`reset_chat_history` 代理至 Session：
     - `set_chat_history`：重置 Session 并写入 full/current。
     - `add_chat_history`：追加到 full/current，并触发 resize 判定（必要时执行）。
     - `reset_chat_history`：清空 Session（full/current）。
   - 未绑定 Session 时保持原行为。

4) **请求前：注入 current_chat_history 到 prompt**
   - 使用 `ModelRequest.extension_handlers.request_prefixes`：
     - 在每次请求前，将 `prompt.chat_history = session.current_chat_history`。

5) **请求后：写回对话到 Session**
   - 使用 `extension_handlers.finally`：
     - 时机：在请求结束时执行（包括 streaming 完成后）。
     - 从 prompt 取用户输入（例如 `prompt.get("input")` 或 request prompt 的标准 slot）。
     - 从结果取 assistant 输出（默认使用 `result.get_text()` 或解析后的消息）。
     - 可选参数：通过数据路径列表筛选“进入记录的结构化节点”（面向 output format）。
     - 可选 handler：直接读取本次请求的返回 data，自定义生成 `ChatMessage` 列表。
     - 统一以 `ChatMessage` 写入 Session。

6) **与 ChatSessionExtension 的关系**
   - 可选择：
     - A. 让 ChatSessionExtension 在 Session 已绑定时委托 Session。
     - B. 保持现状并逐步弃用，避免重复维护。

## SessionExtension 接口草案（agent_extensions）
### 职责
- 作为 Agent 与 Session 的胶水层：接管 chat_history 读写、请求前后钩子、绑定/解绑 Session。
- 不实现 Session 的数据结构与策略逻辑（仍由 Session 插件负责）。

### 建议方法
- `attach_session(session: Session | None = None, *, mode: str | None = None)`
  - 若未传入 session：自动创建 `core.Session()` 并绑定。
  - 可选 `mode` 用于快速配置（lite/memo）。
- `detach_session()`：解除绑定并清理钩子。
- `get_session()` / `session` 属性：返回当前绑定的 Session（只读）。
- `set_chat_history(...)` / `add_chat_history(...)` / `reset_chat_history(...)`
  - 若已绑定 Session：代理到 Session。
  - 否则回退到 BaseAgent 的原有行为。

### 钩子行为（通过 extension_handlers）
- `request_prefixes`：请求前将 `prompt.chat_history` 设为 `session.current_chat_history`。
- `finally`：请求完成后将用户输入与 assistant 输出写回 Session。

### 与配置的关系
- Session 的模式与策略仍由 Session 插件配置或 handler 决定。
- Extension 仅负责“接入”，不持有业务策略。
## 实施步骤（建议分阶段）
1) **阶段一：核心 API 与文档**
   - 明确 Session 模式与配置约定。
   - 完成中文 spec（当前文件）并同步简要使用示例。

2) **阶段二：Agent 绑定与代理**
   - 在 `Agent` 增加 attach/detach/session 接口。
   - chat_history API 在 Session 存在时代理到 Session。

3) **阶段三：请求前后钩子接入**
   - request_prefix：把 Session current_chat_history 写入 prompt。
   - finally：把 user/assistant 消息写回 Session。

4) **阶段四：轻量模式简化入口**
   - 提供辅助方法或快捷配置（例如 `agent.enable_session_lite(...)`）。

## 变更影响与风险
- 默认不装载 Session：保持现有 Agent 行为不变。
- 装载 Session 后：chat_history 行为将改变（从 prompt 内存切换为 Session 数据）。
- 需要明确“输入/输出消息的来源”与“消息格式标准化”规则，避免重复写入或丢失。
- 不再兼容旧配置键（`session.resize.max_current_chars` / `session.resize.keep_last_messages`）。

## 已确认设计决策（由待确认问题落地）
- `session.mode` 作为一级配置入口；`session.memo.enabled` 仅作为显式覆盖。
- `add_chat_history` 语义：追加到 full/current，并触发 resize 判定（必要时执行）。
- 记录时机：请求结束后写回；记录内容支持“数据路径列表”或“自定义 handler”两种扩展方式。

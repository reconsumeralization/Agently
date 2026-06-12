# Development

建议按这个顺序读：

1. [Coding Agents](coding-agents.md)：用 Agently-Skills companion repo 帮 Codex、Claude Code、Cursor 等工具获得当前 Agently 指引。
2. [Skills Executor](skills-executor.md)：框架内通过 Agent API、plan 和 Actions 消费 runtime skills。
3. [Agently 4.1.3.6 Release Notes](release-notes-4.1.3.6.md)：AgentExecution ownership、Result-first 消费、stream-end hardening 和 bounded task-loop slice。
4. [Agently 4.1.3.5 Release Notes](release-notes-4.1.3.5.md)：4.1.3.5 的历史 release note，记录 settings-owned 输出默认值和 prompt 隔离工作；当前合同已由 AgentExecution draft 模型承接。
5. [Agently 4.1.3.4 Release Notes](release-notes-4.1.3.4.md)：结构化输出解析加固、请求重试、运行时能力策略和 AgentTaskLoop first public slice。
6. [Agently 4.1.3.3 Release Notes](release-notes-4.1.3.3.md)：typed settings/options、model profiles、API key pool failover、runtime handler ownership、core package refactors 和 image input。
7. [Agently 4.1.3.2 Release Notes](release-notes-4.1.3.2.md)：bounded AgentExecution task steps、Workspace-backed step context、runtime stall control 和 EventCenter RuntimeEvent delivery。
8. [Agently 4.1.3.1 Release Notes](release-notes-4.1.3.1.md)：Workspace foundation、Recall skeleton 和显式多轮任务信息管理。
9. [Agently 4.1.3 Release Notes](release-notes-4.1.3.md)：4.1.3 最终运行时目标、推荐代码形态和业务价值。
10. [Release Workflows](release-workflows.md)：当前主仓库的 docs、安装包和 PyPI 发布自动化。

DevTools 归 [Observability](../observability/)，因为它消费 observation event。Action、MCP 和服务 API 放在各自文件夹里。

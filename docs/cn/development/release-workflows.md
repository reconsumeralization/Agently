---
title: Release Workflows
description: 当前 Agently 主仓库的 docs、安装包和 PyPI 发布自动化。
keywords: Agently, release, GitHub Actions, PyPI, docs
---

# Release Workflows

> 语言：[English](../../en/development/release-workflows.md) · **中文**

本文记录当前主仓库的自动化入口。

## 开发版本与分支模式

Agently 的版本开发要区分三个概念：

- **当前工作版本**：下一次预定公开发布的工作批次，记录在
  `compatibility/in-development.json`。例如，即使更大的 roadmap 目标是
  `4.1.4`，当前工作版本也可以是 `4.1.3.3`。
- **版本目标**：当前工作批次推进的产品或架构目标，例如 `4.1.4` 的 AgentTask V1。
- **任务分支**：按工作类型和范围命名的实现分支，而不是按版本号命名。

日常开发不要创建 `4.1.3.3` 或 `release/4.1.3.3` 这类版本号分支，除非该分支确实是
release-prep 分支。使用任务范围命名：

- `feature/<scope>`
- `bug-fix/<scope>`
- `update/<scope>`
- `refactor/<scope>`

一个当前工作版本可以包含多个已验收任务分支。每个分支应承载一个边界清晰的 feature、
fix、update 或 refactor；验收后合入 `dev`。随后从 `dev` 组装 release candidate，并按
当前工作版本核对 release notes、compatibility manifests、examples、docs 和 companion
repositories。

如果开始新的版本开发工作时，前一个公开版本已经发布，只要还没有进入更新批次的
release-prep 或 release promotion 动作，就把 `compatibility/in-development.json` 视为
明确的当前工作版本来源，不要在每个任务里反复要求维护者重申该版本。如果 in-development
manifest 缺失、过期或正在被替换，必须先暂停并确认当前工作版本。如果预期任务分支没有被
明确指定，必须先暂停并确认分支，然后才能修改代码、spec、docs、examples 或 compatibility
metadata。

当某个分支只是实现大 roadmap 目标的一部分时，需要同时记录两层事实：feature spec 描述
更大的目标，而 `compatibility/in-development.json` 描述将要发布这个已验收切片的当前工作
版本。

## 文档

GitHub Pages 使用 `main` 分支里的 `docs/` 目录。

`docs/_config.yml` 记录该 branch source 使用的 Pages/Jekyll 设置，包括站点 base URL 和 Markdown 相对链接处理。

旧 `docs` 分支已经废弃，不应再作为文档来源。不要再新增依赖旧 `docs` 分支发布文档的 workflow 或 release 步骤。

每次框架发版都必须在合并 release PR 前更新并核对这些公开文档面：

- 双语最终 release note，例如 `docs/en/development/release-notes-<version>.md`
  和 `docs/cn/development/release-notes-<version>.md`
- 根目录 `README.md`
- 根目录 `README_CN.md`

README 双语文件应保持原有基础结构，但必须反映本次 release 的最终产品定位、当前
版本号、推荐能力入口、示例目录、companion compatibility line 和核心业务价值。不要把
README 更新留作发版后的 marketing 补丁：根目录 `README.md` 同时也是 PyPI long
description，因为 `[project].readme` 指向它。

打开 release PR 前，需要把 release note 与两个 README 文件交叉核对，检查旧版本号、
过期 companion protocol、已删除示例、deprecated 推荐 API 和不一致的业务价值表述。

合并 release PR 前，还必须单独做一次人工 release-note review。这不是机械 checklist。
reviewer 需要确认 release note 描述的是本次真正交付的最终产品叙事、代码形态、示例和
业务价值。如果这次人工确认发现文案缺口、过期声明、README 漏更新、示例表达混乱，或
需要做小范围 API / 文档调整，必须先回到 release PR 内完成修改，重新运行相关验证，再
合并。不要先合并，再把 release-note 修正当作后续 marketing 补丁。

## 升级信息呈现格式

所有面向用户的升级说明都必须同时包含样例代码和核心变动表。这个要求适用于
release notes、release PR body、issue closeout comment、面向维护者的升级摘要，以及
解释新增或变更 public surface 的 docs 页面。

样例代码必须展示当前推荐用法。如果 API 形态发生变化，要给出 before/after snippet。
如果本次升级没有可调用 API，则展示相关配置、CLI 命令、manifest entry 或 workflow YAML。
除非明确标注为 conceptual，否则不要用 pseudo-code。

核心变动表至少包含这些列：

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| Public API / docs / runtime area | 用户可见行为或契约 | 方法、配置、命令或 example path | Additive、breaking、policy-gated、deferred 或 no-op | Tests、examples、specs、compatibility metadata 或 companion validation |

如果某个声明的切片只完成了部分实现，表格里必须包含一行 deferred，写明剩余范围以及
承接它的 spec 或 issue。不要把延期工作藏在表格之后的散文里。

## 验收论证

推荐 release 前，必须为每个用户可见的功能切片写一份 coverage-first 的验收论证。
论证要先从目标合同出发，而不是先盯着已有 example 或 test。

release reviewer 应先列出 roadmap、spec、issue acceptance criteria、
compatibility manifest、docs 和 example 规则里的要求，然后把每一项要求映射到证明它的
证据：

- 带真实 DeepSeek 或本地 Ollama 输出、并包含稳定 `Expected key output` 的场景 example
- 覆盖兼容性、stream/meta 形状、route lifecycle、budget accounting、错误语义和
  workspace record 的确定性测试
- 覆盖公开协议和依赖方向的 protocol/type 测试
- docs、compatibility manifest、spec reconciliation 和 companion guidance
- 如果 runtime event、observation payload、lineage 或 companion protocol 变化，还需要
  DevTools 或其他 companion validation

example 用来证明 release 解决了真实场景，但不能单独证明兼容行为、protocol 边界、
route lifecycle、错误语义、budget counting 或内部架构归属。如果某个要求没有证据，
要么在 release 前实现，要么在相关 spec 和 release note 中明确延期，要么移除该 release
claim。

release PR body 或 review notes 应包含这张覆盖矩阵，或给出简洁链接。不能在没有先检查
证据覆盖目标合同的情况下，直接用已有 examples、tests 或已关闭 issue 作为 release
验收结论。

## Foundation Example Effect Gate

Foundation 层能力是 release-critical framework substrate，不是应用层 use case。
典型例子包括 ModelRequest/ModelResponse、TriggerFlow、Dynamic Task/TaskDAG、
ActionRuntime、ExecutionEnvironment、Workspace/Recall、RuntimeEvent/EventCenter
和 provider protocols。当某个 release 触及或声称这类 substrate 能力时，仅有测试通过
还不够：release reviewer 还必须运行 `examples/` 下对应的核心 example，确认真实效果
仍然能通过推荐 public API 跑通。

AgentExecution、AgentTaskLoop、Skills workflows 和业务示例可以作为 release use-case
checks，但它们本身不是 Foundation checks。只有当 release 同时触及它们依赖的
Foundation substrate，例如 ModelRequest result materialization、TriggerFlow lifecycle
或 Dynamic Task DAG execution 时，才映射到这个 gate。

对每个受影响的 Foundation 能力：

- 写明被保护的 Foundation 能力和用户可见效果
- 列出证明该效果的 runnable core example；如果没有，必须先补 example 再 release
- 在 pyright 和 pytest 之后，用 release candidate 运行该 example
- 如果效果包含模型拥有的 planning、routing、verification 或 response generation，必须使用
  真实 DeepSeek 或本地 Ollama
- 在 release PR body 或 review notes 中记录 command、environment，以及稳定 key output、
  artifact、stream、metadata 或 side-effect 证据

这个 gate fail closed。不要在 Foundation example effect check 缺失或失败时，仅凭
unit tests passed 发布。必须修复 example 或 release candidate，移除 release claim，在
spec 和 release note 中明确延期受影响的 Foundation 能力，或由 maintainer 记录一次性
waiver 和残余风险。

## Release PR 正文

从 `dev` 到 `main` 的 release PR 必须包含足够信息，让 reviewer 不需要重新从 commit
history 里拼接事实，就能判断 release 是否可以接受或必须阻塞。

至少包含：

- release version、release level 和当前 PyPI 已发布版本
- 按用户可见能力分组的变更摘要
- coverage-first 验收论证或覆盖矩阵
- validation commands 和结果，包括被跳过或失败的检查
- 涉及 Foundation 层能力时的 Foundation example effect checks
- clean install smoke 的环境和结果
- compatibility manifest 更新和 companion repository 状态
- 如果 runtime events、observation payload、lineage 或 DevTools 代码变化，需要写明
  DevTools 版本或 protocol 推荐
- issue 关闭或 follow-up issue 状态
- 已知延期范围和残余 release 风险
- main 合并后的 companion promotion 或发布步骤

不要使用只写 "release" 或只列 commits 的极简 PR 正文。Release PR 是长期验收记录的一部分。

## Desktop installers

Desktop installers 不属于当前主仓库 release 流程。

不要为了兼容已经废弃的 `docs` 分支流程，保留或重新启用 desktop installer workflow。

## PyPI 发布

当前主要 PyPI 自动化是 `Publish on version change`。它在 push 到 `main` 且改动 `pyproject.toml` 时运行，检测包版本是否变化；只有版本变化时才用 Poetry 发布。

PyPI 项目列表页展示的是包元数据 `Summary`，来源于 `pyproject.toml` 的 `[project].description`。项目内页展示完整 README，来源于 `[project].readme`。

准备 release 时：

- 保持 `[project].description` 非空且简短
- 保持 `[project].readme` 指向 `README.md`
- 不要预期 PyPI 会原地更新已上传版本的元数据；元数据需要公开变化时应发布新版本

## 干净环境安装 smoke

发版测试阶段必须用 `conda` 或 `uv` 创建一个全新的空环境，安装 release
candidate 包以及最小脚本所需的必要运行时依赖。不要复用已经缓存了可选依赖的开发环境。

这个 smoke 脚本必须验证两件事：

- 已安装包的基础启动路径可用，并且不会因为缺少那些已经明确由
  `agently.utils.LazyImport` 保护的可选依赖而失败。
- 至少故意触发一个 LazyImport 保护的缺失依赖路径，并确认面向用户的安装提示或错误信息正确出现。

除非 smoke 目标就是测试某个集成，否则不要安装 DevTools、ChromaDB、FastMCP、
SQLModel、Playwright 或其他 provider-specific 可选包。缺少这些包不应影响普通
Agently import 或最小 Agent/TriggerFlow 启动。

---
title: Release Workflows
description: 当前 Agently 主仓库的 docs、安装包和 PyPI 发布自动化。
keywords: Agently, release, GitHub Actions, PyPI, docs
---

# Release Workflows

> 语言：[English](../../en/development/release-workflows.md) · **中文**

本文记录当前主仓库的自动化入口。

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

打开或合并 release PR 前，需要把 release note 与两个 README 文件交叉核对，检查
旧版本号、过期 companion protocol、已删除示例、deprecated 推荐 API 和不一致的业务
价值表述。

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

---
title: Workspace
description: 为 Agently 应用提供直接文件访问和可选、懒加载的持久状态。
keywords: Agently, Workspace, 文件, records, recovery, memory, retrieval
---

# Workspace

`Workspace` 是 Agently 的文件与可选持久信息基础边界。它只有一个普通文件根目录：

```text
Workspace(root)
├── 普通项目和文档文件
└── .agently/                 # Agently 私有状态，按需创建
```

传入的 `root` 就是 Agent 读取的目录。不存在 container root、`files_root`、
`content_root`、自动生成的说明文件或标准制品目录。边界确定后，Workspace 提供受路径
约束的文件操作；使用哪些普通目录和文件，由模型或应用决定。

Agent 始终带有一个轻量 Workspace 绑定。默认根目录是入口脚本所在目录，无法确定时回退
到当前工作目录。仅绑定 Workspace、列出文件、读取文件或搜索普通文件不会产生任何持久
写入，也不会创建 `.agently`。

```python
from agently import Agently

agent = Agently.create_agent("repo-reader").use_workspace("/path/to/project")

result = await agent.workspace.read_file("pyproject.toml", max_bytes=16_000)
matches = await agent.workspace.glob_files("*.py", path="agently")
hits = await agent.workspace.grep_files("create_agent", path="agently")
```

## 文件权限与制品

外部 Workspace 文件默认可读、不可写。当应用明确要编辑根目录时，显式授予写权限：

```python
agent.use_workspace("/path/to/project", mode="read_write")
```

在 Action surface 中，通过审批的文件修改可以为该次 Action 获得受限写权限。直接调用
`Workspace.edit_file(...)` 或 `Workspace.apply_patch(...)` 时没有 Action 审批上下文，
因此仍需 `mode="read_write"`。

只读 Workspace 创建新文件时，会写入当前 execution 的私有 fallback 区：

```text
<root>/.agently/files/<execution-id>/<requested-relative-path>
```

请求路径仍可通过普通逻辑文件视图读取；可信 `file_refs` 会记录实际私有路径。已有外部
文件绝不会被静默 shadow 或重定向：修改它必须获得写权限或通过 Action 审批。

Workspace 为已提升文件记录私有身份元数据时，外部文件仍保持只读。元数据只写入保留的
`.agently` 区域，不会因此获得修改外部来源的权限。

面向用户的制品应放入有意义的子目录，例如 `outputs/`、`reports/` 或任务专属目录，
不要直接堆在根目录。这些是应用选择，不是 Workspace 管理的标准区域。

```python
write_result = await agent.workspace.write_file(
    "outputs/weekly-report.md",
    "# Weekly report\n",
)

# 默认只读模式下，这会解析到当前 execution fallback。
readback = await agent.workspace.read_file("outputs/weekly-report.md")
trusted_ref = write_result["file_refs"][0]
```

AgentExecution 或 AgentTask 进入终态时，会激进回收 execution 私有 fallback 文件。
只有被选为最终制品、且可信 ref 通过物理回读的文件会保留。草稿、中间文件、未选输出和
失败残留都会删除。普通外部文件永远不在该清理范围内。

## 懒加载私有状态

`.agently` 用于 Workspace 在普通文件边界之外拥有的全部信息。各组件独立、按需创建：

| 使用行为 | 私有状态影响 |
|---|---|
| 绑定、列出、读取、glob 或 grep 普通文件 | 无 |
| 没有外部写权限时创建文件 | `.agently/files/<execution-id>/...` |
| 写入/搜索持久 records 或保存恢复状态 | 懒创建 `.agently/workspace.db` 所需表 |
| 实际执行 vector 操作 | 懒创建 embedding/vector provider 状态 |
| 启用 SessionMemory | 懒创建 memory records/provider 状态 |
| 安装或物化 Skills | 懒创建 Skills 自有私有状态 |

仅配置 provider 不会初始化数据库、FTS index、embedding provider 或 vector store。
`workspace.capabilities()` 会报告实际已经物化的组件。

```python
assert agent.workspace.capabilities()["materialized_components"] == []
```

### 稳定身份分层

需要持久 provenance 时，Workspace 会明确区分三层身份：

- locator 身份表示归一化 path 或 URL 的来源位置；
- content-version 身份表示该 locator 上一次精确观察到的 digest 与 size；即使 locator
  不变，只要内容变化就创建新版本；
- reference 身份是 task/application 拥有的稳定选择 key，用来指向合格 evidence，
  不要求模型抄写 path、URL 或 canonical storage id。

这些身份使用短类型前缀与可扩展 Base62 编号，在所属 scope 内永不复用。另一个
Workspace 或 task 中出现相同短值时，canonical scope 仍包含 Workspace/task identity，
因此不会混淆。reference-closure cleanup 会清理不可达的旧版本、segment 与 payload，
但保留 allocator high-water mark，已删除 id 不会被重新分配。

身份分配是私有实现行为，不是新的 Workspace public API。普通外部读取保持 zero-state。
显式 promotion、持久 record、recovery 或 accepted-artifact retention 可以在 `.agently`
下写入有界的私有身份元数据；这套 filesystem-first metadata 不会仅为身份分配而要求
record database，除非所选功能本来就使用数据库。

## 持久 Records

Records 用于明确需要跨当前 execution 保留的信息：应用知识、经过选择的 observation、
decision、紧凑 checkpoint、memory 或其他语义状态。默认不要把每个 taskboard tick、
prompt envelope、model response 或 RuntimeEvent 都复制进 records。

```python
ref = await agent.workspace.put(
    content={"status": "failed", "test": "route_fallback"},
    collection="observations",
    kind="test_result",
    summary="route fallback test failed",
    scope={"task_id": "issue-123"},
    source={"type": "command", "name": "pytest"},
)

record = await agent.workspace.get_data(ref)
refs = await agent.workspace.grep(
    "route fallback",
    filters={"collection": "observations", "kind": "test_result"},
)
```

`put(...)` 是标准 record 写入 API，只创建该操作真正需要的表。FTS 通过
`indexed=True` 显式启用；vector index 通过 `vector=True` 显式启用，并且需要已配置
的 embedding 与 vector provider。

运行状态应携带稳定 ref 和有界读取结果，而不是复制大值：

```python
envelope = await agent.workspace.ref_envelope(ref)
segment = await agent.workspace.read_bounded(ref, offset=0, limit=4096)

async for chunk in agent.workspace.stream_read(ref, chunk_size=8192):
    consume(chunk["content"])
```

`workspace.link(...)` 与 `workspace.links(...)` 连接持久 records。
`workspace.link_evidence(...)` 可以附加 execution、operation、event、checkpoint、
exchange 与 artifact 身份事实，但不会让 Workspace 取得执行策略所有权。

## 检索

根据调用方真实需要选择检索 surface：

- `grep(...)`：确定性的持久 record 搜索；
- `grep_files(...)`：确定性的普通文件搜索，可用时使用 `rg`；
- `search(...)` / `search_files(...)`：保持兼容返回形状，并可对宽候选池自动包装；
- `retrieve(...)`：统一的 record/file 检索，支持预算、可选 vector 候选、可选模型
  rerank 与可信 refs；
- `build_context(...)`：为后续 model request 生成有界 `ContextPackage`。

```python
package = await agent.workspace.retrieve(
    "deadline and owner",
    sources=["records", "files"],
    filters={"collection": "project_notes"},
    file_options={"path": "notes", "pattern": "*.md"},
    budget={"chars": 6000, "item_chars": 1200},
    selection="length",
    rerank=False,
)

context = await agent.workspace.build_context(
    goal="Summarize the current release risks.",
    scope={"task_id": "release-42"},
    budget={"tokens": 8000},
    profile="auto",
)
```

### 自然语言答案中的检索引用

后续模型答案需要引用选中的检索结果时，完整 source record 应留在宿主代码中。每个
source 只给模型一个短可信 `ref_id`，以及回答所需的 title/snippet；要求正文使用
application-level token `[[ref:<ref_id>]]`。持久 AgentTask 输出应使用 task-owned
稳定 reference 身份，例如 `[[ref:ref_2]]`。evidence ledger 的 `cite_as`（如 `e1`）
只是请求内显示别名：模型响应离开该次精确 ledger view 前，必须把它归一化为已提供的
稳定 `ref_*` 身份。不得持久化或在后续猜测 `(eN)` 位置。

不要使用裸 `${ref_id}`：`${...}` 已属于 Agently prompt 和 TaskDAG placeholder
家族。`[[ref:...]]` 只是应用渲染约定，不是新的 Workspace 或 Agently public API。

下面的模式会校验模型引用、把 token 转为 Markdown link、发送答案，再发送应用明确
允许的完整 source-card 详情，供前端渲染 hover card、来源列表或回复后的附加结果卡。
raw provider/Workspace record 继续留在宿主侧；发出 card 字段前应完成权限校验和脱敏：

```python
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from agently import Agently


REF_TOKEN = re.compile(r"\[\[ref:([A-Za-z0-9._:-]+)\]\]")


def prepare_refs(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    refs_by_id: dict[str, dict[str, Any]] = {}
    model_refs: list[dict[str, str]] = []
    for index, record in enumerate(records, start=1):
        ref_id = f"r{index}"
        full_record = {**dict(record), "ref_id": ref_id}
        refs_by_id[ref_id] = full_record
        model_refs.append({
            "ref_id": ref_id,
            "title": str(record.get("title", "")),
            "snippet": str(record.get("snippet", "")),
        })
    return model_refs, refs_by_id


def build_source_card(record: Mapping[str, Any]) -> dict[str, Any]:
    # 按应用需要扩展这个显式前端契约；不要直接发送 raw record。
    fields = ("ref_id", "title", "url", "snippet", "source_name", "published_at")
    return {field: record[field] for field in fields if field in record}


def trusted_http_url(value: Any) -> str:
    url = str(value)
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"unsupported source URL: {url}")
    return url


def render_refs(
    answer: str,
    refs_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    used_ids: list[str] = []

    def replace(match: re.Match[str]) -> str:
        ref_id = match.group(1)
        record = refs_by_id.get(ref_id)
        if record is None:
            raise ValueError(f"unknown retrieval ref: {ref_id}")
        used_ids.append(ref_id)
        label = str(record.get("title") or ref_id).replace("\\", "\\\\").replace("]", "\\]")
        return f"[{label}]({trusted_http_url(record.get('url'))})"

    rendered = REF_TOKEN.sub(replace, answer)
    unique_ids = list(dict.fromkeys(used_ids))
    return rendered, [build_source_card(refs_by_id[ref_id]) for ref_id in unique_ids]


async def answer_with_refs(
    question: str,
    retrieved_records: Sequence[Mapping[str, Any]],
    emit: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    model_refs, refs_by_id = prepare_refs(retrieved_records)

    def validate_refs(data: dict[str, Any], _ctx: Any) -> bool | dict[str, Any]:
        cited_ids = REF_TOKEN.findall(data.get("answer", ""))
        if not cited_ids:
            return {"ok": False, "reason": "answer must cite at least one offered ref"}
        unknown = {
            ref_id
            for ref_id in cited_ids
            if ref_id not in refs_by_id
        }
        if unknown:
            return {"ok": False, "reason": f"unknown refs: {sorted(unknown)}"}
        return True

    result = (
        Agently.create_request("retrieval-reference-answer")
        .input({"question": question, "retrieval_results": model_refs})
        .instruct([
            "根据 retrieval_results 回答。",
            "引用来源时写 [[ref:<ref_id>]]，且只能使用已提供的 ref_id。",
            "不要把 source URL 或隐藏 metadata 抄进答案。",
        ])
        .output({
            "answer": (
                str,
                "带内联 [[ref:<ref_id>]] 引用的自然语言答案",
                True,
            ),
        })
        .validate(validate_refs)
        .get_result()
    )

    # 这里没有 progressive consumer，所以直接读取最终 data。
    data = await result.async_get_data()
    rendered_answer, source_cards = render_refs(data["answer"], refs_by_id)

    await emit({"type": "answer", "text": rendered_answer})
    await emit({"type": "retrieval_refs", "items": source_cards})
```

这个协议让引用选择保持 model-owned，同时让身份校验、链接拼装、已授权 source detail
传输与前端渲染保持 deterministic、host-owned。

结构过滤只缩小候选范围，不证明语义相关性或任务完成。模型负责的 rerank 和 verifier
仍在 Workspace 之外。

## File IO Handlers

Workspace 负责路径约束、确定性文件信息、digest、可信 refs 与 handler dispatch。
`WorkspaceFileIOHandler` 实现负责格式特定的读取、写入或导出行为。

```python
read_result = await agent.workspace.read_file("notes/todo.txt")
write_result = await agent.workspace.write_file("outputs/todo.txt", "ship docs")

download = await agent.workspace.materialize_file(
    "sources/specification.pdf",
    pdf_bytes,
    source={"type": "browse", "url": source_url},
    media_type="application/pdf",
)

exported = await agent.workspace.export_file(
    "sources/specification.docx",
    "outputs/specification.md",
    export_kind="markdown",
)
```

`materialize_file(...)` 是二进制/下载边界；`write_file(...)` 保持文本 handler 契约。
Workspace 不会因此变成 shell、browser、renderer lifecycle owner、OCR engine 或模型调用方。

通过 Workspace manager 注册自定义 handler：

```python
Agently.workspace.register_file_io_handler(custom_handler)
```

## 文件 Actions、Shell 与 ACP

文件 Action 和 coding-agent Action 继承直接 Workspace root：

```python
agent.enable_workspace_file_actions(read=True, write=True)
agent.enable_coding_agent_actions()
```

读 Action 访问普通根目录，不会创建私有状态。只读 Workspace 的新文件写入当前
execution fallback；修改已有文件会进入 PolicyApproval。Coding-agent 写入还保留
read-before-write 或 expected-SHA 新鲜度保护。

未显式传入 root/cwd 时，Shell 和 Node.js helper 继承同一个根目录。trusted-local shell
无法强制只读文件系统边界，因此需要 read-write Workspace。Docker 可以把普通根目录以
只读方式挂载，同时把当前 `.agently/files/<execution-id>` fallback 以可写方式挂载。

```python
agent.enable_shell(commands=["rg", "pytest"], sandbox="docker")
agent.use_acp(on_missing="skip")
```

ACP 默认使用 `agent.workspace.root` 作为项目目录。只有 host 明确授权另一个目录时才传入
显式 `root=`。

## TriggerFlow 恢复与 RuntimeEvents

TriggerFlow 可以携带 Workspace runtime resource 用于文件或 record 操作，但默认不会把
该 Workspace 当作 RuntimeEvent store。有限运行不会因为“执行过”就创建数据库。

Pause/resume、intervention 或显式 save/load 可以把 Workspace 激活为 snapshot carrier。
这是恢复状态，不是审计档案。完成、失败或取消的有限运行进入终态后，只要没有真实恢复
lifecycle 仍需要该状态，就会清理该 execution 的临时 recovery snapshot。

RuntimeEvent 持久化必须显式启用：

```python
execution = flow.create_execution(
    workspace=agent.workspace,
    runtime_resources={
        "runtime_event_store": agent.workspace,
    },
)
```

应用需要指定恢复 provider 时，显式传入 `snapshot_store` 或 `durable_provider`。
`runtime_event_store` 特意保持独立：配置恢复不能静默开启完整事件档案。普通审计应交给
EventCenter sinks、日志系统或 DevTools；只有应用明确选择保留内容和方式时才写进
Workspace。

## AgentTask 恢复与终态保留

AgentTask 的 planning、observation、verification 与 taskboard ticks 默认是进程内状态和
观察输出，不会复制为 Workspace records。只有任务确实需要重启恢复时才开启：

```python
task = agent.create_task(
    goal="Prepare the release report.",
    options={"agent_task": {"workspace_recovery": True}},
)
```

该选项只持久化紧凑的可恢复 snapshot，不代表任务选择了完整过程或 RuntimeEvent 档案。
最终可信文件制品仍通过 Workspace 回读和终态选择保留，非制品过程状态会被回收。

## SessionMemory 与插件边界

Workspace 是存储和检索基础能力，不是 memory strategy。`Session.use_memory(...)` 负责
memory 提取、压缩、retrieval query planning、rerank 与 prompt injection；只有显式启用
memory 时才激活 Workspace 持久化。

需要自定义 backend 的应用仍可使用 Workspace provider seams：

```python
Agently.workspace.register_backend_provider("remote", build_remote_backend)
Agently.workspace.register_db_store_provider("postgres", build_db_store)
Agently.workspace.register_embedding_provider("agent", build_embedding_provider)
Agently.workspace.register_vector_store_provider("pgvector", build_vector_store)

agent.use_workspace(
    "/project",
    provider="remote",
    provider_options={"tenant": "acme"},
)
```

Providers 实现存储机制，不拥有 AgentTask continuation、TriggerFlow topology、审批、
语义相关性、memory policy 或业务完成判断。

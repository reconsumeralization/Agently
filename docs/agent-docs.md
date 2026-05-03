---
title: Official Agently Skills for Coding Agents
description: "Official installable Agently Skills for Codex, Claude Code, and similar coding agents."
keywords: "Agently,Skills,Coding Agent,Codex,Claude Code,official skills"
---

# Official Agently Skills for Coding Agents

`Agently-Skills` is the **official installable skills package** for **Codex / Claude Code** and similar coding agents.
It replaces the old `agent_docs.zip` flow, copied doc packs, and earlier local demo skills.

Official repository:

- [AgentEra/Agently-Skills](https://github.com/AgentEra/Agently-Skills)

Official docs:

- English Docs: [agently.tech/docs/en](https://agently.tech/docs/en/)
- 中文文档: [agently.cn/docs](https://agently.cn/docs/)

---

## 1. What the current Skills model is

The current `Agently-Skills` model is not just an API snippet pack. It teaches a coding agent to work along Agently-native capability boundaries:

- recognize whether a request belongs to the request side, extension side, or orchestration side
- choose the right skill or skill combination before implementation
- prefer native Agently surfaces instead of starting with custom wrappers, parsers, or workflow glue
- organize settings, prompts, tools, workflows, and runtime artifacts into a maintainable project shape

The goal is not shallow snippet generation. The goal is a complete project that actually fits Agently.

---

## 2. Correct setup path

Recommended flow:

1. Install the **official Agently Skills** through the skills mechanism supported by your coding agent.
2. Keep `AGENTS.md` / `CLAUDE.md` focused on **repo-specific rules only**.
3. Let the installed official skills provide Agently knowledge and routing instead of vendoring a full Agently docs pack into each repo.

`AGENTS.md` / `CLAUDE.md` should now mostly contain:

- directory layout and module boundaries
- build, test, and release requirements
- local coding conventions
- internal docs that must be read first

They should no longer say:

- "download and unzip `agent_docs.zip`"
- "copy the whole Agently docs pack into this repo"
- "prefer the old local mock skills"

---

## 3. Routing model

Use this current routing model when deciding where to start:

- business goal, product behavior, project refactor, or unclear owner layer: `agently-playbook`
- provider wiring, env vars, or model settings separation: `agently-model-setup`
- prompt structure, prompt config, YAML/JSON prompt behavior, or mappings: `agently-prompt-management`
- stable structured fields, required keys, or machine-readable output: `agently-output-control`
- reuse one model result, access text/data/meta, or consume streaming output: `agently-model-response`
- session continuity, restore, or memory: `agently-session-memory`
- tools, MCP, FastAPIHelper, `auto_func`, or `KeyWaiter`: `agently-agent-extensions`
- embeddings, vector indexing, retrieval, or KB-backed answers: `agently-knowledge-base`
- TriggerFlow orchestration, runtime stream, event-driven fan-out, or mixed sync/async workflow: `agently-triggerflow`
- LangChain / LangGraph migration: start with `agently-migration-playbook`, then route to the matching migration leaf

The key rule is: **prefer Agently-native surfaces before inventing custom wrapper layers.**

---

## 4. Current public catalog

According to the current official repository, the public catalog contains **12 skills**:

### Entry

- `agently-playbook`
  Top-level router for unresolved product, assistant, automation, workflow, evaluator, or project-structure refactor requests.

### Request Side

- `agently-model-setup`
  Provider connection, dotenv-based settings, transport setup, and settings-file-based model separation.
- `agently-prompt-management`
  Prompt composition, prompt config, YAML-backed prompt behavior, mappings, and reusable request-side prompt structure.
- `agently-output-control`
  Output schema, field ordering, required keys, and structured output reliability.
- `agently-model-response`
  `get_response()`, parsed results, metadata, streaming consumption, and response reuse.
- `agently-session-memory`
  Session-backed continuity, memo, restore, and request-side conversational state.

### Request Extensions

- `agently-agent-extensions`
  Tools, MCP, FastAPIHelper, `auto_func`, and `KeyWaiter`.
- `agently-knowledge-base`
  Embeddings plus Chroma-backed indexing, retrieval, and retrieval-to-answer flows.

### Workflow

- `agently-triggerflow`
  TriggerFlow orchestration, runtime state, runtime stream, workflow-side model execution, event-driven fan-out, and mixed sync/async orchestration.

### Migration

- `agently-migration-playbook`
  Top-level migration router for LangChain or LangGraph systems.
- `agently-langchain-to-agently`
  Direct LangChain agent-side migration guidance.
- `agently-langgraph-to-triggerflow`
  Direct LangGraph orchestration migration guidance.

---

## 5. Common install paths

Install the full official skills repository:

```bash
npx skills add AgentEra/Agently-Skills
```

If you want the smallest starting point:

```bash
npx skills add AgentEra/Agently-Skills --skill agently-playbook
```

Common bundles:

`request-core`

```bash
npx skills add AgentEra/Agently-Skills --skill agently-playbook
npx skills add AgentEra/Agently-Skills --skill agently-model-setup
npx skills add AgentEra/Agently-Skills --skill agently-prompt-management
npx skills add AgentEra/Agently-Skills --skill agently-output-control
npx skills add AgentEra/Agently-Skills --skill agently-model-response
```

`request-extensions`

```bash
npx skills add AgentEra/Agently-Skills --skill agently-playbook
npx skills add AgentEra/Agently-Skills --skill agently-agent-extensions
npx skills add AgentEra/Agently-Skills --skill agently-session-memory
npx skills add AgentEra/Agently-Skills --skill agently-knowledge-base
```

`workflow-core`

```bash
npx skills add AgentEra/Agently-Skills --skill agently-playbook
npx skills add AgentEra/Agently-Skills --skill agently-triggerflow
npx skills add AgentEra/Agently-Skills --skill agently-output-control
npx skills add AgentEra/Agently-Skills --skill agently-model-response
npx skills add AgentEra/Agently-Skills --skill agently-session-memory
```

`migration`

```bash
npx skills add AgentEra/Agently-Skills --skill agently-playbook
npx skills add AgentEra/Agently-Skills --skill agently-migration-playbook
npx skills add AgentEra/Agently-Skills --skill agently-langchain-to-agently
npx skills add AgentEra/Agently-Skills --skill agently-langgraph-to-triggerflow
```

---

## 6. Recommended project shape

The current Skills model does not assume one oversized `app.py`. It prefers explicit capability boundaries:

- `SETTINGS.yaml` or a settings layer for provider config, `${ENV.xxx}`, and runtime knobs
- an app or integration layer that loads settings, validates env names when needed, and calls `Agently.set_settings(..., auto_load_env=True)`
- `prompts/` for YAML or JSON prompt contracts
- `workflow/` for TriggerFlow graphs and chunk implementations
- `tools/` for replaceable search, browse, MCP, or external adapters
- `outputs/` and `logs/` for runtime artifacts instead of mixing them into source folders

If a new request still has an unclear owner layer, project initialization and structure routing should start with `agently-playbook`.

---

## 7. Repo guidance

- The old `agent_docs.zip` flow is deprecated and no longer the official distribution path.
- The old local demo skills no longer represent the official skill system.
- This page stays as the introduction and navigation entry for official Agently Skills.
- Skill names, installation flows, and catalog structure should follow the current official repository.

For the human-developer view of Agently capabilities and runnable scenarios, continue with: [Agent Systems Playbook](/en/agent-systems/overview).

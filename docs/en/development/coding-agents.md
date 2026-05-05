---
title: Coding Agents
description: Using Agently with coding agents like Codex, Claude Code, Cursor — the official Agently Skills.
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> Languages: **English** · [中文](../../cn/development/coding-agents.md)

If you're building Agently apps with the help of an external coding agent (Codex, Claude Code, Cursor, etc.), the canonical way to give that agent good Agently context is the **official Agently Skills** packages, distributed in the `Agently-Skills` companion repo.

## What are Agently Skills

A skill is a bundle of:

- a `SKILL.md` describing what the skill does and when to apply it
- references — focused docs the coding agent can pull on demand
- examples — minimal runnable snippets
- validators — scripts the agent can run to confirm the user's project follows the recommended structure

Skills are **not** just documentation. They're structured for coding agents: each one tells the agent which problem it solves, what the recommended path looks like, and how to verify that the user's code is on that path.

## Available skills (representative)

| Skill | Use when the user is |
|---|---|
| `agently-playbook` | starting fresh — picking the right structure for a new Agently project |
| `agently-model-setup` | wiring a model endpoint, env vars, settings file |
| `agently-prompt-management` | shaping how a request is instructed or templated |
| `agently-output-control` | nailing down structured fields, `ensure_keys`, validation |
| `agently-model-response` | reusing a single response, streaming partial output |
| `agently-session-memory` | adding multi-turn continuity / memo |
| `agently-agent-extensions` | adding tool use, MCP, FastAPI exposure |
| `agently-triggerflow` | needing branching, concurrency, pause/resume, save/load |
| `agently-knowledge-base` | embeddings + retrieval-backed answers |
| `agently-langchain-to-agently` | migrating from LangChain agents |
| `agently-langgraph-to-triggerflow` | migrating from LangGraph orchestration |
| `agently-migration-playbook` | deciding which migration skill to start with |

The actual skill list lives in `Agently-Skills/skills/`. Treat the table above as a snapshot.

## Installing the skills

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

Then point your coding agent at the skill directory according to its own loader:

- **Claude Code** — `claude` skills in `~/.claude/skills/` or project `.claude/skills/`
- **Codex** — see your Codex installation's skill / context loader
- **Cursor** — load via the project's rules / context surfaces

The skills are plain text + scripts; nothing Agently-specific runs at install time.

## Why skills, not just docs

Documentation tells humans what's possible. Skills tell coding agents what's recommended **right now** — including which APIs are deprecated, what the current lifecycle looks like, and what to verify before reporting "done". This keeps coding agents aligned with the framework's evolution without users having to update their own context manually.

In particular, skills must NOT recommend deprecated paths like `.end()`, `set_result()`, `wait_for_result=`, or the old `runtime_data` API. If you find a skill recommending one of these, file an issue against `Agently-Skills`.

## When to write your own skill

If your team has internal patterns layered on top of Agently (a particular project layout, a wrapped agent factory, a custom action set), consider authoring a private skill bundle that mirrors the public Agently Skills format. Coding agents will then apply your team's conventions consistently across projects.

## Validation scripts

Several skills ship validation scripts (e.g., `validate/validate_native_usage.py`). Coding agents can run these to confirm a user's project follows the recommended path before declaring a task complete. For example, the TriggerFlow validator checks that no deprecated API is being used as the recommended starting point.

## See also

- [Action Runtime](../actions/action-runtime.md) — the architecture skills assume for tool use
- [DevTools](../observability/devtools.md) — observation, evaluation, and interactive wrapper paths
- [TriggerFlow Compatibility](../triggerflow/compatibility.md) — the migration path skills steer toward

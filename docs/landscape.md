---
title: Framework Landscape
description: "Agently capability landscape to understand framework modules, boundaries, and best practices."
keywords: "Agently,capability landscape,framework architecture,AI agent development"
---

# Framework Landscape

This page summarizes **public English sources** to clarify positioning and common misconceptions across popular frameworks/platforms. It is **not a ranking**, only a structured comparison for selection.

## Two categories

- **Framework / SDK**: code-level building blocks (orchestration, output control, data indexing).
- **Platform / Product**: visual configuration and operational features for cross‑role collaboration.

## Positioning at a glance

- **Agently**: engineering-first framework with output control, event-driven TriggerFlow, and async-first design.
- **LangChain family**: described as a “platform for reliable agents,” expanded with LangGraph (orchestration) and LangSmith (observability/evaluation).
- **LlamaIndex**: data framework focused on connecting, indexing, and retrieving external data for LLM apps.
- **AutoGen**: multi-agent application framework emphasizing agent collaboration and autonomy.
- **CrewAI**: multi-agent automation framework emphasizing lightweight, independent orchestration.
- **Dify**: open-source LLM application platform with workflows, RAG, model management, and observability.

## Comparison table (positioning / scenarios / boundaries)

> Scenarios and boundaries are inferred from official positioning and public descriptions for clarity, not hard limits.

| Framework / Platform | Official positioning (English sources) | Typical scenarios | Boundaries / notes |
| --- | --- | --- | --- |
| Agently | Engineering‑first AI app framework (output control + TriggerFlow + async‑first) | High‑reliability output, event‑driven orchestration, production delivery | Primarily a framework; platform capabilities are external | 
| LangChain | “The platform for reliable agents.” | General LLM app composition, tools, retrieval building blocks | LangGraph/LangSmith are separate products in the same ecosystem |
| LangGraph | Low‑level orchestration framework for long‑running, stateful agents. | Stateful, multi‑step agent workflows | Primarily orchestration; data/observability can be layered separately |
| LangSmith | Debug, evaluate, and monitor language models and intelligent agents. | Evaluation, tracing, monitoring | Observability layer, not orchestration or data layer |
| LlamaIndex | A data framework for LLM applications. | RAG, data ingestion, indexing and retrieval | Data‑layer focus; orchestration/observability are separate concerns | 
| AutoGen | Framework for multi‑agent AI apps that act autonomously or alongside humans. | Multi‑agent collaboration and autonomy | Focus on collaboration/autonomy patterns |
| CrewAI | Fast, flexible multi‑agent automation framework; independent of other agent frameworks. | Role‑based agent automation | Focused on agent collaboration patterns; complex orchestration can be layered | 
| Dify | Open‑source platform for developing LLM apps with workflows, RAG, model management, observability. | Visual configuration, collaboration, workflow‑driven apps | Platform‑first, emphasizing workflows and ops features | 

## Selection tips (scenario‑driven)

- **Output stability & engineering control**: consider Agently for output control + event‑driven orchestration.
- **Data/RAG as the center**: LlamaIndex is a strong data layer; combine with orchestration if needed.
- **Multi‑agent collaboration**: AutoGen or CrewAI for agent‑centric design; add output control for reliability.
- **Visual collaboration & ops**: Dify fits platform‑first teams and workflow‑driven iteration.
- **Observability/evaluation**: LangSmith serves as a dedicated monitoring layer.

## English references

- LangChain README: https://raw.githubusercontent.com/langchain-ai/langchain/master/README.md
- LangGraph README: https://raw.githubusercontent.com/langchain-ai/langgraph/main/README.md
- LangSmith SDK README: https://raw.githubusercontent.com/langchain-ai/langsmith-sdk/main/README.md
- LlamaIndex README: https://raw.githubusercontent.com/run-llama/llama_index/main/README.md
- AutoGen README: https://raw.githubusercontent.com/microsoft/autogen/main/README.md
- CrewAI README: https://raw.githubusercontent.com/joaomdmoura/crewAI/main/README.md
- Dify README: https://raw.githubusercontent.com/langgenius/dify/main/README.md

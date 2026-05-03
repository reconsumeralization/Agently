---
title: Capability Map
description: "Agently capability map for ordinary developers: from one request to stable output, extensions, orchestration, and advanced topics."
keywords: "Agently,capability map,learning path,TriggerFlow,Async First"
---

# Capability Map

This page helps you decide which layer your current problem belongs to.

## The upgrade path

```mermaid
flowchart LR
  A["Layer 1<br/>Run one request"] --> B["Layer 2<br/>Stable output and result consumption"]
  B --> C["Layer 3<br/>Tools / Session / KB / FastAPI"]
  C --> D["Layer 4<br/>TriggerFlow orchestration"]
  D --> E["Layer 5<br/>Migration and advanced topics"]
```

## How to use this page

- still one high-quality request -> stay on the request side
- one request plus external capability -> move into extensions
- explicit stages, branching, concurrency, wait/resume -> move into TriggerFlow
- real services, streaming UI, or workflow runtime -> default to [Async First](/en/async-support)

## Next

- First request path: [Quickstart](/en/quickstart)
- Async production path: [Async First](/en/async-support)
- Workflow boundary: [TriggerFlow Overview](/en/triggerflow/overview)

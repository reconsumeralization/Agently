# Blocks examples

These examples exercise the current Blocks lifecycle. Blocks accepts an
already-prepared `ContextReader` for `context_read`; Skill installation,
selection, file mutation, and persistence remain with their owning layers.

```bash
python examples/blocks/01_blocks_lifecycle_infrastructure_smoke.py
python examples/blocks/02_single_tool_support_ticket_stream.py
python examples/blocks/03_tool_composition_refund_review_stream.py
python examples/blocks/04_mcp_sandbox_settlement_stream.py
python examples/blocks/run_business_complexity_ladder.py
```

The first example is a deterministic `TaskContext -> ContextReader ->
context_read` probe. The business ladder covers explicit tool composition and
MCP/sandbox blocks. Model-owned semantic work belongs in `model_request`
handlers or the higher-level Agent/AgentTask APIs.

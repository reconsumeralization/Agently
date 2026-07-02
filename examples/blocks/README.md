# Blocks Business Examples

These examples prove the Blocks execution lifecycle with real business-shaped
tasks. Each example streams process events while it runs. Model-owned artifacts
are validated by a second Agently model-judge request with explicit semantic
rules and unsupported-claim checks.

Run individual examples:

```bash
python examples/blocks/02_single_tool_support_ticket_stream.py
python examples/blocks/03_tool_composition_refund_review_stream.py
python examples/blocks/04_mcp_sandbox_settlement_stream.py
python examples/blocks/05_single_skill_support_reply_stream.py
python examples/blocks/06_multi_skills_travel_memo_stream.py
python examples/blocks/07_real_complex_bundle_stream.py
python examples/agent_task/real_complex_bundle_goal_stream.py
```

Run the full usability suite:

```bash
python examples/blocks/run_business_complexity_ladder.py
```

Run the local suite without the external-capability bundle:

```bash
BLOCKS_COMPLEXITY_CASES=01_single_tool,02_tool_composition,03_tool_mcp_sandbox,04_single_skill,05_multi_skills \
  python examples/blocks/run_business_complexity_ladder.py
```

The full suite writes
`examples/blocks/_artifacts/blocks_business_complexity_ladder_summary.json`.
Set `BLOCKS_COMPLEXITY_CASES` to a comma-separated list of case ids to run only
part of the suite. Model-backed examples use DeepSeek when `DEEPSEEK_API_KEY`
is available, or local Ollama otherwise.

`07_real_complex_bundle_stream.py` is the Blocks-level external-capability
proof. It requires network access and `AMAP_API_KEY`, uses the built-in Search
action, calls real AMap MCP tools, installs the public
`Cocoon-AI/architecture-diagram-generator` Skill at runtime, and fails closed if
those lower-level capabilities are unavailable. It is not the recommended
high-level business entry point.

`examples/agent_task/real_complex_bundle_goal_stream.py` is the corresponding
high-level Goal Pursuit proof. It mounts Search, AMap MCP, Workspace file
actions, and the CocoonAI Skill through public Agent APIs, then runs the
`.goal(...).effort(...).input(...).output(...).strategy("task")` chain with
streamed natural-language progress deltas. The example uses multi-round
bounded direct steps so it proves the current public AgentTask lifecycle rather
than the still-separate mixed DynamicTask/DAG substrate.

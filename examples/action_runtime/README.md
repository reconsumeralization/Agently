# Action Runtime Examples

These examples are written for the Action-based runtime. Every numbered example uses `agent.get_action_result()` to inspect intermediate `ActionResult` records first, then uses `agent.get_response()` to produce the final DeepSeek reply through `OpenAICompatible`.

Before running them, set:

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL` (optional, defaults to `https://api.deepseek.com/v1`)
- `DEEPSEEK_DEFAULT_MODEL` (optional, defaults to `deepseek-chat`)

Example groups:

- Function actions
  - `1_1_function_action_func_deepseek.py`
  - `1_2_function_register_action_deepseek.py`
- MCP actions
  - `2_1_mcp_stdio_action_deepseek.py`
  - `2_2_mcp_http_action_deepseek.py`
  - `_calculator_mcp_server.py` is the shared local MCP server
- Sandbox actions
  - `3_1_python_sandbox_action_deepseek.py`
  - `3_2_bash_sandbox_action_deepseek.py`
  - `3_3_third_party_sandlock_action_deepseek.py`
  - `3_4_third_party_docker_sandbox_action_deepseek.py`
- Plugin customization examples
  - `4_1_custom_action_executor_plugin_local.py`
  - `4_2_custom_action_runtime_plugin_local.py`
  - `4_3_custom_action_flow_plugin_local.py`

Shared helper:

- `_shared.py` loads `.env`, configures DeepSeek, prints intermediate action records, and prints final reply logs from `response.result.full_result_data["extra"]["action_logs"]`

Notes:

- Every numbered example registers or imports actions, mounts them on an agent, runs a real prompt, prints intermediate action records, and then prints the final reply plus `extra.action_logs`.
- By default, `agent.get_action_result()` stores `action_results` on the current prompt so the following `agent.get_response()` can reuse those intermediate results instead of executing the action loop again. Pass `store_for_reply=False` when you only want isolated inspection.
- `3_3_third_party_sandlock_action_deepseek.py` demonstrates a Linux SandLock third-party sandbox executor registered through the new `ActionExecutor` plugin type.
- `3_4_third_party_docker_sandbox_action_deepseek.py` demonstrates a local Docker third-party sandbox executor registered through the new `ActionExecutor` plugin type.
- `4_1` to `4_3` focus on extension points for `ActionExecutor`, `ActionRuntime`, and `ActionFlow`, and also run through an agent with DeepSeek for the final reply.
- The SandLock example requires Linux 6.7+ and `pip install sandlock`.
- The Docker sandbox example requires a local Docker daemon that is running and not paused. It auto-pulls `alpine:3.20` when the image is not available locally.

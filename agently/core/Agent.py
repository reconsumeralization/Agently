# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import uuid

from collections.abc import Mapping
from typing import Any, Sequence, TYPE_CHECKING, Literal, cast

from agently.core.extension import ExtensionHandlers
from agently.core.application import AgentTask
from agently.core.AgentTurn import AgentTurn
from agently.core.model.AttachmentInput import ImageDetail, build_image_attachment
from agently.core.model import ModelRequest, Prompt, _resolve_quick_prompt_input, _UNSET
from agently.core.orchestration import DynamicTask
from agently.core.runtime import resolve_parent_run_context
from agently.utils import DataFormatter, Settings

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentExecutionMode,
        OutputValidateHandler,
        PromptStandardSlot,
        ChatMessage,
        ChatMessageDict,
        RunContext,
        TaskDAG,
    )
    from agently.types.options import ExecutionOptions


class BaseAgent:
    def __init__(
        self,
        plugin_manager: "PluginManager",
        *,
        parent_settings: "Settings | None" = None,
        name: str | None = None,
    ):
        self.id = uuid.uuid4().hex
        self.name = name if name is not None else self.id[:7]

        self.plugin_manager = plugin_manager
        self.settings = Settings(
            name=f"Agent-{ self.name }-Settings",
            parent=parent_settings,
        )
        self.agent_prompt = Prompt(
            name=f"Agent-{ self.name }-Prompt",
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
        )
        self.extension_handlers = ExtensionHandlers(
            {
                "request_prefixes": [],
                "broadcast_prefixes": [],
                "broadcast_suffixes": [],
                "finally": [],
                "validate_handlers": [],
            },
            name=f"Agent-{ self.name }-ExtensionHandlers",
        )
        self._active_model_key: str | None = None
        self.request = ModelRequest(
            agent_name=self.name,
            agent_id=self.id,
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            parent_prompt=self.agent_prompt,
            parent_extension_handlers=self.extension_handlers,
        )
        self.request_prompt = self.request.prompt
        self.prompt = self.request_prompt
        self._dynamic_task_candidates: list[dict[str, Any]] = []

        self.set_settings = self.settings.set_settings
        self.load_settings = self.settings.load

    def configure_policy_approval(self, *, handler: str | None = None):
        if handler is not None:
            self.settings.set("policy_approval.handler", str(handler))
        return self

    def activate_model(self, model_key: str | None = None):
        """Set the default model key for subsequent Agent-owned requests.

        The model key is resolved through the existing model_pool /
        key_pool_strategy / key_pool settings when a request is consumed.
        Passing None clears the active model key.
        """
        if model_key is None:
            self._active_model_key = None
            self.request._model_key = None
            return self
        normalized = str(model_key).strip()
        if not normalized:
            raise ValueError("activate_model(...) requires a non-empty model_key, or None to clear it.")
        self._active_model_key = normalized
        self.request._model_key = normalized
        return self

    # Create Request
    def create_request(
        self,
        *,
        name: str | None = None,
        inherit_agent_prompt: bool = True,
        inherit_extension_handlers: bool = True,
        model_key: str | None = None,
    ):
        """
        Create a request instance.

        By default this method returns an isolated request that only inherits
        agent settings, which avoids pulling in agent prompt/session handlers
        implicitly.

        Set `inherit_agent_prompt` / `inherit_extension_handlers` to True when
        you intentionally want a request to reuse current agent context.
        """
        return ModelRequest(
            agent_name=name if name is not None else self.name,
            agent_id=self.id,
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            parent_prompt=self.agent_prompt if inherit_agent_prompt else None,
            parent_extension_handlers=self.extension_handlers if inherit_extension_handlers else None,
            model_key=model_key if model_key is not None else self._active_model_key,
        )

    def create_temp_request(self, model_key: str | None = None):
        return self.create_request(
            name=f"{ self.name }-Temp-{ uuid.uuid4().hex }",
            inherit_agent_prompt=False,
            inherit_extension_handlers=False,
            model_key=model_key,
        )

    def create_turn(self):
        request = self.create_request()
        prompt_snapshot = self._snapshot_request_prompt()
        if prompt_snapshot:
            request.prompt.update(prompt_snapshot)
            self.request.prompt.clear()
        return AgentTurn(self, request=request)

    _DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS = {
        "output",
        "output_format",
        "ensure_all_keys",
    }

    def _snapshot_request_prompt(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get()
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, Mapping) else {}

    def _has_dynamic_task_prompt_context(self, prompt_snapshot: Mapping[str, Any]) -> bool:
        for key, value in prompt_snapshot.items():
            if key in self._DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS:
                continue
            if value not in (None, "", [], {}):
                return True
        return False

    def _has_dynamic_task_prompt_context_besides_input(self, prompt_snapshot: Mapping[str, Any]) -> bool:
        for key, value in prompt_snapshot.items():
            if key == "input" or key in self._DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS:
                continue
            if value not in (None, "", [], {}):
                return True
        return False

    def _render_dynamic_task_target_from_prompt(self, prompt_snapshot: Mapping[str, Any]) -> str | None:
        prompt_data = {
            key: value
            for key, value in prompt_snapshot.items()
            if key not in self._DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS
        }
        if not self._has_dynamic_task_prompt_context(prompt_data):
            return None
        prompt = Prompt(
            name=f"{ self.name }-DynamicTaskPromptSnapshot",
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            prompt_dict=dict(prompt_data),
        )
        text = str(prompt.to_text() or "").strip()
        text = text.removesuffix("assistant:").rstrip()
        text = text.removesuffix("[OUTPUT]:").rstrip()
        return text or None

    def _dynamic_task_prompt_defaults(
        self,
        target: Any = None,
        *,
        prompt_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = dict(prompt_snapshot or self._snapshot_request_prompt())
        original_snapshot = dict(snapshot)
        if target is not None:
            snapshot["input"] = target

        output_schema = snapshot.get("output")
        output_format = snapshot.get("output_format")
        input_value = snapshot.get("input")

        target_text: str | None = None
        if target is not None and not self._has_dynamic_task_prompt_context_besides_input(original_snapshot):
            target_text = str(target)
        elif (
            target is None
            and set(key for key, value in snapshot.items() if value not in (None, "", [], {}))
            <= {"input", "output", "output_format", "ensure_all_keys"}
            and isinstance(input_value, str)
            and input_value.strip()
        ):
            target_text = input_value.strip()
        else:
            target_text = self._render_dynamic_task_target_from_prompt(snapshot)

        if target_text is None and input_value is not None:
            if isinstance(input_value, str):
                target_text = input_value.strip() or None
            else:
                target_text = json.dumps(DataFormatter.sanitize(input_value), ensure_ascii=False)

        return {
            "target": target_text,
            "output_schema": output_schema,
            "output_format": output_format,
            "initial_graph_input": input_value,
        }

    def create_dynamic_task(
        self,
        target: str | None = None,
        *,
        plan: "TaskDAG | Mapping[str, Any] | None" = None,
        planner: Any = None,
        model: Any = None,
        actions: Any = None,
        skills: Any = None,
        handlers: Mapping[str, Any] | None = None,
        name: str | None = None,
        max_tasks: int | None = None,
        output_schema: Any = None,
        ensure_keys: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        _prompt_snapshot: Mapping[str, Any] | None = None,
    ) -> DynamicTask:
        prompt_defaults = self._dynamic_task_prompt_defaults(target, prompt_snapshot=_prompt_snapshot)
        resolved_target = target if target is not None and prompt_defaults["target"] is None else prompt_defaults["target"]
        if not resolved_target:
            raise ValueError("agent.create_dynamic_task(...) requires target=... or a configured agent.input(...).")
        resolved_output_schema = output_schema if output_schema is not None else prompt_defaults["output_schema"]
        resolved_output_format = output_format if output_format is not None else prompt_defaults["output_format"]
        initial_graph_input = prompt_defaults["initial_graph_input"]
        if _prompt_snapshot is None:
            self.request.prompt.clear()
        return DynamicTask(
            self.plugin_manager,
            str(resolved_target),
            plan=plan,
            planner=self if planner is None else planner,
            model=self if model is None else model,
            actions=actions,
            skills=skills,
            handlers=handlers,
            parent_settings=self.settings,
            name=name if name is not None else f"{ self.name }-DynamicTask",
            max_tasks=max_tasks,
            output_schema=resolved_output_schema,
            ensure_keys=ensure_keys,
            output_format=cast(Any, resolved_output_format),
            initial_graph_input=initial_graph_input,
        )

    def use_dynamic_task(
        self,
        *,
        mode: Literal["auto", "submitted"] = "auto",
        plan: "TaskDAG | Mapping[str, Any] | None" = None,
        planner: Any = None,
        model: Any = None,
        actions: Any = None,
        skills: Any = None,
        handlers: Mapping[str, Any] | None = None,
        name: str | None = None,
        max_tasks: int | None = None,
        output_schema: Any = None,
        ensure_keys: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        graph_input: Any = _UNSET,
        timeout: float | None = None,
        max_retries: int = 3,
    ):
        if mode not in {"auto", "submitted"}:
            raise ValueError("Dynamic Task mode must be one of: 'auto', 'submitted'.")
        if mode == "submitted" and plan is None:
            raise ValueError("use_dynamic_task(mode='submitted') requires plan=.")
        graph_input_provided = graph_input is not _UNSET
        self._dynamic_task_candidates.append(
            {
                "mode": mode,
                "plan": plan,
                "planner": planner,
                "model": model,
                "actions": actions,
                "skills": skills,
                "handlers": handlers,
                "name": name,
                "max_tasks": max_tasks,
                "output_schema": output_schema,
                "ensure_keys": ensure_keys,
                "output_format": output_format,
                "graph_input": graph_input if graph_input_provided else None,
                "graph_input_provided": graph_input_provided,
                "timeout": timeout,
                "max_retries": max_retries,
            }
        )
        return self

    def _create_agent_turn_run_context(
        self,
        *,
        parent_run_context: "RunContext | None" = None,
    ):
        from agently.types.data import RunContext

        parent_run_context = resolve_parent_run_context(parent_run_context)
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            session_id = str(session_id)
        return RunContext.create(
            run_kind="agent_turn",
            parent=parent_run_context,
            agent_id=self.id,
            agent_name=self.name,
            session_id=session_id,
            meta={"entrypoint": "agent"},
        )

    def _emit_agent_turn_started(self, turn_run_context: "RunContext"):
        from agently.base import emit_runtime

        emit_runtime(
            {
                "event_type": "agent_turn.started",
                "source": "BaseAgent",
                "message": f"Agent turn started for '{ self.name }'.",
                "payload": {
                    "agent_id": self.id,
                    "agent_name": self.name,
                },
                "run": turn_run_context,
            }
        )

    async def _async_emit_agent_turn_started(self, turn_run_context: "RunContext"):
        from agently.base import async_emit_runtime

        await async_emit_runtime(
            {
                "event_type": "agent_turn.started",
                "source": "BaseAgent",
                "message": f"Agent turn started for '{ self.name }'.",
                "payload": {
                    "agent_id": self.id,
                    "agent_name": self.name,
                },
                "run": turn_run_context,
            }
        )

    def _emit_session_runtime_observation(
        self,
        kind: str,
        *,
        message: str,
        payload: dict[str, Any],
        run: "RunContext | None" = None,
        level: str = "INFO",
        error: BaseException | None = None,
    ):
        from agently.core.runtime import emit_session_observation

        emit_session_observation(
            {
                "kind": kind,
                "source": "SessionExtension",
                "level": level,
                "message": message,
                "payload": payload,
                "error": error,
                "run": run,
            }
        )

    async def _async_emit_session_runtime_observation(
        self,
        kind: str,
        *,
        message: str,
        payload: dict[str, Any],
        run: "RunContext | None" = None,
        level: str = "INFO",
        error: BaseException | None = None,
    ):
        from agently.core.runtime import async_emit_session_observation

        await async_emit_session_observation(
            {
                "kind": kind,
                "source": "SessionExtension",
                "level": level,
                "message": message,
                "payload": payload,
                "error": error,
                "run": run,
            }
        )

    def get_response(self, *, parent_run_context: "RunContext | None" = None):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return self.request.get_response(parent_run_context=turn_run_context)

    def get_result(self, *, parent_run_context: "RunContext | None" = None):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return self.request.get_result(parent_run_context=turn_run_context)

    def get_meta(self, *, parent_run_context: "RunContext | None" = None):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return self.request.get_meta(parent_run_context=turn_run_context)

    async def async_get_meta(self, *, parent_run_context: "RunContext | None" = None):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_turn_started(turn_run_context)
        return await self.request.async_get_meta(parent_run_context=turn_run_context)

    def get_text(self, *, parent_run_context: "RunContext | None" = None):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return self.request.get_text(parent_run_context=turn_run_context)

    async def async_get_text(self, *, parent_run_context: "RunContext | None" = None):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_turn_started(turn_run_context)
        return await self.request.async_get_text(parent_run_context=turn_run_context)

    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return self.request.get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=turn_run_context,
        )

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_turn_started(turn_run_context)
        return await self.request.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=turn_run_context,
        )

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return self.request.get_data_object(
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=turn_run_context,
        )

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_turn_started(turn_run_context)
        return await self.request.async_get_data_object(
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=turn_run_context,
        )

    def get_generator(self, *args, parent_run_context: "RunContext | None" = None, **kwargs):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return cast(Any, self.request).get_generator(*args, parent_run_context=turn_run_context, **kwargs)

    def get_async_generator(self, *args, parent_run_context: "RunContext | None" = None, **kwargs):
        turn_run_context = self._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._emit_agent_turn_started(turn_run_context)
        return cast(Any, self.request).get_async_generator(*args, parent_run_context=turn_run_context, **kwargs)

    def start(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        return self.create_execution(parent_run_context=parent_run_context).get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    async def async_start(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        return await self.create_execution(parent_run_context=parent_run_context).async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    def create_execution(
        self,
        *,
        mode: "AgentExecutionMode | str" = "one_turn",
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: "ExecutionOptions | dict[str, Any] | None" = None,
        parent_run_context: "RunContext | None" = None,
    ):
        plugin_name = str(self.settings.get("plugins.AgentOrchestrator.activate", "AgentlyAgentOrchestrator"))
        plugin_class = cast(Any, self.plugin_manager.get_plugin("AgentOrchestrator", plugin_name))
        orchestrator = plugin_class(plugin_manager=self.plugin_manager, settings=self.settings)
        return orchestrator.create_execution(
            self,
            mode=mode,
            lineage=lineage,
            limits=limits,
            options=options,
            parent_run_context=parent_run_context,
        )

    def create_task(
        self,
        *,
        goal: str,
        success_criteria: list[str],
        workspace: str | os.PathLike[str] | None = None,
        max_iterations: int = 3,
        verify: Literal["before_done"] = "before_done",
        recall_profile: str = "software_dev",
        context_budget: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        task_id: str | None = None,
    ):
        return AgentTask(
            self,
            goal=goal,
            success_criteria=success_criteria,
            workspace=workspace,
            max_iterations=max_iterations,
            verify=verify,
            recall_profile=recall_profile,
            context_budget=context_budget,
            limits=limits,
            options=options,
            task_id=task_id,
        )

    def validate(self, handler: "OutputValidateHandler"):
        self.extension_handlers.append("validate_handlers", handler)
        return self

    # Basic Methods
    def set_agent_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ):
        self.agent_prompt.set(key, value, mappings=mappings)
        return self

    def set_request_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ):
        self.request.prompt.set(key, value, mappings=mappings)
        return self

    def remove_agent_prompt(self, key: "PromptStandardSlot | str"):
        self.agent_prompt.set(key, None)
        return self

    def remove_request_prompt(self, key: "PromptStandardSlot | str"):
        self.request.prompt.set(key, None)
        return self

    def _replace_agent_prompt_value(self, key: "PromptStandardSlot | str", value: Any):
        if key in self.agent_prompt:
            del self.agent_prompt[key]
        self.agent_prompt.set(key, value)

    def reset_chat_history(self):
        self._replace_agent_prompt_value("chat_history", [])
        return self

    def set_chat_history(self, chat_history: "Sequence[ChatMessage | ChatMessageDict]"):
        if not isinstance(chat_history, Sequence):
            chat_history = [chat_history]
        self._replace_agent_prompt_value("chat_history", chat_history)
        return self

    def add_chat_history(self, chat_history: "Sequence[ChatMessage | ChatMessageDict] | ChatMessageDict | ChatMessage"):
        if not isinstance(chat_history, Sequence):
            chat_history = [chat_history]
        self.agent_prompt.extend("chat_history", chat_history)
        return self

    def reset_action_results(self):
        if "action_results" in self.agent_prompt:
            del self.agent_prompt["action_results"]
        return self

    def set_action_results(self, action_results: list[dict[str, Any]]):
        self._replace_agent_prompt_value("action_results", action_results)
        return self

    def add_action_results(self, action: str, result: Any):
        self.agent_prompt.append("action_results", {action: result})
        return self

    # Quick Prompt
    def system(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        if always:
            self.agent_prompt.set("system", prompt, mappings=mappings)
            return self
        return self.create_turn().system(prompt, mappings=mappings)

    def rule(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        if always:
            self.agent_prompt.set("instruct", ["{system.rule} ARE IMPORTANT RULES YOU SHALL FOLLOW!"])
            self.agent_prompt.set("system.rule", prompt, mappings=mappings)
            return self
        return self.create_turn().rule(prompt, mappings=mappings)

    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("instruct", ["YOU MUST REACT AND RESPOND AS {system.role}!"])
            self.agent_prompt.set("system.your_role", prompt, mappings=mappings)
            return self
        return self.create_turn().role(prompt, mappings=mappings)

    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("instruct", ["{system.user_info} IS IMPORTANT INFORMATION ABOUT USER!"])
            self.agent_prompt.set("system.user_info", prompt, mappings=mappings)
            return self
        return self.create_turn().user_info(prompt, mappings=mappings)

    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("input", prompt, mappings=mappings)
            return self
        return self.create_turn().input(prompt, mappings=mappings)

    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("info", prompt, mappings=mappings)
            return self
        return self.create_turn().info(prompt, mappings=mappings)

    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("instruct", prompt, mappings=mappings)
            return self
        return self.create_turn().instruct(prompt, mappings=mappings)

    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("examples", prompt, mappings=mappings)
            return self
        return self.create_turn().examples(prompt, mappings=mappings)

    def output(
        self,
        prompt: (
            dict[str, tuple[type, str | None, str, None] | Any]
            | list[tuple[type, str | None, str, None] | Any]
            | tuple[type, str | None, str, None]
            | Any
        ),
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] = "auto",
    ):
        if always:
            self.agent_prompt.set("output", prompt, mappings=mappings)
            self.agent_prompt.set("output_format", format)
            return self
        return self.create_turn().output(prompt, mappings=mappings, format=format)

    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        if always:
            self.agent_prompt.set("attachment", prompt, mappings=mappings)
            return self
        return self.create_turn().attachment(prompt, mappings=mappings)

    def image(
        self,
        *,
        question: str,
        file: str | os.PathLike[str] | None = None,
        url: str | None = None,
        files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
        urls: list[str] | tuple[str, ...] | None = None,
        detail: ImageDetail | None = None,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        attachment = build_image_attachment(
            question=question,
            file=file,
            url=url,
            files=files,
            urls=urls,
            detail=detail,
        )
        if always:
            self.agent_prompt.set("attachment", attachment, mappings=mappings)
            return self
        return self.create_turn().attachment(attachment, mappings=mappings)

    def options(
        self,
        options: dict[str, Any],
        *,
        always: bool = False,
    ):
        if always:
            self.agent_prompt.set("options", options)
            return self
        return self.create_turn().options(options)

    # Prompt
    def get_prompt_text(self):
        return self.request_prompt.to_text()[6:][:-11]

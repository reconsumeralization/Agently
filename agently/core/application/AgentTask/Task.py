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


from __future__ import annotations

from .TaskShared import *
from .StrategyRouter import AgentTaskStrategyRouterMixin
from .TaskBoardStrategy import AgentTaskTaskBoardStrategyMixin
from .ArtifactDelivery import AgentTaskArtifactMixin
from .FlatStrategy import AgentTaskFlatStrategyMixin
from .Carrier import AgentTaskCarrierMixin
from .AcpRecovery import AgentTaskAcpRecoveryMixin
from .Verification import AgentTaskVerificationMixin
from .RuntimeControl import AgentTaskRuntimeMixin
from .Resume import AgentTaskResumeMixin
from .Observation import AgentTaskObservationMixin


class AgentTask(
    AgentTaskStrategyRouterMixin,
    AgentTaskTaskBoardStrategyMixin,
    AgentTaskArtifactMixin,
    AgentTaskFlatStrategyMixin,
    AgentTaskCarrierMixin,
    AgentTaskAcpRecoveryMixin,
    AgentTaskVerificationMixin,
    AgentTaskRuntimeMixin,
    AgentTaskResumeMixin,
    AgentTaskObservationMixin,
):
    """Retained owner for one Agent-managed business task lifecycle."""

    normalize_max_iterations = staticmethod(_normalize_agent_task_max_iterations)

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        goal: str,
        success_criteria: list[str],
        execution: AgentTaskExecutionStrategy | str | None = "auto",
        workspace: str | os.PathLike[str] | None = None,
        max_iterations: int | None = _AGENT_TASK_DEFAULT_MAX_ITERATIONS,
        verify: Literal["before_done"] = "before_done",
        context_profile: str = "auto",
        context_budget: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        task_id: str | None = None,
    ):
        if not str(goal or "").strip():
            raise ValueError("agent.create_task(...) requires a non-empty goal.")
        if not success_criteria:
            raise ValueError("agent.create_task(...) requires at least one success criterion.")
        self.agent = agent
        self.id = task_id or f"agent_task_{uuid.uuid4().hex}"
        self.goal = str(goal)
        self.success_criteria = [str(item) for item in success_criteria if str(item).strip()]
        self.execution_strategy = self.normalize_execution_strategy(execution)
        self.effective_execution_strategy: AgentTaskEffectiveExecutionStrategy | None = (
            cast(AgentTaskEffectiveExecutionStrategy, self.execution_strategy)
            if self.execution_strategy in {"flat", "taskboard"}
            else None
        )
        self.task_shape_analysis: dict[str, Any] = {}
        self.max_iterations = _normalize_agent_task_max_iterations(max_iterations)
        self.verify = verify
        self.context_profile = context_profile
        self.context_budget = dict(context_budget or {"chars": 6000})
        self.limits = dict(limits or {})
        self.options = dict(options or {})
        agent_with_workspace = cast(Any, agent)
        if workspace is not None:
            agent_with_workspace.use_workspace(workspace)
        if getattr(agent, "workspace", None) is None:
            raise RuntimeError(
                "AgentTask requires a Workspace binding. Standard Agents include a lazy Workspace; "
                "pass workspace=... or call agent.use_workspace(...) only when you need an explicit "
                "root, mode, or provider."
            )
        bound_workspace = agent_with_workspace.workspace
        # Bind the task file root as a lineage child of the Agent scope so the
        # task subtree (and any nested executions) lives under the Agent node and
        # can be pruned as one contained subtree (spec section 8.2).
        with_scope_node = getattr(bound_workspace, "with_scope_node", None)
        if callable(with_scope_node):
            self.workspace: Any = with_scope_node(
                "tasks",
                self.id,
                scope={"task_id": self.id},
                search_scope={"task_id": self.id},
            )
        else:
            self.workspace = bound_workspace
        self.status: AgentTaskStatus = "created"
        self.result: Any = None
        self.diagnostics: dict[str, Any] = {}
        self.iterations: list[dict[str, Any]] = []
        self.workspace_refs: dict[str, list[str]] = {
            "observations": [],
            "decisions": [],
            "verification": [],
            "checkpoints": [],
            "evidence_links": [],
            "strategy": [],
            "reflections": [],
            "acp_recovery": [],
        }
        self.reflections: list[dict[str, Any]] = []
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self._completed = False
        self._error: BaseException | None = None
        self._start_lock = asyncio.Lock()
        # Required capabilities are satisfied cumulatively across iterations: a
        # capability used by any bounded step counts as satisfied for the task,
        # so a model_request step and a skills step in different iterations can
        # together satisfy required actions and required skills.
        self._satisfied_required_actions: set[str] = set()
        self._satisfied_required_skills: set[str] = set()
        # Capability ids used in any bounded step, accumulated across iterations so
        # a capability-evidence requirement can be satisfied cumulatively.
        self._satisfied_capabilities: set[str] = set()
        # Action ids that succeeded in any bounded step, accumulated the same way
        # so an action_succeeded evidence requirement is not lost on a later step.
        self._satisfied_succeeded_actions: set[str] = set()
        # Execution shapes that failed earlier in this task. In auto mode the
        # planner should adapt instead of repeatedly selecting the same failing
        # route shape.
        self._failed_execution_shapes: set[str] = set()
        # Durable resume state (populated by AgentTask.async_resume before run).
        self._resumed_from_iteration: int = 0
        self._resumed_iteration_summaries: list[dict[str, Any]] = []
        self._resumed_prior_result: Any = None
        self._stream_items: list[AgentExecutionStreamData] = []
        self._stream_queues: list[asyncio.Queue[Any]] = []
        self._background_stream_tasks: set[asyncio.Task[Any]] = set()
        self._emitted_action_event_keys: set[tuple[str, str, str, str, str]] = set()
        self._last_stream_emit_monotonic = time.monotonic()
        self._flow = self._build_flow()

        self.run: Any = self._run
        self.meta: Any = self._meta
        self.get_meta: Any = self.meta
        self.stream: Any = self.get_async_generator
        self.get_generator: Any = self._get_generator

    def _build_flow(self):
        flow = TriggerFlow(name="agent-task-loop")

        async def loop(data):
            await data.async_set_state("task_id", self.id, emit=False)
            try:
                effective_strategy = await self._resolve_effective_execution_strategy()
            except _AgentTaskDeadlineExceeded as error:
                await self._emit("agent_task.started", self._task_summary())
                await self._terminate_timed_out(
                    0,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state("agent_task.execution_strategy", self.execution_strategy, emit=False)
                await data.async_set_state(
                    "agent_task.effective_execution_strategy",
                    self.effective_execution_strategy,
                    emit=False,
                )
                await data.async_set_state("agent_task.result", self.result, emit=False)
                await data.async_set_state("agent_task.status", self.status, emit=False)
                return
            await data.async_set_state("agent_task.execution_strategy", self.execution_strategy, emit=False)
            await data.async_set_state("agent_task.effective_execution_strategy", effective_strategy, emit=False)
            await self._emit("agent_task.started", self._task_summary())
            start_iteration = self._resumed_from_iteration + 1
            if start_iteration > 1:
                await self._emit(
                    "agent_task.resumed",
                    {"task_id": self.id, "resumed_from_iteration": self._resumed_from_iteration},
                )
            if effective_strategy == "taskboard":
                result = await self._run_taskboard()
                await data.async_set_state("agent_task.latest_iteration", result, emit=False)
                await data.async_set_state("agent_task.result", self.result, emit=False)
                await data.async_set_state("agent_task.status", self.status, emit=False)
                return
            iteration_index = start_iteration
            while self.max_iterations is None or iteration_index <= self.max_iterations:
                result = await self._run_iteration(iteration_index)
                await data.async_set_state("agent_task.latest_iteration", result, emit=False)
                if result["terminal"]:
                    break
                iteration_index += 1
            await data.async_set_state("agent_task.result", self.result, emit=False)
            await data.async_set_state("agent_task.status", self.status, emit=False)

        flow.to(loop, name="agent_task_loop")
        return flow


__all__ = [
    "AgentTask",
    "AgentTaskStatus",
    "AgentTaskExecutionStrategy",
    "AgentTaskEffectiveExecutionStrategy",
]

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

from agently.core.Workspace import Workspace

from .LifecycleState import AgentTaskLifecycleState
from .LifecycleFlow import AgentTaskLifecycleFlowMixin
from .TaskShared import *
from .StrategyRouter import AgentTaskStrategyRouterMixin
from .TaskBoardStrategy import AgentTaskTaskBoardStrategyMixin
from .ArtifactDelivery import AgentTaskArtifactMixin
from .FlatStrategy import AgentTaskFlatStrategyMixin
from .Carrier import AgentTaskCarrierMixin
from .TerminalVerification import AgentTaskTerminalVerificationMixin
from .AcpRecovery import AgentTaskAcpRecoveryMixin
from .Verification import AgentTaskVerificationMixin
from .Guidance import AgentTaskGuidanceMixin
from .RuntimeControl import AgentTaskRuntimeMixin
from .Resume import AgentTaskResumeMixin
from .Observation import AgentTaskObservationMixin


class AgentTask(
    AgentTaskLifecycleFlowMixin,
    AgentTaskStrategyRouterMixin,
    AgentTaskTaskBoardStrategyMixin,
    AgentTaskArtifactMixin,
    AgentTaskFlatStrategyMixin,
    AgentTaskCarrierMixin,
    AgentTaskTerminalVerificationMixin,
    AgentTaskAcpRecoveryMixin,
    AgentTaskVerificationMixin,
    AgentTaskGuidanceMixin,
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
        workspace: str | os.PathLike[str] | Workspace | None = None,
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
        self._task_reference_catalog = TaskReferenceCatalog(self.id)
        self._terminal_convergence_state = TerminalConvergenceState(self.id)
        self.goal = str(goal)
        self.success_criteria = [str(item) for item in success_criteria if str(item).strip()]
        self.execution_strategy = self.normalize_execution_strategy(execution)
        self.effective_execution_strategy: AgentTaskEffectiveExecutionStrategy | None = (
            cast(AgentTaskEffectiveExecutionStrategy, self.execution_strategy)
            if self.execution_strategy in {"flat", "taskboard"}
            else None
        )
        self._lifecycle_state = AgentTaskLifecycleState(
            task_id=self.id,
            requested_strategy=self.execution_strategy,
            effective_strategy=self.effective_execution_strategy,
        )
        self._terminal_inline_values: dict[str, str] = {}
        self._terminal_materialization_diagnostics: list[dict[str, Any]] = []
        self._lifecycle_frames: dict[str, dict[str, Any]] = {}
        self._lifecycle_error: BaseException | None = None
        self.task_shape_analysis: dict[str, Any] = {}
        self.max_iterations = _normalize_agent_task_max_iterations(max_iterations)
        self.verify = verify
        self.context_profile = context_profile
        self.context_budget = dict(context_budget or {"chars": 6000})
        self.limits = dict(limits or {})
        self.options = dict(options or {})
        agent_with_workspace = cast(Any, agent)
        if isinstance(workspace, Workspace):
            bound_workspace = workspace
        else:
            if workspace is not None:
                agent_with_workspace.use_workspace(workspace)
            bound_workspace = getattr(agent, "workspace", None)
        if bound_workspace is None:
            raise RuntimeError(
                "AgentTask requires a Workspace binding. Standard Agents include a Workspace; "
                "pass workspace=... or call agent.use_workspace(...) only when you need an explicit "
                "root, mode, or provider."
            )
        # AgentTask shares the caller's ordinary file root. Only execution
        # identity changes so private fallback files stay task-owned; binding
        # must not materialize storage or invent a scoped file tree.
        self._workspace_execution_id = self.id
        self.workspace: Workspace = bound_workspace._bind_execution(
            self.id,
            scope={"task_id": self.id, "execution_id": self.id},
            search_scope={"task_id": self.id, "execution_id": self.id},
        )
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
            "guidance": [],
        }
        self.guidance_items: list[dict[str, Any]] = []
        self._guidance_sequence = 0
        self._guidance_lock = asyncio.Lock()
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
        self._resumed_taskboard_state: dict[str, Any] | None = None
        self._latest_taskboard_acceptance_index: dict[str, Any] | None = None
        self._taskboard_planned_workspace_deliverables: list[str] = []
        self._resumed_prior_result: Any = None
        self._terminal_deliverable_refs: list[WorkspaceRecordRef] = []
        self._terminal_retained_refs: list[Any] = []
        self._terminal_retention_deferred = False
        # Routed AgentTask construction transfers this exact task-owned Action
        # artifact scope to its parent AgentExecution. Standalone tasks keep the
        # value unset and release the scope in their own terminal seam.
        self._action_artifact_scope_transferred_to_execution_id: str | None = None
        self._terminal_taskboard_state: dict[str, Any] | None = None
        self._stream_items: list[AgentExecutionStreamData] = []
        self._stream_queues: list[asyncio.Queue[Any]] = []
        self._background_stream_tasks: set[asyncio.Task[Any]] = set()
        self._emitted_action_event_keys: set[tuple[str, str, str, str, str]] = set()
        self._last_stream_emit_monotonic = time.monotonic()
        self._last_heartbeat_emit_monotonic = 0.0
        self._flow = self._build_flow()

        self.run: Any = self._run
        self.meta: Any = self._meta
        self.get_meta: Any = self.meta
        self.add_guidance: Any = FunctionShifter.syncify(self.async_add_guidance)
        self.stream: Any = self.get_async_generator
        self.get_generator: Any = self._get_generator

__all__ = [
    "AgentTask",
    "AgentTaskStatus",
    "AgentTaskExecutionStrategy",
    "AgentTaskEffectiveExecutionStrategy",
]

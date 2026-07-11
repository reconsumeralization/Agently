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

import importlib
import sys

from agently.types.data import TaskBoardCard, TaskBoardGraph, TaskBoardPatch, TaskBoardRevision, TaskDAG, TaskDAGNode

from .Agent import BaseAgent
from .application import (
    AgentExecutionContext,
    AgentExecutionLimitExceeded,
    AgentExecutionResult,
    AgentExecutionStream,
    AgentTask,
    Blocks,
    DynamicTask,
    RuntimeStageStallError,
    SkillsExecutor,
    SkillsManager,
)
from .operation import (
    Action,
    ActionDispatcher,
    ActionRegistry,
    ExecutionResourceApprovalDenied,
    ExecutionResourceApprovalRequired,
    ExecutionResourceError,
    ExecutionExchangeManager,
    ExecutionResourceManager,
    PolicyApprovalManager,
    Tool,
)
from .extension import ExtensionHandlers, PluginManager
from .model import (
    AttemptRunner,
    ModelRequest,
    ModelRequestResult,
    ModelResponse,
    Prompt,
    core_attempt_runner_entrypoint,
    is_core_attempt_runner_entrypoint,
)
from .orchestration import (
    CompiledTaskDAG,
    TaskDAGContext,
    TaskDAGHandler,
    TaskDAGResolver,
    TaskDAGExecutor,
    TaskDAGValidation,
    TaskDAGValidator,
    TaskBoard,
    TaskBoardContext,
    TaskBoardEffortProfile,
    TaskBoardEvidenceView,
    TaskBoardHandler,
    TaskBoardPlanningPolicy,
    TaskBoardPlanningResult,
    TaskBoardTickExecution,
    TaskBoardTickResult,
    TaskBoardValidation,
    TaskBoardValidator,
    build_task_board_evidence_view,
    coerce_task_board_planning_result,
    resolve_task_board_effort_profile,
    resolve_task_board_planning_policy,
    task_board_planning_output_schema,
    TriggerFlow,
    TriggerFlowBlueprint,
    TriggerFlowChunk,
    TriggerFlowExecution,
    TriggerFlowExecutionResult,
)
from .runtime import (
    EventCenter,
    ObservationEventEmitter,
    RuntimeEvent,
    RuntimeEventEmitter,
    bind_runtime_context,
)
from .session import Session
_workspace_package = importlib.import_module(f"{__name__}.Workspace")

sys.modules[f"{__name__}.workspace"] = _workspace_package

from .Workspace import (
    AgentEmbeddingProvider,
    CallableEmbeddingProvider,
    ChromaVectorStoreProvider,
    DefaultContextBuilder,
    LazyWorkspace,
    LocalVectorIndex,
    LocalWorkspaceBackend,
    ContextProfile,
    RuleContextPlanner,
    Workspace,
    WorkspaceConfigurationError,
    WorkspaceError,
    WorkspaceManager,
    WorkspacePolicyError,
    WorkspaceRetriever,
    SQLiteVectorStoreProvider,
)

__all__ = [
    "Action",
    "ActionDispatcher",
    "ActionRegistry",
    "AgentEmbeddingProvider",
    "AgentExecutionContext",
    "AgentExecutionLimitExceeded",
    "AgentExecutionResult",
    "AgentExecutionStream",
    "AgentTask",
    "AttemptRunner",
    "BaseAgent",
    "Blocks",
    "CallableEmbeddingProvider",
    "ChromaVectorStoreProvider",
    "CompiledTaskDAG",
    "DefaultContextBuilder",
    "DynamicTask",
    "TaskDAGContext",
    "TaskDAGHandler",
    "TaskDAGResolver",
    "EventCenter",
    "ExecutionResourceApprovalDenied",
    "ExecutionResourceApprovalRequired",
    "ExecutionResourceError",
    "ExecutionExchangeManager",
    "ExecutionResourceManager",
    "ExtensionHandlers",
    "LazyWorkspace",
    "LocalVectorIndex",
    "LocalWorkspaceBackend",
    "ModelRequest",
    "ModelRequestResult",
    "ModelResponse",
    "ObservationEventEmitter",
    "PluginManager",
    "PolicyApprovalManager",
    "Prompt",
    "ContextProfile",
    "RuleContextPlanner",
    "RuntimeEvent",
    "RuntimeEventEmitter",
    "RuntimeStageStallError",
    "Session",
    "SkillsExecutor",
    "SkillsManager",
    "SQLiteVectorStoreProvider",
    "TaskDAG",
    "TaskDAGExecutor",
    "TaskDAGNode",
    "TaskDAGValidation",
    "TaskDAGValidator",
    "TaskBoard",
    "TaskBoardCard",
    "TaskBoardContext",
    "TaskBoardEffortProfile",
    "TaskBoardEvidenceView",
    "TaskBoardGraph",
    "TaskBoardHandler",
    "TaskBoardPatch",
    "TaskBoardPlanningPolicy",
    "TaskBoardPlanningResult",
    "TaskBoardRevision",
    "TaskBoardTickExecution",
    "TaskBoardTickResult",
    "TaskBoardValidation",
    "TaskBoardValidator",
    "Tool",
    "TriggerFlow",
    "TriggerFlowBlueprint",
    "TriggerFlowChunk",
    "TriggerFlowExecution",
    "TriggerFlowExecutionResult",
    "Workspace",
    "WorkspaceConfigurationError",
    "WorkspaceError",
    "WorkspaceManager",
    "WorkspacePolicyError",
    "WorkspaceRetriever",
    "build_task_board_evidence_view",
    "bind_runtime_context",
    "coerce_task_board_planning_result",
    "core_attempt_runner_entrypoint",
    "is_core_attempt_runner_entrypoint",
    "resolve_task_board_effort_profile",
    "resolve_task_board_planning_policy",
    "task_board_planning_output_schema",
]

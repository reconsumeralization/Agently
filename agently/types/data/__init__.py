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


class AVOID_COPY:
    __slots__ = ("id",)

    def __init__(self):
        import uuid

        self.id = uuid.uuid4().hex

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (self.__class__, (), {"id": self.id})


EMPTY = AVOID_COPY()

from .serializable import SerializableData, SerializableMapping, SerializableValue
from .prompt import (
    ChatMessage,
    ChatMessageDict,
    ChatMessageContent,
    TextMessageContent,
    PromptModel,
    PromptOutputStructure,
    PromptStandardSlot,
    ToolMeta,
)
from .request import (
    APIKeyFailoverAction,
    APIKeyFailoverContext,
    APIKeySelectionContext,
    AgentlyRequestData,
    AgentlyRequestDataDict,
)

from .response import (
    AgentExecutionStreamData,
    AgentExecutionStreamHandler,
    AgentlyOriginalResultPayload,
    AgentlyOriginalResponsePayload,
    AgentlyModelResult,
    AgentlyModelResultEvent,
    AgentlyModelResultMessage,
    AgentlyModelResponseEvent,
    AgentlyModelResponseMessage,
    AgentlySpecificResultMessage,
    AgentlySpecificResponseMessage,
    AgentlyResultGenerator,
    AgentlyResponseGenerator,
    InstantStreamingContentType,
    ModelStreamingHandler,
    NormalStreamingContentType,
    OutputValidateContext,
    OutputValidateHandler,
    OutputValidateResult,
    OutputValidateResultDict,
    ResultContentType,
    ResponseContentType,
    SpecificEvents,
    StreamingContentType,
    StreamingData,
)

from .event import (
    ObservationEventLevel,
    RuntimeEventLevel,
    EventDeliveryMode,
    EventDispatchMode,
    EventDeliveryPolicy,
    RunKind,
    ErrorInfoDict,
    RunContextDict,
    ObservationEventDict,
    RuntimeEventDict,
    ErrorInfo,
    RunContext,
    ObservationEvent,
    RuntimeEvent,
    RuntimeEventHook,
    EventHook,
    ObservationEventHook,
)

from .tool import (
    ArgumentDesc,
    KwargsType,
    ReturnType,
    MCPConfig,
    MCPConfigs,
    ToolInfo,
)

from .task_dag import (
    TASK_DAG_SCHEMA_VERSION,
    TaskDAG,
    TaskDAGNode,
)

from .action import (
    ActionApproval,
    ActionArtifact,
    ActionCall,
    ActionDecision,
    ActionDiagnostic,
    ActionPolicy,
    ActionPlanningRequest,
    ActionExecutionRequest,
    ActionResult,
    ActionRunContext,
    ActionSideEffectLevel,
    ActionSpec,
    ActionStatus,
)

from .execution_environment import (
    ExecutionEnvironmentDecision,
    ExecutionEnvironmentHandle,
    ExecutionEnvironmentKind,
    ExecutionEnvironmentPolicy,
    ExecutionEnvironmentRequirement,
    ExecutionEnvironmentScope,
    ExecutionEnvironmentStatus,
)

from .execution_exchange import (
    ExecutionExchangeDispatchState,
    ExecutionExchangeProviderResult,
    ExecutionExchangeRequest,
    ExecutionExchangeWaitMode,
)

from .policy_approval import (
    PolicyApprovalDecision,
    PolicyApprovalHandler,
    PolicyApprovalRequest,
    PolicyApprovalSource,
    PolicyApprovalStatus,
)

from .skill import (
    ExecutionStrategy,
    SkillCapabilityNeed,
    SkillCard,
    SkillContract,
    SkillDecisionCard,
    SkillExecutionDict,
    SkillExecutionPlan,
    SkillExecutionStatus,
    SkillMode,
    SkillRuntimeStreamHandler,
    SkillRuntimeStreamItem,
    SkillsPackRecord,
    SkillPlanRejection,
    SkillPlanSelection,
)

from .workspace import (
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceContextItem,
    WorkspaceContextOmission,
    WorkspaceContextPack,
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecallPlan,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetentionAnchor,
    WorkspaceRuntimeEventRecord,
    WorkspaceSearchResult,
)

from .agent_execution import (
    AgentExecutionDiagnostics,
    AgentExecutionLineage,
    AgentExecutionLimits,
    AgentExecutionMeta,
    AgentExecutionStatus,
    AgentExecutionStreamMeta,
    AgentExecutionWorkspaceRecord,
    AgentExecutionWorkspaceRefs,
)

from .attempt import (
    AttemptDecision,
    AttemptDecisionAction,
    AttemptHandlers,
    AttemptObservation,
    AttemptState,
    AttemptStreamGenerator,
    AttemptStreamMessage,
)

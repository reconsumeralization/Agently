# Copyright 2023-2025 AgentEra(Agently.Tech)
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

from typing import Any, Literal, Awaitable, Callable, TypeAlias, TYPE_CHECKING, Protocol
from typing_extensions import TypedDict, NotRequired, Self

if TYPE_CHECKING:
    from agently.utils import Settings
    from agently.types.data import SerializableData, SerializableValue, ChatMessage, ChatMessageDict

MemoResizeType: TypeAlias = Literal["lite", "deep"] | str
SessionMode: TypeAlias = Literal["lite", "memo"] | str
ResizeForce: TypeAlias = Literal["lite", "deep", False, None] | str


class SessionLimit(TypedDict, total=False):
    chars: int
    messages: int


class SessionConfig(TypedDict, total=False):
    mode: SessionMode
    limit: SessionLimit
    every_n_turns: int


class MemoResizeDecision(TypedDict):
    type: MemoResizeType
    reason: NotRequired[str]
    severity: NotRequired[int]
    meta: NotRequired[dict[str, Any]]


MemoResizePolicyResult: TypeAlias = "MemoResizeType | MemoResizeDecision | None"

MemoResizePolicyHandler: TypeAlias = (
    "Callable[[list[ChatMessage], list[ChatMessage], Settings], MemoResizePolicyResult | Awaitable[MemoResizePolicyResult]]"
)

MemoResizePolicyAsyncHandler: TypeAlias = (
    "Callable[[list[ChatMessage], list[ChatMessage], Settings], Awaitable[MemoResizePolicyResult]]"
)

MemoResizeHandlerResult: TypeAlias = "tuple[list[ChatMessage], list[ChatMessage], SerializableData]"

MemoResizeHandler: TypeAlias = (
    "Callable[[list[ChatMessage], list[ChatMessage], SerializableData, Settings], MemoResizeHandlerResult | Awaitable[MemoResizeHandlerResult]]"
)

MemoResizeAsyncHandler: TypeAlias = (
    "Callable[[list[ChatMessage], list[ChatMessage], SerializableData, Settings], Awaitable[MemoResizeHandlerResult]]"
)

MemoUpdateResult: TypeAlias = "dict[str, Any]"
MemoUpdateHandler: TypeAlias = (
    "Callable[[dict[str, Any], list[ChatMessage], list[AttachmentSummary], Settings], MemoUpdateResult | Awaitable[MemoUpdateResult]]"
)
MemoUpdateAsyncHandler: TypeAlias = (
    "Callable[[dict[str, Any], list[ChatMessage], list[AttachmentSummary], Settings], Awaitable[MemoUpdateResult]]"
)

AttachmentSummary: TypeAlias = "dict[str, Any]"
AttachmentSummaryHandler: TypeAlias = (
    "Callable[[ChatMessage], list[AttachmentSummary] | Awaitable[list[AttachmentSummary]]]"
)

AttachmentSummaryAsyncHandler: TypeAlias = "Callable[[ChatMessage], Awaitable[list[AttachmentSummary]]]"


class SessionProtocol(Protocol):
    id: str
    settings: "Settings"
    memo: "SerializableData"
    full_chat_history: "list[ChatMessage]"
    current_chat_history: "list[ChatMessage]"

    def __init__(
        self,
        *,
        policy_handler: MemoResizePolicyHandler | None = None,
        resize_handlers: dict[MemoResizeType, MemoResizeHandler] | None = None,
        attachment_summary_handler: AttachmentSummaryHandler | None = None,
        memo_update_handler: MemoUpdateHandler | None = None,
        parent_settings: "Settings | None" = None,
        agent: Any | None = None,
    ): ...

    def configure(
        self,
        *,
        mode: SessionMode | None = None,
        limit: SessionLimit | None = None,
        every_n_turns: int | None = None,
    ) -> Self: ...

    def set_limit(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
    ) -> Self: ...

    def use_lite(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
        every_n_turns: int | None = None,
    ) -> Self: ...

    def use_memo(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
        every_n_turns: int | None = None,
    ) -> Self: ...

    def append_message(self, message: "ChatMessage | ChatMessageDict") -> Self: ...

    def set_settings(
        self,
        key: str,
        value: "SerializableValue",
        *,
        auto_load_env: bool = False,
    ) -> "Settings": ...

    def judge_resize(self, force: ResizeForce = False) -> "MemoResizeDecision | None": ...

    def resize(self, force: ResizeForce = False) -> "list[ChatMessage]": ...

    def set_policy_handler(self, policy_handler: MemoResizePolicyHandler) -> Self: ...

    def set_resize_handlers(
        self,
        resize_type: MemoResizeType,
        resize_handler: MemoResizeHandler,
    ) -> Self: ...

    def set_attachment_summary_handler(self, attachment_summary_handler: AttachmentSummaryHandler) -> Self: ...

    def set_memo_update_handler(self, memo_update_handler: MemoUpdateHandler) -> Self: ...

    async def async_judge_resize(self, force: ResizeForce = False) -> "MemoResizeDecision | None": ...

    async def async_resize(self, force: ResizeForce = False) -> "list[ChatMessage]": ...

    def to_json(self) -> str: ...

    def to_yaml(self) -> str: ...

    def load_json(self, value: str) -> Self: ...

    def load_yaml(self, value: str) -> Self: ...

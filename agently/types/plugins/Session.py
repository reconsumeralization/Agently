from __future__ import annotations

from typing import Any, Literal, Awaitable, Callable, TypeAlias, TYPE_CHECKING, Protocol
from typing_extensions import TypedDict, NotRequired, Self

if TYPE_CHECKING:
    from agently.utils import Settings
    from agently.types.data import SerializableData, ChatMessage

MemoResizeType: TypeAlias = Literal["lite", "deep"] | str
SessionMode: TypeAlias = Literal["lite", "memo"] | str


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
    set_settings: Callable[..., Any]
    judge_resize: Callable[..., Any]
    resize: Callable[..., Any]

    def __init__(
        self,
        *,
        policy_handler: MemoResizePolicyHandler | None = None,
        resize_handlers: dict[Literal["lite", "deep"] | str, MemoResizeHandler] | None = None,
        attachment_summary_handler: AttachmentSummaryHandler | None = None,
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

    def append_message(self, message: "ChatMessage | dict[str, Any]") -> Self: ...

    def set_policy_handler(self, policy_handler: MemoResizePolicyHandler) -> Self: ...

    def set_resize_handlers(
        self,
        resize_type: Literal["lite", "deep"] | str,
        resize_handler: MemoResizeHandler,
    ) -> Self: ...

    def set_attachment_summary_handler(self, attachment_summary_handler: AttachmentSummaryHandler) -> Self: ...

    async def async_judge_resize(self, force: Literal["lite", "deep", False, None] | str = False): ...

    async def async_resize(self, force: Literal["lite", "deep", False, None] | str = False): ...

    def to_json(self) -> str: ...

    def to_yaml(self) -> str: ...

    def load_json(self, value: str) -> Self: ...

    def load_yaml(self, value: str) -> Self: ...

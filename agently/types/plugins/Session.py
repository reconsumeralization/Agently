from typing import Tuple, Callable, Sequence, Any, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from agently.types.data import (
        ChatMessage,
        ChatMessageDict,
        SerializableValue,
    )
    from agently.utils import SettingsNamespace

AnalysisHandler = Callable[
    [
        "Sequence[ChatMessage]",
        "Sequence[ChatMessage]",
        "SettingsNamespace",
    ],
    str | None | Awaitable[str | None],
]

StandardAnalysisHandler = Callable[
    [
        "Sequence[ChatMessage]",
        "Sequence[ChatMessage]",
        "SettingsNamespace",
    ],
    Awaitable[str | None],
]

ExecutionHandler = Callable[
    [
        "Sequence[ChatMessage]",
        "Sequence[ChatMessage]",
        "SettingsNamespace",
    ],
    Tuple[
        "Sequence[ChatMessage | ChatMessageDict] | None",
        "Sequence[ChatMessage | ChatMessageDict] | None",
        "SerializableValue",
    ]
    | Awaitable[
        Tuple[
            "Sequence[ChatMessage | ChatMessageDict] | None",
            "Sequence[ChatMessage | ChatMessageDict] | None",
            "SerializableValue",
        ]
    ],
]

StandardExecutionHandler = Callable[
    [
        "Sequence[ChatMessage]",
        "Sequence[ChatMessage]",
        "SettingsNamespace",
    ],
    Awaitable[
        Tuple[
            "Sequence[ChatMessage | ChatMessageDict] | None",
            "Sequence[ChatMessage | ChatMessageDict] | None",
            "SerializableValue",
        ]
    ],
]

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

from typing import (
    Any,
    Literal,
    Mapping,
    Sequence,
    Annotated,
    TypeAlias,
)
from typing_extensions import TypedDict, NotRequired
from pydantic import (
    BaseModel,
    ConfigDict,
    model_validator,
    PlainValidator,
    Field,
    TypeAdapter,
)


class AttachmentMessageContent(BaseModel):
    type: str

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def suffix(self) -> "AttachmentMessageContent":
        if not hasattr(self, self.type):
            setattr(self, self.type, None)
        return self


class TextMessageContent(BaseModel):
    type: Literal["text"]
    text: str


ChatMessageContent = TextMessageContent | AttachmentMessageContent

ChatMessageContentAdapter = TypeAdapter(Annotated[ChatMessageContent, Field(union_mode="left_to_right")])


class ChatMessageDict(TypedDict):
    role: Literal["system", "developer", "tool", "user", "assistant"] | str
    content: str | list[dict[str, Any] | ChatMessageContent]


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "tool", "user", "assistant"] | str = "user"
    content: str | list[dict[str, Any] | ChatMessageContent]

    model_config = ConfigDict(extra="allow")


def validate_chat_history(chat_history) -> list[ChatMessage]:
    if chat_history is None:
        return []
    if isinstance(chat_history, dict):
        return [ChatMessage(**chat_history)]
    if isinstance(chat_history, list):
        new_chat_history = []
        for message in chat_history:
            if isinstance(message, ChatMessage):
                new_chat_history.append(message)
            elif isinstance(message, dict):
                new_chat_history.append(ChatMessage(**message))
            else:
                new_chat_history.append(ChatMessage(content=str(message)))
        return new_chat_history
    return [ChatMessage(content=str(chat_history))]


def validate_attachment(attachment) -> list[ChatMessageContent]:
    if attachment is None:
        return []
    if isinstance(attachment, dict):
        return [ChatMessageContentAdapter.validate_python(attachment)]
    if isinstance(attachment, list):
        attachment_contents = []
        for content in attachment:
            if isinstance(content, ChatMessageContent):
                attachment_contents.append(content)
            elif isinstance(content, dict):
                attachment_contents.append(ChatMessageContentAdapter.validate_python(content))
            else:
                attachment_contents.append(
                    ChatMessageContentAdapter.validate_python(
                        {
                            "type": "text",
                            "text": str(content),
                        }
                    )
                )
        return attachment_contents
    return [
        ChatMessageContentAdapter.validate_python(
            {
                "type": "text",
                "text": str(attachment),
            }
        )
    ]


OutputFormat = Literal["markdown", "text", "json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"]

_SCALAR_TYPES = (str, int, float, bool)


def _is_scalar_field_spec(field_spec: Any) -> bool:
    """Return ``True`` if *field_spec* represents a scalar value.

    Recognises ``(str, ...)``, ``(int, ...)``, ``(bool, ...)``, and
    ``(float, ...)`` tuples.
    """
    if isinstance(field_spec, tuple) and len(field_spec) >= 1:
        first = field_spec[0]
        return isinstance(first, type) and first in _SCALAR_TYPES
    if isinstance(field_spec, type) and field_spec in _SCALAR_TYPES:
        return True
    return False


def _classify_field_spec(field_spec: Any) -> Literal["scalar", "complex"]:
    """Classify a field spec for format selection.

    Returns ``"scalar"`` for str/int/bool/float tuples, ``"complex"`` for
    lists, nested dicts, and everything else.
    """
    return "scalar" if _is_scalar_field_spec(field_spec) else "complex"


def _is_string_field_spec(field_spec: Any) -> bool:
    if isinstance(field_spec, tuple) and field_spec:
        return field_spec[0] is str
    return field_spec is str


def _should_auto_use_hybrid(output: Mapping[str, Any]) -> bool:
    has_non_string_field = False
    has_string_field = False
    for field_spec in output.values():
        if _is_string_field_spec(field_spec):
            has_string_field = True
            continue
        has_non_string_field = True
    return has_non_string_field and has_string_field


def _resolve_auto_format(output: Any) -> Literal["json", "hybrid", "xml_field"]:
    """Determine the best output format from schema shape.

    ===================== =============================================
    Schema shape           Format chosen
    ===================== =============================================
    Flat dict, all strings       ``"xml_field"``
    String + typed data          ``"hybrid"``
    All complex / all controls   ``"json"``
    Non-dict                     ``"json"``
    ===================== =============================================
    """
    if not isinstance(output, Mapping) or not output:
        return "json"

    if all(_is_string_field_spec(value) for value in output.values()):
        return "xml_field"
    if _should_auto_use_hybrid(output):
        return "hybrid"
    return "json"
PromptOutputStructure: TypeAlias = Mapping[str, Any] | list[Any]
PromptStandardSlot = Literal[
    "system",
    "developer",
    "chat_history",
    "info",
    "tools",
    "action_results",
    "instruct",
    "examples",
    "input",
    "attachment",
    "output",
    "ensure_all_keys",
    "output_format",
    "options",
]


class ToolMeta(TypedDict):
    name: str
    desc: str
    kwargs: dict[str, Any]
    returns: NotRequired[Any]


class PromptModel(BaseModel):
    system: Any = None
    developer: Any = None
    chat_history: Annotated[list[ChatMessage], PlainValidator(validate_chat_history)] = []
    info: Any = None
    tools: list[ToolMeta] | None = None
    action_results: Any = None
    instruct: Any = None
    examples: Any = None
    input: Any = None
    attachment: Annotated[list[ChatMessageContent], PlainValidator(validate_attachment)] = []
    output: Any = None
    ensure_all_keys: bool = False
    output_format: OutputFormat | Any = None
    output_format_resolved_from_auto: bool = False
    options: dict[str, Any] = {}

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def set_output_format(self) -> "PromptModel":
        if self.output_format is None:
            if isinstance(self.output, Mapping):
                self.output_format_resolved_from_auto = True
                self.output_format = _resolve_auto_format(self.output)
            elif not isinstance(self.output, str) and isinstance(self.output, Sequence):
                self.output_format = "json"
            elif isinstance(self.output, type):
                if self.output == str:
                    self.output = None
                    self.output_format = "markdown"
                else:
                    self.output = {"value": (self.output,), "reply": (str, "Reply according the result value")}
                    self.output_format = "json"
            else:
                self.output_format = "markdown"
        if self.output_format == "auto":
            self.output_format_resolved_from_auto = True
            self.output_format = _resolve_auto_format(self.output)
        if self.output_format == "flat_markdown":
            if not isinstance(self.output, Mapping):
                import warnings
                warnings.warn(
                    f"output_format='flat_markdown' requires a dict output schema, "
                    f"got {type(self.output).__name__}. Falling back to json.",
                    stacklevel=2,
                )
                self.output_format = "json"
        if self.output_format in ("hybrid", "xml_field", "yaml_literal"):
            if not isinstance(self.output, Mapping):
                import warnings
                warnings.warn(
                    f"output_format='{self.output_format}' requires a dict output schema, "
                    f"got {type(self.output).__name__}. Falling back to json.",
                    stacklevel=2,
                )
                self.output_format = "json"
        if not isinstance(self.output_format, str):
            self.output_format = str(self.output_format)
        return self

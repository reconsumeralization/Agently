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


from pathlib import Path
from uuid import uuid4
from warnings import warn

from json import JSONDecodeError
from typing import Sequence, Any, TYPE_CHECKING

import json
import json5
import yaml

from agently.types.data import ChatMessage, ChatMessageDict
from agently.utils import FunctionShifter, Settings, SettingsNamespace, DataLocator

if TYPE_CHECKING:
    from agently.types.data import SerializableValue
    from agently.types.plugins import (
        AnalysisHandler,
        ExecutionHandler,
        StandardAnalysisHandler,
        StandardExecutionHandler,
    )


class Session:
    def __init__(
        self,
        id: str | None = None,
        *,
        auto_resize: bool = True,
        settings: dict[str, Any] | Settings = {},
    ):
        self.id = id if id is not None else uuid4().hex
        self._auto_resize = auto_resize
        if isinstance(settings, dict):
            from agently.base import settings as global_settings

            self.settings = Settings(settings, parent=global_settings)
        else:
            self.settings = Settings(parent=settings)
        self.session_settings = SettingsNamespace(self.settings, "session")
        self.session_settings.setdefault("max_length", None)
        self._analysis_handler: "StandardAnalysisHandler" = self._default_analysis_handler
        self._execution_handlers: "dict[str, StandardExecutionHandler]" = {
            "simple_cut": self._simple_cut_execution_handler,
        }
        self._full_context: list[ChatMessage] = []
        self._context_window: list[ChatMessage] = []
        self._memo = None

        self.reset_chat_history = FunctionShifter.syncify(self.async_reset_chat_history)
        self.set_chat_history = FunctionShifter.syncify(self.async_set_chat_history)
        self.clean_context_window = FunctionShifter.syncify(self.async_clean_context_window)
        self.clean_window_context = self.clean_context_window
        self.add_chat_history = FunctionShifter.syncify(self.async_add_chat_history)
        self.analyze_context = FunctionShifter.syncify(self.async_analyze_context)
        self.execute_strategy = FunctionShifter.syncify(self.async_execute_strategy)
        self.resize = FunctionShifter.syncify(self.async_resize)
        self.to_json = self.get_json_session
        self.to_yaml = self.get_yaml_session
        self.load_json = self.load_json_session
        self.load_yaml = self.load_yaml_session

    async def _default_analysis_handler(
        self,
        full_context: Sequence[ChatMessage],
        context_window: Sequence[ChatMessage],
        memo: "SerializableValue",
        session_settings: SettingsNamespace,
    ):
        max_length = session_settings.get("max_length", None)
        context_window_length = self._calculate_context_length(context_window)
        if isinstance(max_length, int) and context_window_length > max_length:
            return "simple_cut"
        return None

    async def _simple_cut_execution_handler(
        self,
        full_context: Sequence[ChatMessage],
        context_window: Sequence[ChatMessage],
        memo: "SerializableValue",
        session_settings: SettingsNamespace,
    ):
        max_length = session_settings.get("max_length", None)
        if isinstance(max_length, int):
            new_context_window: list[ChatMessage] = []
            total_length = 0
            for message in reversed(context_window):
                message_length = len(str(message.model_dump()))
                if total_length + message_length > max_length:
                    break
                new_context_window.append(message)
                total_length += message_length
            new_context_window.reverse()

            if len(new_context_window) == 0 and context_window:
                new_content = str(context_window[-1].content)
                new_content = new_content[len(new_content) - max_length :]
                return (
                    None,
                    [
                        ChatMessage(
                            role=context_window[-1].role,
                            content=new_content,
                        )
                    ],
                    None,
                )

            if self._calculate_context_length(new_context_window) <= max_length:
                return (
                    None,
                    self._to_standard_chat_messages(new_context_window),
                    None,
                )
        return None, None, None

    def _calculate_context_length(self, context_window: Sequence[ChatMessage]):
        length = 0
        for message in context_window:
            length += len(str(message.model_dump()))
        return length

    def register_analysis_handler(self, analysis_handler: "AnalysisHandler"):
        """
        Register analysis handler to Session

        :param analysis_handler:

            - input params:

                - full_context <list[ChatMessage]>: Messages contains full context since this session created.
                - context_window <list[ChatMessage]>: Messages of current context window.
                - memo <SerializableValue>: Memo content of this session.
                - session_settings <SettingsNamespace>: Namespace "session" of settings that inherit from global settings or the agent's settings which this session is attached to.

            - output:

                - str | None: message controlling execution strategy name or None(do nothing)
        """
        self._analysis_handler = FunctionShifter.asyncify(analysis_handler)
        return self

    def register_execution_handlers(self, strategy_name: str, execution_handler: "ExecutionHandler"):
        """
        Register analysis handler to Session

        :param strategy_name: message controlling execution strategy name

        :param execution_handler:

            - input params:

                - full_context <list[ChatMessage]>: Messages contains full context since this session created.
                - context_window <list[ChatMessage]>: Messages of current context window.
                - memo <SerializableValue>: Memo content of this session.
                - session_settings <SettingsNamespace>: Namespace "session" of settings that inherit from global settings or the agent's settings which this session is attached to.

            - output:

                - Tuple[list[ChatMessage | ChatMessageDict], list[ChatMessage | ChatMessageDict], SerializableValue]
                - Tuple items in orders:
                    - New full context messages or None(no update)
                    - New context window messages or None(no update)
                    - New memo data or None(no update)
        """
        self._execution_handlers[strategy_name] = FunctionShifter.asyncify(execution_handler)
        return self

    def _to_standard_chat_messages(self, chat_messages: Sequence[ChatMessage | ChatMessageDict]):
        return [
            (
                ChatMessage(
                    role=message["role"],
                    content=message["content"],
                )
                if isinstance(message, dict)
                else message
            )
            for message in chat_messages
        ]

    async def async_reset_chat_history(
        self,
    ):
        self._full_context = []
        self._context_window = []
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_clean_context_window(self):
        self._context_window = []
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_clean_window_context(self):
        return await self.async_clean_context_window()

    async def async_set_chat_history(
        self,
        chat_history: Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict,
    ):
        if isinstance(chat_history, Sequence):
            messages = self._to_standard_chat_messages(chat_history)
            self._full_context = messages
            self._context_window = messages
        else:
            if isinstance(chat_history, dict):
                chat_history = ChatMessage(
                    role=chat_history["role"],
                    content=chat_history["content"],
                )
            self._full_context = [chat_history]
            self._context_window = [chat_history]
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_add_chat_history(
        self,
        chat_history: Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict,
    ):
        if isinstance(chat_history, Sequence):
            messages = self._to_standard_chat_messages(chat_history)
            self._full_context.extend(messages)
            self._context_window.extend(messages)
        else:
            if isinstance(chat_history, dict):
                chat_history = ChatMessage(
                    role=chat_history["role"],
                    content=chat_history["content"],
                )
            self._full_context.append(chat_history)
            self._context_window.append(chat_history)
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_analyze_context(self):
        return await self._analysis_handler(
            self._full_context,
            self._context_window,
            self._memo,
            self.session_settings,
        )

    async def async_execute_strategy(self, strategy_name: str):
        if strategy_name in self._execution_handlers:
            new_full_context, new_context_window, new_memo = await self._execution_handlers[strategy_name](
                self._full_context,
                self._context_window,
                self._memo,
                self.session_settings,
            )
            if new_full_context is not None:
                self._full_context = self._to_standard_chat_messages(new_full_context)
            if new_context_window is not None:
                self._context_window = self._to_standard_chat_messages(new_context_window)
            if new_memo is not None:
                self._memo = new_memo
        else:
            warn(
                f"Can not find strategy '{ strategy_name }' in execution handlers dictionary in Session <{ self.id }>."
            )

    async def async_resize(self):
        strategy_name = await self.async_analyze_context()
        if strategy_name is not None:
            await self.async_execute_strategy(strategy_name)

    @property
    def full_context(self):
        return self._full_context.copy()

    @property
    def context_window(self):
        return self._context_window.copy()

    @property
    def memo(self):
        return self._memo

    def _to_serializable_chat_messages(
        self,
        chat_messages: Sequence[ChatMessage | ChatMessageDict],
    ):
        return [
            message.model_dump() if isinstance(message, ChatMessage) else dict(message) for message in chat_messages
        ]

    def to_serializable_session_data(self):
        return {
            "id": self.id,
            "auto_resize": self._auto_resize,
            "full_context": self._to_serializable_chat_messages(self._full_context),
            "context_window": self._to_serializable_chat_messages(self._context_window),
            "memo": self._memo,
            "session_settings": self.session_settings.data,
        }

    def get_json_session(self):
        return json.dumps(
            self.to_serializable_session_data(),
            indent=2,
            ensure_ascii=False,
        )

    def get_yaml_session(self):
        return yaml.safe_dump(
            self.to_serializable_session_data(),
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )

    def _load_session_data(
        self,
        session_data: dict[str, Any],
    ):
        if "id" in session_data:
            self.id = str(session_data["id"])

        if "auto_resize" in session_data:
            self._auto_resize = bool(session_data["auto_resize"])

        full_context_data = session_data.get("full_context", [])
        if isinstance(full_context_data, (str, bytes)) or not isinstance(full_context_data, Sequence):
            raise TypeError("Cannot load Session data, expect key 'full_context' as a sequence of chat messages.")

        context_window_data = session_data.get("context_window", session_data.get("window_context", full_context_data))
        if isinstance(context_window_data, (str, bytes)) or not isinstance(context_window_data, Sequence):
            raise TypeError("Cannot load Session data, expect key 'context_window' as a sequence of chat messages.")

        self._full_context = self._to_standard_chat_messages(full_context_data)
        self._context_window = self._to_standard_chat_messages(context_window_data)

        if "memo" in session_data:
            self._memo = session_data["memo"]

        session_settings_data = session_data.get("session_settings", None)
        if session_settings_data is not None:
            if not isinstance(session_settings_data, dict):
                raise TypeError("Cannot load Session data, expect key 'session_settings' as a dictionary.")
            self.session_settings.update(session_settings_data)

        return self

    def load_yaml_session(
        self,
        path_or_content: str | Path,
        *,
        session_key_path: str | None = None,
        encoding: str | None = "utf-8",
    ):
        path = Path(path_or_content)
        is_yaml_file = False
        try:
            is_yaml_file = path.exists() and path.is_file()
        except (OSError, ValueError):
            is_yaml_file = False

        if is_yaml_file:
            try:
                with path.open("r", encoding=encoding) as file:
                    session_data = yaml.safe_load(file)
            except yaml.YAMLError as e:
                raise ValueError(f"Cannot load YAML file '{ path_or_content }'.\nError: { e }")
        else:
            try:
                session_data = yaml.safe_load(str(path_or_content))
            except yaml.YAMLError as e:
                raise ValueError(f"Cannot load YAML content or file path not existed.\nError: { e }")

        if not isinstance(session_data, dict):
            raise TypeError(f"Cannot load YAML Session data, expect dictionary data but got: { session_data }")

        if session_key_path is not None:
            session_data = DataLocator.locate_path_in_dict(session_data, session_key_path)

        if not isinstance(session_data, dict):
            raise TypeError(
                f"Cannot load YAML Session data, expect Session data{ ' from [' + session_key_path + '] ' if session_key_path is not None else ' ' }as dictionary but got: { session_data }"
            )

        return self._load_session_data(session_data)

    def load_json_session(
        self,
        path_or_content: str | Path,
        *,
        session_key_path: str | None = None,
        encoding: str | None = "utf-8",
    ):
        path = Path(path_or_content)
        is_json_file = False
        try:
            is_json_file = path.exists() and path.is_file()
        except (OSError, ValueError):
            is_json_file = False

        if is_json_file:
            try:
                with path.open("r", encoding=encoding) as file:
                    session_data = json5.load(file)
            except (JSONDecodeError, ValueError) as e:
                raise ValueError(f"Cannot load JSON file '{ path_or_content }'.\nError: { e }")
        else:
            try:
                session_data = json5.loads(str(path_or_content))
            except (JSONDecodeError, ValueError) as e:
                raise ValueError(f"Cannot load JSON content or file path not existed.\nError: { e }")

        if not isinstance(session_data, dict):
            raise TypeError(f"Cannot load JSON Session data, expect dictionary data but got: { session_data }")

        if session_key_path is not None:
            session_data = DataLocator.locate_path_in_dict(session_data, session_key_path)

        if not isinstance(session_data, dict):
            raise TypeError(
                f"Cannot load JSON Session data, expect Session data{ ' from [' + session_key_path + '] ' if session_key_path is not None else ' ' }as dictionary but got: { session_data }"
            )

        return self._load_session_data(session_data)

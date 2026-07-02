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

from typing import Any, Callable

from agently.types.data import ActionSpec
from agently.types.plugins import ActionExecutor


class ActionRegistry:
    """
    Action is the single first-class executable abstraction in this runtime.
    Avoid parallel nouns unless the lifecycle is materially different.
    """

    def __init__(self, *, name: str | None = None):
        self.name = name
        self._specs: dict[str, ActionSpec] = {}
        self._executors: dict[str, ActionExecutor] = {}
        self._funcs: dict[str, Callable[..., Any]] = {}
        self._tag_mappings: dict[str, set[str]] = {}
        self._action_tags: dict[str, set[str]] = {}

    def register(
        self,
        spec: ActionSpec,
        executor: ActionExecutor,
        *,
        func: Callable[..., Any] | None = None,
    ):
        action_id = str(spec.get("action_id", ""))
        self._specs[action_id] = spec
        self._executors[action_id] = executor
        if func is not None:
            self._funcs[action_id] = func
        tags = spec.get("tags", [])
        if not isinstance(tags, list):
            tags = list(tags) if isinstance(tags, (tuple, set)) else []
        self._action_tags[action_id] = set([str(tag) for tag in tags])
        for tag in self._action_tags[action_id]:
            self._tag_mappings.setdefault(tag, set()).add(action_id)
        return self

    def tag(self, action_ids: str | list[str], tags: str | list[str]):
        if isinstance(action_ids, str):
            action_ids = [action_ids]
        if isinstance(tags, str):
            tags = [tags]
        for action_id in action_ids:
            if action_id not in self._specs:
                raise ValueError(f"Cannot find action named '{ action_id }'")
            self._action_tags.setdefault(action_id, set())
            for tag in tags:
                tag_text = str(tag)
                self._action_tags[action_id].add(tag_text)
                self._tag_mappings.setdefault(tag_text, set()).add(action_id)
            self._specs[action_id]["tags"] = sorted(self._action_tags[action_id])
        return self

    def unregister(self, action_id: str) -> bool:
        """Remove an action and all of its registry bookkeeping.

        Returns True when an action was removed. Used to reverse scoped
        capability mounts so a one-time mount does not persist on the host.
        """
        if action_id not in self._specs:
            return False
        self._specs.pop(action_id, None)
        self._executors.pop(action_id, None)
        self._funcs.pop(action_id, None)
        tags = self._action_tags.pop(action_id, set())
        for tag in tags:
            members = self._tag_mappings.get(tag)
            if members is not None:
                members.discard(action_id)
                if not members:
                    self._tag_mappings.pop(tag, None)
        return True

    def has(self, action_id: str):
        return action_id in self._specs

    def get_spec(self, action_id: str):
        return self._specs.get(action_id)

    def get_executor(self, action_id: str):
        return self._executors.get(action_id)

    def get_func(self, action_id: str):
        return self._funcs.get(action_id)

    def get_tags(self, action_id: str):
        return self._action_tags.get(action_id, set())

    def list_action_ids(self, tags: str | list[str] | None = None):
        if tags is None:
            return list(self._specs.keys())
        if isinstance(tags, str):
            tags = [tags]
        collected: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            for action_id in self._tag_mappings.get(tag, set()):
                if action_id not in seen:
                    seen.add(action_id)
                    collected.append(action_id)
        return collected

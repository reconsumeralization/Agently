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

import copy
import re

from dataclasses import dataclass
from typing import Any, Literal

from agently.utils import StateData

_SUB_FLOW_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAPTURE_TARGET_SCOPES = frozenset({"input", "runtime_data", "flow_data", "resources"})
_CAPTURE_SOURCE_SCOPES = frozenset({"value", "runtime_data", "flow_data", "resources"})
_WRITE_BACK_TARGET_SCOPES = frozenset({"value", "runtime_data", "flow_data"})
_WRITE_BACK_SOURCE_SCOPES = frozenset({"result"})


@dataclass(frozen=True)
class _CompiledSubFlowBinding:
    target_scope: Literal["input", "runtime_data", "flow_data", "resources", "value"]
    target_path: tuple[str, ...]
    source_scope: Literal["value", "runtime_data", "flow_data", "resources", "result"]
    source_path: tuple[str, ...]


def _clone_sub_flow_value(value: Any):
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _read_sub_flow_value_by_path(root_value: Any, path: tuple[str, ...], *, scope: str):
    current = root_value
    for segment in path:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
            continue
        raise KeyError(f"TriggerFlow sub flow path '{ scope }.{ '.'.join(path) }' not found.")
    return _clone_sub_flow_value(current)


class _ParentSubFlowCaptureSource:
    def __init__(self, data):
        flow_data = data.get_flow_data(None, {}, no_warning=True)
        self._scopes = {
            "value": data.value,
            "runtime_data": data.state.to_dict(),
            "flow_data": flow_data if isinstance(flow_data, dict) else {},
            "resources": data.resources.to_dict(),
        }

    def read_path(self, scope: str, path: tuple[str, ...]):
        return _read_sub_flow_value_by_path(self._scopes[scope], path, scope=scope)


class _SubFlowWriteBackSource:
    def __init__(self, result: Any):
        self._result = result

    def read_path(self, scope: str, path: tuple[str, ...]):
        if scope != "result":
            raise KeyError(f"Unsupported TriggerFlow sub flow write back source scope '{ scope }'.")
        if isinstance(self._result, dict) and "$final_result" in self._result:
            try:
                return _read_sub_flow_value_by_path(self._result["$final_result"], path, scope=scope)
            except KeyError:
                return _read_sub_flow_value_by_path(self._result, path, scope=scope)
        return _read_sub_flow_value_by_path(self._result, path, scope=scope)


class _SubFlowCaptureTarget:
    def __init__(self):
        self._has_input = False
        self._input_value = None
        self._runtime_data = StateData()
        self._flow_data = StateData()
        self._resources = StateData()

    def write_path(self, scope: str, path: tuple[str, ...], value: Any):
        copied_value = _clone_sub_flow_value(value)
        if scope == "input":
            if len(path) == 0:
                self._input_value = copied_value
                self._has_input = True
                return
            current_input = self._input_value if isinstance(self._input_value, dict) else {}
            input_data = StateData(_clone_sub_flow_value(current_input))
            input_data.set(".".join(path), copied_value)
            self._input_value = input_data.get(None, {}, inherit=False)
            self._has_input = True
            return

        target_scope = {
            "runtime_data": self._runtime_data,
            "flow_data": self._flow_data,
            "resources": self._resources,
        }[scope]
        target_scope.set(".".join(path), copied_value)

    def build_input(self):
        return _clone_sub_flow_value(self._input_value) if self._has_input else None

    def build_runtime_data(self):
        return self._runtime_data.get(None, {}, inherit=False)

    def build_flow_data(self):
        return self._flow_data.get(None, {}, inherit=False)

    def build_resources(self):
        return self._resources.get(None, {}, inherit=False)


class _SubFlowWriteBackTarget:
    def __init__(self, initial_value: Any):
        self._has_value_binding = False
        self._current_value = _clone_sub_flow_value(initial_value)
        initial_mapping = self._current_value if isinstance(self._current_value, dict) else {}
        self._value_data = StateData(_clone_sub_flow_value(initial_mapping))
        self._runtime_data = StateData()
        self._flow_data = StateData()

    def write_path(self, scope: str, path: tuple[str, ...], value: Any):
        copied_value = _clone_sub_flow_value(value)
        if scope == "value":
            self._has_value_binding = True
            if len(path) == 0:
                self._current_value = copied_value
                return
            if not isinstance(self._current_value, dict):
                self._current_value = {}
                self._value_data = StateData({})
            self._value_data.set(".".join(path), copied_value)
            self._current_value = self._value_data.get(None, {}, inherit=False)
            return

        target_scope = {
            "runtime_data": self._runtime_data,
            "flow_data": self._flow_data,
        }[scope]
        target_scope.set(".".join(path), copied_value)

    def apply(self, data):
        if self._has_value_binding:
            data.value = _clone_sub_flow_value(self._current_value)

        runtime_data_patch = self._runtime_data.get(None, {}, inherit=False)
        for key, value in runtime_data_patch.items():
            data.execution._runtime_data.set(key, _clone_sub_flow_value(value))

        flow_data_patch = self._flow_data.get(None, {}, inherit=False)
        for key, value in flow_data_patch.items():
            data.execution._trigger_flow._flow_data.set(key, _clone_sub_flow_value(value))

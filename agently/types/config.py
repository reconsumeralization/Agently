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

from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict


class AgentlyConfigModel(BaseModel):
    """Typed helper over Agently's durable dict configuration contract."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, populate_by_name=True)

    __settings_namespace__: ClassVar[str | None] = None
    __options_namespace__: ClassVar[str | None] = None
    __secret_fields__: ClassVar[set[str]] = set()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude_unset=True, by_alias=True)

    def to_settings_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def to_options_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def items(self):
        return self.to_dict().items()

    def keys(self):
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


ConfigModelType = type[AgentlyConfigModel]


class ConfigSchemaRegistry:
    def __init__(self, *, name: str):
        self.name = name
        self._schemas: dict[str, tuple[ConfigModelType, str | None]] = {}

    def register(
        self,
        namespace: str,
        schema: ConfigModelType,
        *,
        owner: str | None = None,
    ) -> None:
        if not namespace:
            raise ValueError(f"{ self.name } schema namespace must not be empty.")
        if not isinstance(schema, type) or not issubclass(schema, AgentlyConfigModel):
            raise TypeError(
                f"{ self.name } schema for '{ namespace }' must inherit from AgentlyConfigModel."
            )
        self._schemas[namespace] = (schema, owner)

    def unregister(self, namespace: str) -> None:
        self._schemas.pop(namespace, None)

    def get(self, namespace: str) -> ConfigModelType | None:
        entry = self._schemas.get(namespace)
        return entry[0] if entry is not None else None

    def owner(self, namespace: str) -> str | None:
        entry = self._schemas.get(namespace)
        return entry[1] if entry is not None else None

    def validate(self, namespace: str, value: Any) -> dict[str, Any]:
        schema = self.get(namespace)
        if schema is None:
            if isinstance(value, AgentlyConfigModel):
                return value.to_dict()
            if isinstance(value, Mapping):
                return dict(value)
            raise KeyError(f"{ self.name } schema '{ namespace }' is not registered.")
        if isinstance(value, schema):
            return value.to_dict()
        return schema.model_validate(value).to_dict()

    def items(self) -> dict[str, ConfigModelType]:
        return {namespace: schema for namespace, (schema, _) in self._schemas.items()}

    def metadata(self) -> dict[str, dict[str, str | None]]:
        return {
            namespace: {
                "schema": schema.__name__,
                "owner": owner,
            }
            for namespace, (schema, owner) in self._schemas.items()
        }


settings_schema_registry = ConfigSchemaRegistry(name="settings")
options_schema_registry = ConfigSchemaRegistry(name="options")


def is_settings_model(value: Any) -> bool:
    return isinstance(value, AgentlyConfigModel) and value.__settings_namespace__ is not None


def is_options_model(value: Any) -> bool:
    return isinstance(value, AgentlyConfigModel) and value.__options_namespace__ is not None


def settings_model_to_pair(value: Any) -> tuple[str, dict[str, Any]]:
    if not is_settings_model(value):
        raise TypeError("Expected an Agently settings config model with __settings_namespace__.")
    return str(value.__settings_namespace__), value.to_settings_dict()


def normalize_options(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, AgentlyConfigModel):
        return value.to_options_dict()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Agently options must be a dict or AgentlyConfigModel, got { type(value).__name__ }.")

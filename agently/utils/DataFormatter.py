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

import datetime
import warnings
from typing import (
    Any,
    Literal,
    Mapping,
    Union,
    get_origin,
    get_args,
    overload,
    TYPE_CHECKING,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from agently.types.data import SerializableValue


class DataFormatter:
    @staticmethod
    def sanitize(value: Any, *, remain_type: bool = False) -> Any:
        from .RuntimeData import RuntimeData, RuntimeDataNamespace

        if isinstance(value, (str, int, float, bool)) or value is None:
            return value

        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()

        if issubclass(type(value), RuntimeData) or issubclass(type(value), RuntimeDataNamespace):
            return DataFormatter.sanitize(value.data, remain_type=remain_type)

        if isinstance(value, type) or get_origin(value) is not None:
            if issubclass(value, BaseModel):
                extracted_value = {}
                for name, field in value.model_fields.items():
                    extracted_value.update(
                        {
                            name: (
                                (
                                    DataFormatter.sanitize(field.annotation, remain_type=remain_type),
                                    field.description,
                                )
                                if field.description
                                else (DataFormatter.sanitize(field.annotation, remain_type=remain_type),)
                            )
                        }
                    )
                return extracted_value
            else:
                if remain_type:
                    return value
                else:
                    original_text = get_origin(value)
                    args = get_args(value)
                    if original_text is list:
                        return f"list[{DataFormatter.sanitize(args[0], remain_type=remain_type)}]"
                    if original_text is dict:
                        return f"dict[{DataFormatter.sanitize(args[0], remain_type=remain_type)}, {DataFormatter.sanitize(args[1], remain_type=remain_type)}]"
                    if original_text is tuple:
                        return f"tuple[{', '.join(DataFormatter.sanitize(a, remain_type=remain_type) for a in args)}]"
                    if original_text is Union:
                        return " | ".join(str(DataFormatter.sanitize(a, remain_type=remain_type)) for a in args)
                    if original_text is Literal:
                        return f"Literal[{ ', '.join(str(DataFormatter.sanitize(a, remain_type=remain_type)) for a in args) }]"
                    if isinstance(value, type) and hasattr(value, "__name__"):
                        return value.__name__
                    return str(value)

        if isinstance(value, dict):
            return {str(k): DataFormatter.sanitize(v, remain_type=remain_type) for k, v in value.items()}
        if isinstance(value, list):
            return [DataFormatter.sanitize(v, remain_type=remain_type) for v in value]
        if isinstance(value, set):
            return {DataFormatter.sanitize(v, remain_type=remain_type) for v in value}
        if isinstance(value, tuple):
            return tuple(DataFormatter.sanitize(v, remain_type=remain_type) for v in value)

        return str(value)

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: None = None,
        default: dict[str, Any] | None = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, Any]: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["serializable"],
        default: dict[str, "SerializableValue"] | None = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, "SerializableValue"]: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["str"],
        default: dict[str, str] | None = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, str]: ...

    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["serializable", "str"] | None = None,
        default: dict[str, Any] | None = None,
        inconvertible_warning: bool = False,
    ):
        if isinstance(value, Mapping):
            if value_format is None:
                return {str(DataFormatter.sanitize(k)): v for k, v in value.items()}
            elif value_format == "serializable":
                return {str(DataFormatter.sanitize(k)): DataFormatter.sanitize(v) for k, v in value.items()}
            elif value_format == "str":
                return {str(DataFormatter.sanitize(k)): str(DataFormatter.sanitize(v)) for k, v in value.items()}
        else:
            if inconvertible_warning:
                warnings.warn(
                    f"Error: Non-dictionary value cannot be convert to a string key dictionary.\n"
                    f"Value: { value }\n"
                    "Tips: You can provide parameter 'default_key' to allow DataFormatter.to_str_key_dict() convert non-dictionary value to a dictionary { default_key: value } automatically."
                )
            return default if default is not None else {}

    @staticmethod
    def to_str(value: Any) -> str:
        return str(DataFormatter.sanitize(value))

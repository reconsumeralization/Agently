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

import threading
import warnings
from logging import Logger
from sys import modules
from typing import Type

_warned_once_keys: set[str] = set()
_warning_once_lock = threading.Lock()
_DEPRECATION_WARNING_SETTING_KEY = "runtime.show_deprecation_warnings"


def _mark_warning_key(key: str) -> bool:
    normalized_key = str(key)
    with _warning_once_lock:
        if normalized_key in _warned_once_keys:
            return False
        _warned_once_keys.add(normalized_key)
        return True


def warn_once(
    key: str,
    message: str,
    category: Type[Warning] = UserWarning,
    *,
    stacklevel: int = 2,
):
    if not _mark_warning_key(key):
        return
    warnings.warn(message, category, stacklevel=stacklevel + 1)


def _coerce_warning_setting_enabled(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "disable",
            "disabled",
        }
    return bool(value)


def _deprecation_warnings_enabled() -> bool:
    base_module = modules.get("agently.base")
    settings = getattr(base_module, "settings", None)
    if settings is None:
        return True
    try:
        value = settings.get(_DEPRECATION_WARNING_SETTING_KEY, True)
    except Exception:
        return True
    return _coerce_warning_setting_enabled(value)


def warn_deprecated_once(
    key: str,
    message: str,
    *,
    stacklevel: int = 2,
):
    if not _deprecation_warnings_enabled():
        return
    warn_once(key, message, DeprecationWarning, stacklevel=stacklevel + 1)


def log_warning_once(key: str, logger: Logger, message: str):
    if _mark_warning_key(key):
        logger.warning(message)


def log_deprecated_once(key: str, logger: Logger, message: str):
    if _deprecation_warnings_enabled():
        log_warning_once(key, logger, message)


def _reset_warning_once_registry_for_tests():
    with _warning_once_lock:
        _warned_once_keys.clear()

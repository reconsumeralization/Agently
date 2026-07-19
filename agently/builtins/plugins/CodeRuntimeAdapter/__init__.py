from __future__ import annotations

from .CppCodeRuntimeAdapter import CppCodeRuntimeAdapter
from .GoCodeRuntimeAdapter import GoCodeRuntimeAdapter
from .NodeCodeRuntimeAdapter import NodeCodeRuntimeAdapter
from .PythonCodeRuntimeAdapter import PythonCodeRuntimeAdapter


_ADAPTER_TYPES = (
    PythonCodeRuntimeAdapter,
    NodeCodeRuntimeAdapter,
    GoCodeRuntimeAdapter,
    CppCodeRuntimeAdapter,
)


def get_code_runtime_adapter(language: str):
    canonical = str(language).strip().casefold()
    for adapter_type in _ADAPTER_TYPES:
        adapter = adapter_type()
        if canonical in {adapter.language_id, *adapter.aliases}:
            return adapter
    raise ValueError(f"unsupported code runtime language: {language!r}")


__all__ = [
    "CppCodeRuntimeAdapter",
    "GoCodeRuntimeAdapter",
    "NodeCodeRuntimeAdapter",
    "PythonCodeRuntimeAdapter",
    "get_code_runtime_adapter",
]

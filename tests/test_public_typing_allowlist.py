from __future__ import annotations

import inspect
import importlib
import json
import re
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints


REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = REPO_ROOT / "compatibility" / "public-typing-allowlist.json"
ANY_PATTERN = re.compile(r"(^|[^A-Za-z0-9_])Any([^A-Za-z0-9_]|$)")


def _resolve_symbol(symbol: str) -> Any:
    parts = symbol.split(".")
    for split_index in range(len(parts), 0, -1):
        module_name = ".".join(parts[:split_index])
        try:
            resolved = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for attr in parts[split_index:]:
            resolved = getattr(resolved, attr)
        return resolved
    raise ModuleNotFoundError(symbol)


def _declared_public_callables(surface: type) -> dict[str, Any]:
    callables: dict[str, Any] = {}
    for name, value in surface.__dict__.items():
        if name.startswith("_"):
            continue
        if isinstance(value, (classmethod, staticmethod)):
            value = value.__func__
        if inspect.isfunction(value):
            callables[name] = value
    return callables


def _raw_annotation_contains_any(annotation: Any) -> bool:
    if annotation is inspect.Signature.empty:
        return False
    if annotation is Any:
        return True
    if isinstance(annotation, str):
        return bool(ANY_PATTERN.search(annotation))
    return any(_raw_annotation_contains_any(arg) for arg in get_args(annotation))


def _resolved_annotations(target: Any, owner: type) -> dict[str, Any]:
    try:
        return get_type_hints(
            target,
            globalns=getattr(target, "__globals__", None),
            localns={owner.__name__: owner},
            include_extras=True,
        )
    except Exception:
        return {}


def _callable_violations(surface_symbol: str, owner: type, name: str, target: Any) -> list[tuple[str, str]]:
    symbol = f"{surface_symbol}.{name}"
    signature = inspect.signature(target)
    hints = _resolved_annotations(target, owner)
    violations: list[tuple[str, str]] = []

    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        position = f"param:{parameter.name}"
        annotation = parameter.annotation
        if annotation is inspect.Signature.empty:
            violations.append((symbol, position))
            continue
        resolved_annotation = hints.get(parameter.name, annotation)
        if _raw_annotation_contains_any(resolved_annotation) or _raw_annotation_contains_any(annotation):
            violations.append((symbol, position))

    return_annotation = signature.return_annotation
    if return_annotation is inspect.Signature.empty:
        violations.append((symbol, "return"))
    else:
        resolved_return = hints.get("return", return_annotation)
        if _raw_annotation_contains_any(resolved_return) or _raw_annotation_contains_any(return_annotation):
            violations.append((symbol, "return"))
    return violations


def _is_allowed(symbol: str, position: str, entries: dict[str, set[str]]) -> bool:
    positions = entries.get(symbol, set())
    return position in positions or (position.startswith("param:") and "param:*" in positions)


def test_public_typing_allowlist_is_enforced_for_declared_public_surfaces() -> None:
    allowlist = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    any_entries = allowlist.get("any_allowlist", [])
    allowed_positions: dict[str, set[str]] = {}

    for entry in any_entries:
        symbol = entry.get("symbol")
        positions = entry.get("positions")
        assert isinstance(symbol, str) and symbol
        assert isinstance(positions, list) and positions
        assert all(isinstance(position, str) and position for position in positions)
        assert isinstance(entry.get("owner"), str) and entry["owner"]
        assert isinstance(entry.get("reason"), str) and entry["reason"]
        assert isinstance(entry.get("narrowing_plan"), str) and entry["narrowing_plan"]
        assert isinstance(entry.get("expires"), str) and entry["expires"]
        assert callable(_resolve_symbol(symbol))
        allowed_positions.setdefault(symbol, set()).update(positions)

    observed_violations: list[tuple[str, str]] = []
    for surface in allowlist.get("public_surfaces", []):
        assert surface.get("scan") == "declared_public_callables"
        surface_symbol = surface["symbol"]
        resolved_surface = _resolve_symbol(surface_symbol)
        assert inspect.isclass(resolved_surface)
        for name, target in _declared_public_callables(resolved_surface).items():
            observed_violations.extend(_callable_violations(surface_symbol, resolved_surface, name, target))

    unexpected = [
        f"{symbol}:{position}"
        for symbol, position in observed_violations
        if not _is_allowed(symbol, position, allowed_positions)
    ]
    assert unexpected == []

    observed_by_symbol: dict[str, set[str]] = {}
    for symbol, position in observed_violations:
        observed_by_symbol.setdefault(symbol, set()).add(position)

    stale_entries: list[str] = []
    for symbol, positions in allowed_positions.items():
        observed = observed_by_symbol.get(symbol, set())
        for position in positions:
            if position == "param:*":
                if not any(item.startswith("param:") for item in observed):
                    stale_entries.append(f"{symbol}:{position}")
            elif position not in observed:
                stale_entries.append(f"{symbol}:{position}")
    assert stale_entries == []

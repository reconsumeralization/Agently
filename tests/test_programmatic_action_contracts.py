from __future__ import annotations

import json
import math
import importlib
from typing import Annotated, get_args

import pytest
from pydantic import Field

from agently.core.operation.Action.ActionProgram import (
    build_programmatic_action_catalog,
    build_programmatic_python_source,
    canonical_lossless_json_bytes,
    normalize_programmatic_action_decision,
    validate_lossless_json_value,
)
from agently.types.data import (
    PROGRAMMATIC_ACTION_ARTIFACT_READ_ID,
    PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION,
    PROGRAMMATIC_ACTION_TRANSPORT_ID,
    ActionPlanningProtocol,
    CodeExecutionSchemaValidationError,
    validate_code_execution_json_schema_definition,
)


def _read_spec(**overrides):
    spec = {
        "action_id": "search_docs",
        "desc": "Search documentation.",
        "kwargs": {
            "query": (str, "Search query."),
            "limit": (int, "Maximum results."),
        },
        "required_input_keys": ["query"],
        "returns": list[dict[str, str]],
        "side_effect_level": "read",
        "replay_safe": True,
        "approval_required": False,
        "expose_to_model": True,
    }
    spec.update(overrides)
    return spec


def test_action_planning_protocol_and_reserved_constants_are_typed():
    assert set(get_args(ActionPlanningProtocol)) == {
        "structured_plan",
        "native_tool_calls",
        "programmatic",
    }
    assert PROGRAMMATIC_ACTION_TRANSPORT_ID == "run_action_program"
    assert PROGRAMMATIC_ACTION_ARTIFACT_READ_ID == "read_action_artifact"
    assert PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION.endswith(".v2")


def test_action_package_does_not_export_programmatic_mechanism_helpers():
    action_package = importlib.import_module("agently.core.operation.Action")
    assert not hasattr(action_package, "build_programmatic_action_catalog")
    assert not hasattr(action_package, "build_programmatic_python_source")
    assert not hasattr(action_package, "render_programmatic_action_sdk")


def test_programmatic_catalog_is_lexical_and_byte_deterministic():
    alpha = _read_spec(
        action_id="alpha",
        kwargs={
            "query": (str, "Search query."),
            "limit": (int, "Maximum results."),
        },
    )
    zeta = _read_spec(action_id="zeta")

    first = build_programmatic_action_catalog([zeta, alpha])
    second = build_programmatic_action_catalog(
        [
            {
                **alpha,
                "kwargs": {
                    "limit": (int, "Maximum results."),
                    "query": (str, "Search query."),
                },
            },
            zeta,
        ]
    )

    assert [entry["action_id"] for entry in first["entries"]] == ["alpha", "zeta"]
    assert first["sdk"].encode("utf-8") == second["sdk"].encode("utf-8")
    assert first["catalog_revision"] == second["catalog_revision"]
    assert first["catalog_revision"].startswith("sha256:")
    assert len(first["catalog_revision"]) == len("sha256:") + 64


def test_programmatic_catalog_binds_explicit_action_concurrency_mode():
    exclusive = build_programmatic_action_catalog([_read_spec()])
    parallel = build_programmatic_action_catalog(
        [_read_spec(concurrency_mode="parallel")]
    )

    assert exclusive["entries"][0]["concurrency_mode"] == "exclusive"
    assert parallel["entries"][0]["concurrency_mode"] == "parallel"
    assert exclusive["catalog_revision"] != parallel["catalog_revision"]
    assert '\\"concurrency_mode\\":\\"parallel\\"' in parallel["sdk"]


def test_catalog_mapping_can_use_schema_like_action_ids_without_becoming_one_spec():
    catalog = build_programmatic_action_catalog(
        {
            "name": _read_spec(action_id="name"),
            "kwargs": _read_spec(action_id="kwargs"),
        }
    )
    assert [entry["action_id"] for entry in catalog["entries"]] == ["kwargs", "name"]


def test_programmatic_catalog_revision_binds_contract_renderer_and_host_seed():
    baseline = build_programmatic_action_catalog([_read_spec()], revision_seed="request:v1")
    changed_return = build_programmatic_action_catalog(
        [_read_spec(returns=dict[str, int])],
        revision_seed="request:v1",
    )
    changed_renderer = build_programmatic_action_catalog(
        [_read_spec()],
        renderer_version="agently.programmatic_action.python.v3",
        revision_seed="request:v1",
    )
    changed_seed = build_programmatic_action_catalog([_read_spec()], revision_seed="request:v2")

    revisions = {
        baseline["catalog_revision"],
        changed_return["catalog_revision"],
        changed_renderer["catalog_revision"],
        changed_seed["catalog_revision"],
    }
    assert len(revisions) == 4
    assert "request:v1" not in baseline["sdk"]
    assert baseline["catalog_revision"] not in baseline["sdk"]


def test_python_sdk_uses_attributes_only_for_safe_identifiers_and_compiles():
    catalog = build_programmatic_action_catalog(
        [
            _read_spec(action_id="z-tool"),
            _read_spec(action_id="class"),
            _read_spec(action_id="alpha"),
            _read_spec(action_id="_private"),
        ]
    )

    entries = {entry["action_id"]: entry for entry in catalog["entries"]}
    assert entries["alpha"]["access_expression"] == "actions.alpha"
    assert entries["z-tool"]["access_expression"] == 'actions["z-tool"]'
    assert entries["class"]["access_expression"] == 'actions["class"]'
    assert entries["_private"]["access_expression"] == 'actions["_private"]'
    assert catalog["sdk"].index("# await actions.alpha") < catalog["sdk"].index('# await actions["z-tool"]')
    assert "async def alpha(" in catalog["sdk"]
    assert 'Literal["z-tool"]' in catalog["sdk"]

    namespace: dict[str, object] = {}
    exec(compile(catalog["sdk"], "<programmatic-action-sdk>", "exec"), namespace)
    exact_contracts = json.loads(str(namespace["ACTION_CONTRACTS_JSON"]))
    assert list(exact_contracts) == ["_private", "alpha", "class", "z-tool"]


def test_exotic_action_names_are_quoted_but_invalid_binding_keys_fail_closed():
    catalog = build_programmatic_action_catalog(
        [
            _read_spec(action_id="search docs"),
            _read_spec(action_id=" trailing "),
            _read_spec(action_id="line\nbreak"),
        ]
    )

    assert [entry["action_id"] for entry in catalog["entries"]] == ["search docs"]
    assert catalog["entries"][0]["access_expression"] == 'actions["search docs"]'
    assert [item.get("code") for item in catalog["diagnostics"]] == [
        "action.programmatic.ineligible.invalid_action_id",
        "action.programmatic.ineligible.invalid_action_id",
    ]


def test_sdk_preserves_required_optional_inputs_and_explicit_returns():
    catalog = build_programmatic_action_catalog([_read_spec(returns=list[str])])
    entry = catalog["entries"][0]

    assert entry["required_input_keys"] == ["query"]
    assert entry["input_schema"] == {
        "additionalProperties": False,
        "properties": {
            "limit": {"description": "Maximum results.", "type": "integer"},
            "query": {"description": "Search query.", "type": "string"},
        },
        "required": ["query"],
        "type": "object",
    }
    assert entry["output_schema"] == {"items": {"type": "string"}, "type": "array"}
    assert '"limit": NotRequired[int]' in catalog["sdk"]
    assert '"query": str' in catalog["sdk"]
    assert "Output = list[str]" in catalog["sdk"]


@pytest.mark.parametrize(
    ("returns", "rendered"),
    [
        (dict, "Output = dict[str, JSONValue]"),
        (dict[str, int], "Output = dict[str, int]"),
    ],
)
def test_sdk_renders_open_object_return_contracts_as_dictionaries(returns, rendered):
    catalog = build_programmatic_action_catalog([_read_spec(returns=returns)])
    assert rendered in catalog["sdk"]


def test_explicit_json_schema_constraints_are_retained_exactly():
    returns = {
        "type": "object",
        "properties": {
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "label": {"type": "string", "enum": ["good", "bad"]},
        },
        "required": ["score", "label"],
        "additionalProperties": False,
    }
    catalog = build_programmatic_action_catalog([_read_spec(returns=returns)])

    assert catalog["entries"][0]["output_schema"] == {
        "additionalProperties": False,
        "properties": {
            "label": {"enum": ["good", "bad"], "type": "string"},
            "score": {"maximum": 1, "minimum": 0, "type": "number"},
        },
        "required": ["label", "score"],
        "type": "object",
    }


def test_annotated_action_constraints_are_not_erased():
    catalog = build_programmatic_action_catalog(
        [
            _read_spec(
                kwargs={"limit": (Annotated[int, Field(ge=1, le=50)], "Result limit.")},
                required_input_keys=["limit"],
                returns=Annotated[str, Field(min_length=1, max_length=20)],
            )
        ]
    )

    entry = catalog["entries"][0]
    assert entry["input_schema"]["properties"]["limit"] == {
        "description": "Result limit.",
        "maximum": 50,
        "minimum": 1,
        "type": "integer",
    }
    assert entry["output_schema"] == {
        "maxLength": 20,
        "minLength": 1,
        "type": "string",
    }


def test_unsupported_json_schema_assertions_fail_before_planning():
    catalog = build_programmatic_action_catalog(
        [
            _read_spec(
                returns={
                    "type": "string",
                    "contentEncoding": "base64",
                }
            )
        ]
    )

    assert catalog["entries"] == []
    assert catalog["diagnostics"][0].get("code") == ("action.programmatic.ineligible.contract_not_lossless_json")
    assert "unsupported JSON Schema keyword" in str(catalog["diagnostics"][0].get("message"))


def test_schema_validation_error_is_exported_for_public_validators():
    with pytest.raises(CodeExecutionSchemaValidationError):
        validate_code_execution_json_schema_definition(
            {"type": "string", "contentEncoding": "base64"},
            field_name="test schema",
        )


def test_ordinary_missing_returns_is_ineligible_but_artifact_reader_has_fixed_contract():
    ordinary = _read_spec(action_id="ordinary")
    ordinary.pop("returns")
    artifact_reader = {
        "action_id": PROGRAMMATIC_ACTION_ARTIFACT_READ_ID,
        "desc": "Read a retained Action artifact page.",
        "kwargs": {
            "selection_key": (str, "Run-scoped selection key."),
            "offset": (int, "Optional offset."),
            "max_bytes": (int, "Optional byte limit."),
        },
        "required_input_keys": ["selection_key"],
        "side_effect_level": "read",
        "replay_safe": True,
        "expose_to_model": False,
    }

    catalog = build_programmatic_action_catalog([ordinary, artifact_reader])

    assert [entry["action_id"] for entry in catalog["entries"]] == [PROGRAMMATIC_ACTION_ARTIFACT_READ_ID]
    entry = catalog["entries"][0]
    assert entry["artifact_read_exception"] is True
    assert entry["output_schema"]["required"] == ["ok", "status"]
    assert any(
        diagnostic.get("code") == "action.programmatic.ineligible.returns_missing"
        and diagnostic.get("meta", {}).get("action_id") == "ordinary"
        for diagnostic in catalog["diagnostics"]
    )


def test_none_returns_is_not_an_explicit_null_contract():
    catalog = build_programmatic_action_catalog([_read_spec(returns=None)])
    assert catalog["entries"] == []
    assert catalog["diagnostics"][0].get("code") == ("action.programmatic.ineligible.returns_missing")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expose_to_model": False}, "not_model_visible"),
        ({"side_effect_level": "write"}, "side_effect_not_read"),
        ({"replay_safe": False}, "not_replay_safe"),
        ({"approval_required": True}, "approval_required"),
        ({"default_policy": {"approval_mode": "always"}}, "approval_required"),
    ],
)
def test_programmatic_v1_eligibility_fails_closed(overrides, reason):
    catalog = build_programmatic_action_catalog([_read_spec(**overrides)])
    assert catalog["entries"] == []
    assert catalog["diagnostics"][0].get("code") == f"action.programmatic.ineligible.{reason}"


def test_transport_and_host_only_inputs_are_never_projected():
    transport = _read_spec(action_id=PROGRAMMATIC_ACTION_TRANSPORT_ID)
    eligible = _read_spec(
        kwargs={
            "query": (str, "Search query."),
            "credential": (str, "Injected by host."),
        },
        required_input_keys=["query"],
        meta={
            "host_only_input_keys": ["credential"],
            "env": {"API_KEY": "must-not-appear"},
        },
    )
    catalog = build_programmatic_action_catalog([transport, eligible])

    assert [entry["action_id"] for entry in catalog["entries"]] == ["search_docs"]
    assert "credential" not in catalog["entries"][0]["input_schema"]["properties"]
    assert "must-not-appear" not in catalog["sdk"]
    assert any(
        item.get("code") == "action.programmatic.ineligible.reserved_transport" for item in catalog["diagnostics"]
    )


def test_required_host_only_input_makes_action_ineligible():
    catalog = build_programmatic_action_catalog(
        [
            _read_spec(
                kwargs={"credential": (str, "Injected by host.")},
                required_input_keys=["credential"],
                meta={"host_only_input_keys": ["credential"]},
            )
        ]
    )

    assert catalog["entries"] == []
    assert catalog["diagnostics"][0].get("code") == ("action.programmatic.ineligible.contract_not_lossless_json")


def test_lossless_json_validation_is_canonical_and_size_bounded():
    left = canonical_lossless_json_bytes({"z": [1, True], "a": "文"})
    right = canonical_lossless_json_bytes({"a": "文", "z": [1, True]})

    assert left == right == '{"a":"文","z":[1,true]}'.encode()
    assert validate_lossless_json_value({"ok": True}) == len(b'{"ok":true}')
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        validate_lossless_json_value({"ok": True}, max_bytes=4)


@pytest.mark.parametrize(
    "value",
    [
        {1: "non-string key"},
        ("tuple",),
        b"bytes",
        math.nan,
        math.inf,
    ],
)
def test_lossless_json_validation_rejects_coercion_and_non_finite_numbers(value):
    with pytest.raises(ValueError):
        canonical_lossless_json_bytes(value)


def test_lossless_json_validation_rejects_cycles():
    value: list[object] = []
    value.append(value)
    with pytest.raises(ValueError, match="cyclic"):
        canonical_lossless_json_bytes(value)


def test_programmatic_decision_normalization_preserves_exact_program():
    program = "rows = await actions.search_docs({'query': 'Agently'})\nreturn rows"
    normalized = normalize_programmatic_action_decision(
        {
            "next_action": "execute",
            "description": "Search the docs",
            "program": program,
        },
        max_program_bytes=len(program.encode()),
    )
    response = normalize_programmatic_action_decision(
        {
            "next_action": "response",
            "description": "No Action is needed",
            "program": None,
        },
        max_program_bytes=1,
    )

    assert normalized["program"] == program
    assert response["program"] is None


@pytest.mark.parametrize(
    "decision",
    [
        {"next_action": "execute", "description": "run", "program": None},
        {"next_action": "response", "description": "done", "program": "return 1"},
        {"next_action": "other", "description": "bad", "program": None},
        {"next_action": "response", "description": "", "program": None},
        {
            "next_action": "response",
            "description": "done",
            "program": None,
            "catalog_revision": "model-must-not-copy-this",
        },
    ],
)
def test_programmatic_decision_normalization_rejects_invalid_cross_fields(decision):
    with pytest.raises(ValueError):
        normalize_programmatic_action_decision(decision, max_program_bytes=100)


def test_programmatic_decision_normalization_enforces_utf8_byte_limits():
    with pytest.raises(ValueError, match="max_program_bytes"):
        normalize_programmatic_action_decision(
            {
                "next_action": "execute",
                "description": "run",
                "program": "文",
            },
            max_program_bytes=2,
        )
    with pytest.raises(ValueError, match="max_description_bytes"):
        normalize_programmatic_action_decision(
            {
                "next_action": "response",
                "description": "文",
                "program": None,
            },
            max_program_bytes=1,
            max_description_bytes=2,
        )


def test_programmatic_python_source_wraps_top_level_await_and_return():
    source = build_programmatic_python_source('rows = await actions.search_docs({"query": "Agently"})\nreturn rows')

    assert "from agently_code_bindings import bindings as actions, execute_program" in source
    assert "async def _agently_program():" in source
    assert '    rows = await actions.search_docs({"query": "Agently"})' in source
    assert "    return rows" in source
    assert "asyncio.run(execute_program(_agently_program))" in source
    compile(source, "<programmatic-action-source-test>", "exec")


@pytest.mark.parametrize(
    "program",
    [
        "",
        "   \n",
        "# a comment is not an executable function body",
        "if True:\nreturn 1",
        "return (",
        "value = lambda: 1",
    ],
)
def test_programmatic_python_source_rejects_empty_or_invalid_bodies(program):
    with pytest.raises(ValueError):
        build_programmatic_python_source(program)


def test_r4_nested_async_wrapper_return_does_not_satisfy_program_return_contract():
    with pytest.raises(ValueError, match="explicit return"):
        build_programmatic_python_source(
            "async def run():\n" "    return await actions.search_docs({'query': 'Agently'})"
        )


def test_programmatic_python_source_allows_nested_async_handler_with_outer_return():
    source = build_programmatic_python_source(
        "async def run():\n"
        "    return await actions.search_docs({'query': 'Agently'})\n"
        "return await run()"
    )
    compile(source, "<programmatic-action-handler-test>", "exec")


def test_programmatic_python_source_allows_lambda_with_direct_scope_return():
    source = build_programmatic_python_source("select = lambda item: item['value']\n" "return select({'value': 3})")
    compile(source, "<programmatic-action-lambda-test>", "exec")


def test_description_is_bounded_without_splitting_utf8():
    catalog = build_programmatic_action_catalog(
        [_read_spec(desc="文" * 10)],
        max_description_bytes=8,
    )

    description = catalog["entries"][0]["description"]
    assert description.endswith("…")
    assert len(description.encode("utf-8")) <= 8
    assert catalog["diagnostics"][0].get("code") == "action.programmatic.description_truncated"

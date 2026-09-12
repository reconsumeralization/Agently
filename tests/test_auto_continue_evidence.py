"""Public-path synthetic transport tests; not natural model truncation evidence."""

import json
from typing import Any, ClassVar

import pytest
from test_agent_execution_compatibility import (
    MockAgentExecutionLongOutputRequester,
    _create_long_output_test_agent,
    _get_long_output_meta,
)


class EvidenceRequester(MockAgentExecutionLongOutputRequester):
    name = "ContinuationEvidenceRequester"
    responses: ClassVar[list[Any]] = []
    terminals: ClassVar[list[dict[str, Any]]] = []
    completion: ClassVar[str] = "complete"

    def generate_request_data(self):
        result = super().generate_request_data()
        result.data["prompt_input"] = self.prompt.get("input")
        return result

    async def request_model(self, request_data):
        index = type(self).attempts - 1
        assert index < len(self.responses), "Unexpected redundant model request"
        response = self.responses[index]
        if response is None:
            continuation = request_data.data["continuation"]
            response = {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [],
                "state_summary": "",
                "completion": type(self).completion,
            }
        yield "message", response if isinstance(response, str) else json.dumps(response)

    async def broadcast_response(self, response_generator):
        raw = "".join(
            [
                str(data)
                async for event, data in response_generator
                if event == "message"
            ]
        )
        yield "delta", raw
        yield "done", raw
        yield "meta", self.terminals[type(self).attempts - 1]


def run(tmp_path, schema, responses, terminals):
    EvidenceRequester.reset()
    EvidenceRequester.completion = "complete"
    EvidenceRequester.responses = responses
    EvidenceRequester.terminals = terminals
    execution = (
        _create_long_output_test_agent(EvidenceRequester, "continuation-evidence")
        .use_task_workspace(tmp_path)
        .input("Complete the requested output.")
        .auto_continue()
    )
    if schema is not None:
        execution.output(schema, format="json")
    return execution


def packet(updates, completion="complete"):
    return {
        "updates": {
            key: {"value": value, "is_complete": complete}
            for key, (value, complete) in updates.items()
        },
        "completion": completion,
    }


@pytest.mark.parametrize(
    "terminal", [{}, {"status": "incomplete"}, {"finish_reason": "stop"}]
)
def test_complete_json_needs_no_extra_request(tmp_path, terminal):
    execution = run(tmp_path, {"body": str}, ['{"body":"done"}'], [terminal])
    assert execution.get_data() == {"body": "done"}
    assert EvidenceRequester.attempts == 1


@pytest.mark.parametrize(
    "terminal", [{}, {"status": "incomplete"}, {"finish_reason": "length"}]
)
def test_plain_tail_confirmation_does_not_invent_content(tmp_path, terminal):
    execution = run(tmp_path, None, ["Already finished.", None], [terminal, {}])
    assert execution.get_text() == "Already finished."
    assert EvidenceRequester.attempts == 2


@pytest.mark.parametrize("terminal", [{}, {"finish_reason": "length"}])
@pytest.mark.parametrize(
    "schema,initial,key,expected",
    [
        (
            {"head": str, "body": str},
            '{"head":"fixed","body":"alpha-',
            "p0:body",
            {"head": "fixed", "body": "alpha-omega"},
        ),
        (
            {"items": [str]},
            '{"items":["fixed","alpha-',
            "p0:items[1]",
            {"items": ["fixed", "alpha-omega"]},
        ),
        (
            {"items": [], "body": str},
            '{"items":[],"body":"alpha-',
            "p0:body",
            {"items": [], "body": "alpha-omega"},
        ),
    ],
)
def test_open_string_prefix_is_never_discarded(
    tmp_path, terminal, schema, initial, key, expected
):
    execution = run(
        tmp_path,
        schema,
        [initial, packet({key: ("omega", True)})],
        [terminal, {"finish_reason": "stop"}],
    )
    assert execution.get_data(max_retries=0) == expected
    assert json.loads(execution.get_text()) == expected
    assert _get_long_output_meta(execution)["request_count"] == 2
    assert "completion" not in "".join(execution.get_generator(type="delta"))


@pytest.mark.parametrize(
    "bad", ['{"body":"ok","body":"bad"}', '{"body":"ok"} garbage', '{"body":invalid']
)
def test_unknown_terminal_never_turns_malformed_json_into_truncation(tmp_path, bad):
    execution = run(tmp_path, {"body": str}, [bad], [{}])
    with pytest.raises(ValueError):
        execution.get_data(max_retries=0)
    assert EvidenceRequester.attempts == 1


def test_complete_private_boundary_can_finish_a_length_terminated_response(tmp_path):
    execution = run(tmp_path, None, ["done", None], [{"finish_reason": "length"}] * 2)
    assert execution.get_text() == "done"
    assert EvidenceRequester.attempts == 2


@pytest.mark.parametrize("terminal", [{}, {"finish_reason": "stop"}, {"finish_reason": "length"}])
@pytest.mark.parametrize("schema,initial", [(None, "Accepted text."), ({"body": str}, '{"body":"Accepted"}')])
def test_undetermined_stops_legacy_delivery_without_retry(tmp_path, terminal, schema, initial):
    execution = run(tmp_path, schema, [initial, None], [{"finish_reason": "length"}, terminal])
    EvidenceRequester.completion = "undetermined"
    with pytest.raises(RuntimeError, match="completion is undetermined"):
        execution.get_data(max_retries=3)
    assert EvidenceRequester.attempts == 2
    assert not execution.diagnostics.get("long_output_no_progress")


def test_legacy_packet_has_one_three_state_completion_field():
    from agently.builtins.plugins.AgentExecution.modules.long_output import (
        _ContinuationEnvelope,
    )

    properties = _ContinuationEnvelope.model_json_schema()["properties"]
    assert properties["completion"]["enum"] == ["complete", "incomplete", "undetermined"]
    assert "is_final" not in properties


def test_explicit_filter_is_not_overridden_by_structural_closure(tmp_path):
    execution = run(
        tmp_path,
        {"body": str},
        ['{"body":"done"}'],
        [{"finish_reason": "content_filter"}],
    )
    with pytest.raises(RuntimeError, match="terminal fact"):
        execution.get_data(max_retries=0)
    assert EvidenceRequester.attempts == 1

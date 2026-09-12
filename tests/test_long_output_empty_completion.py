"""Synthetic delivery protocol tests, not evidence of model semantic quality."""
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from agently.types.data import AgentlyRequestData
from agently.types.data import OutputValidateContext
from agently.types.plugins.AgentExecution import AgentExecution
from test_agent_execution_compatibility import (
    MockAgentExecutionLongOutputRequester,
    _create_long_output_test_agent,
    _get_long_output_meta,
)


class EmptyCompletionRequester(MockAgentExecutionLongOutputRequester):
    name = 'EmptyCompletionRequester'
    initial_text = 'The requested text is already complete.'
    malformed: ClassVar[str | None] = None
    final: ClassVar[bool] = True

    async def request_model(self, request_data: AgentlyRequestData) -> AsyncGenerator[tuple[str, Any], None]:
        continuation = request_data.data.get('continuation')
        if not isinstance(continuation, dict):
            yield 'message', self.initial_text
            return
        envelope = {
            'base_revision': continuation['base_revision'],
            'base_digest': continuation['base_digest'],
            'anchor': continuation['anchor'],
            'updates': [], 'state_summary': '', 'completion': 'complete' if self.final else 'incomplete',
        }
        if self.malformed == 'stale':
            envelope['base_digest'] = 'stale'
        raw = json.dumps(envelope)
        if self.malformed == 'tail':
            raw += '\n</think>'
        elif self.malformed == 'duplicate':
            raw = raw[:-1] + ',"completion":"complete"}'
        elif self.malformed == 'open':
            raw = raw[:-1]
        yield 'message', raw


@pytest.fixture(autouse=True)
def reset_requester() -> None:
    EmptyCompletionRequester.reset()
    EmptyCompletionRequester.malformed = None
    EmptyCompletionRequester.final = True
    EmptyCompletionRequester.initial_text = 'The requested text is already complete.'


def execution(tmp_path: Path) -> AgentExecution:
    return (_create_long_output_test_agent(EmptyCompletionRequester, 'empty-completion')
        .use_task_workspace(tmp_path).input('Deliver the requested text.').auto_continue())


def test_complete_empty_acknowledgement_needs_no_filler(tmp_path: Path) -> None:
    run = execution(tmp_path)
    assert run.get_text() == EmptyCompletionRequester.initial_text
    assert EmptyCompletionRequester.attempts == 2
    meta = _get_long_output_meta(run)
    assert meta['accepted_unit_count'] == 1
    assert meta['replayed_unit_count'] == 1
    assert meta['no_progress_event_count'] == 0
    assert not run.diagnostics.get('long_output_no_progress')


def test_empty_acknowledgement_reuses_one_typed_result_across_readers(tmp_path: Path) -> None:
    class Body(BaseModel):
        body: str
    EmptyCompletionRequester.initial_text = '{"body":"already complete"}'
    run = execution(tmp_path).output(Body, format='json')
    expected = {'body':'already complete'}
    assert run.get_data() == expected
    assert json.loads(run.get_text()) == expected
    typed = run.get_data_object()
    assert isinstance(typed, Body)
    assert typed.body == expected['body']
    assert run.get_full_data() == expected
    assert _get_long_output_meta(run)['request_count'] == 2
    text = ''.join(run.get_generator(type='delta'))
    assert 'base_revision' not in text
    assert EmptyCompletionRequester.attempts == 2


@pytest.mark.parametrize('kind', ['tail', 'duplicate', 'open'])
def test_malformed_empty_acknowledgement_cannot_authorize_completion(tmp_path: Path, kind: str) -> None:
    EmptyCompletionRequester.malformed = kind
    run = execution(tmp_path)
    with pytest.raises(RuntimeError, match='no durable progress'):
        run.get_text()
    assert EmptyCompletionRequester.attempts == 4


def test_stale_empty_acknowledgement_fails_without_retry(tmp_path: Path) -> None:
    EmptyCompletionRequester.malformed = 'stale'
    with pytest.raises(RuntimeError, match='manifest revision, digest, and anchor'):
        execution(tmp_path).get_text()
    assert EmptyCompletionRequester.attempts == 2


def test_empty_acknowledgement_does_not_bypass_original_validator(tmp_path: Path) -> None:
    observed: list[dict[str, Any]] = []
    def reject(data: dict[str, Any], context: OutputValidateContext) -> bool:
        observed.append(data)
        return False
    with pytest.raises(RuntimeError, match='Validation failed'):
        execution(tmp_path).get_data(validate_handler=reject, max_retries=0)
    assert len(observed) == 1
    assert EmptyCompletionRequester.attempts == 2


def test_incomplete_empty_packets_still_stop_after_three_no_progress_requests(tmp_path: Path) -> None:
    EmptyCompletionRequester.final = False
    with pytest.raises(RuntimeError, match='no durable progress'):
        execution(tmp_path).get_text()
    assert EmptyCompletionRequester.attempts == 4


def test_empty_acknowledgement_cannot_supply_missing_required_field(tmp_path: Path) -> None:
    EmptyCompletionRequester.initial_text = '{"head":"kept"'
    run = execution(tmp_path).output({'head': (str,'Existing value',True), 'tail':(str,'Required value',True)})
    with pytest.raises(RuntimeError, match='no durable progress'):
        run.get_data(max_retries=0)
    assert EmptyCompletionRequester.attempts == 4

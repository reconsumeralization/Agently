"""Synthetic syntax/state probes, not natural model truncation evidence."""
import json

import pytest

from agently.utils import StreamingJSONParser

inspect_prefix = StreamingJSONParser._inspect_json_prefix


def test_complete_and_partial_framing():
    assert inspect_prefix('```json\n{"body":"done"}\n```').root_complete
    assert not inspect_prefix('```json\n{"body":"done"}').root_complete
    evidence = inspect_prefix('```json\n{"body":"keep trailing space ')
    assert evidence.decoded_prefix == 'keep trailing space '
    with pytest.raises(ValueError):
        inspect_prefix('```json\n{"body":"done"}\n```\nextra')


@pytest.mark.parametrize('value', [
    '长正文\n引号"反斜杠\\😀',
    {'title': '目录', 'section': {'body': '甲\n乙😀', 'done': True}},
    {'chapters': [{'title': '一', 'body': '第一段'}, {'title': '二', 'body': '后续正文😀'}], 'end': None},
    ['首篇', '第二篇😀', ['嵌套字符串']],
    {'a.b[0]': {'': '路径不能按点号拆开'}, 'n': -1.25e-13},
])
@pytest.mark.parametrize('ascii_mode', [True, False])
def test_every_synthetic_cut_preserves_closed_values(value, ascii_mode):
    raw = json.dumps(value, ensure_ascii=ascii_mode)
    whole = inspect_prefix(raw)
    assert whole.root_complete and whole.closed[()] == value
    for cut in range(len(raw) + 1):
        evidence = inspect_prefix(raw[:cut])
        assert evidence.root_complete is (cut == len(raw))
        for path, closed in evidence.closed.items():
            assert closed == whole.closed[path]
        if evidence.open_string_path is not None:
            assert whole.closed[evidence.open_string_path].startswith(evidence.decoded_prefix)


def test_partial_array_element_is_not_a_closed_item():
    state = inspect_prefix('{"chapters":[{"body":"first"},{"title":"second","body":"long prefix')
    assert state.closed[('chapters', 0)] == {'body': 'first'}
    assert state.closed[('chapters', 1, 'title')] == 'second'
    assert ('chapters', 1) not in state.closed
    assert ('chapters',) not in state.closed
    assert state.open_string_path == ('chapters', 1, 'body')
    assert state.decoded_prefix == 'long prefix'


def test_closed_string_does_not_close_parent_or_reopen_field():
    state = inspect_prefix('{"body":"done","next":')
    assert state.closed == {('body',): 'done'}
    assert state.open_string_path is None
    assert not state.root_complete


def test_surrogate_pending_is_not_silently_dropped():
    state = inspect_prefix('["ok\\uD83D\\uDE')
    assert state.open_string_path == (0,)
    assert state.decoded_prefix == 'ok'
    assert state.pending_escape == '\\uD83D\\uDE'
    assert state.closed == {}


@pytest.mark.parametrize('raw', [
    '{"a":"x","a":"y', '{"a":"x"} garbage', '{"a":"\\q',
    '{"a":"bad\n', '[1,]', '{"a":1,}', '{"a" 1}',
    '{"a":NaN}', '{"a":Infinity}', '{"a":01}', '{"a":truex',
    '{"a":"\\uDE00', '{"a":"\\uD83Dx', '{"a":"\\uZZ',
    '{"a":[}', '{"a":false false}', 'prose {"a":1}',
    '{"a":-e', '{"a":-.', '{"a":.', '{"a":e',
    '{"a":1.e', '{"a":-.e',
])
def test_invalid_is_not_mislabeled_as_incomplete(raw):
    with pytest.raises(ValueError):
        inspect_prefix(raw)

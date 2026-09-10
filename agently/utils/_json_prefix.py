"""Strict lexical evidence for StreamingJSONParser; never repairs provider text."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

Path = tuple[str | int, ...]
_NUMBER_PREFIX = r'-?(?:0|[1-9][0-9]*)(?:(?:\.[0-9]*)|(?:\.[0-9]+)?[eE][+-]?[0-9]*)?'


@dataclass
class PrefixEvidence:
    closed: dict[Path, Any] = field(default_factory=dict)
    open_string_path: Path | None = None
    decoded_prefix: str = ''
    pending_escape: str = ''
    root_complete: bool = False


class _End(Exception):
    pass


def inspect_json_prefix(text: str, *, terminal_complete: bool = False) -> PrefixEvidence:
    """All returned closure facts come from delimiters in the supplied raw text.

    Numbers at EOF remain provisional since another digit may follow. Unclosed keys are
    not offered as value strings. No synthetic close enters `closed`.
    """
    result = PrefixEvidence()
    n = len(text)
    decoder = json.JSONDecoder()
    escapes = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}

    def space(i: int) -> int:
        while i < n and text[i] in ' \t\r\n':
            i += 1
        return i

    def unfinished(path: Path | None, decoded: list[str], pending: str = '') -> None:
        if path is not None:
            result.open_string_path = path
            result.decoded_prefix = ''.join(decoded)
            result.pending_escape = pending
        raise _End

    def string(i: int, path: Path | None) -> tuple[str, int]:
        i += 1
        decoded: list[str] = []
        while i < n:
            char = text[i]
            if char == '"':
                return ''.join(decoded), i + 1
            if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF:
                raise ValueError('Invalid unescaped character')
            if char != '\\':
                decoded.append(char)
                i += 1
                continue
            start = i
            if i + 1 == n:
                unfinished(path, decoded, text[start:])
            char = text[i + 1]
            if char in escapes:
                decoded.append(escapes[char])
                i += 2
                continue
            if char != 'u':
                raise ValueError('Invalid escape')
            digits = text[i + 2:i + 6]
            if any(c not in '0123456789abcdefABCDEF' for c in digits):
                raise ValueError('Invalid Unicode escape')
            if len(digits) < 4:
                unfinished(path, decoded, text[start:])
            high = int(digits, 16)
            if 0xD800 <= high <= 0xDBFF:
                tail = text[i + 6:]
                if tail in {'', '\\', '\\u'}:
                    unfinished(path, decoded, text[start:])
                if not tail.startswith('\\u'):
                    raise ValueError('Invalid surrogate pair')
                digits = tail[2:6]
                if any(c not in '0123456789abcdefABCDEF' for c in digits):
                    raise ValueError('Invalid low surrogate')
                if len(digits) < 4:
                    unfinished(path, decoded, text[start:])
                low = int(digits, 16)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise ValueError('Invalid low surrogate')
                decoded.append(chr(0x10000 + ((high - 0xD800) << 10) + low - 0xDC00))
                i += 12
            elif 0xDC00 <= high <= 0xDFFF:
                raise ValueError('Unpaired low surrogate')
            else:
                decoded.append(chr(high))
                i += 6
        unfinished(path, decoded)
        raise AssertionError('unreachable')

    def value(i: int, path: Path) -> tuple[Any, int]:
        i = space(i)
        if i == n:
            raise _End
        char = text[i]
        if char == '"':
            item, end = string(i, path)
        elif char in '{[':
            mapping = char == '{'
            close = '}' if mapping else ']'
            item = {} if mapping else []
            seen: set[str] = set()
            end = space(i + 1)
            if end == n:
                raise _End
            if text[end] != close:
                while True:
                    if mapping:
                        assert isinstance(item, dict)
                        if text[end] != '"':
                            raise ValueError('Expected object key')
                        key, end = string(end, None)
                        if key in seen:
                            raise ValueError('Duplicate key')
                        seen.add(key)
                        end = space(end)
                        if end == n:
                            raise _End
                        if text[end] != ':':
                            raise ValueError('Expected colon')
                        child, end = value(end + 1, (*path, key))
                        item[key] = child
                    else:
                        assert isinstance(item, list)
                        child, end = value(end, (*path, len(item)))
                        item.append(child)
                    end = space(end)
                    if end == n:
                        raise _End
                    if text[end] == close:
                        break
                    if text[end] != ',':
                        raise ValueError('Expected separator')
                    end = space(end + 1)
                    if end == n:
                        raise _End
                    if text[end] == close:
                        raise ValueError('Trailing comma')
            end += 1
        else:
            tail = text[i:]
            if any(token.startswith(tail) and token != tail for token in ('true', 'false', 'null')):
                raise _End
            try:
                item, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError as error:
                if tail == '-' or re.fullmatch(_NUMBER_PREFIX, tail):
                    raise _End from error
                raise ValueError('Invalid scalar') from error
            if isinstance(item, float) and not (-float('inf') < item < float('inf')):
                raise ValueError('Nonfinite number')
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if re.fullmatch(_NUMBER_PREFIX, tail) and (not terminal_complete or end != n):
                    raise _End
            boundary = space(end)
            if boundary < n and text[boundary] not in ',]}':
                raise ValueError('Invalid scalar suffix')
        result.closed[path] = item
        return item, end

    try:
        _, end = value(0, ())
        if space(end) != n:
            raise ValueError('Trailing content')
        result.root_complete = True
    except _End:
        pass
    return result

"""Deterministic punctuation boundaries, not semantic sentence understanding."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import AsyncIterable, AsyncIterator

from agently.types.data.audio import TextSegmentOptions, TextSource
from agently.types.plugins.AudioModelRequester import TextSegmenter

_CLOSE = '\"\'”’」』）)]}'
_END = "。！？!?"


def positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def validate_segments(options: TextSegmentOptions) -> int:
    positive_int(options.expect_chars, "expect_chars")
    positive_int(options.max_input_chars, "max_input_chars")
    if isinstance(options.grace_chars, bool) or not isinstance(options.grace_chars, int) or options.grace_chars < 0:
        raise ValueError("grace_chars must be a non-negative integer.")
    r = options.tolerance_ratio
    if isinstance(r, bool) or not math.isfinite(r) or not 0 <= r < 1:
        raise ValueError("tolerance_ratio must be finite and in [0, 1).")
    return math.floor(options.expect_chars * (1 + r)) + options.grace_chars


def boundaries(text: str, *, final: bool) -> list[tuple[int, int]]:
    """(prefix length, priority), including bounded lookahead for punctuation runs."""
    found: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        char = text[i]
        priority = 0 if char in "\r\n" else 1 if char in _END else 2 if char in ",，" else -1
        if char == ".":
            # A dot between digits/letters is not a delimiter: decimals, domains,
            # and abbreviations are structural cases, not a semantic classifier.
            if i + 1 == len(text) and not final:
                break
            if i + 1 < len(text) and text[i + 1].isalnum():
                i += 1
                continue
            priority = 1
        if priority < 0:
            i += 1
            continue
        end = i + 1
        allowed = "\r\n" if priority == 0 else _END + "." + _CLOSE if priority == 1 else ""
        while end < len(text) and text[end] in allowed:
            end += 1
        if end == len(text) and not final:
            break
        found.append((end, priority))
        i = end
    return found


def safe_cut(text: str, end: int) -> int:
    # Keep combining marks, variation selectors and ZWJ sequences on the next
    # side where possible. This is not a full Unicode grapheme-break engine.
    while 0 < end < len(text) and (
        unicodedata.combining(text[end]) or text[end] in "\ufe0f\u200d" or text[end - 1] == "\u200d"
    ):
        end -= 1
    return end


class GreedyTextSegmenter:
    def __init__(self, options: TextSegmentOptions):
        self.limit = validate_segments(options)
        self.lower = max(1, math.ceil(options.expect_chars * (1 - options.tolerance_ratio)))
        self.upper = math.floor(options.expect_chars * (1 + options.tolerance_ratio))

    def cut(self, text: str, *, final: bool) -> int | None:
        if not final and len(text) <= self.upper:
            return None
        candidates = boundaries(text, final=final)
        normal = [(pos, rank) for pos, rank in candidates if self.lower <= pos <= self.upper]
        if normal:
            return min(normal, key=lambda item: (item[1], -item[0]))[0]
        if not final and len(text) <= self.limit:
            return None
        grace = [(pos, rank) for pos, rank in candidates if self.upper < pos <= self.limit]
        if grace:
            return min(grace, key=lambda item: (item[1], -item[0]))[0]
        early = [(pos, rank) for pos, rank in candidates if pos < self.lower]
        if early:
            return min(early, key=lambda item: (item[1], -item[0]))[0]
        if final and len(text) <= self.limit:
            return len(text) or None
        return safe_cut(text, self.limit) or self.limit


async def text_parts(source: TextSource, options: TextSegmentOptions, strategy: TextSegmenter) -> AsyncIterator[str]:
    """Own the adapter, not the caller's device/iterator. Bound each source item."""
    limit = validate_segments(options)
    buffer = ""
    iterator = None if isinstance(source, (str, AsyncIterable)) else iter(source)
    async_iterator = source.__aiter__() if isinstance(source, AsyncIterable) else None
    single = source if isinstance(source, str) else None
    ended = False
    while not ended:
        if single is not None:
            item, single, ended = single, None, True
        elif async_iterator is not None:
            try:
                item = await anext(async_iterator)
            except StopAsyncIteration:
                item, ended = "", True
        elif iterator is not None:
            try:
                item = next(iterator)
            except StopIteration:
                item, ended = "", True
        else:
            item, ended = "", True
        if not isinstance(item, str):
            raise TypeError("Text streams must yield str.")
        if len(item) > options.max_input_chars:
            raise ValueError("Text source item exceeds max_input_chars; provide smaller input chunks.")
        # Feed only enough lookahead to keep results independent of source chunking.
        offset = 0
        while offset < len(item) or (ended and buffer):
            take = max(0, limit + 1 - len(buffer))
            buffer += item[offset:offset + take]
            offset += min(take, len(item) - offset)
            final = ended and offset == len(item)
            cut = strategy.cut(buffer, final=final)
            if cut is None and final:
                cut = len(buffer)
            if cut is not None:
                if isinstance(cut, bool) or not isinstance(cut, int) or not 0 < cut <= len(buffer):
                    raise ValueError("TextSegmenter must return a positive in-range prefix length or None.")
                part, buffer = buffer[:cut], buffer[cut:]
                if part.strip():
                    yield part
            elif len(buffer) >= limit + 1:
                raise ValueError("TextSegmenter did not produce a boundary within the configured limit.")
            else:
                break
        if not item and not ended:
            # A cooperative empty source must not monopolize the event loop.
            import asyncio
            await asyncio.sleep(0)

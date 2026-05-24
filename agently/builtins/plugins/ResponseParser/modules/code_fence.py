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

"""Code-fence cleanup for scalar string fields.

Models routinely wrap a single artifact (HTML, SVG, JSON, Markdown, source
code) in a fenced block — ```` ```html … ``` ```` — even when the field is
expected to hold the raw value. This helper unwraps that outer fence so the
stored value is the artifact itself, never the Markdown presentation of it.

It is deliberately language-agnostic and conservative: it only unwraps text
that is *wholly* one fenced block (opening fence on the first line, its
matching closing fence as the last line, nothing after it). Prose that merely
*contains* a code block is left untouched.
"""

from __future__ import annotations

import re

# A fence line is 3+ backticks or 3+ tildes. The opener may carry an info
# string (language tag); the closer may not. Backtick fences cannot contain a
# backtick in their info string (CommonMark), which keeps inline code safe.
_FENCE_LINE = re.compile(r"^(`{3,}|~{3,})([^\n]*)$")
_INFO_TOKEN = re.compile(r"[\w.+#-]*")


def _is_closing_fence(line: str, *, fence_char: str, min_len: int) -> bool:
    match = _FENCE_LINE.match(line)
    if not match:
        return False
    marker = match.group(1)
    return marker[0] == fence_char and len(marker) >= min_len and not match.group(2).strip()


def strip_enclosing_code_fence(text: str) -> str:
    """Return the body of *text* if it is wholly a single fenced code block.

    ```` ```html … ``` ````, ```` ```json … ``` ````, ```` ``` … ``` ```` and
    ``~~~ … ~~~`` all qualify. Anything that is not a clean full wrap — prose
    around a block, two adjacent blocks, an unterminated fence — is returned
    unchanged so we never corrupt legitimate content.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    lines = stripped.split("\n")
    if len(lines) < 2:
        return text

    open_match = _FENCE_LINE.match(lines[0])
    if not open_match:
        return text
    marker, info = open_match.group(1), open_match.group(2).strip()
    fence_char, open_len = marker[0], len(marker)
    # The opener's info string must be a bare language token; spaces or other
    # punctuation mean line 0 is regular text that happens to start with fences.
    if not _INFO_TOKEN.fullmatch(info):
        return text

    # The first matching closing fence must be the very last line. If it lands
    # earlier, the text holds more than one block (or trailing prose) and we
    # leave it alone rather than merge unrelated content.
    for index in range(1, len(lines)):
        if _is_closing_fence(lines[index], fence_char=fence_char, min_len=open_len):
            if index != len(lines) - 1:
                return text
            return "\n".join(lines[1:index])
    return text

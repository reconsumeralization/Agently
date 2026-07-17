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


BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE62_RADIX = len(BASE62_ALPHABET)
_BASE62_INDEX = {character: index for index, character in enumerate(BASE62_ALPHABET)}


def encode_base62(value: int) -> str:
    """Encode one non-negative arbitrary-precision integer minimally."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Base62 values must be integers.")
    if value < 0:
        raise ValueError("Base62 values cannot be negative.")
    if value == 0:
        return BASE62_ALPHABET[0]
    characters: list[str] = []
    remaining = value
    while remaining:
        remaining, index = divmod(remaining, _BASE62_RADIX)
        characters.append(BASE62_ALPHABET[index])
    return "".join(reversed(characters))


def decode_base62(value: str) -> int:
    """Decode one minimal case-sensitive Base62 string."""

    if not isinstance(value, str):
        raise TypeError("Base62 text must be a string.")
    if not value:
        raise ValueError("Base62 text cannot be empty.")
    if len(value) > 1 and value[0] == BASE62_ALPHABET[0]:
        raise ValueError("Base62 text must use its minimal representation.")
    decoded = 0
    for character in value:
        try:
            index = _BASE62_INDEX[character]
        except KeyError as error:
            raise ValueError(f"Invalid Base62 character: {character!r}.") from error
        decoded = (decoded * _BASE62_RADIX) + index
    return decoded

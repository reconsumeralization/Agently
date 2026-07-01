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

from collections.abc import Mapping
from typing import Any, TypedDict


class LanguagePolicy(TypedDict, total=False):
    language: str
    output_language: str
    process_language: str
    progress_language: str
    accept_language: str


_LANGUAGE_ALIASES = {
    "": "auto",
    "auto": "auto",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh_hans": "zh-CN",
    "cn": "zh-CN",
    "chinese": "zh-CN",
    "simplified chinese": "zh-CN",
    "simplified_chinese": "zh-CN",
    "中文": "zh-CN",
    "简体中文": "zh-CN",
    "简中": "zh-CN",
    "english": "en",
    "en-us": "en",
    "en_us": "en",
}

_ACCEPT_LANGUAGE_BY_LANGUAGE = {
    "zh-CN": "zh-CN,zh;q=0.9,en;q=0.6",
    "zh-TW": "zh-TW,zh;q=0.9,en;q=0.6",
    "en": "en-US,en;q=0.9",
    "ja": "ja-JP,ja;q=0.9,en;q=0.6",
    "ko": "ko-KR,ko;q=0.9,en;q=0.6",
    "fr": "fr-FR,fr;q=0.9,en;q=0.6",
    "de": "de-DE,de;q=0.9,en;q=0.6",
    "es": "es-ES,es;q=0.9,en;q=0.6",
}


def normalize_language(value: Any = "auto") -> str:
    text = str(value if value is not None else "auto").strip()
    lowered = text.lower().replace("_", "-")
    return _LANGUAGE_ALIASES.get(lowered, text or "auto")


def resolve_language_policy(
    language: Any = None,
    *,
    output_language: Any = None,
    process_language: Any = None,
    progress_language: Any = None,
    accept_language: Any = None,
    base: Mapping[str, Any] | None = None,
) -> LanguagePolicy:
    base_policy = dict(base or {})
    normalized_language = normalize_language(
        language
        if language is not None
        else base_policy.get("language")
        or base_policy.get("output_language")
        or "auto"
    )
    output = normalize_language(output_language if output_language is not None else base_policy.get("output_language") or normalized_language)
    process = normalize_language(process_language if process_language is not None else base_policy.get("process_language") or output)
    progress = normalize_language(progress_language if progress_language is not None else base_policy.get("progress_language") or process)

    policy: LanguagePolicy = {
        "language": normalized_language,
        "output_language": output,
        "process_language": process,
        "progress_language": progress,
    }

    accept = accept_language if accept_language is not None else base_policy.get("accept_language")
    if accept is None:
        accept = _ACCEPT_LANGUAGE_BY_LANGUAGE.get(output)
    if accept is not None and str(accept).strip():
        policy["accept_language"] = str(accept).strip()
    return policy


def language_policy_from_options(options: Any) -> LanguagePolicy | None:
    if not isinstance(options, Mapping):
        return None
    raw_policy = options.get("language_policy")
    if isinstance(raw_policy, Mapping):
        return resolve_language_policy(base=raw_policy)
    raw_language = options.get("language")
    if raw_language is not None:
        return resolve_language_policy(raw_language)
    return None


def language_policy_from_prompt_snapshot(prompt_snapshot: Any) -> LanguagePolicy | None:
    if not isinstance(prompt_snapshot, Mapping):
        return None
    policy = language_policy_from_options(prompt_snapshot.get("options"))
    if policy is not None:
        return policy
    raw = prompt_snapshot.get("language_policy")
    if isinstance(raw, Mapping):
        raw_policy = raw.get("policy") if isinstance(raw.get("policy"), Mapping) else raw
        return resolve_language_policy(base=raw_policy)
    return None


def language_policy_prompt(policy: Mapping[str, Any] | None) -> str:
    resolved = resolve_language_policy(base=policy or {})
    output = resolved.get("output_language", "auto")
    process = resolved.get("process_language", output)
    lines = [
        "Language policy:",
        f"- Write final user-facing answers and deliverables in {output}.",
        f"- Write important process text, progress, verification notes, and task status text in {process}.",
        "- Keep citations, URLs, code identifiers, file paths, command output, error text, and direct source quotes in their original form when accuracy requires it.",
        "- When forming Search queries or locale-sensitive requests, prefer the policy language unless the task or source requires another locale.",
    ]
    return "\n".join(lines)


def apply_language_policy_to_prompt(prompt: Any, policy: Mapping[str, Any]) -> None:
    resolved = resolve_language_policy(base=policy)
    existing_options = prompt.get("options", {}, inherit=False) if hasattr(prompt, "get") else {}
    merged_options = dict(existing_options) if isinstance(existing_options, Mapping) else {}
    merged_options["language_policy"] = dict(resolved)
    prompt.set("options", merged_options)
    prompt.set(
        "language_policy",
        {
            "policy": dict(resolved),
            "instruction": language_policy_prompt(resolved),
        },
    )

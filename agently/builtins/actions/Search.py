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


from typing import Any, Literal

from agently.utils import LazyImport, FunctionShifter

SearchBackend = Literal[
    "auto",
    "all",
    "bing",
    "brave",
    "duckduckgo",
    "google",
    "grokipedia",
    "mojeek",
    "startpage",
    "wikipedia",
    "yahoo",
    "yandex",
]
NewsSearchBackend = Literal["auto", "all", "bing", "duckduckgo", "yahoo"]


class Search:
    DEFAULT_TEXT_FALLBACK_BACKENDS = ("yahoo", "brave", "duckduckgo", "google", "startpage", "mojeek")
    DEFAULT_NEWS_FALLBACK_BACKENDS = ("yahoo", "duckduckgo", "bing")

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: int | None = None,
        backend: SearchBackend | str | None = "auto",
        search_backend: SearchBackend | str | None = None,
        news_backend: NewsSearchBackend | str | None = None,
        region: Literal[
            "xa-ar",
            "xa-en",
            "ar-es",
            "au-en",
            "at-de",
            "be-fr",
            "be-nl",
            "br-pt",
            "bg-bg",
            "ca-en",
            "ca-fr",
            "ct-ca",
            "cl-es",
            "cn-zh",
            "co-es",
            "hr-hr",
            "cz-cs",
            "dk-da",
            "ee-et",
            "fi-fi",
            "fr-fr",
            "de-de",
            "gr-el",
            "hk-tzh",
            "hu-hu",
            "in-en",
            "id-id",
            "id-en",
            "ie-en",
            "il-he",
            "it-it",
            "jp-jp",
            "kr-kr",
            "lv-lv",
            "lt-lt",
            "xl-es",
            "my-ms",
            "my-en",
            "mx-es",
            "nl-nl",
            "nz-en",
            "no-no",
            "pe-es",
            "ph-en",
            "ph-tl",
            "pl-pl",
            "pt-pt",
            "ro-ro",
            "ru-ru",
            "sg-en",
            "sk-sk",
            "sl-sl",
            "za-en",
            "es-es",
            "se-sv",
            "ch-de",
            "ch-fr",
            "ch-it",
            "tw-tzh",
            "th-th",
            "tr-tr",
            "ua-uk",
            "uk-en",
            "us-en",
            "ue-es",
            "ve-es",
            "vn-vi",
        ] = "us-en",
        fallback_backends: list[str] | tuple[str, ...] | str | None = None,
        search_fallback_backends: list[str] | tuple[str, ...] | str | None = None,
        news_fallback_backends: list[str] | tuple[str, ...] | str | None = None,
        options: dict[str, Any] | None = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.ddgs = None
        self.backends = {
            "search": search_backend if search_backend is not None else backend,
            "news": news_backend if news_backend is not None else backend,
        }
        self.region = region
        self.fallback_backends = {
            "search": search_fallback_backends if search_fallback_backends is not None else fallback_backends,
            "news": news_fallback_backends if news_fallback_backends is not None else fallback_backends,
        }
        self._extra_options = options or {}

    def _get_ddgs(self):
        if self.ddgs is None:
            ddgs_module = LazyImport.import_package("ddgs", version_constraint=">=9.10.0")
            self.ddgs = ddgs_module.DDGS(proxy=self.proxy, timeout=self.timeout)
        return self.ddgs

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_prefix: str = "",
        expose_to_model: bool = True,
        default_policy: dict[str, Any] | None = None,
    ) -> list[str]:
        prefix = action_prefix.strip()

        def action_name(name: str):
            return f"{ prefix }{ name }" if prefix else name

        specs = [
            (
                "search",
                "Search the web with {query}.",
                {
                    "query": (str, "Search query."),
                    "timelimit": ("d | w | m | y | None", "Optional time limit."),
                    "max_results": (int, "Maximum number of results. Default: 10."),
                },
                "search",
            ),
            (
                "search_news",
                "Search recent news with {query}.",
                {
                    "query": (str, "News search query."),
                    "timelimit": ("d | w | m | None", "Optional time limit."),
                    "max_results": (int, "Maximum number of results. Default: 10."),
                },
                "search_news",
            ),
            (
                "search_wikipedia",
                "Search Wikipedia with {query}.",
                {
                    "query": (str, "Wikipedia search query."),
                    "timelimit": ("d | w | m | y | None", "Optional time limit."),
                    "max_results": (int, "Maximum number of results. Default: 10."),
                },
                "search_wikipedia",
            ),
            (
                "search_arxiv",
                "Search arXiv with {query}.",
                {
                    "query": (str, "arXiv search query."),
                    "max_results": (int, "Maximum number of results. Default: 10."),
                },
                "search_arxiv",
            ),
        ]
        action_ids: list[str] = []
        for base_action_id, desc, kwargs, method_name in specs:
            action_id = action_name(base_action_id)
            action.register_action(
                action_id=action_id,
                desc=desc,
                kwargs=kwargs,
                executor=action.create_action_executor(
                    "SearchActionExecutor",
                    search=self,
                    method_name=method_name,
                ),
                tags=tags,
                default_policy=default_policy,
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={
                    "component": "builtins.actions.Search",
                    "legacy_tool_facade": "agently.builtins.tools.Search",
                    "provider": "ddgs",
                    "ddgs_min_version": "9.10.0",
                    "ddgs_latest_recommended": True,
                    "base_action_id": base_action_id,
                    "backend": self.backends.get("search" if base_action_id != "search_news" else "news", "auto"),
                    "region": self.region,
                },
            )
            action_ids.append(action_id)
        return action_ids

    async def search(
        self,
        query: str,
        timelimit: Literal["d", "w", "m", "y"] | None = None,
        max_results: int | None = 10,
    ) -> list[dict[str, str]]:
        """
        General search from the internet. The most common search tool to be used.

        Args:
            query: text search query.
            timelimit: d, w, m, y. Defaults to None.
            max_results: maximum number of results. Defaults to 10.

        Returns:
            List of dictionaries with search results.
        """
        return self._call_ddgs_with_fallback_result(
            category="search",
            method_name="text",
            query=query,
            timelimit=timelimit,
            max_results=max_results,
        )["data"]

    async def search_news(
        self,
        query: str,
        timelimit: Literal["d", "w", "m"] | None = None,
        max_results: int | None = 10,
    ):
        """
        News search from the internet. A tool to search recent news and stories of the query keywords.

        Args:
            query: news search query.
            timelimit: d, w, m. Defaults to None.
            max_results: maximum number of results. Defaults to 10.

        Returns:
            List of dictionaries with news search results.
        """
        return self._call_ddgs_with_fallback_result(
            category="news",
            method_name="news",
            query=query,
            timelimit=timelimit,
            max_results=max_results,
        )["data"]

    async def _execute_action_method(self, method_name: str, **kwargs) -> dict[str, Any] | list[dict[str, Any]]:
        custom_method = self.__dict__.get(method_name)
        if callable(custom_method):
            output = await FunctionShifter.asyncify(custom_method)(**kwargs)
            if isinstance(output, dict) and "status" in output:
                return output
            return {
                "ok": True,
                "success": True,
                "status": "success",
                "data": output,
                "result": output,
                "diagnostics": [],
                "meta": {
                    "provider": "custom",
                    "method": method_name,
                },
            }
        if method_name == "search":
            return self._call_ddgs_with_fallback_result(
                category="search",
                method_name="text",
                query=str(kwargs.get("query") or ""),
                timelimit=kwargs.get("timelimit"),
                max_results=kwargs.get("max_results", 10),
            )
        if method_name == "search_news":
            return self._call_ddgs_with_fallback_result(
                category="news",
                method_name="news",
                query=str(kwargs.get("query") or ""),
                timelimit=kwargs.get("timelimit"),
                max_results=kwargs.get("max_results", 10),
            )
        method = getattr(self, method_name)
        return method(**kwargs)

    async def search_wikipedia(
        self,
        query: str,
        timelimit: Literal["d", "w", "m", "y"] | None = None,
        max_results: int | None = 10,
    ):
        """
        Search only from wikipedia.

        Args:
            query: text search query.
            timelimit: d, w, m, y. Defaults to None.
            max_results: maximum number of results. Defaults to 10.

        Returns:
            List of dictionaries with search results.
        """
        ddgs = self._get_ddgs()
        search_wikipedia = FunctionShifter.auto_options_func(ddgs.text)
        try:
            return search_wikipedia(
                query=query,
                timelimit=timelimit,
                max_results=max_results,
                backend="wikipedia",
                region=self.region,
                **self._extra_options,
            )
        except Exception as error:
            if self._is_no_results_error(error):
                return []
            raise

    def _call_ddgs_with_fallback_result(
        self,
        *,
        category: Literal["search", "news"],
        method_name: Literal["text", "news"],
        query: str,
        timelimit: str | None = None,
        max_results: int | None = 10,
    ) -> dict[str, Any]:
        ddgs = self._get_ddgs()
        method = FunctionShifter.auto_options_func(getattr(ddgs, method_name))
        errors: list[dict[str, str]] = []
        empty_backends: list[str] = []
        candidates = self._candidate_backends(category)
        for backend in self._candidate_backends(category):
            try:
                result = method(
                    query=query,
                    timelimit=timelimit,
                    max_results=max_results,
                    backend=backend,
                    region=self.region,
                    **self._extra_options,
                )
            except Exception as error:
                errors.append(
                    {
                        "backend": backend,
                        "type": error.__class__.__name__,
                        "message": str(error),
                    }
                )
                continue
            results = result if isinstance(result, list) else list(result) if result is not None else []
            if results:
                diagnostics = [
                    {
                        "code": "search_backend_failed",
                        "backend": item["backend"],
                        "error_type": item["type"],
                        "message": item["message"],
                    }
                    for item in errors
                ]
                diagnostics.extend(
                    {
                        "code": "search_backend_empty",
                        "backend": backend_name,
                        "message": "Backend returned no parsed results.",
                    }
                    for backend_name in empty_backends
                )
                status = "partial_success" if diagnostics else "success"
                return {
                    "ok": True,
                    "success": True,
                    "status": status,
                    "data": results,
                    "result": results,
                    "diagnostics": diagnostics,
                    "meta": {
                        "provider": "ddgs",
                        "category": category,
                        "backend": backend,
                        "attempted_backends": candidates,
                        "failed_backends": [item["backend"] for item in errors],
                        "empty_backends": empty_backends,
                    },
                }
            empty_backends.append(backend)
        if errors and all(self._is_no_results_message(item["message"]) for item in errors):
            errors = []
        if errors:
            raise RuntimeError(
                "Search failed after trying backends: "
                + "; ".join(f"{item['backend']}={item['type']}({item['message']})" for item in errors)
            )
        return {
            "ok": True,
            "success": True,
            "status": "success",
            "data": [],
            "result": [],
            "diagnostics": [
                {
                    "code": "search_backend_empty",
                    "backend": backend,
                    "message": "Backend returned no parsed results.",
                }
                for backend in empty_backends
            ],
            "meta": {
                "provider": "ddgs",
                "category": category,
                "backend": None,
                "attempted_backends": candidates,
                "failed_backends": [],
                "empty_backends": empty_backends,
            },
        }

    def _candidate_backends(self, category: Literal["search", "news"]) -> list[str]:
        configured = self.backends.get(category, "auto")
        configured_items = self._normalize_backend_list(configured)
        explicit_fallback = self._normalize_backend_list(self.fallback_backends.get(category))
        default_fallback = (
            list(self.DEFAULT_NEWS_FALLBACK_BACKENDS)
            if category == "news"
            else list(self.DEFAULT_TEXT_FALLBACK_BACKENDS)
        )
        if not configured_items or any(item in {"auto", "all"} for item in configured_items):
            candidates = explicit_fallback or default_fallback
        else:
            candidates = configured_items + (explicit_fallback or default_fallback)
        seen: set[str] = set()
        normalized: list[str] = []
        for item in candidates:
            backend = str(item).strip()
            if not backend or backend in seen:
                continue
            seen.add(backend)
            normalized.append(backend)
        return normalized or ["auto"]

    @staticmethod
    def _normalize_backend_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    @classmethod
    def _is_no_results_error(cls, error: Exception) -> bool:
        return cls._is_no_results_message(str(error))

    @staticmethod
    def _is_no_results_message(message: str) -> bool:
        return "No results found" in message

    async def search_arxiv(
        self,
        query: str,
        max_results: int | None = 10,
    ):
        LazyImport.import_package("httpx")
        LazyImport.import_package("feedparser")
        from httpx import AsyncClient
        import feedparser

        url = f"https://export.arxiv.org/api/query?search_query=all:{ query }&max_results={ max_results }"

        async with AsyncClient(
            proxy=self.proxy,
            timeout=self.timeout,
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP Error: { response.status_code } { response.text }")
            feed = feedparser.parse(response.text)
            if isinstance(feed.feed, dict):
                result = {
                    "feed_title": feed.feed.get("title"),
                    "updated": feed.feed.get("updated"),
                    "entries": [],
                }
            else:
                result = {
                    "entries": [],
                }
            for entry in feed.entries:
                result["entries"].append(
                    {
                        "title": entry.get("title"),
                        "summary": entry.get("summary"),
                        "published": entry.get("published"),
                        "updated": entry.get("updated"),
                        "authors": [author.name for author in entry.authors],
                        "links": [{"href": link.href, "rel": link.rel, "type": link.type} for link in entry.links],
                    }
                )
            return result

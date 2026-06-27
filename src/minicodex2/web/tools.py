from __future__ import annotations

from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
import hashlib
import ipaddress
import json
import os
import re
import time
import urllib.parse
import urllib.request

from minicodex2.config.settings import AppSettings, WebSearchSettings
from minicodex2.tools.results import ToolResult


@dataclass(slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    published_at: str | None = None


class WebTools:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.config = settings.web_search
        self.cache_root = settings.artifact_root / "web_cache"
        self.search_cache = self.cache_root / "search"
        self.page_cache = self.cache_root / "pages"

    def web_search(
        self,
        query: str,
        max_results: int | None = None,
        freshness: str | None = None,
        domains: list[str] | None = None,
    ) -> ToolResult:
        if not isinstance(query, str) or not query.strip():
            return _invalid_web_arguments("web_search", "query must be a non-empty string")
        if not self.config.enabled:
            return _blocked_web_result(
                "web_search",
                "web_search is disabled. Enable [web_search] in minicodex2.toml before using public web search.",
                "web_search_disabled",
            )
        provider = self.config.provider.lower().strip()
        limit = max(1, min(int(max_results or self.config.max_results), 20))
        clean_domains = [str(item).strip() for item in domains or [] if str(item).strip()]
        cache_key = _cache_key(
            {
                "provider": provider,
                "query": query.strip(),
                "max_results": limit,
                "freshness": freshness or "",
                "domains": clean_domains,
            }
        )
        cached = self._read_cache(self.search_cache / f"{cache_key}.json")
        if cached is not None:
            return ToolResult(
                ok=True,
                content=json.dumps(cached["results"], ensure_ascii=False, indent=2),
                metadata={
                    "tool": "web_search",
                    "provider": provider,
                    "query": query,
                    "cache_hit": True,
                    "results": cached["results"],
                },
            )

        try:
            if provider == "mock":
                results = _mock_search(query, limit)
            elif provider in {"duckduckgo", "duckduckgo_html"}:
                results = _duckduckgo_html_search(self.config, query, limit, clean_domains)
            elif provider == "brave":
                results = _brave_search(self.config, query, limit, freshness, clean_domains)
            else:
                return _blocked_web_result(
                    "web_search",
                    f"unsupported web_search provider: {provider}",
                    "unsupported_web_search_provider",
                    {"provider": provider},
                )
        except Exception as exc:
            return ToolResult(
                ok=False,
                content=f"web_search failed: {exc}",
                blocked=False,
                block_reason="web_search failed",
                metadata={
                    "tool": "web_search",
                    "provider": provider,
                    "query": query,
                    "failure_kind": "web_search_failed",
                    "error": str(exc),
                },
            )

        payload = {"created_at": time.time(), "results": [asdict(item) for item in results]}
        self._write_cache(self.search_cache / f"{cache_key}.json", payload)
        result_dicts = payload["results"]
        return ToolResult(
            ok=True,
            content=json.dumps(result_dicts, ensure_ascii=False, indent=2),
            metadata={
                "tool": "web_search",
                "provider": provider,
                "query": query,
                "cache_hit": False,
                "results": result_dicts,
            },
        )

    def fetch_web_page(self, url: str, max_chars: int = 12_000) -> ToolResult:
        if not isinstance(url, str) or not url.strip():
            return _invalid_web_arguments("fetch_web_page", "url must be a non-empty string")
        normalized_url = url.strip()
        block_reason = _public_url_block_reason(normalized_url)
        if block_reason:
            return _blocked_web_result(
                "fetch_web_page",
                block_reason,
                "blocked_private_or_invalid_url",
                {"url": normalized_url},
            )
        max_chars = max(1_000, min(int(max_chars), 80_000))
        cache_key = _cache_key({"url": normalized_url})
        cached = self._read_cache(self.page_cache / f"{cache_key}.json")
        if cached is not None:
            content = str(cached["text"])[:max_chars]
            return ToolResult(
                ok=True,
                content=content,
                metadata={
                    "tool": "fetch_web_page",
                    "url": normalized_url,
                    "cache_hit": True,
                    "title": cached.get("title", ""),
                    "content_type": cached.get("content_type", ""),
                    "truncated": len(str(cached["text"])) > max_chars,
                },
            )
        try:
            fetched = _fetch_public_page(self.config, normalized_url)
        except Exception as exc:
            return ToolResult(
                ok=False,
                content=f"fetch_web_page failed: {exc}",
                blocked=False,
                block_reason="fetch_web_page failed",
                metadata={
                    "tool": "fetch_web_page",
                    "url": normalized_url,
                    "failure_kind": "fetch_web_page_failed",
                    "error": str(exc),
                },
            )
        self._write_cache(self.page_cache / f"{cache_key}.json", {"created_at": time.time(), **fetched})
        content = str(fetched["text"])[:max_chars]
        return ToolResult(
            ok=True,
            content=content,
            metadata={
                "tool": "fetch_web_page",
                "url": normalized_url,
                "cache_hit": False,
                "title": fetched.get("title", ""),
                "content_type": fetched.get("content_type", ""),
                "truncated": len(str(fetched["text"])) > max_chars,
            },
        )

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if self.config.cache_ttl_seconds <= 0 or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(payload.get("created_at", 0))
        except Exception:
            return None
        if time.time() - created_at > self.config.cache_ttl_seconds:
            return None
        return payload

    def _write_cache(self, path: Path, payload: dict[str, Any]) -> None:
        if self.config.cache_ttl_seconds <= 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _brave_search(
    config: WebSearchSettings,
    query: str,
    max_results: int,
    freshness: str | None,
    domains: list[str],
) -> list[WebSearchResult]:
    if not config.api_key:
        raise RuntimeError(
            f"provider brave requires an API key in {config.api_key_env or 'web_search.api_key'}"
        )
    effective_query = query.strip()
    if domains:
        effective_query = f"{effective_query} " + " ".join(f"site:{domain}" for domain in domains)
    params = {"q": effective_query, "count": str(max_results)}
    if freshness:
        params["freshness"] = freshness
    base_url = config.base_url or "https://api.search.brave.com/res/v1/web/search"
    request = urllib.request.Request(
        f"{base_url}?{urllib.parse.urlencode(params)}",
        headers={
            "Accept": "application/json",
            "User-Agent": config.user_agent,
            "X-Subscription-Token": config.api_key,
        },
    )
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    raw_results = payload.get("web", {}).get("results", [])
    results: list[WebSearchResult] = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            WebSearchResult(
                title=str(item.get("title") or url),
                url=url,
                snippet=str(item.get("description") or ""),
                source=str(item.get("profile", {}).get("name") or ""),
                published_at=str(item.get("age")) if item.get("age") else None,
            )
        )
    return results


def _mock_search(query: str, max_results: int) -> list[WebSearchResult]:
    quoted = urllib.parse.quote(query)
    return [
        WebSearchResult(
            title=f"Mock result for {query}",
            url=f"https://example.com/search?q={quoted}",
            snippet="Deterministic mock web search result for tests and offline development.",
            source="mock",
        )
    ][:max_results]


def _duckduckgo_html_search(
    config: WebSearchSettings,
    query: str,
    max_results: int,
    domains: list[str],
) -> list[WebSearchResult]:
    effective_query = query.strip()
    if domains:
        effective_query = f"{effective_query} " + " ".join(f"site:{domain}" for domain in domains)
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": effective_query})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        html = response.read(1_500_000).decode("utf-8", errors="replace")
    return _parse_duckduckgo_html(html, max_results)


def _parse_duckduckgo_html(html: str, max_results: int) -> list[WebSearchResult]:
    results: list[WebSearchResult] = []
    for match in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    ):
        raw_url = _html_unescape(match.group(1))
        title = _squash_ws(_strip_tags(_html_unescape(match.group(2))))
        resolved_url = _resolve_duckduckgo_redirect(raw_url)
        if not title or not resolved_url:
            continue
        results.append(WebSearchResult(title=title, url=resolved_url, source="duckduckgo"))
        if len(results) >= max_results:
            break
    snippets = [
        _squash_ws(_strip_tags(_html_unescape(item)))
        for item in re.findall(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', html, flags=re.I | re.S)
    ]
    if not snippets:
        snippets = [
            _squash_ws(_strip_tags(_html_unescape(item)))
            for item in re.findall(r'<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>', html, flags=re.I | re.S)
        ]
    for index, snippet in enumerate(snippets[: len(results)]):
        results[index].snippet = snippet
    return results


def _resolve_duckduckgo_redirect(raw_url: str) -> str:
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("uddg"):
            return params["uddg"][0]
    return raw_url


def _fetch_public_page(config: WebSearchSettings, url: str) -> dict[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": config.user_agent})
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        body = response.read(2_000_000)
        content_type = response.headers.get("content-type", "")
    charset = _charset_from_content_type(content_type) or "utf-8"
    text = body.decode(charset, errors="replace")
    if "html" in content_type.lower() or _looks_like_html(text):
        title, extracted = _extract_html_text(text)
        return {"url": url, "title": title, "text": extracted, "content_type": content_type}
    return {"url": url, "title": "", "text": _squash_ws(text), "content_type": content_type}


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)


def _extract_html_text(html: str) -> tuple[str, str]:
    parser = _HtmlTextExtractor()
    parser.feed(html)
    title = _squash_ws(" ".join(parser.title_parts))
    text = _squash_ws(" ".join(parser.parts))
    return title, text


def _public_url_block_reason(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return f"fetch_web_page only supports public http/https URLs, got scheme: {parsed.scheme or '<empty>'}"
    host = parsed.hostname
    if not host:
        return "fetch_web_page requires a URL hostname"
    lowered = host.lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return "fetch_web_page blocks localhost/private targets; use http_request or browser_test for local services."
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return "fetch_web_page blocks private/reserved IP targets; use http_request or browser_test for local services."
    return None


def _charset_from_content_type(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
    return match.group(1).strip("\"'") if match else None


def _looks_like_html(text: str) -> bool:
    prefix = text[:500].lower()
    return "<html" in prefix or "<!doctype html" in prefix or "<body" in prefix


def _squash_ws(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*", "\n", text)).strip()


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def _html_unescape(text: str) -> str:
    import html

    return html.unescape(text)


def _cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _invalid_web_arguments(tool_name: str, reason: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=reason,
        blocked=True,
        block_reason="invalid tool arguments",
        metadata={"tool": tool_name, "failure_kind": "invalid_tool_arguments"},
    )


def _blocked_web_result(
    tool_name: str,
    reason: str,
    failure_kind: str,
    extra: dict[str, Any] | None = None,
) -> ToolResult:
    metadata = {"tool": tool_name, "failure_kind": failure_kind}
    if extra:
        metadata.update(extra)
    return ToolResult(
        ok=False,
        content=reason,
        blocked=True,
        block_reason=reason,
        metadata=metadata,
    )

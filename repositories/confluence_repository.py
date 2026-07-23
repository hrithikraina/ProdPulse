"""Read-only, bounded Confluence Cloud incident-evidence retrieval."""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
import re
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlparse

import httpx

from core.config import ConfluenceSettings
from domain.models import ConfluenceSource, Incident

_TERM_LIMIT = 500
_EXCERPT_LIMIT = 4000
_CQL_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
_SPACE_FROM_URL = re.compile(r"/spaces/([^/]+)/")


class ConfluenceRepositoryError(RuntimeError):
    """A prompt-safe Confluence failure with no upstream response details."""


class ConfluenceRepository(Protocol):
    async def search(self, incident: Incident) -> list[ConfluenceSource]: ...


class ConfluenceCloudRepository:
    def __init__(self, settings: ConfluenceSettings, timeout: float = 20.0) -> None:
        self._settings = settings
        self._timeout = timeout
        self._allowed_spaces = set(settings.space_keys)

    async def search(self, incident: Incident) -> list[ConfluenceSource]:
        cql = _build_cql(incident, self._settings.space_keys)
        if cql is None:
            return []

        async with httpx.AsyncClient(
            base_url=self._settings.base_url,
            auth=httpx.BasicAuth(self._settings.email, self._settings.api_token),
            headers={"Accept": "application/json"},
            timeout=self._timeout,
        ) as client:
            search_payload = await self._get_json(
                client,
                "/wiki/rest/api/search",
                params={"cql": cql, "limit": self._settings.result_limit},
            )
            results = search_payload.get("results")
            if not isinstance(results, list):
                raise ConfluenceRepositoryError("Confluence search returned an invalid response.")

            sources: list[ConfluenceSource] = []
            page_failures = 0
            for result in results[: self._settings.result_limit]:
                if not isinstance(result, dict):
                    page_failures += 1
                    continue
                page_id = _page_id(result)
                if not page_id:
                    page_failures += 1
                    continue
                try:
                    page = await self._get_json(
                        client,
                        f"/wiki/api/v2/pages/{quote(page_id, safe='')}",
                        params={"body-format": "view"},
                    )
                    source = self._map_source(page_id, result, page)
                except ConfluenceRepositoryError:
                    page_failures += 1
                    continue
                if source is not None:
                    sources.append(source)

            if sources:
                return sources
            if results and page_failures:
                raise ConfluenceRepositoryError("Confluence page evidence could not be retrieved.")
            return []

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise ConfluenceRepositoryError(
                f"Confluence request failed with HTTP {error.response.status_code}."
            ) from error
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise ConfluenceRepositoryError("Confluence request timed out or was unavailable.") from error
        except ValueError as error:
            raise ConfluenceRepositoryError("Confluence returned malformed JSON.") from error
        if not isinstance(payload, dict):
            raise ConfluenceRepositoryError("Confluence returned an invalid response.")
        return payload

    def _map_source(
        self,
        page_id: str,
        result: dict[str, Any],
        page: dict[str, Any],
    ) -> ConfluenceSource | None:
        body = page.get("body")
        view = body.get("view") if isinstance(body, dict) else None
        html = view.get("value") if isinstance(view, dict) else None
        if not isinstance(html, str):
            raise ConfluenceRepositoryError("Confluence page content was missing.")
        excerpt = _html_to_text(html)
        if not excerpt:
            return None

        title = page.get("title") or _nested(result, "content", "title") or result.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ConfluenceRepositoryError("Confluence page metadata was incomplete.")

        webui = (
            _nested(page, "_links", "webui")
            or _nested(result, "content", "_links", "webui")
            or _nested(result, "_links", "webui")
            or result.get("url")
        )
        space_key = (
            _nested(page, "space", "key")
            or _nested(result, "content", "space", "key")
            or _nested(result, "space", "key")
            or _space_from_url(webui)
        )
        if not space_key and len(self._allowed_spaces) == 1:
            space_key = next(iter(self._allowed_spaces))
        if not isinstance(space_key, str) or space_key not in self._allowed_spaces:
            return None

        relative_url = webui if isinstance(webui, str) and webui.strip() else f"/wiki/pages/viewpage.action?pageId={quote(page_id, safe='')}"
        absolute_url = _absolute_page_url(self._settings.base_url, relative_url, page_id)
        last_modified = _parse_datetime(
            _nested(page, "version", "createdAt")
            or page.get("lastModified")
            or result.get("lastModified")
        )
        return ConfluenceSource(
            pageId=page_id,
            title=title.strip(),
            url=absolute_url,
            spaceKey=space_key,
            lastModified=last_modified,
            excerpt=excerpt[:_EXCERPT_LIMIT],
        )


def _build_cql(incident: Incident, space_keys: tuple[str, ...]) -> str | None:
    terms = [
        escaped
        for value in (incident.title, incident.service, incident.symptoms)
        if (escaped := _escape_cql_term(value))
    ]
    if not terms or not space_keys:
        return None
    spaces = ", ".join(f'"{key}"' for key in space_keys)
    conditions = " OR ".join(f'text ~ "{term}"' for term in terms)
    return f"type = page AND space IN ({spaces}) AND ({conditions})"


def _escape_cql_term(value: str) -> str:
    normalized = " ".join(value.split())[:_TERM_LIMIT]
    return _CQL_SPECIAL.sub(r"\\\1", normalized)


def _page_id(result: dict[str, Any]) -> str | None:
    value = _nested(result, "content", "id") or result.get("id")
    return str(value) if value is not None and str(value).strip() else None


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _space_from_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _SPACE_FROM_URL.search(value)
    return match.group(1) if match else None


def _absolute_page_url(base_url: str, value: str, page_id: str) -> str:
    relative = value.strip()
    if relative.startswith("/spaces/"):
        relative = f"/wiki{relative}"
    candidate = urljoin(f"{base_url}/", relative)
    if urlparse(candidate).netloc != urlparse(base_url).netloc:
        return f"{base_url}/wiki/pages/viewpage.action?pageId={quote(page_id, safe='')}"
    return candidate


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception as error:
        raise ConfluenceRepositoryError("Confluence page HTML could not be parsed.") from error
    return " ".join(" ".join(parser.parts).split())[:_EXCERPT_LIMIT]

"""Adapters that search code for the code-investigation agent."""

import base64
import json
from pathlib import Path
from typing import Protocol

import httpx


class CodeSearchResult:
    """A small, prompt-safe excerpt returned by a code search."""

    def __init__(self, path: str, excerpt: str) -> None:
        self.path = path
        self.excerpt = excerpt


class CodeRepository(Protocol):
    def search(self, query: str, limit: int = 3) -> list[CodeSearchResult]: ...


class JsonCodeRepository:
    """Local stand-in for GitHub, used until a repository is connected."""

    def __init__(self, data_directory: Path) -> None:
        self._path = data_directory / "simulated-github-code.json"

    def search(self, query: str, limit: int = 3) -> list[CodeSearchResult]:
        try:
            with self._path.open(encoding="utf-8") as file:
                files = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise RuntimeError("Code-search data could not be read.") from error

        terms = {term.casefold() for term in query.split() if len(term) > 3}
        matches: list[CodeSearchResult] = []
        for file in files:
            content = file["content"]
            if any(term in content.casefold() for term in terms):
                matches.append(CodeSearchResult(path=file["path"], excerpt=_matching_excerpt(content, terms)))
        return matches[:limit]


class GithubCodeRepository:
    """Legacy direct GitHub code-search adapter; automatic proposals use GitHub MCP instead."""

    def __init__(self, repository: str, token: str) -> None:
        self._repository = repository
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def search(self, query: str, limit: int = 3) -> list[CodeSearchResult]:
        terms = " ".join(term for term in query.split() if len(term) > 3)
        if not terms:
            return []
        try:
            with httpx.Client(base_url="https://api.github.com", headers=self._headers, timeout=15) as client:
                response = client.get("/search/code", params={"q": f"{terms} repo:{self._repository}", "per_page": limit})
                response.raise_for_status()
                items = response.json().get("items", [])
                query_terms = {term.casefold() for term in terms.split()}
                return [self._fetch_excerpt(client, item, query_terms) for item in items]
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise RuntimeError(f"GitHub code search failed: {error}") from error

    def _fetch_excerpt(
        self, client: httpx.Client, item: dict, query_terms: set[str]
    ) -> CodeSearchResult:
        response = client.get(item["url"])
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        return CodeSearchResult(
            path=item["path"], excerpt=_matching_excerpt(content, query_terms)
        )


def _matching_excerpt(content: str, terms: set[str], radius: int = 240) -> str:
    lowered = content.casefold()
    position = min((lowered.find(term) for term in terms if term in lowered), default=0)
    start = max(0, position - radius // 2)
    return content[start : start + radius].strip()

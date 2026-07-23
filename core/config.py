"""Environment-based application settings."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SPACE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class ConfluenceSettings:
    base_url: str
    email: str
    api_token: str
    space_keys: tuple[str, ...]
    result_limit: int

@dataclass(frozen=True, slots=True)
class Settings:
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str
    azure_openai_chat_deployment: str
    azure_openai_embedding_deployment: str
    rag_backend: str
    azure_search_endpoint: str | None
    azure_search_api_key: str | None
    azure_search_index_name: str | None
    azure_search_api_version: str
    data_directory: Path
    github_repository: str | None
    github_token: str | None
    confluence: ConfluenceSettings | None

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            azure_openai_endpoint=_required("AZURE_OPENAI_ENDPOINT").rstrip("/"),
            azure_openai_api_key=_required("AZURE_OPENAI_API_KEY"),
            azure_openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            azure_openai_chat_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini"),
            azure_openai_embedding_deployment=os.getenv(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
            ),
            rag_backend=os.getenv("RAG_BACKEND", "azure-search").casefold(),
            azure_search_endpoint=_optional_url("AZURE_SEARCH_ENDPOINT"),
            azure_search_api_key=os.getenv("AZURE_SEARCH_API_KEY"),
            azure_search_index_name=os.getenv("AZURE_SEARCH_INDEX_NAME"),
            azure_search_api_version=os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01"),
            data_directory=Path(os.getenv("DATA_DIRECTORY", PROJECT_ROOT / "data")),
            github_repository=os.getenv("GITHUB_REPOSITORY"),
            github_token=os.getenv("GITHUB_TOKEN"),
            confluence=_confluence_settings(),
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be configured.")
    return value


def _optional_url(name: str) -> str | None:
    value = os.getenv(name)
    return value.rstrip("/") if value else None


def _confluence_settings() -> ConfluenceSettings | None:
    base_url = (os.getenv("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
    email = (os.getenv("CONFLUENCE_EMAIL") or "").strip()
    api_token = (os.getenv("CONFLUENCE_API_TOKEN") or "").strip()
    raw_spaces = os.getenv("CONFLUENCE_SPACE_KEYS") or ""
    space_keys = tuple(dict.fromkeys(key.strip() for key in raw_spaces.split(",") if key.strip()))
    parsed_url = urlparse(base_url)

    if (
        not base_url
        or parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or not email
        or not api_token
        or not space_keys
        or any(not _SPACE_KEY.fullmatch(key) for key in space_keys)
    ):
        return None

    try:
        result_limit = int(os.getenv("CONFLUENCE_RESULT_LIMIT", "3"))
    except ValueError:
        return None

    return ConfluenceSettings(
        base_url=base_url,
        email=email,
        api_token=api_token,
        space_keys=space_keys,
        result_limit=max(1, min(result_limit, 10)),
    )

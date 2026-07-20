"""Environment-based application settings."""
from dataclasses import dataclass
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

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
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be configured.")
    return value


def _optional_url(name: str) -> str | None:
    value = os.getenv(name)
    return value.rstrip("/") if value else None

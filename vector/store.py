"""Protocol shared by local and Azure AI Search incident retrieval."""

from typing import Protocol

from domain.models import Incident, SimilarIncident


MIN_HISTORICAL_SIMILARITY = 0.85
MAX_HISTORICAL_INCIDENTS = 3


def qualifying_historical_matches(matches: list[SimilarIncident]) -> list[SimilarIncident]:
    """Return only user-visible historical evidence above the 85% similarity bar."""
    return sorted(
        (match for match in matches if match.similarity >= MIN_HISTORICAL_SIMILARITY),
        key=lambda match: match.similarity,
        reverse=True,
    )[:MAX_HISTORICAL_INCIDENTS]


class IncidentStore(Protocol):
    async def search(self, incident: Incident, limit: int) -> list[SimilarIncident]: ...

    @property
    def count(self) -> int | None: ...

"""Protocol shared by local and Azure AI Search incident retrieval."""

from typing import Protocol

from domain.models import Incident, SimilarIncident


class IncidentStore(Protocol):
    async def search(self, incident: Incident, limit: int) -> list[SimilarIncident]: ...

    @property
    def count(self) -> int | None: ...

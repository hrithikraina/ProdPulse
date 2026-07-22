"""In-memory incident vectors and cosine-similarity search."""
from dataclasses import dataclass
from math import sqrt
from domain.models import Incident, SimilarIncident
from services.azure_openai import AzureOpenAIClient
from vector.store import qualifying_historical_matches

@dataclass(frozen=True, slots=True)
class VectorizedIncident:
    incident: Incident
    vector: list[float]

class InMemoryIncidentVectorStore:
    def __init__(self, client: AzureOpenAIClient, entries: list[VectorizedIncident]) -> None:
        self._client, self._entries = client, entries
    @classmethod
    async def build(cls, incidents: list[Incident], client: AzureOpenAIClient) -> "InMemoryIncidentVectorStore":
        entries = [VectorizedIncident(incident, await client.embed(incident.searchable_text())) for incident in incidents]
        return cls(client, entries)
    async def search(self, incident: Incident, limit: int) -> list[SimilarIncident]:
        query_vector = await self._client.embed(incident.searchable_text())
        matches = [SimilarIncident(incident=entry.incident, similarity=cosine_similarity(query_vector, entry.vector)) for entry in self._entries]
        return qualifying_historical_matches(matches)
    @property
    def count(self) -> int:
        return len(self._entries)

def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions do not match.")
    denominator = sqrt(sum(value * value for value in left)) * sqrt(sum(value * value for value in right))
    if denominator == 0:
        raise ValueError("Cannot compare a zero-length embedding.")
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator

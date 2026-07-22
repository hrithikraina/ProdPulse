"""Azure AI Search hybrid retrieval for resolved historical incidents."""

from typing import Any

import httpx

from domain.models import Incident, SimilarIncident
from vector.store import MAX_HISTORICAL_INCIDENTS, MIN_HISTORICAL_SIMILARITY


class AzureAISearchIncidentStore:
    """Queries a vectorizer-enabled Azure AI Search index.

    The service sends the plain-text incident to Search. Search's Azure OpenAI
    vectorizer creates the query embedding, while the same request also runs
    BM25 keyword matching. Do not upload an open incident to this index.
    """

    def __init__(self, endpoint: str, api_key: str, index_name: str, api_version: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._index_name = index_name
        self._api_version = api_version
        self._count: int | None = None

    async def search(self, incident: Incident, limit: int) -> list[SimilarIncident]:
        query = incident.searchable_text()
        # A hybrid @search.score is an RRF rank, not a percentage. First run a
        # vector-only query so the 85% threshold uses actual cosine similarity.
        vector_data = await self._request({
            "count": True,
            "search": "",
            "select": "id,title,service,severity,symptoms,rootCause,resolution",
            "top": 50,
            "vectorQueries": [{
                "kind": "text",
                "text": query,
                "fields": "contentVector",
                "k": 50,
            }],
        })
        count = vector_data.get("@odata.count")
        if isinstance(count, int):
            self._count = count
        vector_values = vector_data.get("value")
        if not isinstance(vector_values, list):
            raise RuntimeError("Azure AI Search returned an invalid search response.")
        similarity_by_id = {
            document["id"]: self._cosine_similarity(document.get("@search.score"))
            for document in vector_values
            if isinstance(document, dict) and isinstance(document.get("id"), str)
        }
        qualified_ids = {
            incident_id for incident_id, similarity in similarity_by_id.items()
            if similarity >= MIN_HISTORICAL_SIMILARITY
        }
        if not qualified_ids:
            return []

        # Preserve hybrid keyword + vector ranking for the qualifying evidence.
        hybrid_data = await self._request({
            "search": query,
            "searchFields": "title,symptoms,service,content",
            "select": "id,title,service,severity,symptoms,rootCause,resolution",
            "top": 50,
            "vectorQueries": [{"kind": "text", "text": query, "fields": "contentVector", "k": 50}],
        })
        values = hybrid_data.get("value")
        if not isinstance(values, list):
            raise RuntimeError("Azure AI Search returned an invalid search response.")
        matches = [
            SimilarIncident(incident=Incident.model_validate(document), similarity=similarity_by_id[document["id"]])
            for document in values
            if isinstance(document, dict) and document.get("id") in qualified_ids
        ]
        return matches[:MAX_HISTORICAL_INCIDENTS]

    @property
    def count(self) -> int | None:
        return self._count

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"/indexes/{self._index_name}/docs/search"
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, timeout=30) as client:
                response = await client.post(
                    path,
                    params={"api-version": self._api_version},
                    headers={"api-key": self._api_key},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError(f"Azure AI Search request failed: {error}") from error
        if not isinstance(data, dict):
            raise RuntimeError("Azure AI Search returned an invalid response.")
        return data

    @staticmethod
    def _cosine_similarity(score: Any) -> float:
        """Convert Azure's vector-search score to cosine similarity."""
        try:
            numeric_score = float(score)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Azure AI Search returned an invalid vector score.") from error
        if numeric_score <= 0:
            raise RuntimeError("Azure AI Search returned an invalid vector score.")
        return 2 - (1 / numeric_score)

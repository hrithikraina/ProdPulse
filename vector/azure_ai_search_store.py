"""Azure AI Search hybrid retrieval for resolved historical incidents."""

from typing import Any

import httpx

from domain.models import Incident, SimilarIncident


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
        payload = {
            "count": True,
            "search": query,
            "searchFields": "title,symptoms,service,content",
            "select": "id,title,service,severity,symptoms,rootCause,resolution",
            "top": limit,
            "vectorQueries": [{
                "kind": "text",
                "text": query,
                "fields": "contentVector",
                # Let each retriever contribute enough candidates to RRF.
                "k": max(50, limit),
            }],
        }
        data = await self._request(payload)
        count = data.get("@odata.count")
        if isinstance(count, int):
            self._count = count
        values = data.get("value")
        if not isinstance(values, list):
            raise RuntimeError("Azure AI Search returned an invalid search response.")
        return [self._to_match(document) for document in values]

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
    def _to_match(document: Any) -> SimilarIncident:
        if not isinstance(document, dict):
            raise RuntimeError("Azure AI Search returned an invalid incident document.")
        try:
            historical = Incident.model_validate(document)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"Azure AI Search returned an invalid incident: {error}") from error
        # Hybrid search uses Reciprocal Rank Fusion, so this is a retrieval
        # score, not cosine similarity. It is only shown to the advisor/user.
        score = document.get("@search.rerankerScore", document.get("@search.score", 0.0))
        return SimilarIncident(incident=historical, similarity=float(score))

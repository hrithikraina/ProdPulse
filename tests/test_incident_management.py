import pytest
from domain.models import AgentFinding, Incident, IncidentAnalysis, InitialAssessment, SimilarIncident
from agents.code_investigation import extract_error
from core.config import PROJECT_ROOT
from services.incident_management import IncidentManagementService
from services.azure_openai import AzureOpenAIClient
from vector.in_memory_store import cosine_similarity
from vector.azure_ai_search_store import AzureAISearchIncidentStore
from vector.store import qualifying_historical_matches
from services.analysis_sessions import AnalysisChatService, AnalysisSessionStore, IncidentToolRegistry

def incident(service: str = "checkout-api") -> Incident:
    return Incident(id="INC-1", title="Timeout", service=service, severity="SEV-1", symptoms="Requests time out")

def test_cosine_similarity_returns_one_for_identical_vectors() -> None:
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)

async def test_low_similarity_runs_deployment_agent() -> None:
    class Store:
        async def search(self, _incident: Incident, _limit: int) -> list[SimilarIncident]:
            return [SimilarIncident(incident=incident(), similarity=0.4)]
    class Advisor:
        async def recommend(self, _incoming, _matches, findings) -> InitialAssessment:
            assert len(findings) == 1
            return InitialAssessment(summary="Investigate the release.", nextActionSteps=["Check deployment"], rca=["Release is a hypothesis."], codeChanges=None)
    class DeploymentAgent:
        def investigate(self, _incident: Incident) -> AgentFinding:
            return AgentFinding(agentName="DeploymentCheckAgent", status="DEPLOYMENT_FOUND", summary="Found one", evidence="DEP-1")
    class CodeAgent:
        async def investigate(self, _incident: Incident) -> AgentFinding:
            return AgentFinding(agentName="CodeInvestigationAgent", status="CODE_EVIDENCE_FOUND", summary="Found code", evidence="file.py")
    service = IncidentManagementService(Store(), Advisor(), DeploymentAgent(), CodeAgent())
    result = await service.analyze(incident("reporting-api"))
    assert result.agent_findings[0].status == "DEPLOYMENT_FOUND"
    assert result.summary == "Investigate the release."
    assert result.next_action_steps == ["Check deployment"]

def test_error_extraction_prefers_exception_from_logs() -> None:
    logs = "INFO export started\nERROR TimestampFormatError: completed_at is a string\nAttributeError: 'str' object has no attribute 'strftime'"
    assert extract_error(logs) == "AttributeError: 'str' object has no attribute 'strftime'"


async def test_azure_openai_client_parses_embedding_and_chat_responses() -> None:
    client = AzureOpenAIClient(
        endpoint="https://example.openai.azure.com",
        api_key="test-key",
        api_version="2024-10-21",
        embedding_deployment="embedding-deployment",
        chat_deployment="chat-deployment",
    )

    async def fake_request(path: str, _payload: dict, timeout: float) -> dict:
        assert timeout in (90, 120)
        if path.endswith("/embeddings"):
            return {"data": [{"embedding": [0.1, 0.2]}]}
        return {"choices": [{"message": {"content": "Investigate the release."}}]}

    client._request = fake_request  # type: ignore[method-assign]
    assert await client.embed("incident text") == [0.1, 0.2]
    assert await client.generate("prompt") == "Investigate the release."


async def test_azure_ai_search_store_uses_hybrid_text_vector_query() -> None:
    store = AzureAISearchIncidentStore(
        endpoint="https://example.search.windows.net",
        api_key="test-key",
        index_name="historical-incidents",
        api_version="2025-09-01",
    )

    async def fake_request(payload: dict) -> dict:
        assert payload["vectorQueries"][0]["kind"] == "text"
        assert payload["vectorQueries"][0]["fields"] == "contentVector"
        if payload["search"] == "":
            return {"@odata.count": 1, "value": [{
                "id": "INC-OLD-1", "title": "Old timeout", "service": "checkout-api",
                "severity": "SEV-2", "symptoms": "Gateway timeout", "rootCause": "Bad pool",
                "resolution": "Restarted safely", "@search.score": 0.9090909,
            }]}
        assert payload["searchFields"] == "title,symptoms,service,content"
        return {"@odata.count": 1, "value": [{
            "id": "INC-OLD-1", "title": "Old timeout", "service": "checkout-api",
            "severity": "SEV-2", "symptoms": "Gateway timeout", "rootCause": "Bad pool",
            "resolution": "Restarted safely", "@search.score": 0.021,
        }]}

    store._request = fake_request  # type: ignore[method-assign]
    matches = await store.search(incident(), 3)
    assert matches[0].incident.id == "INC-OLD-1"
    assert matches[0].similarity == pytest.approx(0.9)
    assert store.count == 1


def test_historical_match_filter_requires_85_percent_and_caps_at_three() -> None:
    matches = [
        SimilarIncident(incident=Incident(id=f"INC-{index}", title="Old", service="checkout-api", severity="SEV-2", symptoms="timeout"), similarity=similarity)
        for index, similarity in enumerate([0.99, 0.95, 0.90, 0.85, 0.849])
    ]
    assert [match.incident.id for match in qualifying_historical_matches(matches)] == ["INC-0", "INC-1", "INC-2"]


async def test_analysis_session_is_temporary_and_removed_on_delete() -> None:
    store = AnalysisSessionStore()
    analysis = IncidentAnalysis(
        incomingIncident=incident(), similarIncidents=[], agentFindings=[], summary="Check evidence.",
        nextActionSteps=[], rca=[], codeChanges=None, evidenceSummary="None", agentFlow=[]
    )
    session_id = await store.create(analysis)
    assert (await store.get(session_id)) is not None
    assert await store.delete(session_id) is True
    assert await store.get(session_id) is None


async def test_chat_executes_only_registered_tool_and_returns_its_source() -> None:
    class Store:
        async def search(self, _incident: Incident, _limit: int) -> list[SimilarIncident]:
            return [SimilarIncident(incident=Incident(id="INC-OLD-1", title="Old", service="checkout-api", severity="SEV-2", symptoms="timeout"), similarity=0.9)]

    class DeploymentAgent:
        def investigate(self, _incident: Incident) -> AgentFinding:
            return AgentFinding(agentName="DeploymentCheckAgent", status="DEPLOYMENT_FOUND", summary="Found", evidence="deploymentId=DEP-1")

    class CodeRepository:
        def search(self, _query: str, limit: int = 3):
            return []

    class Client:
        def __init__(self) -> None:
            self.call_count = 0
        async def chat_with_tools(self, _messages, tools):
            self.call_count += 1
            assert {tool["function"]["name"] for tool in tools} == {
                "search_historical_incidents", "get_deployment_evidence", "get_code_evidence", "inspect_code_change_impact", "run_deep_rca_evidence"
            }
            if self.call_count == 1:
                return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "function": {"name": "search_historical_incidents", "arguments": '{"investigation_focus":"prior resolutions"}'}}]}}
            return {"message": {"role": "assistant", "content": "A prior incident exists.", "tool_calls": []}}

    sessions = AnalysisSessionStore()
    analysis = IncidentAnalysis(incomingIncident=incident(), similarIncidents=[], agentFindings=[], summary="Initial", nextActionSteps=[], rca=[], codeChanges=None, evidenceSummary="None", agentFlow=[])
    session_id = await sessions.create(analysis)
    chat = AnalysisChatService(sessions, IncidentToolRegistry(Store(), DeploymentAgent(), CodeRepository()), Client())
    response = await chat.chat(session_id, "Have we seen this before?")
    assert response is not None
    assert response.answer == "A prior incident exists."
    assert response.agent_calls == ["search_historical_incidents"]
    assert response.sources == ["INC-OLD-1"]

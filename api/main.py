"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import logging
import sys
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# Initialize logging configuration to direct info/error outputs to stdout for GCP ingestion
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("api.main")


from agents.deployment_check import DeploymentCheckAgent
from agents.code_investigation import CodeInvestigationAgent
from core.config import Settings
from domain.models import AnalysisChatRequest, AnalysisChatResponse, AnalyzeRequest, HealthResponse, IncidentAnalysis
from repositories.json_repository import DeploymentHistoryRepository, JsonIncidentRepository
from repositories.code_repository import GithubCodeRepository, JsonCodeRepository
from services.advisor import IncidentAdvisor
from services.analysis_sessions import AnalysisChatService, AnalysisSessionStore, IncidentToolRegistry
from services.incident_management import IncidentManagementService
from services.azure_openai import AzureOpenAIClient
from vector.in_memory_store import InMemoryIncidentVectorStore
from vector.azure_ai_search_store import AzureAISearchIncidentStore
from vector.store import IncidentStore

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_environment()
    azure_openai = AzureOpenAIClient(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        embedding_deployment=settings.azure_openai_embedding_deployment,
        chat_deployment=settings.azure_openai_chat_deployment,
    )
    vector_store: IncidentStore
    if settings.rag_backend == "azure-search":
        if not all((settings.azure_search_endpoint, settings.azure_search_api_key, settings.azure_search_index_name)):
            raise RuntimeError(
                "AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_API_KEY, and AZURE_SEARCH_INDEX_NAME "
                "must be configured when RAG_BACKEND=azure-search."
            )
        vector_store = AzureAISearchIncidentStore(
            settings.azure_search_endpoint,
            settings.azure_search_api_key,
            settings.azure_search_index_name,
            settings.azure_search_api_version,
        )
    elif settings.rag_backend == "local":
        incidents = JsonIncidentRepository(settings.data_directory).load_historical_incidents()
        vector_store = await InMemoryIncidentVectorStore.build(incidents, azure_openai)
    else:
        raise RuntimeError("RAG_BACKEND must be either 'azure-search' or 'local'.")
    deployment_agent = DeploymentCheckAgent(DeploymentHistoryRepository(settings.data_directory))
    code_repository = (GithubCodeRepository(settings.github_repository, settings.github_token)
                       if settings.github_repository and settings.github_token
                       else JsonCodeRepository(settings.data_directory))
    app.state.incident_service = IncidentManagementService(
        vector_store=vector_store,
        advisor=IncidentAdvisor(azure_openai),
        deployment_agent=deployment_agent,
        code_agent=CodeInvestigationAgent(),
    )
    app.state.analysis_sessions = AnalysisSessionStore()
    app.state.analysis_chat_service = AnalysisChatService(
        app.state.analysis_sessions,
        IncidentToolRegistry(vector_store, deployment_agent, code_repository),
        azure_openai,
    )
    app.state.historical_incident_count = vector_store.count or 0
    yield

app = FastAPI(title="Incident Management API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def service_from(request: Request) -> IncidentManagementService:
    return request.app.state.incident_service

@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request) -> HealthResponse:
    logger.info("Health check endpoint queried.")
    return HealthResponse(status="ok", historicalIncidentCount=request.app.state.historical_incident_count)

@app.post("/api/v1/incidents/analyze", response_model=IncidentAnalysis, tags=["incidents"])
async def analyze_incident(payload: AnalyzeRequest, request: Request) -> IncidentAnalysis:
    logger.info(f"Received analysis request for incident ID: {payload.incident.id}, service: {payload.incident.service}")
    try:
        analysis = await service_from(request).analyze(payload.incident, payload.limit)
        analysis.analysis_id = await request.app.state.analysis_sessions.create(analysis)
        logger.info(f"Successfully analyzed incident {payload.incident.id}. Created session: {analysis.analysis_id}")
        return analysis
    except (RuntimeError, ValueError) as error:
        logger.error(f"Failed to analyze incident {payload.incident.id}: {error}", exc_info=True)
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/analysis-sessions/{analysis_id}/chat", response_model=AnalysisChatResponse, tags=["analysis sessions"])
async def chat(analysis_id: str, payload: AnalysisChatRequest, request: Request) -> AnalysisChatResponse:
    logger.info(f"Received chat message for session {analysis_id}")
    try:
        response = await request.app.state.analysis_chat_service.chat(analysis_id, payload.message)
    except RuntimeError as error:
        logger.error(f"Error during chat handling in session {analysis_id}: {error}", exc_info=True)
        raise HTTPException(status_code=503, detail=str(error)) from error
    if response is None:
        logger.warning(f"Chat request failed: session {analysis_id} not found or expired.")
        raise HTTPException(status_code=404, detail="Analysis session was not found or has expired.")
    logger.info(f"Successfully generated response for session {analysis_id}. Agent calls: {response.agent_calls}")
    return response


@app.delete("/api/v1/analysis-sessions/{analysis_id}", status_code=204, tags=["analysis sessions"])
async def end_chat(analysis_id: str, request: Request) -> None:
    logger.info(f"Received request to delete/end session {analysis_id}")
    deleted = await request.app.state.analysis_sessions.delete(analysis_id)
    if not deleted:
        logger.warning(f"Delete request failed: session {analysis_id} not found or expired.")
        raise HTTPException(status_code=404, detail="Analysis session was not found or has expired.")
    logger.info(f"Session {analysis_id} successfully deleted.")

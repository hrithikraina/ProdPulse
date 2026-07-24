"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import json
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
from domain.models import AnalysisChatRequest, AnalysisChatResponse, AnalyzeRequest, CodeChangeProposal, DraftPrCreateRequest, DraftPrCreateResponse, DraftPrPreviewRequest, DraftPrPreviewResponse, HealthResponse, IncidentAnalysis
from repositories.json_repository import DeploymentHistoryRepository, JsonIncidentRepository
from repositories.confluence_repository import ConfluenceCloudRepository
from agents.chat_evidence_agents import ChatEvidenceAgents
from services.advisor import IncidentAdvisor
from services.analysis_sessions import AnalysisChatService, AnalysisSessionStore, IncidentToolRegistry
from services.confluence_summarizer import ConfluenceEvidenceSummarizer
from services.incident_management import IncidentManagementService
from services.azure_openai import AzureOpenAIClient
from services.draft_pr_proposal import DraftPrProposalService
from services.code_change_proposal import CodeChangeProposalService
from services.github_draft_pr import DraftPrError, GithubDraftPrService
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
    confluence_repository = (
        ConfluenceCloudRepository(settings.confluence) if settings.confluence is not None else None
    )
    app.state.github_draft_pr = GithubDraftPrService(None, settings.github_token)
    evidence_agents = ChatEvidenceAgents()
    app.state.code_change_proposal = CodeChangeProposalService(
        settings.github_owner,
        app.state.github_draft_pr,
        evidence_agents,
    )
    app.state.incident_service = IncidentManagementService(
        vector_store=vector_store,
        advisor=IncidentAdvisor(azure_openai),
        deployment_agent=deployment_agent,
        code_agent=CodeInvestigationAgent(),
        confluence_repository=confluence_repository,
        confluence_summarizer=ConfluenceEvidenceSummarizer(azure_openai),
        code_change_proposal=app.state.code_change_proposal,
    )
    app.state.analysis_sessions = AnalysisSessionStore()
    app.state.analysis_chat_service = AnalysisChatService(
        app.state.analysis_sessions,
        IncidentToolRegistry(vector_store, deployment_agent, evidence_agents),
        azure_openai,
    )
    app.state.azure_openai = azure_openai
    app.state.draft_pr_proposal = DraftPrProposalService(app.state.github_draft_pr, azure_openai)
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


@app.post("/api/v1/analysis-sessions/{analysis_id}/draft-pr/preview", response_model=DraftPrPreviewResponse, tags=["draft pull requests"])
async def preview_draft_pr(analysis_id: str, payload: DraftPrPreviewRequest, request: Request) -> DraftPrPreviewResponse:
    session = await request.app.state.analysis_sessions.get(analysis_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Analysis session was not found or has expired.")
    try:
        assessment = json.loads(session.initial_assessment)
        code_changes = assessment.get("codeChanges")
        try:
            suggested_change = CodeChangeProposal.model_validate(code_changes)
        except ValueError as error:
            raise DraftPrError("This analysis does not include a verified suggested code change.") from error
        requested_base_branch = payload.base_branch or "main"
        github = request.app.state.github_draft_pr.for_repository(payload.repository)
        if (
            payload.repository.strip().casefold() == suggested_change.repository.casefold()
            and payload.file_path == suggested_change.file_path
            and requested_base_branch == suggested_change.base_branch
        ):
            preview = await github.preview(payload.file_path, suggested_change.code_changes, requested_base_branch)
            preview["patch"] = suggested_change.code_changes
        else:
            if not session.code_change_intent:
                raise DraftPrError("This analysis cannot regenerate a code proposal for a different target.")
            proposal = DraftPrProposalService(github, request.app.state.azure_openai)
            preview = await proposal.preview(
                session.incident,
                session.code_change_intent,
                payload.file_path,
                payload.base_branch,
            )
        preview_id = await request.app.state.analysis_sessions.save_draft_preview(session, {key: str(preview[key]) for key in ("repository", "filePath", "baseBranch", "patch")})
        return DraftPrPreviewResponse(previewId=preview_id, repository=str(preview["repository"]), baseBranch=str(preview["baseBranch"]), filePath=str(preview["filePath"]), patch=str(preview["patch"]))
    except DraftPrError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        logger.error("Draft PR preview failed: %s", error)
        raise HTTPException(status_code=503, detail="Unable to generate a draft PR preview. Please try again later.") from error


@app.post("/api/v1/analysis-sessions/{analysis_id}/draft-pr", response_model=DraftPrCreateResponse, tags=["draft pull requests"])
async def create_draft_pr(analysis_id: str, payload: DraftPrCreateRequest, request: Request) -> DraftPrCreateResponse:
    session = await request.app.state.analysis_sessions.get(analysis_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Analysis session was not found or has expired.")
    preview = await request.app.state.analysis_sessions.get_draft_preview(session, payload.preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="Draft PR preview was not found or has expired.")
    title = f"fix: investigate {session.incident.id} - {session.incident.title}"[:240]
    body = f"AI-assisted remediation for incident {session.incident.id}.\n\nThis draft pull request was created after a user reviewed the generated patch."
    try:
        github = request.app.state.github_draft_pr.for_repository(preview["repository"])
        created = await github.create_from_patch(session.incident.id, preview["filePath"], preview["patch"], title, body, preview["baseBranch"])
        return DraftPrCreateResponse(**created)
    except DraftPrError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/v1/analysis-sessions/{analysis_id}", status_code=204, tags=["analysis sessions"])
async def end_chat(analysis_id: str, request: Request) -> None:
    logger.info(f"Received request to delete/end session {analysis_id}")
    deleted = await request.app.state.analysis_sessions.delete(analysis_id)
    if not deleted:
        logger.warning(f"Delete request failed: session {analysis_id} not found or expired.")
        raise HTTPException(status_code=404, detail="Analysis session was not found or has expired.")
    logger.info(f"Session {analysis_id} successfully deleted.")

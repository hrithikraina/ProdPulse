# Incident Management API

A FastAPI application that searches historical incidents using Azure OpenAI embeddings, gathers deployment and code evidence for weak matches, and uses Azure OpenAI chat completions to return bounded operational guidance.

## Request flow

1. At startup, the API loads `data/historical-incidents.json` and builds an in-memory embedding index.
2. `POST /api/v1/incidents/analyze` embeds the incoming incident and returns the closest historical incidents.
3. If the top similarity is below `SIMILARITY_THRESHOLD` (default `0.85`), the Deployment Check Agent adds the latest successful deployment for the affected service.
4. A low-similarity incident that includes optional `logs` also runs the Code Investigation Agent. It extracts the most specific error signature, searches configured code, and supplies matching source excerpts as evidence.
5. The chat model receives only the incident, matched history, and agent evidence, then produces an RCA hypothesis and safe resolution steps.

## Project layout

- `api/` — FastAPI application and routes
- `agents/` — deployment and code-investigation agents
- `core/` — configuration
- `domain/` — request and response models
- `repositories/` — JSON data and code-search adapters
- `services/` — orchestration, Azure OpenAI client, and advisor
- `vector/` — in-memory embedding search
- `data/` — banking incident fixtures, logs, deployment history, and simulated code-search results
- `banking-demo/` — six small Java 17 services arranged in a three-layer banking payment flow

## Prerequisites

Create an Azure OpenAI resource with one chat-model deployment and one embedding-model deployment. The deployment names, rather than base model names, are used by the API.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com"
export AZURE_OPENAI_API_KEY="your-azure-openai-key"
export AZURE_OPENAI_CHAT_DEPLOYMENT="your-chat-deployment"
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT="your-embedding-deployment"
uvicorn api.main:app --reload
```

Configuration is through environment variables:

- `AZURE_OPENAI_ENDPOINT` — required, for example `https://your-resource.openai.azure.com`
- `AZURE_OPENAI_API_KEY` — required Azure OpenAI API key
- `AZURE_OPENAI_API_VERSION` (default `2024-10-21`)
- `AZURE_OPENAI_CHAT_DEPLOYMENT` (default `gpt-4o-mini`)
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (default `text-embedding-3-small`)
- `SIMILARITY_THRESHOLD` (default `0.85`)
- `DATA_DIRECTORY` (default `./data`)
- `GITHUB_REPOSITORY` and `GITHUB_TOKEN` — optional. Set both to search your real `owner/repository` with GitHub's code-search API. Without them, the demo uses `data/simulated-github-code.json`.

Open the API documentation at `http://127.0.0.1:8000/docs`. Send an incident to `POST /api/v1/incidents/analyze` using an `incident` object from `data/new-incident.json`.

To run the tests:

```bash
pytest
```

Update `data/historical-incidents.json` to add historical cases and `data/new-incident.json` to maintain sample incoming incidents.

## Banking demo fixtures

`banking-demo/` contains two services in each layer: channel, processing, and data. Each service is a self-contained Maven application. See [banking-demo/README.md](banking-demo/README.md) for the flow and commands.

`INC-NEW-BNK-01` and `INC-NEW-BNK-02` are intentional low-similarity incidents with runnable Java error logs: a risk-decision `IndexOutOfBoundsException` and database connection-pool exhaustion. They activate both agents: deployment history identifies releases `DEP-BNK-2003` and `DEP-BNK-2004`, while the code agent locates the matching Java source excerpt in `data/simulated-github-code.json`.

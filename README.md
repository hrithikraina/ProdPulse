# Incident Management API

A FastAPI application that searches historical incidents using Ollama embeddings, gathers deployment and code evidence for weak matches, and uses an Ollama chat model to return bounded operational guidance.

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
- `services/` — orchestration, Ollama client, and advisor
- `vector/` — in-memory embedding search
- `data/` — compact sample incidents, logs, deployment history, and simulated GitHub code

## Prerequisites

Run an Ollama server with a chat model and an embedding-capable model:

```bash
ollama pull nomic-embed-text
ollama pull mistral
ollama serve
```

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn api.main:app --reload
```

Configuration is through environment variables:

- `OLLAMA_BASE_URL` (default `http://localhost:11434`)
- `OLLAMA_CHAT_MODEL` (default `mistral:latest`)
- `OLLAMA_EMBEDDING_MODEL` (default `nomic-embed-text:latest`)
- `SIMILARITY_THRESHOLD` (default `0.85`)
- `DATA_DIRECTORY` (default `./data`)
- `GITHUB_REPOSITORY` and `GITHUB_TOKEN` — optional. Set both to search your real `owner/repository` with GitHub's code-search API. Without them, the demo uses `data/simulated-github-code.json`.

Open the API documentation at `http://127.0.0.1:8000/docs`. Send an incident to `POST /api/v1/incidents/analyze` using the `incident` object from `data/new-incident.json`.

To run the tests:

```bash
pytest
```

Update `data/historical-incidents.json` to add historical cases and `data/new-incident.json` to maintain sample incoming incidents.

`INC-NEW-06` is an intentional low-similarity example with realistic application logs. Because `reporting-api` has no historical incidents, it activates both agents: the deployment agent finds `DEP-9005`, and the code agent extracts the timestamp exception and finds the simulated source evidence in `data/simulated-github-code.json`.

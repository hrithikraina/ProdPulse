# Incident Management API

A FastAPI application that retrieves resolved historical incidents from Azure AI Search, gathers deployment and code evidence, and uses Azure OpenAI chat completions to return bounded operational guidance.

## Request flow

1. Azure Blob Storage contains only **resolved** incidents as JSON; `data/historical-incidents.json` is the upload-ready sample.
2. An Azure AI Search blob indexer uses the Azure OpenAI embedding skill to index each incident's `content` field.
3. `POST /api/v1/incidents/analyze` runs a hybrid search: exact terms over `title`, `symptoms`, `service`, and `content`, plus a query-time text-to-vector search over `contentVector`.
4. The Deployment Check Agent always adds the latest relevant deployment; the Code Investigation Agent runs whenever logs are present.
5. The chat model receives only the new incident, retrieved history, and those findings, then produces an evidence-bounded RCA hypothesis and safe next steps.

Never add the new/open incident to the historical container before analysis. Add it only after resolution, then run the indexer so it can become evidence for future incidents.

## Project layout

- `api/` — FastAPI application and routes
- `agents/` — deployment and code-investigation agents
- `core/` — configuration
- `domain/` — request and response models
- `repositories/` — JSON data and code-search adapters
- `services/` — orchestration, Azure OpenAI client, and advisor
- `vector/` — incident-retrieval adapters: Azure AI Search hybrid retrieval (default) and an optional local in-memory fallback for development
- `data/` — banking incident fixtures, logs, deployment history, and simulated code-search results
- `banking-demo/` — six small Java 17 services arranged in a three-layer banking payment flow

## Prerequisites

Create an Azure OpenAI resource with one chat-model deployment and a `text-embedding-3-small` deployment (1536 dimensions). Also create a Storage account/container and an Azure AI Search service. The deployment names, rather than base model names, are used by the API.

### Your Azure OpenAI values

Your existing resource is already suitable:

- Resource group: `rg-incident-learning`
- Azure OpenAI resource: `indicentlearning`
- Endpoint: `https://indicentlearning.openai.azure.com`
- Embedding deployment: `text-embedding-3-small`
- Chat deployment: `gpt-5-mini`

Only copy the endpoint and keys from the portal; never put a key in this README or commit it to Git.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export AZURE_OPENAI_ENDPOINT="https://indicentlearning.openai.azure.com"
export AZURE_OPENAI_API_KEY="your-azure-openai-key"
export AZURE_OPENAI_CHAT_DEPLOYMENT="gpt-5-mini"
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT="text-embedding-3-small"
export AZURE_SEARCH_ENDPOINT="https://your-search-service.search.windows.net"
export AZURE_SEARCH_API_KEY="your-search-admin-key"
export AZURE_SEARCH_INDEX_NAME="historical-incidents"
uvicorn api.main:app --reload
```

Configuration is through environment variables:

- `AZURE_OPENAI_ENDPOINT` — required, for example `https://your-resource.openai.azure.com`
- `AZURE_OPENAI_API_KEY` — required Azure OpenAI API key
- `AZURE_OPENAI_API_VERSION` (default `2024-10-21`)
- `AZURE_OPENAI_CHAT_DEPLOYMENT` (default `gpt-4o-mini`)
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (default `text-embedding-3-small`)
- `RAG_BACKEND` (default `azure-search`) — use `local` only to retain the old in-memory demo mode
- `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME` — required for `azure-search`
- `AZURE_SEARCH_API_VERSION` (default `2025-09-01`)
- `DATA_DIRECTORY` (default `./data`)
- `GITHUB_REPOSITORY` and `GITHUB_TOKEN` — optional. Set both to search your real `owner/repository` with GitHub's code-search API. Without them, the demo uses `data/simulated-github-code.json`.

Open the API documentation at `http://127.0.0.1:8000/docs`. Send an incident to `POST /api/v1/incidents/analyze` using an `incident` object from `data/new-incident.json`.

To run the tests:

```bash
pytest
```
app runs on : http://127.0.0.1:8000/docs
## Azure setup (trial-credit friendly)

Use one resource group in a region where both Azure OpenAI model deployments and Azure AI Search are available. Start with Azure AI Search **Free**, Storage account **Standard_LRS**, and small Azure OpenAI deployments. Free Search is enough for this proof of concept; it has service limits, so move to Basic only when those limits or production availability require it. Set a Cost Management budget/alert before creating resources.

1. In the Azure portal, create the resource group, then a Storage account (Standard, LRS, public network access for this prototype). In **Data storage > Containers**, create a private container named `historical-incidents`.
2. Upload `data/historical-incidents.json` to that container. It is a JSON array and every item includes `content`, the exact text that will be embedded. Future resolved incidents must retain the same fields.
3. Your Azure OpenAI resource and model deployments are already created: `indicentlearning`, `text-embedding-3-small`, and `gpt-5-mini`. Copy its endpoint and one key from **Keys and Endpoint**. The resource URI and embedding deployment name have already been filled into the index and skillset JSON files; only their `REPLACE_WITH_YOUR_AOAI_KEY` values still need your key.
4. Create an Azure AI Search service on Free. From **Keys**, copy an admin key (use a query key for read-only production applications later). In **Data sources**, create a Blob Storage data source named `historical-incidents-blob-data-source`; select the storage account and container. Or use [`azure/incident-history-datasource.json`](azure/incident-history-datasource.json), replacing the connection-string placeholder. Use the connection string method for this small trial setup.
5. In **Search management > Indexes**, create an index using [`azure/incident-history-index.json`](azure/incident-history-index.json). Replace all `REPLACE_...` values first. The schema is deliberately fixed to 1536 dimensions for `text-embedding-3-small`; change both `dimensions` values if you select a different embedding model/dimension.
6. In **Skillsets**, create the skillset from [`azure/incident-history-skillset.json`](azure/incident-history-skillset.json), again replacing its resource URL, embedding deployment name, and key. For a production tier, replace stored keys with managed identity and RBAC.
7. In **Indexers**, create the indexer from [`azure/incident-history-indexer.json`](azure/incident-history-indexer.json). Run it once, then inspect **Indexer status**. It must show six succeeded documents and no skill errors before starting the API. `jsonArray` is essential: it turns each array item into a separate search document.
8. Set the environment variables above and start the API. Call `POST /api/v1/incidents/analyze` with `data/new-incident.json`. Azure AI Search receives the incident text once and combines keyword matching with its Azure OpenAI vectorizer at query time; the app never sends the incident to the historical indexer.
9. After an incident is closed, add a fully sanitized resolved record (including `rootCause`, `resolution`, and `content`) to the Blob JSON, upload it, and run the indexer again. Keep secrets, access tokens, customer data, and raw sensitive logs out of indexed content.

For the portal API shapes and current tier behavior, use Microsoft’s documentation on [JSON Blob indexing](https://learn.microsoft.com/azure/search/search-how-to-index-azure-blob-json), [integrated vectorization](https://learn.microsoft.com/azure/search/search-how-to-integrated-vectorization), and [hybrid queries](https://learn.microsoft.com/azure/search/hybrid-search-how-to-query).

If you prefer REST over the portal, send each of the four JSON definitions to the matching Azure AI Search endpoint with your admin key: `PUT /datasources/historical-incidents-blob-data-source`, `PUT /indexes/historical-incidents`, `PUT /skillsets/historical-incidents-embedding-skillset`, and `PUT /indexers/historical-incidents-blob-indexer`, all with `?api-version=2025-09-01`. Do not commit a file after replacing its secret placeholders.

### Easier setup helper

The recommended path for this project is to skip the rest of the Import data wizard and run this command from the project folder instead:

```bash
bash azure/create-search-resources.sh
```

It asks for the Azure AI Search endpoint and primary admin key, the Azure OpenAI `KEY 1`, and the Storage connection string. It creates the four objects with the exact names and fields required by the app, starts the indexer, and only writes secrets into a temporary folder that is removed at the end. It does not edit or commit any template file.

## Banking demo fixtures

`banking-demo/` contains two services in each layer: channel, processing, and data. Each service is a self-contained Maven application. See [banking-demo/README.md](banking-demo/README.md) for the flow and commands.

`INC-NEW-BNK-01` and `INC-NEW-BNK-02` are intentional low-similarity incidents with runnable Java error logs: a risk-decision `IndexOutOfBoundsException` and database connection-pool exhaustion. They activate both agents: deployment history identifies releases `DEP-BNK-2003` and `DEP-BNK-2004`, while the code agent locates the matching Java source excerpt in `data/simulated-github-code.json`.

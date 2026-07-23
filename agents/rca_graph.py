"""Multi-component RCA workflow using LangGraph.

Install: pip install langgraph langchain-openai pydantic
Set:     AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
         AZURE_OPENAI_API_KEY=...
         AZURE_OPENAI_DEPLOYMENT=<your Azure model deployment name>
Run:     python rca_graph.py --log-file error.log

Replace `get_*` functions with calls to your observability, deployment and
repository APIs. They intentionally return example data in this starter.
"""
import json
import logging
import operator
import os
import asyncio
import sys
import re
import sqlite3
from functools import lru_cache

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_openai import AzureChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from dotenv import load_dotenv


# Windows terminals often default to CP1252, which cannot render characters
# such as arrows that may appear in model-generated RCA summaries.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = PROJECT_ROOT
load_dotenv(PROJECT_ROOT / ".env")
LOGGER = logging.getLogger("rca.database_routing")
ARCHITECTURE = json.loads((PROJECT_ROOT / "data" / "architecture_context.json").read_text(encoding="utf-8"))
COMPONENTS = list(ARCHITECTURE["components"])

# Secrets are loaded from environment variables--never store them in the JSON context.
# Azure needs the *deployment name* you created in Azure AI Foundry; it is not
# necessarily the same as the base model name.
MODEL = AzureChatOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini")),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    temperature=1,
)

# These are process-level caches. The GitHub MCP connection, its tool discovery,
# and the LangChain agent are initialized once and reused for later evidence
# requests in the same program run.
_github_client: MultiServerMCPClient | None = None
_github_agent: Any | None = None
_github_agent_lock: asyncio.Lock | None = None

# The SQL agent is also created lazily. Keeping the imports in get_sql_agent()
# means GitHub-only RCA runs do not require the optional SQL dependencies.
_sql_agent: Any | None = None
_sql_agent_database_path: Path | None = None
_sql_agent_lock: asyncio.Lock | None = None

DATABASE_ERROR_WORDS = {
    "database",
    "db",
    "sqlite",
    "sql",
    "query",
    "deadlock",
    "lock wait",
    "connection pool",
    "connection refused",
    "constraint",
    "foreign key",
}
KEY_VALUE_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([^\s,;]+)")
DATABASE_ROUTING_FIELD_EXCLUSIONS = {"level", "message", "service", "component"}


def sqlite_database_path(database_path: Path | None = None) -> Path:
    """Return the configured SQLite path without initializing the SQL agent."""
    return (database_path or BASE_DIR / os.getenv("SQLITE_DB_PATH", "PaymentsPlatform.db")).resolve()


def quote_sqlite_identifier(identifier: str) -> str:
    """Quote a SQLite table or column identifier sourced from database metadata."""
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


@lru_cache(maxsize=1)
def sqlite_schema(database_path: str) -> dict[str, list[dict[str, str]]]:
    """Read SQLite metadata for routing without loading the SQL-agent dependencies."""
    path = Path(database_path)
    if not path.is_file():
        LOGGER.warning("Database routing preflight skipped: SQLite file not found at %s", path)
        return {}

    try:
        LOGGER.info("Connecting to SQLite database for routing preflight: %s", path)
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            schema: dict[str, list[dict[str, str]]] = {}
            for (table_name,) in table_rows:
                columns = connection.execute(
                    f"PRAGMA table_info({quote_sqlite_identifier(table_name)})"
                ).fetchall()
                schema[table_name] = [
                    {"name": column[1], "type": column[2] or "unknown"} for column in columns
                ]
        LOGGER.info(
            "Database routing preflight connected successfully; discovered %d table(s): %s",
            len(schema),
            ", ".join(sorted(schema)) or "none",
        )
        return schema
    except (OSError, sqlite3.Error) as exc:
        # Routing must not fail an RCA run merely because the database is unavailable.
        LOGGER.warning("Database routing preflight failed for %s: %s", path, exc)
        return {}


def database_routing_matches(error_logs: str) -> tuple[dict[str, list[dict[str, str]]], set[str], set[tuple[str, str]]]:
    """Find log fields that correspond to real schema columns and stored values."""
    schema = sqlite_schema(str(sqlite_database_path()))
    fields = {
        name.lower(): value.strip("'\"")
        for name, value in KEY_VALUE_PATTERN.findall(error_logs)
        if name.lower() not in DATABASE_ROUTING_FIELD_EXCLUSIONS and value.strip("'\"")
    }
    LOGGER.info(
        "Database routing extracted log field(s): %s",
        ", ".join(sorted(fields)) or "none",
    )
    matching_columns: set[tuple[str, str]] = set()
    value_matches: set[tuple[str, str]] = set()

    for table_name, columns in schema.items():
        for column in columns:
            # Normalizing preserves strong matching while accepting request_id/requestId variations.
            normalized_column = re.sub(r"[^a-z0-9]", "", column["name"].lower())
            for field_name, value in fields.items():
                if normalized_column != re.sub(r"[^a-z0-9]", "", field_name):
                    continue
                matching_columns.add((table_name, column["name"]))
                try:
                    with sqlite3.connect(f"file:{sqlite_database_path().as_posix()}?mode=ro", uri=True) as connection:
                        row = connection.execute(
                            f"SELECT 1 FROM {quote_sqlite_identifier(table_name)} "
                            f"WHERE {quote_sqlite_identifier(column['name'])} = ? LIMIT 1",
                            (value,),
                        ).fetchone()
                    if row:
                        value_matches.add((table_name, column["name"]))
                except sqlite3.Error:
                    # Metadata matches are still useful even if an individual lookup is unavailable.
                    LOGGER.warning(
                        "Could not check whether a logged identifier exists in %s.%s",
                        table_name,
                        column["name"],
                    )
                    pass
    LOGGER.info(
        "Database routing schema matches: %s; exact-value matches: %s",
        ", ".join(f"{table}.{column}" for table, column in sorted(matching_columns)) or "none",
        ", ".join(f"{table}.{column}" for table, column in sorted(value_matches)) or "none",
    )
    return schema, matching_columns, value_matches


class RouteDecision(BaseModel):
    components: list[Literal["channel", "processing", "data"]] = Field(
    description=(
        "The component(s) with an explicit error in their owned service or "
        "repository. Usually return exactly one component."
    )
)
    reasoning: str = Field(description="Brief explanation based only on logs and architecture.")


class StudyState(TypedDict):
    error_logs: str
    architecture: dict[str, Any]
    selected_components: list[str]
    routing_reason: str
    # Reducers let parallel nodes append without overwriting shared state.
    findings: Annotated[list[dict[str, Any]], operator.add]
    evidence_requests: Annotated[list[dict[str, Any]], operator.add]
    github_evidence_round_1: Annotated[list[dict[str, Any]], operator.add]
    database_evidence_round_1: Annotated[list[dict[str, Any]], operator.add]
    github_evidence_round_2: Annotated[list[dict[str, Any]], operator.add]
    database_evidence_round_2: Annotated[list[dict[str, Any]], operator.add]
    summary: str


async def text(prompt: str) -> str:
    return (await MODEL.ainvoke(prompt)).content





async def router(state: StudyState) -> dict:
    """Classify logs against the architecture. It may choose one, two, or all components."""
    structured_model = MODEL.with_structured_output(RouteDecision)
    decision = await structured_model.ainvoke(
    "You are an incident router. Identify the component that OWNS the "
    "service or repository where the error originated. Select exactly one "
    "component when the logs name a known service or repository. "
    "Select additional components only when the logs contain an explicit "
    "ERROR from a service/repository owned by that component. "
    "An upstream/downstream caller, a dependency name, or a retry target "
    "does not by itself mean that component should be investigated.\n\n"
    f"Architecture:\n{json.dumps(state['architecture'], indent=2)}\n\n"
    f"Error logs:\n{state['error_logs']}"
)
    return {"selected_components": decision.components, "routing_reason": decision.reasoning}


def routes(state: StudyState) -> list[str]:
    # Conditional edges fan out: selected agents run concurrently in the next superstep.
    selected = [f"investigate_{component}" for component in state["selected_components"]]
    return selected or ["summarizer"]


def build_sql_agent_prefix() -> str:
    """Build the SQL agent instructions from the RCA architecture context."""
    prefix = f"You are a database assistant for the {ARCHITECTURE['application_name']}.\n\n"
    prefix += "Database contains application data for:\n"

    for component_name, component in ARCHITECTURE["components"].items():
        prefix += f"\n{component_name}\n- {component['description']}\n"

    prefix += """
Rules:
- Only generate SQLite SQL.
- Never generate INSERT, UPDATE, DELETE, DROP, ALTER, or TRUNCATE statements.
- Use JOINs when information spans multiple tables.
- Prefer aggregation over returning raw data when appropriate.
- Explain the result in business language.
"""
    return prefix


async def get_sql_agent(database_path: Path | None = None) -> Any:
    """Create the SQL agent once, then return the cached agent on later calls."""
    global _sql_agent, _sql_agent_database_path, _sql_agent_lock

    path = sqlite_database_path(database_path)

    if _sql_agent is not None:
        if path != _sql_agent_database_path:
            raise ValueError(
                "The cached SQL agent is configured for "
                f"{_sql_agent_database_path}; start a new process to use {path}."
            )
        return _sql_agent

    if _sql_agent_lock is None:
        _sql_agent_lock = asyncio.Lock()

    async with _sql_agent_lock:
        if _sql_agent is not None:
            return _sql_agent

        if not path.is_file():
            LOGGER.error("SQL agent was selected, but SQLite file is not available at %s", path)
            raise FileNotFoundError(
                f"SQLite database not found: {path}. "
                "Set SQLITE_DB_PATH in .env to its path."
            )

        try:
            from langchain_community.agent_toolkits import (
                SQLDatabaseToolkit,
                create_sql_agent,
            )
            from langchain_community.utilities import SQLDatabase
        except ImportError as exc:
            raise RuntimeError(
                "SQL evidence needs langchain-community and sqlalchemy. "
                "Install them with: .\\venv\\Scripts\\python.exe -m pip install "
                "langchain-community sqlalchemy"
            ) from exc

        # as_posix() produces a SQLAlchemy-compatible SQLite URL on Windows.
        LOGGER.info("Initializing SQL agent with SQLite database: %s", path)
        database = SQLDatabase.from_uri(f"sqlite:///{path.as_posix()}")
        toolkit = SQLDatabaseToolkit(db=database, llm=MODEL)
        _sql_agent = create_sql_agent(
            llm=MODEL,
            toolkit=toolkit,
            verbose=True,
            prefix=build_sql_agent_prefix(),
            agent_type="openai-tools",
        )
        _sql_agent_database_path = path
        LOGGER.info("SQL agent initialized successfully for %s", path)

        return _sql_agent


async def fetch_sql_evidence(question: str, database_path: Path | None = None) -> str:
    """Run a read-only SQL evidence request with the cached SQL agent."""
    LOGGER.info("Calling SQL agent for database evidence")
    sql_agent = await get_sql_agent(database_path)

    # AgentExecutor.invoke() is synchronous, so run it off the async event loop.
    response = await asyncio.to_thread(sql_agent.invoke, {"input": question})
    return str(response["output"])


async def get_github_agent() -> Any:
    """Create the read-only GitHub agent once, then return the cached agent."""
    global _github_client, _github_agent, _github_agent_lock

    if _github_agent is not None:
        return _github_agent

    # A parallel LangGraph branch can reach this function at the same time.
    # The lock ensures only the first call performs MCP tool discovery.
    if _github_agent_lock is None:
        _github_agent_lock = asyncio.Lock()

    async with _github_agent_lock:
        if _github_agent is not None:
            return _github_agent

        _github_client = MultiServerMCPClient(
            {
                "github": {
                    "transport": "http",
                    "url": "https://api.githubcopilot.com/mcp/",
                    "headers": {
                        "Authorization": f"Bearer {os.getenv('GITHUB_PAT') or os.environ['GITHUB_TOKEN']}"
                    },
                }
            }
        )
        github_tools = await _github_client.get_tools()
        _github_agent = create_agent(MODEL, github_tools)

        return _github_agent


async def fetch_github_evidence(
    owner: str,
    repo: str,
    branch: str,
    error_logs: str,
    round_number: int = 1,
    shared_evidence: list[dict[str, Any]] | None = None,
) -> str:
    # The first call initializes the agent. All later calls only invoke it with
    # this request's repository and incident-log context.
    github_agent = await get_github_agent()

    result = await github_agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": f"""
You are a read-only GitHub evidence collector.

You may access ONLY this repository:
Owner: {owner}
Repository: {repo}
Branch: {branch}

Incident error logs:
{error_logs}

Investigation round: {round_number}
Shared evidence from round 1 (use this only to validate or refine findings):
{json.dumps(shared_evidence or [], indent=2)}

Collect only:
1. The five most recent commits.
2. The five most recent pull requests.
3. Code-search results for error-related terms in the logs.
4. At most three relevant source files.

Return a concise report containing commit details, PR details,
matching file paths, functions/classes, and relevant code lines.

Never create, modify, merge, comment on, close, or delete anything.
"""
                }
            ]
        }
    )

    return str(result["messages"][-1].content)

def make_component_agent(component: str):
    def investigate(state: StudyState) -> dict:
        component_context = state["architecture"]["components"][component]
        error_logs = state["error_logs"]
        github_repositories = []

        # The component node does not collect evidence itself. It makes a
        # scoped plan that decides which specialist nodes will be invoked.
        for repository in component_context["repositories"]:
            github = repository.get("github")
            if github and repository["name"].lower() in error_logs.lower():
                github_repositories.append(
                    {
                        "repository": repository["name"],
                        "owner": github["owner"],
                        "repo": github["repo"],
                        "branch": github.get("branch", "main"),
                    }
                )

        configured_table = component_context.get("table")
        schema, matching_columns, value_matches = database_routing_matches(error_logs)
        database_tables: set[str] = set()
        database_reasons: list[str] = []

        # Criterion 1: preserve the existing explicit-table signal.
        if configured_table and configured_table.lower() in error_logs.lower():
            database_tables.add(configured_table)
            database_reasons.append(f"configured table name '{configured_table}' appears in the logs")

        # Criterion 2/3: request_id=..., payment_id=..., etc. match actual
        # schema fields, with an even stronger reason when the logged value exists.
        for table_name, column_name in matching_columns:
            database_tables.add(table_name)
            reason = f"log field matches database column {table_name}.{column_name}"
            if (table_name, column_name) in value_matches:
                reason += " and its value exists in the table"
            database_reasons.append(reason)

        # Criterion 4: explicit database failures merit inspection even when
        # the logs omit an identifier; keep this scoped to the component's table.
        normalized_logs = error_logs.lower()
        if configured_table and any(word in normalized_logs for word in DATABASE_ERROR_WORDS):
            database_tables.add(configured_table)
            database_reasons.append("logs contain a database-specific failure signal")

        if database_tables:
            LOGGER.info(
                "Database agent selected for component '%s'; table(s): %s; criteria: %s",
                component,
                ", ".join(sorted(database_tables)),
                "; ".join(database_reasons),
            )
        else:
            LOGGER.info(
                "Database agent NOT selected for component '%s': no configured-table mention, "
                "schema-column/value match, or database-error signal was found. "
                "Schema available: %s",
                component,
                bool(schema),
            )

        request = {
            "component": component,
            "github_repositories": github_repositories,
            "database_tables": sorted(database_tables),
            "database_reasons": database_reasons,
            "database_schema_available": bool(schema),
        }
        return {
            "evidence_requests": [request],
            "findings": [
                {
                    "component": component,
                    "analysis": "Evidence collection planned; specialist agents run next.",
                    "evidence_request": request,
                }
            ],
        }

    return investigate


def active_evidence_agents(state: StudyState) -> list[str]:
    """Return the specialist agents selected by all active component nodes."""
    requests = state["evidence_requests"]
    agents = []
    if any(request["github_repositories"] for request in requests):
        agents.append("github")
    if any(request["database_tables"] for request in requests):
        agents.append("database")
    LOGGER.info("Evidence agents selected for this RCA: %s", ", ".join(agents) or "none")
    return agents


def join_component_requests(state: StudyState) -> dict:
    """Barrier: all selected component nodes have finished planning."""
    return {}


def route_evidence_round_1(state: StudyState) -> list[str]:
    agents = active_evidence_agents(state)
    return [f"{agent}_round_1" for agent in agents] or ["summarizer"]


def route_evidence_round_2(state: StudyState) -> list[str]:
    # Invoke exactly the same specialist agents selected for round 1.
    return [f"{agent}_round_2" for agent in active_evidence_agents(state)]


def shared_round_1_evidence(state: StudyState) -> list[dict[str, Any]]:
    return state["github_evidence_round_1"] + state["database_evidence_round_1"]


async def github_round_1(state: StudyState) -> dict:
    results = []
    for request in state["evidence_requests"]:
        for repository in request["github_repositories"]:
            try:
                evidence = await fetch_github_evidence(
                        owner=repository["owner"],
                        repo=repository["repo"],
                        branch=repository["branch"],
                        error_logs=state["error_logs"],
                        round_number=1,
                    )
                results.append({"component": request["component"], **repository, "evidence": evidence})
            except Exception as exc:
                results.append({"component": request["component"], **repository, "unavailable": str(exc)})
    return {"github_evidence_round_1": results}


def database_question(
    table_name: str,
    component: str,
    error_logs: str,
    round_number: int,
    shared: list[dict[str, Any]],
    routing_reasons: list[str],
) -> str:
    return f"""
You are collecting read-only SQLite database evidence for RCA round {round_number}.

Investigate only table: {table_name}
Component: {component}
Why this database investigation was selected:
{json.dumps(routing_reasons, indent=2)}
Incident logs:
{error_logs}

Shared round-one evidence:
{json.dumps(shared, indent=2)}

Inspect the schema and run only the smallest relevant SELECT queries. Return
factual records, counts, statuses, or timestamps that correlate with the
incident. Do not query unrelated tables and do not modify data.
"""


async def database_round_1(state: StudyState) -> dict:
    results = []
    for request in state["evidence_requests"]:
        for table_name in request["database_tables"]:
            try:
                evidence = await fetch_sql_evidence(
                        database_question(
                            table_name,
                            request["component"],
                            state["error_logs"],
                            1,
                            [],
                            request["database_reasons"],
                        )
                )
                results.append({"component": request["component"], "table": table_name, "evidence": evidence})
            except Exception as exc:
                LOGGER.exception(
                    "SQL evidence collection failed for component '%s', table '%s'",
                    request["component"],
                    table_name,
                )
                results.append({"component": request["component"], "table": table_name, "unavailable": str(exc)})
    return {"database_evidence_round_1": results}


def join_evidence_round_1(state: StudyState) -> dict:
    """Barrier: all active round-one specialist agents have completed."""
    return {}


async def github_round_2(state: StudyState) -> dict:
    results = []
    shared = shared_round_1_evidence(state)
    for request in state["evidence_requests"]:
        for repository in request["github_repositories"]:
            try:
                evidence = await fetch_github_evidence(
                        owner=repository["owner"],
                        repo=repository["repo"],
                        branch=repository["branch"],
                        error_logs=state["error_logs"],
                        round_number=2,
                        shared_evidence=shared,
                    )
                results.append({"component": request["component"], **repository, "evidence": evidence})
            except Exception as exc:
                results.append({"component": request["component"], **repository, "unavailable": str(exc)})
    return {"github_evidence_round_2": results}


async def database_round_2(state: StudyState) -> dict:
    results = []
    shared = shared_round_1_evidence(state)
    for request in state["evidence_requests"]:
        for table_name in request["database_tables"]:
            try:
                evidence = await fetch_sql_evidence(
                        database_question(
                            table_name,
                            request["component"],
                            state["error_logs"],
                            2,
                            shared,
                            request["database_reasons"],
                        )
                )
                results.append({"component": request["component"], "table": table_name, "evidence": evidence})
            except Exception as exc:
                LOGGER.exception(
                    "SQL evidence collection failed for component '%s', table '%s'",
                    request["component"],
                    table_name,
                )
                results.append({"component": request["component"], "table": table_name, "unavailable": str(exc)})
    return {"database_evidence_round_2": results}


async def summarizer(state: StudyState) -> dict:
    summary = await text(
        "You are the incident commander. Produce a concise RCA report. Separate confirmed "
        "facts from hypotheses; never claim placeholders are production evidence. Include: "
        "incident summary, affected components in flow order, probable root cause, confidence, "
        "recommended next actions, and owners/questions to validate.\n\n"
        f"Architecture:\n{json.dumps(state['architecture'], indent=2)}\n\n"
        f"Router reason:\n{state['routing_reason']}\n\n"
        f"Error logs:\n{state['error_logs']}\n\n"
        f"Component evidence plans:\n{json.dumps(state['findings'], indent=2)}\n\n"
        f"GitHub evidence, round 1:\n{json.dumps(state['github_evidence_round_1'], indent=2)}\n\n"
        f"Database evidence, round 1:\n{json.dumps(state['database_evidence_round_1'], indent=2)}\n\n"
        f"GitHub evidence, round 2:\n{json.dumps(state['github_evidence_round_2'], indent=2)}\n\n"
        f"Database evidence, round 2:\n{json.dumps(state['database_evidence_round_2'], indent=2)}"
    )
    return {"summary": summary}


def build_graph():
    graph = StateGraph(StudyState)
    graph.add_node("router", router)
    for component in COMPONENTS:
        graph.add_node(f"investigate_{component}", make_component_agent(component))
        graph.add_edge(f"investigate_{component}", "join_component_requests")

    graph.add_node("join_component_requests", join_component_requests)
    graph.add_node("github_round_1", github_round_1)
    graph.add_node("database_round_1", database_round_1)
    graph.add_node("join_evidence_round_1", join_evidence_round_1)
    graph.add_node("github_round_2", github_round_2)
    graph.add_node("database_round_2", database_round_2)
    graph.add_node("summarizer", summarizer)

    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", routes)

    # Component nodes plan the optional specialist-agent work.
    graph.add_conditional_edges("join_component_requests", route_evidence_round_1)

    # Active GitHub/database branches finish round 1 before round 2 starts.
    graph.add_edge("github_round_1", "join_evidence_round_1")
    graph.add_edge("database_round_1", "join_evidence_round_1")
    graph.add_conditional_edges("join_evidence_round_1", route_evidence_round_2)

    # Active round-two branches append their final evidence before summarizing.
    graph.add_edge("github_round_2", "summarizer")
    graph.add_edge("database_round_2", "summarizer")
    graph.add_edge("summarizer", END)
    return graph.compile()


def print_agent_flow(result: StudyState) -> None:
    """Print the actual route chosen for this incident.

    Component investigations are sibling branches and may run concurrently;
    therefore this tree shows workflow order, not a guaranteed finish order.
    """
    selected = result["selected_components"]
    flow_order = result["architecture"]["flow_order"]
    # Keep the display in the application's business-flow order.
    ordered_components = [c for c in flow_order if c in selected]
    ordered_components.extend(c for c in selected if c not in ordered_components)

    print("\nAgent flow followed:")
    print("START")
    print("â””â”€â”€ router")
    print(f"    â”œâ”€â”€ selected: {', '.join(ordered_components) or 'none'}")

    for index, component in enumerate(ordered_components):
        is_last = index == len(ordered_components) - 1
        branch = "â””â”€â”€" if is_last else "â”œâ”€â”€"
        print(f"    {branch} investigate_{component} (parallel branch)")

    evidence_agents = active_evidence_agents(result)
    if evidence_agents:
        print("    â””â”€â”€ join_component_requests")
        print(f"        â””â”€â”€ round 1: {', '.join(evidence_agents)}")
        print("        â””â”€â”€ join_evidence_round_1")
        print(f"        â””â”€â”€ round 2: {', '.join(evidence_agents)}")
    print("    â””â”€â”€ summarizer (after active evidence agents)")
    print("        â””â”€â”€ END")



async def run_rca(logs: str) -> StudyState:
    """Run the complete RCA graph using Incident.logs, not a CLI log file."""
    return await build_graph().ainvoke({
        "error_logs": logs,
        "architecture": ARCHITECTURE,
        "selected_components": [],
        "routing_reason": "",
        "findings": [],
        "evidence_requests": [],
        "github_evidence_round_1": [],
        "database_evidence_round_1": [],
        "github_evidence_round_2": [],
        "database_evidence_round_2": [],
        "summary": "",
    })


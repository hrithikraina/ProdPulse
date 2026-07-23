"""Independent log-context agent.

Expands a few anchor log lines (plus an optional explicit timestamp) into the surrounding
log window from a configured backend. Not yet wired into the LangGraph RCA flow
(agents/rca_graph.py) — this module is deliberately self-contained so it can be built and
tested on its own, then called from a future graph node once the placement is finalized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from domain.models import AgentFinding, Incident
from repositories.log_repository import LogRepository

DEFAULT_WINDOW = timedelta(minutes=5)

_LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<level>[A-Z]+)\s+(?P<service>\S+)\s+\S+\s+(?P<message>.*)$"
)

_ISO_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")

# Deliberately duplicated rather than imported from agents/rca_graph.py, which reads
# AZURE_OPENAI_ENDPOINT/AZURE_OPENAI_API_KEY at *import* time — pulling from it would
# break this module's "importable/runnable with zero env vars" property.
_KEY_VALUE_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=([^\s,;]+)")
_CORRELATION_KEY_EXCLUSIONS = {"active", "max", "waited_ms", "depth"}

_STATUS_SUMMARY = {
    "NO_LOGS_SUPPLIED": "No application logs were supplied for log-context expansion.",
    "NO_LOG_SOURCE_MATCHED": "Could not resolve a configured log source for the incident's service.",
    "NO_TIMESTAMP_FOUND": "No parseable timestamp was found in the supplied log lines.",
    "NO_CONTEXT_FOUND": "No log entries were found in the resolved backend for this time window.",
}


@dataclass(frozen=True, slots=True)
class CorrelatedSource:
    source: str
    status: str  # "CORRELATED_MATCH" | "NO_MATCHING_ENTRIES"
    matched_lines: list[str]


@dataclass(frozen=True, slots=True)
class LogContextResult:
    status: str
    source: str | None
    window_start: datetime | None
    window_end: datetime | None
    matched_lines: list[str]
    correlation_key: str | None = None
    correlated_sources: list[CorrelatedSource] = field(default_factory=list)


def parse_anchor_lines(anchor_logs: str) -> list[dict[str, str]]:
    """Parse '<timestamp> <LEVEL> <service> <trace-id> <message>' lines; skip lines that don't match."""
    parsed = []
    for line in anchor_logs.splitlines():
        match = _LOG_LINE_PATTERN.match(line.strip())
        if match:
            parsed.append(match.groupdict())
    return parsed


def resolve_log_source(anchor_logs: str, service_hint: str | None, architecture: dict[str, Any]) -> str | None:
    """Find the configured log source for the service that owns these anchor lines.

    Prefers a repository whose name appears in the logs and has a "logging" source
    configured (mirrors the GitHub-repo matching in agents/rca_graph.py's
    make_component_agent), then falls back to the raw service field parsed from the
    anchor lines when no repository config has a source for it.

    The repo-name substring check runs unconditionally, not only when the strict
    per-line parse succeeds — some callers (e.g. agents/code_investigation.py,
    services/analysis_sessions.py) wrap the raw log line inside a larger
    "Service: X\\nSymptoms: Y\\nLogs: <line>" block before it reaches here, which the
    strict <timestamp> <LEVEL> <service> <trace-id> <message> parser won't match line-by-line,
    but the repository name is still present in the text and should still resolve.
    """
    parsed = parse_anchor_lines(anchor_logs)
    candidate_service = parsed[-1]["service"] if parsed else service_hint

    haystack = f"{anchor_logs} {service_hint or ''}".casefold()
    for component in architecture.get("components", {}).values():
        for repository in component.get("repositories", []):
            logging_config = repository.get("logging")
            if logging_config and repository["name"].casefold() in haystack:
                return logging_config["source"]

    return candidate_service


def extract_anchor_timestamp(anchor_logs: str, override: datetime | None) -> datetime | None:
    if override is not None:
        return override
    for entry in reversed(parse_anchor_lines(anchor_logs)):
        try:
            return datetime.fromisoformat(entry["timestamp"])
        except ValueError:
            continue
    # The strict per-line parse above requires the timestamp to be the first token of a
    # line. Some callers wrap the actual log line inside "Service: X\nSymptoms: Y\nLogs:
    # <line>" (see resolve_log_source's docstring), which buries the timestamp mid-line —
    # fall back to finding any ISO-8601 timestamp anywhere in the text.
    for candidate in reversed(_ISO_TIMESTAMP_PATTERN.findall(anchor_logs)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def extract_correlation_value(anchor_logs: str) -> str | None:
    """Pick the most likely cross-service correlation identifier from the anchor logs.

    Prefers *_id fields (payment_id, request_id, ...) — the most reliable cross-service
    signal available in this dataset — over incidental key=value pairs like active=2 or
    max=2, which are service-local and won't appear verbatim in another service's logs.
    Matching downstream is on this VALUE alone, not the key name, since independently
    logged services often name the same identifier differently (payment_id vs paymentId).
    """
    candidates = [
        (key, value)
        for key, value in _KEY_VALUE_PATTERN.findall(anchor_logs)
        if key.casefold() not in _CORRELATION_KEY_EXCLUSIONS
    ]
    id_like = [pair for pair in candidates if pair[0].casefold().endswith("_id")]
    pool = id_like or candidates
    return pool[-1][1] if pool else None


def sibling_sources(anchor_source: str, architecture: dict[str, Any]) -> list[str]:
    """Other configured log sources in the same architecture component as anchor_source.

    Deliberately bounded to same-component repositories rather than every configured
    service — architecture_context.json already groups related services together (e.g.
    transaction-validation-service and ledger-posting under "processing"), so this reuses
    that existing structure instead of requiring a new adjacency config.
    """
    for component in architecture.get("components", {}).values():
        sources = {
            repository["logging"]["source"]
            for repository in component.get("repositories", [])
            if repository.get("logging")
        }
        if anchor_source in sources:
            return sorted(sources - {anchor_source})
    return []


def find_correlated_neighbors(
    anchor_logs: str,
    anchor_source: str,
    repository: LogRepository,
    architecture: dict[str, Any],
    start: datetime,
    end: datetime,
) -> tuple[str | None, list[CorrelatedSource]]:
    """Search sibling services for the same correlation value within the anchor's window.

    Returns (None, []) when no correlation value could be extracted at all — deliberately
    does not fan out on time window alone, since that would surface unrelated noise from
    whatever else a sibling service happened to be doing at the same moment. Every sibling
    that IS searched gets an explicit CorrelatedSource outcome, match or not — absence is
    reported, never silently dropped.
    """
    correlation_value = extract_correlation_value(anchor_logs)
    if not correlation_value:
        return None, []

    results = []
    for source in sibling_sources(anchor_source, architecture):
        entries = repository.fetch(source, start, end)
        matched = [entry.raw for entry in entries if correlation_value in entry.raw]
        status = "CORRELATED_MATCH" if matched else "NO_MATCHING_ENTRIES"
        results.append(CorrelatedSource(source, status, matched))
    return correlation_value, results


def fetch_log_context(
    anchor_logs: str,
    repository: LogRepository,
    architecture: dict[str, Any],
    anchor_timestamp: datetime | None = None,
    service_hint: str | None = None,
    window: timedelta = DEFAULT_WINDOW,
) -> LogContextResult:
    """Pure orchestration: resolve source + timestamp, then fetch and filter surrounding lines."""
    if not anchor_logs.strip():
        return LogContextResult("NO_LOGS_SUPPLIED", None, None, None, [])

    source = resolve_log_source(anchor_logs, service_hint, architecture)
    if not source:
        return LogContextResult("NO_LOG_SOURCE_MATCHED", None, None, None, [])

    timestamp = extract_anchor_timestamp(anchor_logs, anchor_timestamp)
    if timestamp is None:
        return LogContextResult("NO_TIMESTAMP_FOUND", source, None, None, [])

    start, end = timestamp - window, timestamp + window
    entries = repository.fetch(source, start, end)
    if not entries:
        return LogContextResult("NO_CONTEXT_FOUND", source, start, end, [])

    correlation_key, correlated_sources = find_correlated_neighbors(
        anchor_logs, source, repository, architecture, start, end
    )
    return LogContextResult(
        "LOG_CONTEXT_FOUND",
        source,
        start,
        end,
        [entry.raw for entry in entries],
        correlation_key=correlation_key,
        correlated_sources=correlated_sources,
    )


class LogContextAgent:
    """Wraps fetch_log_context() into the AgentFinding shape the other agents already return."""

    def __init__(self, repository: LogRepository, architecture: dict[str, Any], window: timedelta = DEFAULT_WINDOW) -> None:
        self._repository = repository
        self._architecture = architecture
        self._window = window

    def investigate(self, incident: Incident, anchor_timestamp: datetime | None = None) -> AgentFinding:
        result = fetch_log_context(
            incident.logs or "",
            self._repository,
            self._architecture,
            anchor_timestamp=anchor_timestamp,
            service_hint=incident.service,
            window=self._window,
        )
        if result.status != "LOG_CONTEXT_FOUND":
            return AgentFinding(
                agentName="LogContextAgent",
                status=result.status,
                summary=_STATUS_SUMMARY[result.status],
                evidence="No surrounding log lines were retrieved.",
            )

        lines = [f"[{result.source}] {line}" for line in result.matched_lines]
        for correlated in result.correlated_sources:
            lines.extend(f"[{correlated.source}] {line}" for line in correlated.matched_lines)

        if result.correlation_key is None:
            correlation_note = " No correlation identifier found; cross-service search was not attempted."
        else:
            matched_sources = [c.source for c in result.correlated_sources if c.status == "CORRELATED_MATCH"]
            checked_sources = [c.source for c in result.correlated_sources]
            correlation_note = (
                f" Correlated by {result.correlation_key} across {', '.join(matched_sources) or 'no other services'}"
                f" (checked: {', '.join(checked_sources) or 'none configured'})."
            )

        return AgentFinding(
            agentName="LogContextAgent",
            status="LOG_CONTEXT_FOUND",
            summary=(
                f"Retrieved {len(result.matched_lines)} log line(s) for {result.source} "
                f"between {result.window_start.isoformat()} and {result.window_end.isoformat()}."
                + correlation_note
            ),
            evidence="\n".join(lines),
        )


if __name__ == "__main__":
    import json
    import sys

    from dotenv import load_dotenv

    from core.config import PROJECT_ROOT
    from repositories.log_repository import build_log_repository

    # Only the CLI runner touches .env / env vars — importing this module never requires them.
    load_dotenv(PROJECT_ROOT / ".env")

    architecture_context = json.loads((PROJECT_ROOT / "data" / "architecture_context.json").read_text(encoding="utf-8"))
    incidents = json.loads((PROJECT_ROOT / "data" / "new-incident.json").read_text(encoding="utf-8"))
    incident_id = sys.argv[1] if len(sys.argv) > 1 else incidents[0]["id"]
    incident_data = next((item for item in incidents if item["id"] == incident_id), None)
    if incident_data is None:
        raise SystemExit(f"No incident with id {incident_id!r} in data/new-incident.json")

    # build_log_repository() wraps the Loki backend in ShiftedLokiLogRepository, which
    # transparently applies the same recency shift scripts/seed_loki.py used at push time —
    # no anchor-timestamp override needed here anymore.
    repository = build_log_repository(PROJECT_ROOT / "data")
    agent = LogContextAgent(repository, architecture_context)
    finding = agent.investigate(Incident.model_validate(incident_data))
    print(f"status: {finding.status}")
    print(f"summary: {finding.summary}")
    print("evidence:")
    print(finding.evidence)

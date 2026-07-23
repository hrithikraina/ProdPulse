"""Connect an incident's log string to the async LangGraph RCA workflow."""

import re

from domain.models import AgentFinding, Incident


class CodeInvestigationAgent:
    """Runs only for low-similarity incidents that include application logs."""

    async def investigate(self, incident: Incident) -> AgentFinding:
        logs = incident.logs or ""
        if not logs.strip():
            return AgentFinding(
                agentName="CodeInvestigationAgent",
                status="NO_LOGS_SUPPLIED",
                summary="No application logs were supplied for RCA.",
                evidence="Provide ERROR-level logs or a stack trace.",
            )
        try:
            # Delay graph initialization until logs actually require RCA. This
            # keeps lightweight API imports and log-free requests independent
            # of the RCA-only Azure/MCP configuration.
            from agents.rca_graph import run_rca
            combined_context = f"Service: {incident.service}\nSymptoms: {incident.symptoms}\nLogs: {logs}"
            result = await run_rca(combined_context)
        except RuntimeError as error:
            return AgentFinding(
                agentName="CodeInvestigationAgent",
                status="RCA_UNAVAILABLE",
                summary="The RCA workflow could not complete.",
                evidence=str(error),
            )
        return AgentFinding(
            agentName="RcaGraphAgent",
            status="RCA_COMPLETED",
            summary="LangGraph RCA completed from incident.logs.",
            evidence=result["summary"],
        )


def extract_error(logs: str) -> str | None:
    """Return the most specific exception/error line from application logs."""
    lines = [line.strip() for line in logs.splitlines() if line.strip()]
    patterns = (
        r"(?:[A-Za-z_][\w.]*)(?:Exception|Error):\s*.+",
        r"ERROR\s+.+",
    )
    for pattern in patterns:
        for line in reversed(lines):
            match = re.search(pattern, line)
            if match:
                return match.group(0)[-500:]
    return None

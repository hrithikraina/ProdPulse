"""Prompt construction for bounded, evidence-based incident advice."""
import json

from domain.models import AgentFinding, Incident, InitialAssessment, SimilarIncident
from services.azure_openai import AzureOpenAIClient

class IncidentAdvisor:
    def __init__(self, client: AzureOpenAIClient) -> None:
        self._client = client
    async def recommend(self, incoming: Incident, matches: list[SimilarIncident], findings: list[AgentFinding]) -> InitialAssessment:
        history = "\n".join(f"- ID: {match.incident.id}\n  Retrieval score: {match.similarity:.3f} (ranking signal; not proof of cause)\n  Details: {match.incident.similarity_text()}\n  Root cause: {match.incident.root_cause}\n  Resolution: {match.incident.resolution}" for match in matches) or "No historical incidents retrieved."
        evidence = "\n".join(f"- {finding.agent_name} [{finding.status}]: {finding.summary} Evidence: {finding.evidence}" for finding in findings) or "No additional agent evidence."
        prompt = ("You are an incident commander. Use only the supplied historical incidents and agent evidence. "
                  "Treat retrieved documentation as supporting evidence rather than proof of root cause. "
                  "Assess whether the matches are relevant, state the likely cause as a hypothesis, and give immediate, safe "
                  "investigation/remediation steps. Say explicitly when evidence is insufficient. "
                  "Return ONLY valid JSON with exactly these fields: summary (one short paragraph), "
                  "nextActionSteps (array of concise actions), rca (array of at most 10 concise lines), "
                  "and codeChangeIntent (a concise, evidence-backed description of the required code change; use null when code is not evidenced).\n\n"
                  f"NEW INCIDENT:\n{incoming.similarity_text()}\n\nHISTORICAL INCIDENTS:\n{history}\n\nAGENT EVIDENCE:\n{evidence}")
        response = await self._client.generate(prompt)
        try:
            return InitialAssessment.model_validate(json.loads(_json_content(response)))
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("Azure OpenAI did not return the required structured initial assessment.") from error


def _json_content(response: str) -> str:
    """Accept a JSON response wrapped in a Markdown fence, but nothing else."""
    stripped = response.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped.split("\n", 1)[1].rsplit("\n", 1)[0]
    return stripped

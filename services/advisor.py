"""Prompt construction for bounded, evidence-based incident advice."""
from domain.models import AgentFinding, Incident, SimilarIncident
from services.azure_openai import AzureOpenAIClient

class IncidentAdvisor:
    def __init__(self, client: AzureOpenAIClient) -> None:
        self._client = client
    async def recommend(self, incoming: Incident, matches: list[SimilarIncident], findings: list[AgentFinding]) -> str:
        history = "\n".join(f"- ID: {match.incident.id}\n  Retrieval score: {match.similarity:.3f} (ranking signal; not proof of cause)\n  Details: {match.incident.searchable_text()}\n  Root cause: {match.incident.root_cause}\n  Resolution: {match.incident.resolution}" for match in matches) or "No historical incidents retrieved."
        evidence = "\n".join(f"- {finding.agent_name} [{finding.status}]: {finding.summary} Evidence: {finding.evidence}" for finding in findings) or "No additional agent evidence."
        prompt = ("You are an incident commander. Use only the supplied historical incidents and agent evidence. "
                  "Assess whether the matches are relevant, state the likely cause as a hypothesis, and give immediate, safe "
                  "investigation/remediation steps. Say explicitly when evidence is insufficient.\n\n"
                  f"NEW INCIDENT:\n{incoming.searchable_text()}\n\nHISTORICAL INCIDENTS:\n{history}\n\nAGENT EVIDENCE:\n{evidence}")
        return await self._client.generate(prompt)

"""Deterministic confidence scoring for incident analysis evidence."""

from domain.models import AgentFinding, ConfidenceAssessment, ConfidenceScore, Incident, InitialAssessment, SimilarIncident


def assess_confidence(
    incident: Incident,
    matches: list[SimilarIncident],
    findings: list[AgentFinding],
    assessment: InitialAssessment,
) -> ConfidenceAssessment:
    """Score evidence already collected by the service; never ask the model to guess."""
    rca_score = 0
    recommendation_score = 0
    rca_reasons: list[str] = []
    recommendation_reasons: list[str] = []

    strongest = max(matches, key=lambda match: match.similarity, default=None)
    if strongest and strongest.similarity >= 0.85:
        if strongest.similarity >= 0.95:
            rca_score += 3
            recommendation_score += 2
        elif strongest.similarity >= 0.90:
            rca_score += 2
            recommendation_score += 1
        else:
            rca_score += 1
            recommendation_score += 1
        rca_reasons.append(f"historical match {strongest.incident.id} ({strongest.similarity:.0%})")
        recommendation_reasons.append(f"historical match {strongest.incident.id} ({strongest.similarity:.0%})")
        if strongest.incident.root_cause:
            rca_score += 1
            rca_reasons.append("documented historical root cause")
        if strongest.incident.resolution:
            recommendation_score += 3
            recommendation_reasons.append("documented historical resolution")

    statuses = {finding.status for finding in findings}
    if "DEPLOYMENT_FOUND" in statuses:
        rca_score += 2
        recommendation_score += 1
        rca_reasons.append("deployment evidence")
        recommendation_reasons.append("deployment evidence")
    if incident.logs and incident.logs.strip():
        rca_score += 2
        recommendation_score += 1
        rca_reasons.append("incident logs supplied")
        recommendation_reasons.append("incident logs supplied")
    if statuses & {"RCA_COMPLETED", "CODE_EVIDENCE_FOUND", "CODE_IMPACT_FOUND"}:
        rca_score += 2
        recommendation_score += 2
        rca_reasons.append("code/RCA evidence")
        recommendation_reasons.append("code/RCA evidence")
    if _has_clear_mitigation(assessment.next_action_steps):
        recommendation_score += 1
        recommendation_reasons.append("clear mitigation action")

    return ConfidenceAssessment(
        rca=ConfidenceScore(
            score=min(rca_score, 10),
            reason=_reason(rca_reasons, "No corroborating RCA evidence was collected."),
        ),
        recommendation=ConfidenceScore(
            score=min(recommendation_score, 10),
            reason=_reason(recommendation_reasons, "Recommendations require more supporting evidence."),
        ),
    )


def _has_clear_mitigation(steps: list[str]) -> bool:
    terms = ("rollback", "roll back", "hotfix", "mitigate", "fail-safe", "fail safe")
    return any(term in step.casefold() for step in steps for term in terms)


def _reason(parts: list[str], fallback: str) -> str:
    return f"Based on: {', '.join(parts)}." if parts else fallback

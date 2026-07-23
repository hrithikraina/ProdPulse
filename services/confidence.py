"""Deterministic confidence scoring from the strongest agent evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re

from domain.models import AgentFinding, ConfidenceAssessment, ConfidenceScore, ConfluenceSource, Incident, InitialAssessment, SimilarIncident

_USEFUL_LOG = re.compile(r"\b(error|exception|traceback|failed|failure|timeout|timed out|5\d\d)\b", re.IGNORECASE)
_SPECIFIC_LOG = re.compile(r"\b[A-Za-z_][\w.]+(?:Exception|Error)\b")
_DEPLOYED_AT = re.compile(r"\bdeployedAt=([^,\s]+)")
_SOLUTION_TERMS = ("check", "verify", "validate", "investigate", "restart", "rollback", "roll back", "revert", "hotfix", "patch", "scale", "increase", "decrease", "disable", "enable", "drain", "failover", "configure", "clear", "mitigate", "replace", "restore", "replay")
_IRRELEVANT_TERMS = ("not relevant", "no relevant cause", "no relevant solution", "does not provide a relevant cause", "does not provide a relevant solution", "insufficient information")
_STOP_WORDS = {"about", "after", "again", "could", "current", "from", "have", "incident", "into", "possible", "related", "service", "should", "that", "their", "there", "these", "this", "with"}


@dataclass(frozen=True, slots=True)
class _EvidenceSignal:
    category: str
    label: str
    rca_strength: int
    recommendation_strength: int
    detail: str


def assess_confidence(incident: Incident, matches: list[SimilarIncident], findings: list[AgentFinding], assessment: InitialAssessment, confluence_sources: list[ConfluenceSource] | None = None) -> ConfidenceAssessment:
    """Use strongest evidence plus independent support; the model never chooses a score."""
    del assessment
    signals = [signal for signal in (
        _historical_signal(matches), _deployment_signal(incident, findings),
        _log_signal(incident.logs), _code_signal(findings),
        _confluence_signal(incident, confluence_sources or []),
    ) if signal is not None]
    rca_score, rca_reason = _score_dimension(signals, "rca_strength", "No relevant RCA evidence was collected.")
    recommendation_score, recommendation_reason = _score_dimension(signals, "recommendation_strength", "No grounded recommendation evidence was collected.")
    return ConfidenceAssessment(rca=ConfidenceScore(score=rca_score, reason=rca_reason), recommendation=ConfidenceScore(score=recommendation_score, reason=recommendation_reason))


def _historical_signal(matches: list[SimilarIncident]) -> _EvidenceSignal | None:
    strongest = max(matches, key=lambda match: match.similarity, default=None)
    if strongest is None or strongest.similarity < 0.85:
        return None
    if strongest.similarity >= 0.95:
        rca_strength, recommendation_strength = 6, 5
    elif strongest.similarity >= 0.90:
        rca_strength, recommendation_strength = 5, 4
    else:
        rca_strength, recommendation_strength = 4, 3
    if strongest.incident.root_cause:
        rca_strength = min(rca_strength + 2, 8)
    if strongest.incident.resolution:
        recommendation_strength = 8 if strongest.similarity >= 0.95 else 7
    documented = []
    if strongest.incident.root_cause:
        documented.append("root cause")
    if strongest.incident.resolution:
        documented.append("resolution")
    suffix = f" with documented {' and '.join(documented)}" if documented else ""
    return _EvidenceSignal("historical", "historical incident", rca_strength, recommendation_strength, f"{strongest.incident.id} matched at {strongest.similarity:.0%}{suffix}")


def _deployment_signal(incident: Incident, findings: list[AgentFinding]) -> _EvidenceSignal | None:
    finding = next((item for item in findings if item.status == "DEPLOYMENT_FOUND"), None)
    if finding is None:
        return None
    relevance = _evidence_relevance(incident, finding.evidence)
    age_band = _deployment_age_band(finding.evidence)
    if relevance:
        strengths = {"recent": (7, 5), "limited": (6, 3), "old": (4, 2), "unknown": (5, 3)}
        detail = f"deployment change matches incident terms; timing is {age_band}"
    else:
        strengths = {"recent": (4, 2), "limited": (3, 1), "old": (1, 0), "unknown": (2, 1)}
        detail = f"deployment was found but change relevance is limited; timing is {age_band}"
    rca_strength, recommendation_strength = strengths[age_band]
    return _EvidenceSignal("deployment", "deployment evidence", rca_strength, recommendation_strength, detail)


def _log_signal(logs: str | None) -> _EvidenceSignal | None:
    if not logs or not _USEFUL_LOG.search(logs):
        return None
    specific = bool(_SPECIFIC_LOG.search(logs))
    detail = "logs contain a specific exception/error" if specific else "logs contain diagnostic failure signals"
    return _EvidenceSignal("logs", "diagnostic logs", 7 if specific else 6, 3, detail)


def _code_signal(findings: list[AgentFinding]) -> _EvidenceSignal | None:
    finding = next((item for item in findings if item.status in {"RCA_COMPLETED", "CODE_EVIDENCE_FOUND", "CODE_IMPACT_FOUND"}), None)
    if finding is None:
        return None
    completed_rca = finding.status == "RCA_COMPLETED"
    return _EvidenceSignal("code", "code/RCA evidence", 8 if completed_rca else 7, 6 if completed_rca else 5, f"{finding.agent_name} completed relevant {finding.status.casefold()} evidence")


def _confluence_signal(incident: Incident, sources: list[ConfluenceSource]) -> _EvidenceSignal | None:
    relevant_count = 0
    solution_count = 0
    for source in sources:
        summary = (source.issue_summary or "").strip()
        if not summary or any(term in summary.casefold() for term in _IRRELEVANT_TERMS) or not _evidence_relevance(incident, summary):
            continue
        relevant_count += 1
        if _contains_any(summary, _SOLUTION_TERMS):
            solution_count += 1
    if not relevant_count:
        return None
    detail = f"{relevant_count} incident-relevant page(s); {solution_count} contain supported investigation or solution guidance"
    return _EvidenceSignal("confluence", "Confluence evidence", 6 if relevant_count >= 2 else 5, 7 if solution_count else 4, detail)


def _score_dimension(signals: list[_EvidenceSignal], attribute: str, fallback: str) -> tuple[int, str]:
    eligible = [signal for signal in signals if getattr(signal, attribute) > 0]
    if not eligible:
        return 0, fallback
    strongest = max(eligible, key=lambda signal: getattr(signal, attribute))
    strongest_score = getattr(strongest, attribute)
    supporters = [signal for signal in eligible if signal.category != strongest.category and getattr(signal, attribute) >= 3]
    supporters.sort(key=lambda signal: getattr(signal, attribute), reverse=True)
    bonus = min(len(supporters), 2)
    final_score = min(strongest_score + bonus, 10)
    reason = f"Strongest evidence: {strongest.label} ({strongest_score}/8) - {strongest.detail}."
    if supporters:
        support_text = ", ".join(f"{signal.label} ({getattr(signal, attribute)}/8)" for signal in supporters[:2])
        reason += f" +{bonus} independent support from {support_text}."
    return final_score, f"{reason} Final score: {final_score}/10."


def _deployment_age_band(evidence: str) -> str:
    match = _DEPLOYED_AT.search(evidence)
    if not match:
        return "unknown"
    try:
        deployed_at = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        if deployed_at.tzinfo is None:
            deployed_at = deployed_at.replace(tzinfo=UTC)
    except ValueError:
        return "unknown"
    age = datetime.now(UTC) - deployed_at.astimezone(UTC)
    if timedelta(0) <= age <= timedelta(days=3):
        return "recent"
    if timedelta(0) <= age <= timedelta(days=30):
        return "limited"
    return "old"


def _evidence_relevance(incident: Incident, evidence: str) -> bool:
    lowered = evidence.casefold()
    if incident.service and incident.service.casefold() in lowered:
        return True
    incident_terms = _terms(f"{incident.title} {incident.symptoms}")
    return len(incident_terms & _terms(evidence)) >= 2


def _terms(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9][a-z0-9_-]{3,}", value.casefold()) if token not in _STOP_WORDS}


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(term in lowered for term in terms)

"""Build verified, MCP-discovered code-change proposals for initial analysis."""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any

from agents.chat_evidence_agents import ChatEvidenceAgents
from domain.models import AgentFinding, CodeChangeProposal, Incident
from services.github_draft_pr import DraftPrError, GithubDraftPrService, apply_unified_patch


logger = logging.getLogger(__name__)


class CodeChangeProposalService:
    """Turns an advisor intent into one repository-verified replacement file."""

    def __init__(
        self,
        github_owner: str | None,
        github: GithubDraftPrService,
        evidence_agents: ChatEvidenceAgents,
    ) -> None:
        self._github_owner = github_owner
        self._github = github
        self._evidence_agents = evidence_agents

    async def propose(
        self,
        incident: Incident,
        change_intent: str | None,
        findings: list[AgentFinding],
    ) -> CodeChangeProposal | None:
        if not self._github_owner or not change_intent or not change_intent.strip():
            return None

        try:
            candidate = self._candidate(
                await self._evidence_agents.propose_code_change(
                    self._github_owner,
                    incident,
                    change_intent.strip(),
                    findings,
                )
            )
            if candidate is None or not self._is_owned_repository(candidate["repository"]):
                return None

            github = self._github.for_repository(candidate["repository"])
            source = await github.read_file(candidate["filePath"], "main")
            proposed_code = candidate["proposedCode"]
            if source["baseBranch"] != "main" or proposed_code == source["content"]:
                return None

            patch = "".join(
                difflib.unified_diff(
                    source["content"].splitlines(keepends=True),
                    proposed_code.splitlines(keepends=True),
                    fromfile=f"a/{candidate['filePath']}",
                    tofile=f"b/{candidate['filePath']}",
                )
            )
            if "@@ " not in patch or apply_unified_patch(source["content"], patch) != proposed_code:
                return None

            return CodeChangeProposal(
                repository=candidate["repository"],
                filePath=candidate["filePath"],
                baseBranch="main",
                proposedCode=proposed_code,
                codeChanges=patch,
            )
        except Exception as error:
            logger.warning("Unable to create a verified GitHub MCP code proposal: %s", error)
            return None

    def _is_owned_repository(self, repository: str) -> bool:
        owner, separator, name = repository.strip().partition("/")
        return bool(separator and name and self._github_owner and owner.casefold() == self._github_owner.casefold())

    @staticmethod
    def _candidate(response: str) -> dict[str, str] | None:
        try:
            payload: Any = json.loads(_json_content(response))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        repository = payload.get("repository")
        file_path = payload.get("filePath")
        base_branch = payload.get("baseBranch")
        proposed_code = payload.get("proposedCode")
        if (
            not isinstance(repository, str)
            or not isinstance(file_path, str)
            or base_branch != "main"
            or not isinstance(proposed_code, str)
            or not repository.strip()
            or not file_path.strip()
            or not proposed_code
        ):
            return None
        return {
            "repository": repository.strip(),
            "filePath": file_path.strip(),
            "proposedCode": proposed_code,
        }


def _json_content(response: str) -> str:
    stripped = response.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped.split("\n", 1)[1].rsplit("\n", 1)[0]
    return stripped

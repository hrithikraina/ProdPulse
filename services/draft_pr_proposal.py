"""Generate bounded, reviewable patches for incident code suggestions."""

from domain.models import Incident
from services.azure_openai import AzureOpenAIClient
from services.github_draft_pr import DraftPrError, GithubDraftPrService


class DraftPrProposalService:
    def __init__(self, github: GithubDraftPrService, advisor: AzureOpenAIClient) -> None:
        self._github = github
        self._advisor = advisor

    async def preview(self, incident: Incident, code_changes: str, file_path: str, base_branch: str | None = None) -> dict:
        source = await self._github.read_file(file_path, base_branch)
        patch = await self._advisor.generate(
            "Create exactly one conservative unified diff for the selected existing file. "
            "Use only the current file and suggested code below. Preserve unrelated code. "
            "Return only the diff, with --- a/<path>, +++ b/<path>, and valid @@ hunks; no Markdown or prose. "
            f"\n\nINCIDENT\nID: {incident.id}\nTitle: {incident.title}\nService: {incident.service}\nSymptoms: {incident.symptoms}"
            f"\n\nSELECTED PATH\n{file_path}\n\nSUGGESTED CODE\n{code_changes}"
            f"\n\nCURRENT FILE\n{source['content']}"
        )
        if not patch.startswith("--- a/"):
            raise DraftPrError("Unable to generate a safe single-file patch. Please refine the selected file path.")
        preview = await self._github.preview(file_path, patch, source["baseBranch"])
        return {**preview, "patch": patch}

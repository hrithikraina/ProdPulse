import json
from core.config import Settings
from domain.models import Incident
from services.code_change_proposal import CodeChangeProposalService
from services.github_draft_pr import DraftPrError, apply_unified_patch


class FakeEvidenceAgents:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def propose_code_change(self, owner, _incident, change_intent, _findings) -> str:
        self.calls.append((owner, change_intent))
        return self.response


class FakeRepository:
    def __init__(self, content: str, branch: str = "main") -> None:
        self.content = content
        self.branch = branch
        self.calls: list[tuple[str, str]] = []

    async def read_file(self, path: str, branch: str) -> dict[str, str]:
        self.calls.append((path, branch))
        if path == "missing.py":
            raise DraftPrError("The configured repository, branch, or file was not found.")
        return {"repository": "acme/payments", "filePath": path, "baseBranch": self.branch, "content": self.content}


class FakeGithub:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.selected: list[str] = []

    def for_repository(self, repository: str) -> FakeRepository:
        self.selected.append(repository)
        if "/" not in repository:
            raise DraftPrError("Enter a GitHub repository as owner/repository.")
        return self.repository


def incident() -> Incident:
    return Incident(id="INC-1", title="Validation fails", service="payments", severity="P1", symptoms="Payments are rejected", logs="ValidationException")


async def test_mcp_discovers_target_and_server_derives_matching_diff() -> None:
    source = "def validate(value):\n    return value\n"
    proposed = "def validate(value):\n    if value is None:\n        raise ValueError('value is required')\n    return value\n"
    evidence = FakeEvidenceAgents(json.dumps({
        "repository": "acme/payments",
        "filePath": "src/validation.py",
        "baseBranch": "main",
        "proposedCode": proposed,
    }))
    github = FakeGithub(FakeRepository(source))
    service = CodeChangeProposalService("acme", github, evidence)  # type: ignore[arg-type]

    result = await service.propose(incident(), "Reject missing validation input.", [])

    assert result is not None
    assert result.repository == "acme/payments"
    assert result.file_path == "src/validation.py"
    assert result.base_branch == "main"
    assert result.proposed_code == proposed
    assert apply_unified_patch(source, result.code_changes) == proposed
    assert evidence.calls == [("acme", "Reject missing validation input.")]
    assert github.selected == ["acme/payments"]
    assert github.repository.calls == [("src/validation.py", "main")]


async def test_mcp_proposal_rejects_other_owner_invalid_target_and_noop() -> None:
    source = "print('ok')\n"
    for payload in (
        {"repository": "other/payments", "filePath": "src/app.py", "baseBranch": "main", "proposedCode": "print('changed')\n"},
        {"repository": "acme/payments", "filePath": "missing.py", "baseBranch": "main", "proposedCode": "print('changed')\n"},
        {"repository": "acme/payments", "filePath": "src/app.py", "baseBranch": "main", "proposedCode": source},
    ):
        response = json.dumps(payload)
        service = CodeChangeProposalService("acme", FakeGithub(FakeRepository(source)), FakeEvidenceAgents(response))  # type: ignore[arg-type]
        assert await service.propose(incident(), "Change application behavior.", []) is None


def test_settings_uses_github_owner_without_a_repository(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    settings = Settings.from_environment()

    assert settings.github_owner == "acme"
    assert not hasattr(settings, "github_repository")

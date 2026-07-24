"""GitHub REST adapter for user-confirmed, single-file draft pull requests."""

import base64
import re

import httpx


class DraftPrError(RuntimeError):
    pass


class GithubDraftPrService:
    def __init__(self, repository: str | None, token: str | None) -> None:
        self.repository, self.token = repository, token

    def _configured(self) -> None:
        if not self.repository or not self.token:
            raise DraftPrError("Draft PR creation is not configured. Ask an administrator to configure GitHub repository write access.")

    def for_repository(self, repository: str) -> "GithubDraftPrService":
        repository = repository.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise DraftPrError("Enter a GitHub repository as owner/repository.")
        return GithubDraftPrService(repository, self.token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28"}

    async def preview(self, file_path: str, patch: str, base_branch: str | None = None) -> dict:
        self._configured(); _validate_patch_target(patch, file_path)
        branch, source = await self._file(file_path, base_branch)
        return {"repository": self.repository, "baseBranch": branch, "filePath": file_path, "content": apply_unified_patch(source["content"], patch)}

    async def read_file(self, file_path: str, base_branch: str | None = None) -> dict:
        self._configured()
        branch, source = await self._file(file_path, base_branch)
        return {"repository": self.repository, "baseBranch": branch, "filePath": file_path, "content": source["content"]}

    async def create_from_patch(self, incident_id: str, file_path: str, patch: str, title: str, body: str, base_branch: str | None = None) -> dict:
        self._configured(); _validate_patch_target(patch, file_path)
        branch, source = await self._file(file_path, base_branch)
        content = apply_unified_patch(source["content"], patch)
        safe = re.sub(r"[^a-z0-9-]+", "-", incident_id.lower()).strip("-")[:48] or "incident"
        head = f"incident/{safe}-draft"
        async with httpx.AsyncClient(base_url="https://api.github.com", headers=self._headers, timeout=20) as client:
            base = await self._request(client, "GET", f"/repos/{self.repository}/git/ref/heads/{branch}")
            for suffix in range(1, 20):
                candidate = head if suffix == 1 else f"{head}-{suffix}"
                result = await client.post(f"/repos/{self.repository}/git/refs", json={"ref": f"refs/heads/{candidate}", "sha": base["object"]["sha"]})
                if result.status_code == 201:
                    head = candidate; break
                if result.status_code != 422:
                    await self._raise(result)
            else:
                raise DraftPrError("Could not create a unique incident branch. Please try again.")
            await self._request(client, "PUT", f"/repos/{self.repository}/contents/{file_path}", json={"message": title, "content": base64.b64encode(content.encode()).decode(), "branch": head, "sha": source["sha"]})
            pr = await self._request(client, "POST", f"/repos/{self.repository}/pulls", json={"title": title, "body": body, "head": head, "base": branch, "draft": True})
        return {"url": pr["html_url"], "number": pr["number"], "branch": head}

    async def _file(self, path: str, branch: str | None) -> tuple[str, dict]:
        if not path or "\\" in path or path.startswith("/") or ".." in path.split("/"):
            raise DraftPrError("The selected file path is not allowed.")
        async with httpx.AsyncClient(base_url="https://api.github.com", headers=self._headers, timeout=20) as client:
            repo = await self._request(client, "GET", f"/repos/{self.repository}")
            target = branch or repo["default_branch"]
            file = await self._request(client, "GET", f"/repos/{self.repository}/contents/{path}", params={"ref": target})
        if file.get("encoding") != "base64" or "content" not in file:
            raise DraftPrError("The selected file could not be safely read from GitHub.")
        try:
            file["content"] = base64.b64decode(file["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise DraftPrError("The selected file is not a supported UTF-8 text file.") from error
        return target, file

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise DraftPrError("GitHub is currently unavailable. Please try again later.") from error
        if response.is_success:
            return response.json()
        await self._raise(response)
        raise AssertionError("unreachable")

    async def _raise(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise DraftPrError("GitHub denied this action. Verify the server token has repository contents and pull-request write access.")
        if response.status_code == 404:
            raise DraftPrError("The configured repository, branch, or file was not found.")
        raise DraftPrError("GitHub could not complete the draft PR action. Please try again later.")


def apply_unified_patch(content: str, patch: str) -> str:
    _validate_single_file_patch(patch)
    lines, output, position = content.splitlines(keepends=True), [], 0
    if not any(line.startswith("@@ ") for line in patch.splitlines()) or not patch.lstrip().startswith(("--- ", "@@ ")):
        raise DraftPrError("The suggested change is not a valid unified diff.")
    for raw in patch.splitlines(keepends=True):
        if raw.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if not match:
                raise DraftPrError("The suggested patch contains an invalid hunk.")
            start = int(match.group(1)) - 1
            if start < position or start > len(lines):
                raise DraftPrError("The suggested patch cannot be applied to the latest file.")
            output.extend(lines[position:start]); position = start
        elif raw.startswith(("--- ", "+++ ", "\\ No newline")):
            continue
        elif raw.startswith(" "):
            if position >= len(lines) or lines[position] != raw[1:]: raise DraftPrError("The suggested patch conflicts with the latest file.")
            output.append(lines[position]); position += 1
        elif raw.startswith("-"):
            if position >= len(lines) or lines[position] != raw[1:]: raise DraftPrError("The suggested patch conflicts with the latest file.")
            position += 1
        elif raw.startswith("+"):
            output.append(raw[1:])
        elif raw.strip():
            raise DraftPrError("The suggested patch contains unsupported content.")
    output.extend(lines[position:])
    return "".join(output)


def _validate_single_file_patch(patch: str) -> None:
    if not patch.lstrip().startswith(("--- ", "@@ ")):
        raise DraftPrError("The suggested change is not a valid unified diff.")
    headers = [line for line in patch.splitlines() if line.startswith(("--- ", "+++ "))]
    if headers:
        if len(headers) != 2 or not headers[0].startswith("--- ") or not headers[1].startswith("+++ "):
            raise DraftPrError("The suggested patch must modify exactly one file.")
        before, after = headers[0][4:].strip(), headers[1][4:].strip()
        if before == "/dev/null" or after == "/dev/null" or not before.startswith("a/") or not after.startswith("b/") or before[2:] != after[2:] or before[2:].startswith("/") or ".." in before[2:].split("/"):
            raise DraftPrError("The suggested patch must modify exactly one existing file.")


def _validate_patch_target(patch: str, file_path: str) -> None:
    _validate_single_file_patch(patch)
    headers = [line for line in patch.splitlines() if line.startswith(("--- ", "+++ "))]
    if headers and headers[0][4:].strip() != f"a/{file_path}":
        raise DraftPrError("The suggested patch does not match the selected file.")

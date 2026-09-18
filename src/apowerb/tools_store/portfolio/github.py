"""GitHub tools -- read repositories, files, code and pull requests.

Read-only tools for agents working on the user's GitHub repositories, through
the "GitHub" integration of the Integrations page. Nothing here writes to
GitHub: no commit, no comment, no pull request. ``tool_index_repository_docs``
copies a repository's documentation into a RAG knowledge base -- it writes to
the RAG service, never to GitHub.

The token belongs to the user who runs the agent. It is resolved on every call
as a LOCAL value from that user's integration row, never through a
process-global environment variable -- a global races across concurrent
invocations on the single worker (see ``microsoft_auth``, incident 2026-07-03).

The integration may be backed by a GitHub App or an OAuth App:

* A GitHub App only reaches the repositories where it is installed. A
  repository the user can open on github.com may therefore answer 404 here;
  the tools say access may be missing rather than claiming it does not exist.
* A GitHub App may expire user tokens after 8 hours. On a 401 the stored
  refresh token is exchanged once, the rotated pair is persisted on the
  invoker's row, and the call is retried. Without a refresh token, the user is
  asked to reconnect.
"""

import base64
import re
import tempfile
from logging import getLogger
from pathlib import Path
from urllib.parse import quote

import httpx

from apowerb.configs.settings import get_settings
# The module, not the function: importing ``tool_create_knowledge`` here would
# list it a second time, as a GitHub tool (discovery takes every ``tool_*``
# attribute of the module -- which is how it shows up twice in db_to_rag).
from apowerb.tools_store.portfolio import rag as _rag
from apowerb.tools_store.portfolio.integration_status import (
    INTEGRATION_ERROR,
    INTEGRATION_EXPIRED,
    INTEGRATION_MISSING,
    IntegrationStatusError,
)

logger = getLogger(__name__)

_PROVIDER = "github"
_API = "https://api.github.com"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
# Owner (GitHub login rules) / name. Rejects paths, dots-only names and blanks.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.+$)[A-Za-z0-9._-]{1,100}$")
_PR_STATES = ("open", "closed", "all")

# Bounds on what is handed back to the model: a whole lockfile or a 5 000-line
# diff would fill the context and push the question out of it.
_MAX_FILE_CHARS = 60_000
_MAX_PATCH_CHARS = 4_000
_MAX_DIFF_FILES = 50
_MAX_BODY_CHARS = 8_000

# Repository documentation indexed into RAG: prose, not code, and never the
# dependencies vendored next to it.
_DOC_EXTENSIONS = ".md,.mdx,.rst,.txt"
_EXCLUDED_DIRS = frozenset({
    "node_modules", "vendor", ".git", ".venv", "venv", "site-packages",
    "dist", "build", "__pycache__", ".next", "coverage",
})
_MAX_DOC_BYTES = 300_000
_MAX_INDEX_FILES = 200


# ---------------------------------------------------------------- auth

def _missing() -> IntegrationStatusError:
    return IntegrationStatusError(
        code=INTEGRATION_MISSING,
        provider=_PROVIDER,
        message=(
            "GitHub is not connected. The user must connect their GitHub "
            "account from the Integrations page first."
        ),
    )


def _expired() -> IntegrationStatusError:
    return IntegrationStatusError(
        code=INTEGRATION_EXPIRED,
        provider=_PROVIDER,
        message=(
            "The GitHub token has expired or been revoked. The user must "
            "reconnect their GitHub account from the Integrations page."
        ),
    )


def _stored_tokens() -> dict:
    """The invoker's GitHub tokens, resolved for this call only."""
    from apowerb.integrations.helpers import fetch_integration_configs

    try:
        configs = fetch_integration_configs(_PROVIDER)
    except RuntimeError as exc:
        # "No github integration found for user_id=…" — never connected.
        raise _missing() from exc
    if not configs.get("access_token"):
        raise _missing()
    return configs


def _refresh(refresh_token: str | None) -> str:
    """Exchange the refresh token for a new access token and persist the pair."""
    if not refresh_token:
        raise _expired()

    settings = get_settings()
    resp = httpx.post(
        _TOKEN_URL,
        data={
            "client_id": settings.github_integration_client_id,
            "client_secret": settings.github_integration_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    # GitHub answers 200 even when it refuses the refresh, with an ``error``.
    data = resp.json() if resp.status_code == 200 else {}
    access_token = data.get("access_token")
    if not access_token:
        error = data.get("error", f"HTTP {resp.status_code}")
        logger.warning("GitHub token refresh refused: %s", error)
        if error in ("bad_refresh_token", "invalid_grant", "unauthorized"):
            raise _expired()
        raise IntegrationStatusError(
            code=INTEGRATION_ERROR,
            provider=_PROVIDER,
            message=f"GitHub token refresh failed ({error}). This is not a reconnect issue.",
        )

    try:
        from apowerb.integrations.helpers import persist_refreshed_tokens

        # GitHub rotates the refresh token: the old one is now dead.
        persist_refreshed_tokens(
            _PROVIDER,
            access_token=access_token,
            refresh_token=data.get("refresh_token"),
        )
    except Exception as exc:
        logger.error(
            "GitHub: persist of rotated tokens FAILED -- the stored refresh "
            "token may be stale (reconnect may be required): %s",
            exc,
        )
    return access_token


def _get(path: str, params: dict | None = None, accept: str | None = None) -> httpx.Response:
    """GET on the GitHub API as the invoker, refreshing the token once on 401."""
    configs = _stored_tokens()

    def _call(token: str) -> httpx.Response:
        headers = {**_BASE_HEADERS, "Authorization": f"Bearer {token}"}
        if accept:
            headers["Accept"] = accept
        return httpx.get(f"{_API}{path}", headers=headers, params=params, timeout=30)

    resp = _call(configs["access_token"])
    if resp.status_code == 401:
        resp = _call(_refresh(configs.get("refresh_token")))
        if resp.status_code == 401:
            raise _expired()
    return resp


# ---------------------------------------------------------------- results

def _error(message: str, retry: bool = False) -> dict:
    return {"status": "error", "message": message, "retry": retry}


def _api_error(resp: httpx.Response, what: str) -> dict:
    if resp.status_code == 404:
        return _error(
            f"{what} was not found, or the connected GitHub account has no access "
            "to it. If it exists, the user must grant access to this repository "
            "on GitHub (e.g. install the app on it). Do not retry."
        )
    if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        return _error("GitHub rate limit reached. Tell the user to try again in a few minutes.")
    return _error(
        f"GitHub API error (HTTP {resp.status_code}): {resp.text[:300]}. "
        "Do not retry -- inform the user."
    )


def _run(action) -> dict:
    """Common error envelope: integration statuses, timeouts, the rest."""
    try:
        return action()
    except IntegrationStatusError as exc:
        return exc.as_tool_result()
    except httpx.TimeoutException:
        return _error("GitHub did not answer in time. You may retry once.", retry=True)
    except Exception as exc:
        logger.exception("GitHub tool failed")
        return _error(f"GitHub request failed: {exc}. Do not retry -- inform the user.")


def _check_repository(repository: str) -> dict | None:
    if not isinstance(repository, str) or not _REPOSITORY_RE.match(repository.strip()):
        return _error(
            f"Invalid repository {repository!r}: expected 'owner/name', "
            "e.g. 'acme/data-models'. Use tool_list_repositories to find it."
        )
    return None


def _clamp(value: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low


# ---------------------------------------------------------------- tools

def tool_list_repositories(query: str = "", max_results: int = 30) -> dict:
    """List the GitHub repositories the user has connected, most recent first.

    Use this first to find the exact ``owner/name`` of a repository before
    reading its files or pull requests.

    Args:
        query: Optional text to keep only repositories whose name or
            description contains it (case-insensitive).
        max_results: Maximum number of repositories to return (1-100).
            Defaults to 30.

    Returns:
        dict with keys: status, repositories (full_name, private, description,
        default_branch, updated_at, url), total.
    """

    def _action():
        limit = _clamp(max_results, 1, 100)
        resp = _get("/user/repos", params={"per_page": "100", "sort": "updated"})
        if resp.status_code >= 400:
            return _api_error(resp, "The repository list")

        needle = (query or "").strip().lower()
        repos = []
        for r in resp.json() or []:
            text = f"{r.get('full_name', '')} {r.get('description') or ''}".lower()
            if needle and needle not in text:
                continue
            repos.append({
                "full_name": r.get("full_name"),
                "private": bool(r.get("private")),
                "description": r.get("description") or "",
                "default_branch": r.get("default_branch"),
                "updated_at": r.get("updated_at"),
                "url": r.get("html_url"),
            })
            if len(repos) >= limit:
                break
        return {"status": "success", "repositories": repos, "total": len(repos)}

    return _run(_action)


def tool_read_file(repository: str, path: str = "", ref: str = "") -> dict:
    """Read a file, or list a folder, in a GitHub repository.

    Args:
        repository: The repository as ``owner/name`` (e.g. "acme/data-models").
        path: Path inside the repository, e.g. "models/sales.sql". Leave empty
            to list the repository root.
        ref: Optional branch, tag or commit SHA. Defaults to the default branch.

    Returns:
        For a file: status, type "file", path, content, truncated, size, url.
        A binary file is described (binary: true) but not returned.
        For a folder: status, type "directory", entries (name, path, type, size).
    """
    invalid = _check_repository(repository)
    if invalid:
        return invalid

    def _action():
        clean_path = quote((path or "").strip("/"), safe="/")
        params = {"ref": ref} if ref else None
        resp = _get(f"/repos/{repository.strip()}/contents/{clean_path}", params=params)
        if resp.status_code >= 400:
            return _api_error(resp, f"'{path or '/'}' in {repository}")

        data = resp.json()
        if isinstance(data, list):
            entries = [
                {"name": e.get("name"), "path": e.get("path"), "type": e.get("type"), "size": e.get("size")}
                for e in data
            ]
            return {"status": "success", "type": "directory", "path": path or "/", "entries": entries}

        if data.get("type") != "file":
            return _error(f"'{path}' is a {data.get('type')}, not a file or folder. Do not retry.")

        result = {
            "status": "success",
            "type": "file",
            "path": data.get("path"),
            "size": data.get("size"),
            "url": data.get("html_url"),
        }
        if data.get("encoding") != "base64" or not data.get("content"):
            # GitHub omits the content of files above 1 MB.
            result.update(binary=False, content="", truncated=True,
                          note="The file is too large for GitHub to return its content.")
            return result

        raw = base64.b64decode(data["content"])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            result.update(binary=True, note="Binary file: its content cannot be shown as text.")
            return result

        result.update(
            binary=False,
            content=text[:_MAX_FILE_CHARS],
            truncated=len(text) > _MAX_FILE_CHARS,
        )
        return result

    return _run(_action)


def tool_search_code(query: str, repository: str = "", max_results: int = 20) -> dict:
    """Search code in the user's GitHub repositories.

    Args:
        query: Words or identifiers to find, e.g. "revenue_by_region".
        repository: Optional ``owner/name`` to search a single repository.
            Strongly recommended: a search across every repository is slower
            and noisier.
        max_results: Maximum number of matches to return (1-50). Defaults to 20.

    Returns:
        dict with keys: status, results (repository, path, url, fragments),
        total_count.
    """
    if not (query or "").strip():
        return _error("An empty search matches nothing: give words to look for.")
    if repository:
        invalid = _check_repository(repository)
        if invalid:
            return invalid

    def _action():
        q = query.strip() + (f" repo:{repository.strip()}" if repository else "")
        resp = _get(
            "/search/code",
            params={"q": q, "per_page": str(_clamp(max_results, 1, 50))},
            accept="application/vnd.github.text-match+json",
        )
        if resp.status_code >= 400:
            return _api_error(resp, "The code search")

        data = resp.json() or {}
        results = [
            {
                "repository": (item.get("repository") or {}).get("full_name"),
                "path": item.get("path"),
                "url": item.get("html_url"),
                "fragments": [m.get("fragment") for m in item.get("text_matches") or [] if m.get("fragment")],
            }
            for item in data.get("items") or []
        ]
        return {"status": "success", "results": results, "total_count": data.get("total_count", len(results))}

    return _run(_action)


def tool_list_pull_requests(repository: str, state: str = "open", max_results: int = 20) -> dict:
    """List pull requests of a GitHub repository, most recently updated first.

    Args:
        repository: The repository as ``owner/name``.
        state: "open" (default), "closed" or "all".
        max_results: Maximum number of pull requests to return (1-100).
            Defaults to 20.

    Returns:
        dict with keys: status, pull_requests (number, title, state, draft,
        author, head, base, created_at, updated_at, url), total.
    """
    invalid = _check_repository(repository)
    if invalid:
        return invalid
    if state not in _PR_STATES:
        return _error(f"Invalid state {state!r}: use one of {', '.join(_PR_STATES)}.")

    def _action():
        resp = _get(
            f"/repos/{repository.strip()}/pulls",
            params={
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": str(_clamp(max_results, 1, 100)),
            },
        )
        if resp.status_code >= 400:
            return _api_error(resp, f"The pull requests of {repository}")

        prs = [
            {
                "number": p.get("number"),
                "title": p.get("title"),
                "state": p.get("state"),
                "draft": bool(p.get("draft")),
                "author": (p.get("user") or {}).get("login"),
                "head": (p.get("head") or {}).get("ref"),
                "base": (p.get("base") or {}).get("ref"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
                "url": p.get("html_url"),
            }
            for p in resp.json() or []
        ]
        return {"status": "success", "pull_requests": prs, "total": len(prs)}

    return _run(_action)


def tool_get_pull_request(repository: str, number: int, include_diff: bool = True) -> dict:
    """Read one pull request: description, figures and, optionally, its diff.

    Use this to summarise or review a pull request.

    Args:
        repository: The repository as ``owner/name``.
        number: The pull request number, e.g. 12.
        include_diff: Whether to include the changed files and their patches
            (bounded in size). Defaults to True.

    Returns:
        dict with keys: status, pull_request (number, title, body, state, merged,
        author, head, base, additions, deletions, changed_files, url) and, with
        include_diff, files (filename, status, additions, deletions, patch,
        patch_truncated) and files_truncated.
    """
    invalid = _check_repository(repository)
    if invalid:
        return invalid
    try:
        number = int(number)
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        return _error("The pull request number must be a positive integer, e.g. 12.")

    def _action():
        base = f"/repos/{repository.strip()}/pulls/{number}"
        resp = _get(base)
        if resp.status_code >= 400:
            return _api_error(resp, f"Pull request #{number} of {repository}")

        p = resp.json() or {}
        body = p.get("body") or ""
        result = {
            "status": "success",
            "pull_request": {
                "number": p.get("number"),
                "title": p.get("title"),
                "body": body[:_MAX_BODY_CHARS],
                "state": p.get("state"),
                "merged": bool(p.get("merged")),
                "author": (p.get("user") or {}).get("login"),
                "head": (p.get("head") or {}).get("ref"),
                "base": (p.get("base") or {}).get("ref"),
                "additions": p.get("additions"),
                "deletions": p.get("deletions"),
                "changed_files": p.get("changed_files"),
                "url": p.get("html_url"),
            },
        }
        if not include_diff:
            return result

        files_resp = _get(f"{base}/files", params={"per_page": str(_MAX_DIFF_FILES)})
        if files_resp.status_code >= 400:
            return _api_error(files_resp, f"The files of pull request #{number}")

        files = []
        for f in files_resp.json() or []:
            patch = f.get("patch") or ""
            files.append({
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": patch[:_MAX_PATCH_CHARS],
                "patch_truncated": len(patch) > _MAX_PATCH_CHARS,
            })
        result["files"] = files
        result["files_truncated"] = (p.get("changed_files") or 0) > len(files)
        return result

    return _run(_action)


def tool_index_repository_docs(
    repository: str,
    knowledge_name: str = "",
    path: str = "",
    ref: str = "",
    extensions: str = _DOC_EXTENSIONS,
    max_files: int = 50,
    wait_for_completion: bool = True,
) -> dict:
    """Index a GitHub repository's documentation into a RAG knowledge base.

    Collects the documentation files of the repository (Markdown, reST, text),
    then creates a knowledge base the agent can query afterwards with
    ``tool_search_knowledge``. Each document starts with its GitHub URL, so
    answers can cite where they come from. Code, binaries and vendored
    dependencies (node_modules, vendor, .venv…) are left out.

    Args:
        repository: The repository as ``owner/name`` (e.g. "acme/data-models").
        knowledge_name: Name of the knowledge base. Defaults to
            "GitHub owner/name".
        path: Optional folder to index only, e.g. "docs". Defaults to the
            whole repository.
        ref: Optional branch, tag or commit SHA. Defaults to the default branch.
        extensions: Comma-separated file extensions to index.
            Defaults to ".md,.mdx,.rst,.txt".
        max_files: Maximum number of documents to index (1-200). Defaults to 50.
        wait_for_completion: Wait until indexing finishes. Defaults to True.

    Returns:
        dict with keys: status, knowledge_id, repository, ref, indexed_files,
        skipped (counts by reason: excluded_folder, other_extension, too_large,
        binary, unreadable, over_limit), tree_truncated, message.
    """
    invalid = _check_repository(repository)
    if invalid:
        return invalid

    def _action():
        repo = repository.strip()
        branch = (ref or "").strip()
        if not branch:
            meta = _get(f"/repos/{repo}")
            if meta.status_code >= 400:
                return _api_error(meta, f"Repository {repo}")
            branch = (meta.json() or {}).get("default_branch") or "main"

        tree_resp = _get(f"/repos/{repo}/git/trees/{quote(branch, safe='')}", params={"recursive": "1"})
        if tree_resp.status_code >= 400:
            return _api_error(tree_resp, f"The file tree of {repo}@{branch}")
        tree = tree_resp.json() or {}

        wanted = tuple(e.strip().lower() for e in (extensions or _DOC_EXTENSIONS).split(",") if e.strip())
        prefix = (path or "").strip("/")
        skipped = dict.fromkeys(
            ("excluded_folder", "other_extension", "too_large", "binary", "unreadable", "over_limit"), 0,
        )
        candidates = []
        for entry in tree.get("tree") or []:
            if entry.get("type") != "blob":
                continue
            file_path = entry.get("path") or ""
            if prefix and not (file_path == prefix or file_path.startswith(prefix + "/")):
                continue
            if any(part in _EXCLUDED_DIRS for part in file_path.split("/")[:-1]):
                skipped["excluded_folder"] += 1
            elif not file_path.lower().endswith(wanted):
                skipped["other_extension"] += 1
            elif (entry.get("size") or 0) > _MAX_DOC_BYTES:
                skipped["too_large"] += 1
            else:
                candidates.append(file_path)
        candidates.sort()

        if not candidates:
            return {
                "status": "empty",
                "repository": repo,
                "ref": branch,
                "indexed_files": [],
                "skipped": skipped,
                "tree_truncated": bool(tree.get("truncated")),
                "message": f"No documentation file matching {', '.join(wanted)} in {repo}@{branch}. Nothing was indexed.",
            }

        limit = _clamp(max_files, 1, _MAX_INDEX_FILES)
        indexed: list[str] = []
        with tempfile.TemporaryDirectory(prefix="github-docs-") as tmp:
            written: list[str] = []
            for position, file_path in enumerate(candidates):
                if len(indexed) >= limit:
                    skipped["over_limit"] = len(candidates) - position
                    break
                resp = _get(f"/repos/{repo}/contents/{quote(file_path, safe='/')}", params={"ref": branch})
                data = resp.json() if resp.status_code < 400 else {}
                if not isinstance(data, dict) or data.get("encoding") != "base64" or not data.get("content"):
                    skipped["unreadable"] += 1
                    continue
                try:
                    text = base64.b64decode(data["content"]).decode("utf-8")
                except UnicodeDecodeError:
                    skipped["binary"] += 1
                    continue
                source = f"https://github.com/{repo}/blob/{branch}/{file_path}"
                # Flattened name: two README.md in different folders must not collide.
                target = Path(tmp) / file_path.replace("/", "__")
                target.write_text(f"Source: {source}\n\n{text}", encoding="utf-8")
                written.append(str(target))
                indexed.append(file_path)

            if not written:
                return {
                    "status": "empty",
                    "repository": repo,
                    "ref": branch,
                    "indexed_files": [],
                    "skipped": skipped,
                    "tree_truncated": bool(tree.get("truncated")),
                    "message": f"No readable documentation file in {repo}@{branch}. Nothing was indexed.",
                }

            # Upload happens inside the call: the temporary copies can go right after.
            rag_result = _rag.tool_create_knowledge(
                name=knowledge_name.strip() or f"GitHub {repo}",
                description=(
                    f"Documentation of the GitHub repository {repo} ({branch}"
                    + (f", folder {prefix}" if prefix else "")
                    + ")"
                ),
                files=written,
                wait_for_completion=wait_for_completion,
            )

        result = {
            "repository": repo,
            "ref": branch,
            "indexed_files": indexed,
            "skipped": skipped,
            "tree_truncated": bool(tree.get("truncated")),
        }
        if rag_result.get("status") == "error":
            result.update(
                status="error",
                retry=False,
                message=f"The documents were collected but indexing failed: {rag_result.get('message')}",
            )
            return result

        result.update(
            status=rag_result.get("status"),
            knowledge_id=rag_result.get("knowledge_id"),
            message=(
                f"Indexed {len(indexed)} document(s) from {repo}@{branch}. "
                "Query them with tool_search_knowledge and this knowledge_id."
            ),
        )
        if result["tree_truncated"]:
            result["message"] += " GitHub truncated the file tree: some files may be missing; index a folder instead."
        return result

    return _run(_action)


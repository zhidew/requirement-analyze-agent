import re
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

from services.db_service import metadata_db
from services.git_utils import (
    build_git_auth_header,
    build_noninteractive_git_command,
    git_noninteractive_env,
)
from services.kb_indexer import KnowledgeBaseError


BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"
DEFAULT_GIT_TIMEOUT_SECONDS = 120


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _slugify_path_component(value: str, default: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or default).strip())
    normalized = normalized.strip("-.")
    return normalized or default


def _slugify_branch(branch: str) -> str:
    return _slugify_path_component(branch, "main")


def _resolve_effective_root(repo_root: Path, repo_path: str | None) -> Path:
    if not repo_path:
        return repo_root.resolve()

    candidate = (repo_root / repo_path).resolve()
    repo_root_resolved = repo_root.resolve()
    try:
        candidate.relative_to(repo_root_resolved)
    except ValueError as exc:
        raise KnowledgeBaseError("Git knowledge base repo_path must stay inside the repository root.") from exc
    if not candidate.exists():
        raise KnowledgeBaseError(f"Git knowledge base repo_path not found: {repo_path}")
    if not candidate.is_dir():
        raise KnowledgeBaseError(f"Git knowledge base repo_path is not a directory: {repo_path}")
    return candidate


def _run_git(args: list[str], *, url: str, username: str | None, token: str | None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    extra_configs = []
    auth_header = build_git_auth_header(username, token, url)
    if auth_header:
        extra_configs.append(f"http.extraHeader={auth_header}")
    return subprocess.run(
        build_noninteractive_git_command(args, extra_configs=extra_configs),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=git_noninteractive_env(),
    )


def sync_git_knowledge_base(project_id: str, kb_config: Dict[str, Any]) -> Dict[str, Any]:
    kb_id = str(kb_config.get("id") or "").strip()
    if not kb_id:
        raise KnowledgeBaseError("Git knowledge base is missing id.")

    source_repository_id = kb_config.get("source_repository_id")
    source_repo = None
    if source_repository_id:
        source_repo = metadata_db.get_repository(project_id, source_repository_id, include_secrets=True)
        if source_repo is None:
            raise KnowledgeBaseError(f"Source repository not found for source_repository_id='{source_repository_id}'.")

    url = kb_config.get("url") or (source_repo or {}).get("url")
    if not url:
        raise KnowledgeBaseError(f"Git knowledge base '{kb_id}' is missing url.")

    branch = kb_config.get("branch") or (source_repo or {}).get("branch") or "main"
    username = kb_config.get("username") or (source_repo or {}).get("username")
    token = kb_config.get("token") or (source_repo or {}).get("token")
    target_root = (PROJECTS_DIR / project_id / "knowledge_repos").resolve()
    target_dir = (target_root / f"{_slugify_path_component(kb_id, 'kb')}--{_slugify_branch(branch)}").resolve()
    try:
        target_dir.relative_to(target_root)
    except ValueError as exc:
        raise KnowledgeBaseError("Git knowledge base target directory must stay inside knowledge_repos.") from exc
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    git_timeout = _env_int("KB_GIT_SYNC_TIMEOUT_SECONDS", DEFAULT_GIT_TIMEOUT_SECONDS)

    try:
        if target_dir.exists():
            _run_git(["-C", str(target_dir), "fetch", "origin", "--prune"], url=url, username=username, token=token, timeout=git_timeout)
            checkout = subprocess.run(
                build_noninteractive_git_command(["-C", str(target_dir), "checkout", branch]),
                check=False,
                capture_output=True,
                text=True,
                timeout=git_timeout,
                env=git_noninteractive_env(),
            )
            if checkout.returncode != 0:
                _run_git(
                    ["-C", str(target_dir), "checkout", "-B", branch, f"origin/{branch}"],
                    url=url,
                    username=username,
                    token=token,
                    timeout=git_timeout,
                )
            _run_git(["-C", str(target_dir), "pull", "--ff-only", "origin", branch], url=url, username=username, token=token, timeout=git_timeout)
        else:
            _run_git(["clone", "--branch", branch, url, str(target_dir)], url=url, username=username, token=token, timeout=git_timeout)

        commit_hash = _run_git(["-C", str(target_dir), "rev-parse", "HEAD"], url=url, username=username, token=token, timeout=git_timeout).stdout.strip()
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or exc.stdout or str(exc)).strip()
        raise KnowledgeBaseError(f"Failed to sync Git knowledge base '{kb_id}': {error}") from exc
    except subprocess.TimeoutExpired as exc:
        raise KnowledgeBaseError(f"Timed out syncing Git knowledge base '{kb_id}'.") from exc

    effective_root = _resolve_effective_root(target_dir, kb_config.get("repo_path"))
    if kb_config.get("local_path") != str(target_dir):
        metadata_db.upsert_knowledge_base(
            project_id,
            {
                **kb_config,
                "local_path": str(target_dir),
            },
        )

    return {
        "project_id": project_id,
        "kb_id": kb_id,
        "local_path": str(target_dir),
        "effective_root": str(effective_root),
        "branch": branch,
        "commit_hash": commit_hash,
    }

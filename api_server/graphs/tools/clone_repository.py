import subprocess
import re
from pathlib import Path
from typing import Any, Dict

from services.db_service import metadata_db
from services.git_utils import (
    build_noninteractive_git_command,
    build_git_url_with_credentials,
    git_noninteractive_env,
)


BASE_DIR = Path(__file__).resolve().parents[3]
PROJECTS_DIR = BASE_DIR / "projects"


def _slugify_branch(branch: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", branch.strip())
    normalized = normalized.strip("-.")
    return normalized or "default"


def _resolve_repo_cache_dir(project_id: str, repo_id: str, branch: str) -> tuple[Path, str]:
    repos_root = PROJECTS_DIR / project_id / "cloned_repos"
    branch_dir = repos_root / f"{repo_id}--{_slugify_branch(branch)}"
    legacy_dir = repos_root / repo_id

    repos_root.mkdir(parents=True, exist_ok=True)
    if branch_dir.exists():
        return branch_dir, "branch_scoped"
    if legacy_dir.exists():
        try:
            legacy_dir.rename(branch_dir)
            return branch_dir, "migrated_legacy"
        except OSError:
            return legacy_dir, "legacy"
    return branch_dir, "branch_scoped"


def _checkout_and_update_branch(target_dir: Path, branch: str) -> None:
    env = git_noninteractive_env()
    subprocess.run(
        build_noninteractive_git_command(["-C", str(target_dir), "fetch", "origin", "--prune"]),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    checkout = subprocess.run(
        build_noninteractive_git_command(["-C", str(target_dir), "checkout", branch]),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if checkout.returncode != 0:
        subprocess.run(
            build_noninteractive_git_command(["-C", str(target_dir), "checkout", "-B", branch, f"origin/{branch}"]),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    subprocess.run(
        build_noninteractive_git_command(["-C", str(target_dir), "pull", "--ff-only", "origin", branch]),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _resolve_project_id(root_dir: Path, tool_input: Dict[str, Any]) -> str:
    project_id = tool_input.get("project_id")
    if isinstance(project_id, str) and project_id.strip():
        return project_id.strip()

    try:
        relative = root_dir.resolve().relative_to(PROJECTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("`project_id` is required when `root_dir` is outside the projects directory.") from exc

    parts = relative.parts
    if not parts:
        raise ValueError("Unable to infer project ID from root_dir.")
    return parts[0]


def _inject_credentials(url: str, username: str | None, token: str | None) -> str:
    return build_git_url_with_credentials(url, username, token, default_username="token")


def clone_repository(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    repo_id = tool_input.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ValueError("`repo_id` must be a non-empty string.")

    project_id = _resolve_project_id(root_dir, tool_input)
    repo_config = metadata_db.get_repository(project_id, repo_id, include_secrets=True)
    if repo_config is None and not tool_input.get("repo_url"):
        raise ValueError(f"Repository config not found for repo_id='{repo_id}'.")

    repo_url = tool_input.get("repo_url") or repo_config["url"]
    branch = tool_input.get("branch") or repo_config.get("branch") or "main"
    depth = tool_input.get("depth")
    if depth is not None:
        try:
            depth = int(depth)
        except (TypeError, ValueError) as exc:
            raise ValueError("`depth` must be an integer when provided.") from exc
        if depth < 1:
            raise ValueError("`depth` must be >= 1.")

    target_dir, cache_mode = _resolve_repo_cache_dir(project_id, repo_id, branch)
    if target_dir.exists():
        _checkout_and_update_branch(target_dir, branch)
    else:
        command_args = ["clone", "--branch", branch]
        if depth:
            command_args.extend(["--depth", str(depth)])
        clone_url = _inject_credentials(
            repo_url,
            repo_config.get("username") if repo_config else None,
            repo_config.get("token") if repo_config else None,
        )
        command_args.extend([clone_url, str(target_dir)])
        subprocess.run(
            build_noninteractive_git_command(command_args),
            check=True,
            capture_output=True,
            text=True,
            env=git_noninteractive_env(),
        )

    commit_hash = subprocess.run(
        build_noninteractive_git_command(["-C", str(target_dir), "rev-parse", "HEAD"]),
        check=True,
        capture_output=True,
        text=True,
        env=git_noninteractive_env(),
    ).stdout.strip()
    file_count = sum(1 for path in target_dir.rglob("*") if path.is_file())
    local_path = str(target_dir)
    project_relative_path = Path("cloned_repos") / target_dir.name

    if repo_config is not None and repo_config.get("local_path") != local_path:
        metadata_db.upsert_repository(
            project_id,
            {
                **repo_config,
                "local_path": local_path,
            },
        )

    return {
        "project_id": project_id,
        "repo_id": repo_id,
        "local_path": local_path,
        "project_relative_path": project_relative_path.as_posix(),
        "search_hint": project_relative_path.as_posix(),
        "cache_scope": "project_shared",
        "cache_mode": cache_mode,
        "branch": branch,
        "commit_hash": commit_hash,
        "file_count": file_count,
    }


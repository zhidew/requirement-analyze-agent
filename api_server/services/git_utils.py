"""Shared helpers for constructing safe, non-interactive git commands."""
import os
from base64 import b64encode
from typing import Iterable, Mapping
from urllib.parse import quote, urlsplit, urlunsplit


NONINTERACTIVE_GIT_CONFIGS = (
    "credential.helper=",
    "core.askPass=",
    "credential.interactive=never",
)


def default_git_username(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "x-access-token"
    if host == "gitlab.com" or host.endswith(".gitlab.com"):
        return "oauth2"
    return "git"


def build_git_url_with_credentials(
    url: str,
    username: str | None,
    token: str | None,
    *,
    default_username: str | None = None,
) -> str:
    if not token:
        return url

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return url

    auth_username = username or default_username or default_git_username(url)
    netloc = f"{quote(auth_username, safe='')}:{quote(token, safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def build_git_auth_header(username: str | None, token: str | None, url: str) -> str | None:
    if not token:
        return None
    auth_username = username or default_git_username(url)
    encoded = b64encode(f"{auth_username}:{token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {encoded}"


def build_git_command(args: Iterable[str], *, configs: Iterable[str] = ()) -> list[str]:
    """Build a git command with global -c configs before the subcommand."""
    command = ["git"]
    for config in configs:
        command.extend(["-c", config])
    command.extend(args)
    return command


def build_noninteractive_git_command(
    args: Iterable[str],
    *,
    extra_configs: Iterable[str] = (),
) -> list[str]:
    return build_git_command(
        args,
        configs=(*NONINTERACTIVE_GIT_CONFIGS, *tuple(extra_configs)),
    )


def git_noninteractive_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    # Force Git/GCM into non-interactive mode so command outcomes reflect supplied config.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    env["GCM_MODAL_PROMPT"] = "0"
    return env


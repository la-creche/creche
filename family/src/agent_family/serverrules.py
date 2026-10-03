"""Value rules for `mcp/<name>/server.yaml` (contract 01b).

The shape is already parsed. Everything here is a rule the reader must state
in one sentence, because the author fixes it with one edit."""

from __future__ import annotations

import re
from typing import Final

from .grammar import (
    ALL_TOOLS,
    DESCRIPTION_MAX,
    ENV_VAR_NAME,
    EXACT_VERSION,
    GITHUB_REPO,
    IDENTITY_MAX,
    SECRET_NAME_MARKERS,
    SECRET_PREFIX,
    SERVER_NAME,
    SHA256_HEX,
    TOOL_NAME,
    VERSION_RANGE_CHARS,
    InstallSource,
)
from .report import Issues
from .server import FenceEntry, McpServerFile

#: Which `install` fields each source requires, and which it refuses. A field
#: that belongs to another source is an error, not a field quietly ignored.
_REQUIRED: Final[dict[InstallSource, tuple[str, ...]]] = {
    InstallSource.PYPI: ("package", "version", "lock"),
    InstallSource.GITHUB_RELEASE: ("repo", "version", "asset", "sha256"),
    # Nothing. Contract 01b §3.3: the `mcp-servers` component tag is the
    # version of every server that repository ships, so an `agent-mcp`
    # file declares no pin at all and `ref` is refused like any other
    # source's field.
    InstallSource.AGENT_MCP: (),
}
_PINNING_FIELDS: Final = ("package", "version", "lock", "repo", "asset", "sha256", "ref")

#: Contract 01b §3.4 rule 3: `install.lock` is a plain repository path, and
#: `handover.mcpserver` refuses the same four shapes before it reads
#: the file. Saying it HERE is what makes §4.1 step 2 true — CI refuses a
#: traversing lock path before the byte reaches the host.
_LOCK_PATH: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")

#: One argument name, or two joined by `/` (contract 01b §7.2).
_ARG_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARG_PARTS_MAX: Final = 2


def collapse(text: str) -> str:
    return " ".join(text.split())


def check_server(server: McpServerFile, directory: str, issues: Issues) -> None:
    """Every single-file rule of contract 01b, in file order."""
    _check_name(server, directory, issues)
    _check_install(server, issues)
    _check_run(server, issues)
    _check_tools(server, issues)
    _check_fences(server, issues)
    _check_shared(server, issues)


def _check_name(server: McpServerFile, directory: str, issues: Issues) -> None:
    if SERVER_NAME.fullmatch(server.name) is None:
        issues.error(
            "name",
            f"'{server.name}' is not a server name; use [a-z][a-z0-9-]{{1,30}}, "
            "hyphens and never an underscore",
        )

    if server.name != directory:
        issues.error("name", f"'{server.name}' does not match its directory 'mcp/{directory}'")

    identity = collapse(server.identity)
    if not 1 <= len(identity) <= IDENTITY_MAX:
        issues.error(
            "identity",
            f"identity is {len(identity)} characters; it must be 1 to {IDENTITY_MAX} and say "
            "whose credential this server holds",
        )


def _check_install(server: McpServerFile, issues: Issues) -> None:
    install = server.install
    try:
        source = InstallSource(install.source)
    except ValueError:
        allowed = ", ".join(str(member) for member in InstallSource)
        issues.error("install.source", f"'{install.source}' is not a source; use one of {allowed}")
        return

    present = {name for name in _PINNING_FIELDS if getattr(install, name) is not None}
    for name in _REQUIRED[source]:
        if name not in present:
            issues.error(f"install.{name}", f"source '{source}' requires 'install.{name}'")

    for name in sorted(present - set(_REQUIRED[source])):
        issues.error(f"install.{name}", f"source '{source}' does not take 'install.{name}'")

    _check_pin_values(server, issues)


def _check_pin_values(server: McpServerFile, issues: Issues) -> None:
    install = server.install
    version = install.version
    if version is not None and (
        EXACT_VERSION.fullmatch(version) is None
        or any(char in version for char in VERSION_RANGE_CHARS)
    ):
        issues.error(
            "install.version",
            f"'{version}' is not an exact version; a range makes a hash meaningless",
        )

    if install.sha256 is not None and SHA256_HEX.fullmatch(install.sha256) is None:
        issues.error("install.sha256", "sha256 must be 64 lowercase hexadecimal characters")

    if install.repo is not None and GITHUB_REPO.fullmatch(install.repo) is None:
        issues.error("install.repo", f"'{install.repo}' is not <owner>/<name> on GitHub")

    _check_lock(server, issues)


def _check_lock(server: McpServerFile, issues: Issues) -> None:
    """Contract 01b §3.4 rule 3. A path out of an operator-written file that
    root opens next, so the four shapes that leave `mcp/<name>/` are named
    here as well as at the executor."""
    lock = server.install.lock
    if lock is None:
        return

    if _LOCK_PATH.fullmatch(lock) is None:
        issues.error("install.lock", f"'{collapse(lock)[:80]}' is not a repository path")

        return

    if ".." in lock.split("/") or lock.endswith("/") or "//" in lock:
        issues.error("install.lock", "install.lock is not a plain repository path")


def _check_run(server: McpServerFile, issues: Issues) -> None:
    run = server.run
    if "/" in run.entrypoint or not run.entrypoint:
        issues.error(
            "run.entrypoint",
            f"'{run.entrypoint}' must be a console script name with no path; the PEP builds the "
            "absolute path from install.source",
        )

    for name, value in sorted(run.env.items()):
        _check_env_entry(name, value, issues)

    if run.state_dir and run.state_dir_env is None:
        issues.error("run.state_dir_env", "state_dir: true requires 'run.state_dir_env'")

    if not run.state_dir and run.state_dir_env is not None:
        issues.error("run.state_dir_env", "state_dir_env needs 'run.state_dir: true'")


def _check_env_entry(name: str, value: str, issues: Issues) -> None:
    loc = f"run.env.{name}"
    if ENV_VAR_NAME.fullmatch(name) is None:
        issues.error(loc, f"'{name}' is not an environment variable name; use [A-Z][A-Z0-9_]*")

    secret_shaped = any(marker in name for marker in SECRET_NAME_MARKERS)
    if secret_shaped and not value.startswith(SECRET_PREFIX):
        issues.error(
            loc,
            f"'{name}' names a credential, so its value must be 'secret:<key>'; a literal here "
            "puts a credential in git",
        )
        return

    if value.startswith(SECRET_PREFIX) and not value[len(SECRET_PREFIX) :]:
        issues.error(loc, "'secret:' names no key")


def _check_shared(server: McpServerFile, issues: Issues) -> None:
    """Contract 01b §4.3, as far as ONE file can be read.

    The agreement itself is registry-wide and root checks it
    (`handover.mcpserver._one_upstream_per_secret`). What is visible
    here is a declaration that could not be acted on whatever the other
    file says.
    """
    named = {
        value[len(SECRET_PREFIX) :]
        for value in server.run.env.values()
        if value.startswith(SECRET_PREFIX)
    }
    seen: set[str] = set()
    for index, entry in enumerate(server.shared_secrets):
        loc = f"shared_secrets[{index}]"
        if entry.secret not in named:
            issues.error(loc, f"'{entry.secret}' is not a secret this file's run.env names")

        if entry.server == server.name:
            issues.error(loc, "a server cannot agree with itself; name the OTHER server")

        if SERVER_NAME.fullmatch(entry.server) is None:
            issues.error(loc, f"'{entry.server}' is not a server name")

        if entry.secret in seen:
            issues.error(loc, f"'{entry.secret}' is shared twice; one secret has one partner")

        seen.add(entry.secret)


def _check_tools(server: McpServerFile, issues: Issues) -> None:
    seen: set[str] = set()
    for index, entry in enumerate(server.tools):
        loc = f"tools[{index}]"
        if TOOL_NAME.fullmatch(entry.name) is None:
            issues.error(loc, f"'{entry.name}' is not a tool name; use [A-Za-z][A-Za-z0-9_-]*")

        if entry.name in seen:
            issues.error(loc, f"'{entry.name}' is declared twice in this file")

        seen.add(entry.name)
        description = collapse(entry.description)
        if not 1 <= len(description) <= DESCRIPTION_MAX:
            issues.error(
                loc,
                f"description is {len(description)} characters; it must be 1 to {DESCRIPTION_MAX}",
            )


def _check_fences(server: McpServerFile, issues: Issues) -> None:
    declared = server.tool_names()
    for field, entries in (("arg_allows", server.arg_allows), ("arg_denies", server.arg_denies)):
        for index, entry in enumerate(entries):
            _check_fence(entry, f"{field}[{index}]", declared, issues)


def _check_fence(entry: FenceEntry, loc: str, declared: tuple[str, ...], issues: Issues) -> None:
    scope = entry.tools
    if isinstance(scope, str) and scope != ALL_TOOLS:
        issues.error(f"{loc}.tools", f"'{scope}' must be a list of tool names or '{ALL_TOOLS}'")

    if isinstance(scope, list):
        unknown = [name for name in scope if name not in declared]
        if unknown:
            issues.error(
                f"{loc}.tools",
                f"{', '.join(sorted(unknown))} are not declared in this file's 'tools'",
            )

    parts = entry.arg.split("/")
    if len(parts) > _ARG_PARTS_MAX or any(_ARG_NAME.fullmatch(part) is None for part in parts):
        issues.error(
            f"{loc}.arg",
            f"'{entry.arg}' must be one argument name, or two joined by '/'",
        )

    if not entry.values:
        issues.error(f"{loc}.values", "a fence entry needs at least one value")

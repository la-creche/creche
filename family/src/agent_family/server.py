"""The server file schema, `mcp/<name>/server.toml` (contract 01b).

One file declares one MCP server (invariant 18). Shape only, for the same
reason as `model.py`: the value rules live in `serverrules.py`."""

from __future__ import annotations

from typing import Final

from pydantic import Field

from .grammar import ALL_TOOLS, DEFAULT_PYTHON, SECRET_PREFIX
from .model import Strict

#: `tools: all`, or a list of this server's tool names (contract 01b §7).
FenceScope = list[str] | str


class InstallBlock(Strict):
    """The pin. Which fields are required depends on `source` (contract 01b
    §3), so every one is optional here and `validate.py` decides."""

    source: str
    package: str | None = None
    version: str | None = None
    #: Contract 01b §3.4: the committed closure root installs FROM, and a
    #: required field for `source: pypi`. It was in the contract's field
    #: table and in `stage7-releases.md` §4.2 and in `handover`'s own
    #: reader, and NOT here, so this validator refused every real `pypi`
    #: declaration with "unknown field 'lock'" — in CI at §4.1 step 2, and
    #: again in `caregiver`'s registry read, which is where invariant 18's
    #: near end looks for a declared server.
    lock: str | None = None
    python: str = DEFAULT_PYTHON
    #: The DNS name of the Forgejo or Gitea host of a `forge-release` pin.
    #: `serverrules` holds the rule, as `handover.mcpserver` rule 17.
    host: str | None = None
    repo: str | None = None
    asset: str | None = None
    sha256: str | None = None
    ref: str | None = None
    #: Contract 01b §3.2: `tar` (the default) or `binary`, `github-release`
    #: and `forge-release` only. `serverrules` holds the rule, as
    #: `handover.mcpserver` rule 16.
    format: str | None = None


class RunBlock(Strict):
    entrypoint: str
    args: list[str] = Field(default_factory=list[str])
    env: dict[str, str] = Field(default_factory=dict[str, str])
    state_dir: bool = False
    state_dir_env: str | None = None


class SharedSecretEntry(Strict):
    """Contract 01b §4.3: this file's `run.env` names `secret`, and the
    server named here names it too, on purpose.

    Without this field the schema refuses the whole file, and `caregiver`
    then drops a server the registry really declares.
    """

    secret: str
    server: str


class ToolEntry(Strict):
    name: str
    description: str
    write: bool = False


class FenceEntry(Strict):
    tools: FenceScope
    arg: str
    values: list[str]

    def covers(self, tool: str, declared: tuple[str, ...]) -> bool:
        if self.tools == ALL_TOOLS:
            return tool in declared

        return isinstance(self.tools, list) and tool in self.tools


class McpServerFile(Strict):
    name: str
    identity: str
    install: InstallBlock
    run: RunBlock
    tools: list[ToolEntry] = Field(default_factory=list[ToolEntry])
    arg_allows: list[FenceEntry] = Field(default_factory=list[FenceEntry])
    arg_denies: list[FenceEntry] = Field(default_factory=list[FenceEntry])
    #: The Home Assistant entity domains this server's identity may read.
    #: caregiver writes them into the `ha-entities` fence of each app that
    #: holds a grant to this server (chaperone design section 12). `None`
    #: leaves the server unfenced. An empty list lets a call name no entity.
    entity_domains: list[str] | None = None
    shared_secrets: list[SharedSecretEntry] = Field(default_factory=list[SharedSecretEntry])

    def tool_names(self) -> tuple[str, ...]:
        """The catalog: what COULD be granted. Nothing is granted by being
        declared here (contract 01b §5 rule 1)."""
        return tuple(entry.name for entry in self.tools)

    def secret_keys(self) -> frozenset[str]:
        """The keys after `secret:` in the values of `run.env` (contract 01b
        §4)."""
        return frozenset(
            value.removeprefix(SECRET_PREFIX)
            for value in self.run.env.values()
            if value.startswith(SECRET_PREFIX)
        )


SERVER_FIELDS: Final = tuple(McpServerFile.model_fields)
INSTALL_FIELDS: Final = tuple(InstallBlock.model_fields)
RUN_FIELDS: Final = tuple(RunBlock.model_fields)

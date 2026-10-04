"""`mcp/<name>/server.yaml`, read as hostile bytes (contract 01b).

One file declares one MCP server (invariant 18). This module reads the part
root must act on — the pin and the entrypoint — and validates the whole file
while it is there, because a field this module ignores is still a field a
later reader will trust.

**Why root parses this file again.** `agent_family.server` already models it
for `caregiver`, which runs as the operator. Root installs FROM it: it builds a venv,
downloads an artifact and puts a tree at `/opt/mcp/<name>`. A validation
performed by another package, in another process, at another time, is not a
fact root may rely on for that (`stage7-releases.md` §3, "root trusts no byte
of a request"). So root reads the bytes itself, with its own closed key set
and its own patterns, and the two readers agreeing is a property tests check
rather than an assumption either one makes.

## Every assumption this module makes about its input

A reviewer should find each of these either enforced below or refused.

1. The file is at most `MAX_SERVER_BYTES`. The cap is applied to the BYTES,
   before the parse, never after.
2. It is a YAML mapping, loaded with the safe loader of PyYAML. No tag, no
   anchor cycle and no Python object can come out of that. The loader has
   a bound on merge keys (`boundedyaml.load`).
3. Its top-level keys are exactly `REQUIRED_KEYS`, plus any of
   `OPTIONAL_KEYS`, and nothing else. An unknown key is a refusal, not a
   value that is quietly dropped: contract 01b's field set is closed, and a
   field this reader skips is a field a future reader might honour.
4. `name` matches `SERVER_NAME_RE` AND equals the directory the file sits in.
   The name becomes a path segment (`/opt/mcp/<name>`), a unix user name
   (`mcp-<name>`) and a PEP upstream key, so it may hold no separator, no
   dot and no upper case.
5. Every scalar passes `re.fullmatch` against its own pattern. `$` also
   matches before a trailing newline, and these values become argv words.
6. No pinned scalar may start with `-`, hold `=`, whitespace or a path
   separator, because each one becomes an argv word that `uv` or `curl`
   reads. The patterns all start with an alphanumeric class for that reason.
7. `install.source` decides which pin fields are required AND which are
   refused. A `pypi` entry that also carries `sha256` is refused rather than
   ignored: two pins in one file means a reader can choose the weaker one.
8. `version` holds no hyphen, so `<package>==<version>` cannot be re-split,
   and no `+` local segment, which `--require-hashes` would resolve to a
   different artifact than the one a reviewer read.
9. `install.ref` belongs to no source. A `source: agent-mcp` server is
   built from the tree this release already fetched at the component tag
   the phone showed, so a ref in the file is a second version root would
   have to obey and could only obey by fetching again after the tap
   (contract 01b §3.3). The key stays in
   `INSTALL_KEYS` so the refusal says which rule it broke rather than
   "unknown key".
14. `lock` is a REPOSITORY-relative path with no leading `/`, no `..`
    component, no `//` and no trailing `/`. It names the committed closure
    root installs from (contract 01b §3.4). Where the file is opened,
    containment under the registry checkout is checked again.
10. `run.entrypoint` is a console-script NAME. It holds no `/`, so root
    resolves it inside `/opt/mcp/<name>/bin/` and a file cannot name
    `/bin/sh` (`stage7-releases.md` §6 row 9).
11. `run.env` never holds a credential VALUE. A variable whose name contains
    `TOKEN`, `KEY`, `PASSWORD` or `SECRET` must use `secret:<name>`
    (contract 01b §4.1 rule 2, invariant 13). The build never reads `env` at
    all — it is validated here and then left to the PEP.
12. Lists are capped: `MAX_TOOLS` tools, `MAX_ARGS` argv words, `MAX_ENV`
    variables, `MAX_FENCES` fence entries. A file inside the byte cap can
    still hold thousands of short entries.
13. A refusal's `detail` names the field, never the value, unless the value
    has already passed `errors.safe_token`.
15. A fence's `arg` is one argument name, or exactly two joined by a single
    `/` (contract 01b §7.2). It is the only scalar in the file that may
    hold a separator, and it may because it never becomes a path or an
    argv word: the PEP splits it on `/` and reads each half out of the
    call's own arguments (`fences._composite`).
16. Two servers may name ONE secret, and only when both files say so
    (contract 01b §4.3). `_shared` checks what one file can check and
    `_one_upstream_per_secret` checks the agreement, because a pair is a
    property of two files and neither one holds it alone. Sharing is
    never inferred from a shared package, version or entrypoint: the two
    GitHub servers are one binary with two credentials on purpose.

The one thing this module does NOT check is whether the package exists. That
is the build's job, and it checks it by hash (`executor/mcpbuild.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from .boundedyaml import MergeLimits, load
from .errors import Refusal, RefusalCode, safe_token

#: Contract 01b §8.2's real file is about 4 KiB. 64 KiB leaves room for a
#: long `tools` list and refuses anything that is not a declaration.
MAX_SERVER_BYTES: Final = 64 * 1024

#: What the merge keys of one server file can copy (`boundedyaml.py`).
#:
#: CONTRACT-QUESTION: contract 01b gives no limit for a merge key. The
#: reading taken is the two limits of the Rust reader of this file, so that
#: the two readers refuse the same text. No server file needs a merge key.
#: Another number costs one line here and one line in the Rust reader.
MERGE_LIMITS: Final = MergeLimits(depth=400, pairs=100_000)

SERVER_FILE_NAME: Final = "server.yaml"

#: Contract 01b §1: hyphens, never an underscore, because the PEP's call
#: name is `<server>__<tool>`.
SERVER_NAME_RE: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")

#: A PyPI project name. It may hold `-`, `_` and `.`, and it may not START
#: with one: the value becomes an argv word.
PACKAGE_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: Exact, never a range (contract 01b §3). No hyphen and no `+`, so
#: `<package>==<version>` holds exactly one `==` and names exactly one
#: artifact.
VERSION_RE: Final = re.compile(r"[0-9][0-9A-Za-z.]{0,63}")

#: A repo-relative path to the committed closure (contract 01b §3.4). It is
#: a path out of a hostile file, so the pattern refuses a leading `/` and
#: `_lock` below refuses every other shape that leaves the registry.
LOCK_PATH_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}")

#: `<owner>/<name>` on GitHub. Exactly one slash, checked below.
REPO_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")

#: One release asset's file name. No slash, so it cannot name a directory,
#: and no leading `-`, so `curl` cannot read it as an option.
ASSET_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")

SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")

#: A console-script name, resolved inside `/opt/mcp/<name>/bin/`. No slash.
ENTRYPOINT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

#: An environment variable name, as the shell and systemd both spell one.
ENV_NAME_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

#: Contract 01b §5: unique in the file, and the closed set the PEP exposes.
TOOL_NAME_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")

#: One argument key of a tool call, as a JSON object names one. It is
#: `family/src/agent_family/serverrules._ARG_NAME` character for
#: character: two readers of this file that disagree about which files
#: are valid let CI pass what root then refuses.
ARG_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

#: Contract 01b §7.2: one argument name, or exactly two joined by a single
#: `/`. The composite is the whole point of the field — `owner/repo` fences
#: the half that carries the identity, and `repo` alone lets
#: `someone-else/agent-control` through a deny meant for `<owner>/`. It
#: is not `ENTRYPOINT_RE`, which holds no `/` and would refuse both GitHub
#: servers.
FENCE_ARG_RE: Final = re.compile(rf"{ARG_NAME_RE.pattern}(/{ARG_NAME_RE.pattern})?")

#: The two interpreters in use (contract 01b §3, `stage7-releases.md` §4.2).
PYTHONS: Final = ("3.12", "3.13")
DEFAULT_PYTHON: Final = "3.12"

#: Contract 01b §4.1 rule 2: a variable whose name holds one of these words
#: carries a credential, so it must name a secret and never a value. This is
#: the one mistake that would put a credential in git.
SECRET_WORDS: Final = ("TOKEN", "KEY", "PASSWORD", "SECRET")
SECRET_PREFIX: Final = "secret:"

#: Contract 01b §4.1's `secret:<key>`, the same pattern the intake's store
#: and `caregiver`'s gap writer apply. The name becomes a file name under
#: root's secrets directory, so a name root could not read is a name root
#: does not carry forward.
SECRET_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,62}")

#: Contract 01b §7: `tools: all`, or a list of this server's tool names.
ALL_TOOLS: Final = "all"

REQUIRED_KEYS: Final = frozenset({"name", "identity", "install", "run"})
OPTIONAL_KEYS: Final = frozenset({"tools", "arg_allows", "arg_denies", "shared_secrets"})

INSTALL_KEYS: Final = frozenset(
    {"source", "package", "version", "lock", "python", "repo", "asset", "sha256", "ref"}
)
RUN_KEYS: Final = frozenset({"entrypoint", "args", "env", "state_dir", "state_dir_env"})
TOOL_KEYS: Final = frozenset({"name", "description", "write"})
FENCE_KEYS: Final = frozenset({"tools", "arg", "values"})
SHARE_KEYS: Final = frozenset({"secret", "server"})

MAX_TOOLS: Final = 200
MAX_ARGS: Final = 32
MAX_ENV: Final = 32
MAX_FENCES: Final = 64
MAX_FENCE_VALUES: Final = 256
#: Contract 01b §4.3. One server may share at most this many of its
#: secrets, and each with exactly one partner. The real number is one.
MAX_SHARED: Final = 8
MAX_IDENTITY_CHARS: Final = 1000
MAX_DESCRIPTION_CHARS: Final = 200
MAX_ARG_CHARS: Final = 256
MAX_VALUE_CHARS: Final = 256


class Source(StrEnum):
    """Contract 01b §3's three pin kinds. Each one pins by hash (§3 rule 1)."""

    PYPI = "pypi"
    GITHUB_RELEASE = "github-release"
    AGENT_MCP = "agent-mcp"


#: Which pin fields each source requires, and therefore which it refuses.
#: `python` is shared and defaulted, so it is in no row.
_REQUIRED_PIN: Final[dict[Source, frozenset[str]]] = {
    Source.PYPI: frozenset({"package", "version", "lock"}),
    Source.GITHUB_RELEASE: frozenset({"repo", "version", "asset", "sha256"}),
    # Nothing at all. The `mcp-servers` component tag IS the version of
    # every server that ships in that repository (contract 01b §3.3), so
    # an `agent-mcp` file carries no pin field of its own and this row
    # makes `_check_pin_fields` refuse every one it could carry — `ref`
    # included.
    Source.AGENT_MCP: frozenset(),
}


@dataclass(frozen=True)
class ServerPin:
    """`install`, after every per-source rule has been applied."""

    source: Source
    python: str
    package: str | None = None
    version: str | None = None
    #: Contract 01b §3.4: the committed closure root installs FROM. It is an
    #: input and never something the release produces — a lock generated
    #: during the install would have `--require-hashes` check the hashes
    #: root had just written against themselves.
    lock: str | None = None
    repo: str | None = None
    asset: str | None = None
    sha256: str | None = None

    def requirement(self) -> str:
        """`<dist>==<version>`: one artifact, one `==`, no range."""
        return f"{self.package}=={self.version}"

    def asset_url(self) -> str:
        """The proven URL shape. `bin/creche-deploy` has fetched
        github-mcp-server from it on every deploy since it was written, and
        the tag is `v` + the declared version."""
        return f"https://github.com/{self.repo}/releases/download/v{self.version}/{self.asset}"


@dataclass(frozen=True)
class ServerFence:
    """One `arg_denies` entry, after §7's shape rules.

    `tools` is None for contract 01b's `all`. The roster writer expands it
    against the declared set, because the PEP's own reader takes a list and
    has no word for `all` (`mcp_client._parse_arg_denies`).
    """

    tools: tuple[str, ...] | None
    arg: str
    values: tuple[str, ...]

    def covers(self, declared: tuple[str, ...]) -> tuple[str, ...]:
        return declared if self.tools is None else self.tools


@dataclass(frozen=True)
class SharedSecret:
    """One `shared_secrets` entry (contract 01b §4.3).

    It says: this file's `run.env` names `secret`, and exactly one other
    server — `server` — names it too, on purpose. The pair is only
    accepted when the other file says the mirror of this, so sharing is
    declared by both and inferred by nobody.
    """

    secret: str
    server: str

    def mirrors(self, other: SharedSecret, by: str) -> bool:
        """True when `other`, declared by server `by`, is this one's
        other half."""
        return other.secret == self.secret and other.server == by


@dataclass(frozen=True)
class ServerFile:
    """One `mcp/<name>/server.yaml` that root is willing to install from."""

    name: str
    pin: ServerPin
    entrypoint: str
    tools: tuple[str, ...]
    #: Every `secret:<name>` this server's `run.env` names, sorted and
    #: deduplicated. It is here so that `read_registry` can hold
    #: `stage7-releases.md` §6 row 8 — one secret name binds to at most one
    #: upstream — which needs every server's names in one place.
    secrets: tuple[str, ...] = ()
    #: `run.args` and `run.env` as the file declared them: the roster is
    #: these fields plus a command root builds itself.
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    #: §7's deny rules. The PEP applies them on the upstream BY NAME, before
    #: any grant is read, so a released server with no roster fence is a
    #: server whose declared fences do not exist.
    arg_denies: tuple[ServerFence, ...] = ()
    #: §7's allow rules, carried and NOT written into the roster:
    #: `UpstreamSpec` has no field for them and the PEP's reader would
    #: refuse the file. They are here so the release can SAY that a
    #: declared fence is not applied, instead of dropping it in silence.
    arg_allows: tuple[ServerFence, ...] = ()
    #: §4.3's declarations. `read_registry` needs both halves of a pair
    #: in hand at once, so the entries are carried per file and matched
    #: across the registry in `_one_upstream_per_secret`.
    shared: tuple[SharedSecret, ...] = ()
    #: §4.2: the variable this server reads its state directory from, or
    #: None when the file declares no `run.state_dir`. The file names the
    #: variable and never the path: root builds that.
    state_dir_env: str | None = None

    #: `mcp-<name>`, the unprivileged user this server's child runs as
    #: (contract 01b §9, contract 06 §1 rule 3). Derived from the validated
    #: name and never read out of the file.
    @property
    def user(self) -> str:
        return f"mcp-{self.name}"


def _refuse(name: str, detail: str) -> Refusal:
    return Refusal(RefusalCode.SERVER, f"mcp/{safe_token(name)}", detail)


def read_server_dir(directory: Path) -> ServerFile:
    """Read `<directory>/server.yaml`, whose name must be the directory's.

    The directory name is the authority: it is a path component root already
    walked, and the file's own `name` field has to agree with it (contract
    01b §1).
    """
    path = directory / SERVER_FILE_NAME
    if path.is_symlink() or not path.is_file():
        raise _refuse(directory.name, "server.yaml is missing or is not a regular file")

    raw = path.read_bytes()

    return parse_server(raw, directory.name)


def parse_server(raw: bytes, directory: str) -> ServerFile:
    """Contract 01b, every rule, against the bytes as they arrived."""
    if len(raw) > MAX_SERVER_BYTES:
        raise _refuse(directory, f"server.yaml is larger than {MAX_SERVER_BYTES} bytes")

    body = _mapping(directory, raw)
    _check_keys(directory, frozenset(body))
    name = _name(directory, body["name"])
    _identity(name, body["identity"])
    pin = _install(name, body["install"])
    run = _mapping_value(name, "run", body["run"])
    tools = _tools(name, body.get("tools", []))
    allows = _fences(name, "arg_allows", body.get("arg_allows", []), tools)
    denies = _fences(name, "arg_denies", body.get("arg_denies", []), tools)
    entrypoint = _run(name, run)
    state_dir_env = _state_dir(name, run)
    env = _mapping_value(name, "run.env", run.get("env", {}))
    secrets = _secrets(name, env)

    return ServerFile(
        name=name,
        pin=pin,
        entrypoint=entrypoint,
        tools=tools,
        secrets=secrets,
        args=tuple(cast(list[str], run.get("args", []))),
        env=tuple((key, str(value)) for key, value in sorted(env.items())),
        arg_denies=denies,
        arg_allows=allows,
        shared=_shared(name, body.get("shared_secrets", []), secrets),
        state_dir_env=state_dir_env,
    )


def _secrets(name: str, value: Any) -> tuple[str, ...]:
    """Every `secret:<name>` in `run.env`, sorted and deduplicated.

    `_env` has already proved the shape of every entry. This reads the
    NAMES out of it, because §6 row 8 is a rule about names and nothing
    was carrying them: `ServerFile` had four fields and none of them was
    this one.

    A name that fails `SECRET_NAME_RE` is refused rather than dropped.
    Root would never be able to store it — the intake's store and
    `caregiver`'s gap writer both check the same pattern — so the server
    would install and then start with an unresolved reference.
    """
    found: set[str] = set()
    for key, inner in cast(dict[str, Any], value).items():
        if not isinstance(inner, str) or not inner.startswith(SECRET_PREFIX):
            continue

        secret = inner.removeprefix(SECRET_PREFIX)
        if SECRET_NAME_RE.fullmatch(secret) is None:
            raise _refuse(name, f"run.env[{safe_token(key)}] names no secret this can store")

        found.add(secret)

    return tuple(sorted(found))


def _shared(name: str, value: Any, secrets: tuple[str, ...]) -> tuple[SharedSecret, ...]:
    """Contract 01b §4.3's single-file rules. The pairing is registry-wide
    and lives in `_one_upstream_per_secret`.

    Three refusals here, and each one is a declaration that could not be
    acted on:

    1. A `secret` this file's `run.env` does not name. The entry would
       relax §6 row 8 for a name this server has no claim on, which is
       the whole of the rule it relaxes.
    2. A `server` that is this file's own name. A file cannot agree with
       itself, and self-agreement is exactly the shape that would turn a
       typo into a sanctioned duplicate.
    3. One `secret` named twice. Each secret has at most one partner
       (§4.3 rule 4), so two entries for one name are two different
       partners and the file does not say which.
    """
    if not isinstance(value, list):
        raise _refuse(name, "field 'shared_secrets' is not a list")

    entries = cast(list[Any], value)
    if len(entries) > MAX_SHARED:
        raise _refuse(name, f"field 'shared_secrets' holds more than {MAX_SHARED} entries")

    found = tuple(_one_share(name, one, secrets) for one in entries)
    named = [one.secret for one in found]
    if len(set(named)) != len(named):
        raise _refuse(name, "shared_secrets names one secret twice")

    return found


def _one_share(name: str, value: Any, secrets: tuple[str, ...]) -> SharedSecret:
    body = _mapping_value(name, "shared_secrets[]", value)
    unknown = frozenset(body) - SHARE_KEYS
    if unknown:
        raise _refuse(name, f"shared_secrets has unknown key {safe_token(sorted(unknown)[0])}")

    missing = SHARE_KEYS - frozenset(body)
    if missing:
        raise _refuse(name, f"shared_secrets has no {safe_token(sorted(missing)[0])}")

    secret = _text(name, "shared_secrets[].secret", body["secret"], SECRET_NAME_RE)
    partner = _text(name, "shared_secrets[].server", body["server"], SERVER_NAME_RE)
    if secret not in secrets:
        raise _refuse(name, f"shared_secrets names {safe_token(secret)}, which run.env does not")

    if partner == name:
        raise _refuse(name, "shared_secrets names this server as its own partner")

    return SharedSecret(secret=secret, server=partner)


def _mapping(directory: str, raw: bytes) -> dict[str, Any]:
    # Each error of the reader, as `manifest.parse_manifest` takes it.
    try:
        loaded: Any = load(raw.decode("utf-8"), MERGE_LIMITS)
    except Exception:
        raise _refuse(directory, "server.yaml is not readable YAML") from None

    if not isinstance(loaded, dict):
        raise _refuse(directory, "server.yaml is not a mapping")

    body: dict[str, Any] = {}
    for key, value in cast(dict[Any, Any], loaded).items():
        if not isinstance(key, str):
            raise _refuse(directory, "server.yaml has a non-string key")

        body[key] = value

    return body


def _check_keys(directory: str, keys: frozenset[str]) -> None:
    missing = REQUIRED_KEYS - keys
    if missing:
        raise _refuse(directory, f"server.yaml has no {safe_token(sorted(missing)[0])}")

    unknown = keys - REQUIRED_KEYS - OPTIONAL_KEYS
    if unknown:
        raise _refuse(directory, f"server.yaml has unknown key {safe_token(sorted(unknown)[0])}")


def _name(directory: str, value: Any) -> str:
    if not isinstance(value, str) or not SERVER_NAME_RE.fullmatch(value):
        raise _refuse(directory, "field 'name' is malformed")

    if value != directory:
        raise _refuse(directory, "field 'name' differs from the directory name")

    return value


def _identity(name: str, value: Any) -> None:
    """Prose a reviewer reads on the phone. Shape only, and a length cap."""
    if not isinstance(value, str) or not value.strip():
        raise _refuse(name, "field 'identity' is malformed")

    if len(value) > MAX_IDENTITY_CHARS:
        raise _refuse(name, f"field 'identity' is longer than {MAX_IDENTITY_CHARS} characters")


def _mapping_value(name: str, field: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refuse(name, f"field '{field}' is not a mapping")

    body: dict[str, Any] = {}
    for key, inner in cast(dict[Any, Any], value).items():
        if not isinstance(key, str):
            raise _refuse(name, f"field '{field}' has a non-string key")

        body[key] = inner

    return body


def _text(name: str, field: str, value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _refuse(name, f"field '{field}' is malformed")

    return value


def _install(name: str, value: Any) -> ServerPin:
    body = _mapping_value(name, "install", value)
    unknown = frozenset(body) - INSTALL_KEYS
    if unknown:
        raise _refuse(name, f"install has unknown key {safe_token(sorted(unknown)[0])}")

    source = _source(name, body.get("source"))
    _check_pin_fields(name, source, frozenset(body) - {"source", "python"})
    python = _python(name, body.get("python", DEFAULT_PYTHON))
    pin = ServerPin(
        source=source,
        python=python,
        package=_optional(name, body, "package", PACKAGE_RE),
        version=_optional(name, body, "version", VERSION_RE),
        lock=_lock(name, body.get("lock")),
        repo=_optional(name, body, "repo", REPO_RE),
        asset=_optional(name, body, "asset", ASSET_RE),
        sha256=_optional(name, body, "sha256", SHA256_RE),
    )

    return pin


def _source(name: str, value: Any) -> Source:
    try:
        return Source(value)
    except ValueError:
        raise _refuse(name, "install.source is not one of contract 01b's three") from None


def _check_pin_fields(name: str, source: Source, present: frozenset[str]) -> None:
    """Rule 7: the source decides what is required AND what is refused."""
    required = _REQUIRED_PIN[source]
    missing = required - present
    if missing:
        raise _refuse(name, f"install.{safe_token(sorted(missing)[0])} is required for {source}")

    extra = present - required
    if extra:
        raise _refuse(name, f"install.{safe_token(sorted(extra)[0])} does not belong to {source}")


def _python(name: str, value: Any) -> str:
    if value not in PYTHONS:
        raise _refuse(name, "install.python is not one of the two interpreters in use")

    return str(value)


def _optional(name: str, body: dict[str, Any], field: str, pattern: re.Pattern[str]) -> str | None:
    if field not in body:
        return None

    return _text(name, f"install.{field}", body[field], pattern)


def _lock(name: str, value: Any) -> str | None:
    """Assumption 14. A repo-relative path, and none of the four shapes
    that would read a file outside the registry checkout.

    Containment is checked again where the file is opened, because a check
    that rests on a pattern in another module is a check a refactor can
    delete (`mcpbuild`, assumption 2's reason).
    """
    if value is None:
        return None

    path = _text(name, "install.lock", value, LOCK_PATH_RE)
    if ".." in path.split("/") or path.endswith("/") or "//" in path:
        raise _refuse(name, "install.lock is not a plain repository path")

    return path


def _run(name: str, body: dict[str, Any]) -> str:
    unknown = frozenset(body) - RUN_KEYS
    if unknown:
        raise _refuse(name, f"run has unknown key {safe_token(sorted(unknown)[0])}")

    entrypoint = _text(name, "run.entrypoint", body.get("entrypoint"), ENTRYPOINT_RE)
    _args(name, body.get("args", []))
    _env(name, body.get("env", {}))

    return entrypoint


def _args(name: str, value: Any) -> None:
    """Fixed argv, never shell-parsed. Shape and size only: the words reach
    the PEP's child, not one of root's, and the PEP holds its own rules."""
    if not isinstance(value, list):
        raise _refuse(name, "run.args is not a list")

    words = cast(list[Any], value)
    if len(words) > MAX_ARGS:
        raise _refuse(name, f"run.args holds more than {MAX_ARGS} words")

    for word in words:
        if not isinstance(word, str) or len(word) > MAX_ARG_CHARS or "\x00" in word:
            raise _refuse(name, "run.args holds a word that is not plain text")


def _env(name: str, value: Any) -> None:
    """Rule 11. Names and `secret:` references, never a credential value."""
    body = _mapping_value(name, "run.env", value)
    if len(body) > MAX_ENV:
        raise _refuse(name, f"run.env holds more than {MAX_ENV} variables")

    for key, inner in body.items():
        if not ENV_NAME_RE.fullmatch(key):
            raise _refuse(name, "run.env has a malformed variable name")

        if not isinstance(inner, str) or len(inner) > MAX_VALUE_CHARS or "\x00" in inner:
            raise _refuse(name, f"run.env[{safe_token(key)}] is not plain text")

        if _looks_secret(key) and not inner.startswith(SECRET_PREFIX):
            raise _refuse(name, f"run.env[{safe_token(key)}] must name a secret, not a value")


def _looks_secret(key: str) -> bool:
    return any(word in key for word in SECRET_WORDS)


def _state_dir(name: str, body: dict[str, Any]) -> str | None:
    """Contract 01b §4.2: `state_dir_env` is required when `state_dir` is
    true, and means nothing without it. Answers the variable, or None for
    a file that keeps no state."""
    wants = body.get("state_dir", False)
    if not isinstance(wants, bool):
        raise _refuse(name, "run.state_dir is not a boolean")

    variable = body.get("state_dir_env")
    if wants and (not isinstance(variable, str) or not ENV_NAME_RE.fullmatch(variable)):
        raise _refuse(name, "run.state_dir_env is required when run.state_dir is true")

    if not wants and variable is not None:
        raise _refuse(name, "run.state_dir_env is set without run.state_dir")

    return cast(str, variable) if wants else None


def _tools(name: str, value: Any) -> tuple[str, ...]:
    """Contract 01b §5. The closed set the PEP may expose for this server."""
    if not isinstance(value, list):
        raise _refuse(name, "field 'tools' is not a list")

    entries = cast(list[Any], value)
    if len(entries) > MAX_TOOLS:
        raise _refuse(name, f"field 'tools' holds more than {MAX_TOOLS} entries")

    found: list[str] = []
    for entry in entries:
        found.append(_one_tool(name, entry))

    if len(set(found)) != len(found):
        raise _refuse(name, "field 'tools' names one tool twice")

    return tuple(found)


def _one_tool(name: str, value: Any) -> str:
    body = _mapping_value(name, "tools[]", value)
    unknown = frozenset(body) - TOOL_KEYS
    if unknown:
        raise _refuse(name, f"a tool has unknown key {safe_token(sorted(unknown)[0])}")

    tool = _text(name, "tools[].name", body.get("name"), TOOL_NAME_RE)
    description = body.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= MAX_DESCRIPTION_CHARS:
        raise _refuse(name, f"tool {safe_token(tool)} has a malformed description")

    if not isinstance(body.get("write", False), bool):
        raise _refuse(name, f"tool {safe_token(tool)} has a malformed 'write'")

    return tool


def _fences(name: str, field: str, value: Any, tools: tuple[str, ...]) -> tuple[ServerFence, ...]:
    """Contract 01b §7. Shape here, and the PEP applies what comes back."""
    if not isinstance(value, list):
        raise _refuse(name, f"field '{field}' is not a list")

    entries = cast(list[Any], value)
    if len(entries) > MAX_FENCES:
        raise _refuse(name, f"field '{field}' holds more than {MAX_FENCES} entries")

    return tuple(_one_fence(name, field, entry, tools) for entry in entries)


def _one_fence(name: str, field: str, value: Any, tools: tuple[str, ...]) -> ServerFence:
    body = _mapping_value(name, field, value)
    unknown = frozenset(body) - FENCE_KEYS
    if unknown:
        raise _refuse(name, f"{field} has unknown key {safe_token(sorted(unknown)[0])}")

    missing = FENCE_KEYS - frozenset(body)
    if missing:
        raise _refuse(name, f"{field} has no {safe_token(sorted(missing)[0])}")

    return ServerFence(
        tools=_fence_scope(name, field, body["tools"], tools),
        arg=_text(name, f"{field}.arg", body["arg"], FENCE_ARG_RE),
        values=_fence_values(name, field, body["values"]),
    )


def _fence_scope(
    name: str, field: str, value: Any, tools: tuple[str, ...]
) -> tuple[str, ...] | None:
    """The tools one entry covers. None is contract 01b's `all`.

    `all` over a file that declares NO tool is refused rather than read as
    an empty scope. The PEP's roster reader takes a non-empty list and has
    no word for `all`, so an expanded empty scope would make the roster
    unparseable and the whole reload would keep the old set — a deny that
    silently became "no upstream at all", with one line in a log.
    """
    if value == ALL_TOOLS:
        if not tools:
            raise _refuse(name, f"{field} says 'all' on a file that declares no tool")

        return None

    if not isinstance(value, list):
        raise _refuse(name, f"{field}.tools is neither 'all' nor a list")

    found: list[str] = []
    for entry in cast(list[Any], value):
        tool = _text(name, f"{field}.tools[]", entry, TOOL_NAME_RE)
        if tool not in tools:
            raise _refuse(name, f"{field} names a tool this file does not declare")

        found.append(tool)

    if not found:
        raise _refuse(name, f"{field}.tools is empty; use 'all' or name a tool")

    return tuple(found)


def _fence_values(name: str, field: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _refuse(name, f"{field}.values is not a list")

    entries = cast(list[Any], value)
    if len(entries) > MAX_FENCE_VALUES:
        raise _refuse(name, f"{field}.values holds more than {MAX_FENCE_VALUES} entries")

    if not entries:
        raise _refuse(name, f"{field}.values is empty; a fence with no value fences nothing")

    for entry in entries:
        if not isinstance(entry, str) or len(entry) > MAX_VALUE_CHARS or "\x00" in entry:
            raise _refuse(name, f"{field}.values holds a value that is not plain text")

    return tuple(cast(list[str], entries))


def read_registry(mcp_root: Path, limit: int) -> tuple[ServerFile, ...]:
    """Every `mcp/<name>/server.yaml` under a registry checkout, by name.

    The walk is what makes the directory name the authority: root lists the
    directories it finds, validates each name itself, and never follows a
    symlink into one. A registry with no `mcp/` directory declares no server,
    which is not an error — it is the state before the first one is written.
    """
    if not mcp_root.is_dir() or mcp_root.is_symlink():
        return ()

    names: list[str] = []
    for entry in sorted(mcp_root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue

        if not SERVER_NAME_RE.fullmatch(entry.name):
            raise _refuse(entry.name, "is not a server name")

        names.append(entry.name)

    if len(names) > limit:
        raise _refuse("registry", f"declares more than {limit} servers")

    found = tuple(read_server_dir(mcp_root / one) for one in names)
    _one_upstream_per_secret(found)

    return found


def _one_upstream_per_secret(servers: tuple[ServerFile, ...]) -> None:
    """`stage7-releases.md` §6 row 8, and contract 01b §4.3's exception.

    > Declare an MCP server whose `env` names `github_token`, to read it.
    > A secret name binds to at most one upstream. The manifest validator
    > refuses a second binding.

    This is that validator, and here is where it belongs: the rule is
    about the registry as a whole, and this is the one place every
    declaration is in hand at once. Per file it is invisible.

    The gap layout cannot catch it: `caregiver._write_gap` writes
    `secret-gaps/<secret name>.json`, so a second server declaring one
    name would overwrite the first server's gap and the operator would be pushed
    one link naming one of the two.

    **The exception is declared and never inferred.** `ha` and `ha-read`
    hold ONE Home Assistant token on purpose: one user, two reaches, which
    contract 01b §5 rule 5 makes two files. Refusing that pair would
    refuse the whole registry.

    A pair is accepted when, and only when, BOTH files declare it. One
    side alone is still a refusal, and it names the side that is missing
    rather than the side that is present — the file that forgot the
    declaration is the file to edit. Nothing is read off the packages,
    the versions or the entrypoints: `github-code` and `github-platform`
    are also one binary, and they hold two credentials on purpose.
    """
    bound: dict[str, str] = {}
    declared = {one.name: one for one in servers}
    for server in servers:
        for secret in server.secrets:
            first = bound.setdefault(secret, server.name)
            if first == server.name:
                continue

            _check_pair(declared[first], server, secret)


def _check_pair(first: ServerFile, second: ServerFile, secret: str) -> None:
    """Both halves of one §4.3 declaration, or a refusal naming the half
    that is missing."""
    mine = next((one for one in second.shared if one.secret == secret), None)
    if mine is None or mine.server != first.name:
        raise _refuse(
            second.name,
            f"names the secret {safe_token(secret)}, which {safe_token(first.name)} already "
            f"binds (stage7-releases.md §6 row 8); declare the sharing in both files "
            f"(contract 01b §4.3)",
        )

    theirs = next((one for one in first.shared if one.secret == secret), None)
    if theirs is None or not mine.mirrors(theirs, second.name):
        raise _refuse(
            first.name,
            f"does not declare sharing {safe_token(secret)} with {safe_token(second.name)}, "
            f"which declares sharing it with {safe_token(first.name)} (contract 01b §4.3)",
        )

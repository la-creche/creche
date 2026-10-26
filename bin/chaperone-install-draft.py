#!/usr/bin/env python3
# OPERATOR on a development machine, and CI. The command:
#   `uv run bin/chaperone-install-draft.py --approval-listen HOST:PORT [--out-dir DIR] REGISTRY`
"""Print a draft of chaperone's `install.toml` and one manifest for each MCP server.

The Rust chaperone reads no roster. Each MCP server of the registry becomes
an effect plugin of chaperone: one `plugins/effect/added/<name>/plugin.toml`
and one entry in `install.toml` (chaperone `docs/design.md` §6, §7.4 and
§7.5). The script reads `mcp/<name>/server.toml` of each server directory of
REGISTRY. It prints each file after one line `--- <path>`. The path is
relative to chaperone's configuration directory. With `--out-dir`, the
script writes the same files into DIR, which must not exist. The script
writes nothing else. The operator reviews the draft and installs it as root.

`install.toml` holds:

1. `mcp-http` with the guard `host-firewall`: the sandboxes dial the LAN
   address.
2. `attendance` with its socket and its two door tokens, `release` with its
   spool and its one requester, and `ha-approval` with its hook and the
   address of `--approval-listen`.
3. The slot `approval_channel`, filled by `ha-approval`.
4. One entry for each server: the user `mcp-<name>`, `secret_delivery =
   "env"`, and each secret of the server.

The plugin `ha-call` is not in the draft.

Each fact of a server comes from its file, as handover's roster writer
reads it (`handover/src/handover/executor/roster.py`):

1. The program is `<mcp root>/<name>/bin/<entrypoint>`, then `run.args`.
2. A value `secret:<key>` of `run.env` is a secret. The secret file name is
   `<key>`: the key of the sops file that the Python chaperone reads.
3. Each other value of `run.env` is one word `NAME=value` of `/usr/bin/env`
   in front of the program, because a manifest has no field for the
   environment.
4. A server with `run.state_dir` gets the variable of `run.state_dir_env`,
   set to `<MCP state root>/<name>`. It replaces a value of the same name.

The fences of a server file go into `policy.toml`, which caregiver writes.
The tools of a server come from the server itself (`tools/list`).

Exit status: 0 for a draft, and 2 for a command line, a registry or an
output directory that the script refuses. A refusal names the server and
the field, and no value.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

PROGRAM: Final = "chaperone-install-draft"
EXIT_OK: Final = 0
EXIT_REFUSED: Final = 2

#: The host paths. `--state-root` and `--mcp-root` replace the first two.
STATE_ROOT: Final = "/srv/agents/state/rework"
MCP_ROOT: Final = "/opt/mcp"
MCP_STATE_ROOT: Final = PurePosixPath("/var/lib/agent-mcp")
SOCKET: Final = "sock/sessiond.sock"
REQUESTS_DIR: Final = "/var/lib/creche-handover/releases/requests"
REQUESTED_BY: Final = "agent-control"
HOOK_URL: Final = "http://127.0.0.1:1881/hook/approval"

#: The secret parameter of each fixed plugin, and the name of its secret file.
ATTENDANCE_SECRETS: Final = (
    ("DELEGATE_TOKEN", "door-delegate"),
    ("DISPATCH_TOKEN", "door-dispatch"),
)
APPROVAL_SECRETS: Final = (("APPROVAL_TOKEN", "approval-token"), ("RESUME_TOKEN", "resume-token"))

ENV_PROGRAM: Final = "/usr/bin/env"
MANIFEST_VERSION: Final = "1.0.0"
USER_PREFIX: Final = "mcp-"
SECRET_PREFIX: Final = "secret:"
HEADER: Final = "--- "
INSTALL_PATH: Final = PurePosixPath("install.toml")
ADDED_DIR: Final = PurePosixPath("plugins/effect/added")
SERVER_FILE: Final = "server.toml"
MAX_SERVER_BYTES: Final = 64 * 1024

#: The plugins of chaperone. A server of the same name is a second plugin of that name.
CHAPERONE_PLUGINS: Final = frozenset(
    {"attendance", "audit-jsonl", "bearer", "deny-all", "ha-approval", "ha-call"}
    | {"mcp-http", "release", "secret-files", "system-clock"}
)

#: The patterns of handover's server reader (`handover/src/handover/mcpserver.py`).
SERVER_NAME_RE: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")
ENTRYPOINT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ENV_NAME_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
SECRET_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,62}")
RUN_KEYS: Final = frozenset({"entrypoint", "args", "env", "state_dir", "state_dir_env"})
CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")

#: `host:port` with a host that is not the unspecified address.
LISTEN_RE: Final = re.compile(r"(?P<host>[0-9A-Za-z.-]+|\[[0-9A-Fa-f:.]+\]):[0-9]{1,5}")
UNSPECIFIED: Final = frozenset({"0.0.0.0", "[::]"})


class Refused(Exception):
    """The script cannot make a draft. The message names what and where."""


@dataclass(frozen=True, slots=True)
class Roots:
    state: PurePosixPath
    mcp: PurePosixPath


@dataclass(frozen=True, slots=True)
class Server:
    """What the draft takes from one server file."""

    name: str
    program: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    secrets: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DraftFile:
    path: PurePosixPath
    text: str


def read_servers(registry: Path, roots: Roots) -> tuple[Server, ...]:
    """Each server directory under `mcp/`, in name order."""
    servers_dir = registry / "mcp"
    if not servers_dir.is_dir():
        raise Refused(f"{registry} holds no directory mcp")

    dirs = sorted(path for path in servers_dir.iterdir() if path.is_dir())
    if not dirs:
        raise Refused(f"{servers_dir} holds no server directory")

    return tuple(read_server(path, roots) for path in dirs)


def read_server(directory: Path, roots: Roots) -> Server:
    where = f"mcp/{directory.name}"
    path = directory / SERVER_FILE
    if not path.is_file():
        raise Refused(f"{where}: no {SERVER_FILE}. Convert a YAML file with bin/yaml-to-toml.py")

    raw = path.read_bytes()
    if len(raw) > MAX_SERVER_BYTES:
        raise Refused(f"{where}: {SERVER_FILE} is larger than {MAX_SERVER_BYTES} bytes")

    try:
        body = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise Refused(f"{where}: {SERVER_FILE} is not TOML: {error}") from None

    name = body.get("name")
    if name != directory.name or SERVER_NAME_RE.fullmatch(directory.name) is None:
        raise Refused(f"{where}: name is not a server name equal to the directory name")
    if name in CHAPERONE_PLUGINS:
        raise Refused(f"{where}: {name} is the name of a plugin of chaperone")

    run = body.get("run")
    if not isinstance(run, dict):
        raise Refused(f"{where}: run is not a table")

    return _server(where, name, run, roots)


def _server(where: str, name: str, run: dict[str, Any], roots: Roots) -> Server:
    unknown = sorted(set(run) - RUN_KEYS)
    if unknown:
        raise Refused(f"{where}: run.{unknown[0]} is a key the draft cannot place")

    entrypoint = run.get("entrypoint")
    if not isinstance(entrypoint, str) or ENTRYPOINT_RE.fullmatch(entrypoint) is None:
        raise Refused(f"{where}: run.entrypoint is not a program name")

    args = run.get("args", [])
    if not isinstance(args, list) or not all(_is_word(one) for one in args):
        raise Refused(f"{where}: run.args is not a list of texts with no control character")

    env: dict[str, str] = {}
    secrets: dict[str, str] = {}
    table = run.get("env", {})
    if not isinstance(table, dict):
        raise Refused(f"{where}: run.env is not a table")

    for variable, value in table.items():
        if ENV_NAME_RE.fullmatch(variable) is None or not _is_word(value):
            raise Refused(f"{where}: run.env.{variable} is not a variable with a text value")

        if not value.startswith(SECRET_PREFIX):
            env[variable] = value
            continue

        key = value.removeprefix(SECRET_PREFIX)
        if SECRET_NAME_RE.fullmatch(key) is None:
            raise Refused(f"{where}: run.env.{variable} names no valid secret")

        secrets[variable] = key

    _state_dir(where, name, run, env, secrets)
    program = str(roots.mcp / name / "bin" / entrypoint)

    return Server(
        name=name,
        program=(program, *args),
        env=tuple(sorted(env.items())),
        secrets=tuple(sorted(secrets.items())),
    )


def _state_dir(
    where: str, name: str, run: dict[str, Any], env: dict[str, str], secrets: dict[str, str]
) -> None:
    """Rule 4: the state directory of the server, written last."""
    wants = run.get("state_dir", False)
    variable = run.get("state_dir_env")
    if not isinstance(wants, bool):
        raise Refused(f"{where}: run.state_dir is not a boolean")
    if wants != (variable is not None):
        raise Refused(f"{where}: run.state_dir and run.state_dir_env must come together")
    if variable is None:
        return
    if not isinstance(variable, str) or ENV_NAME_RE.fullmatch(variable) is None:
        raise Refused(f"{where}: run.state_dir_env is not a variable name")

    secrets.pop(variable, None)
    env[variable] = str(MCP_STATE_ROOT / name)


def _is_word(value: object) -> bool:
    return isinstance(value, str) and CONTROL_RE.search(value) is None


def _text(value: str) -> str:
    """A TOML basic string. Each value passed `_is_word`, so JSON's escapes are TOML's."""
    return json.dumps(value, ensure_ascii=False)


def _array(words: Sequence[str]) -> str:
    return "[" + ", ".join(_text(one) for one in words) + "]"


def _inline(pairs: Sequence[tuple[str, str]]) -> str:
    return "{ " + ", ".join(f"{key} = {_text(value)}" for key, value in pairs) + " }"


def render_install(servers: Sequence[Server], roots: Roots, listen: str) -> str:
    socket = str(roots.state / SOCKET)
    parts = [
        "# A draft of chaperone's install file, from the server files of one registry.\n"
        f"# {PROGRAM} printed it. Review each entry before you install it.\n"
        "version = 1\n",
        '\n[plugins.mcp-http]\nguard = "host-firewall"\n',
        f"\n[plugins.attendance]\nargs = {_array(['--socket', socket])}\n"
        f"secrets = {_inline(ATTENDANCE_SECRETS)}\n",
        "\n[plugins.release]\n"
        f"args = {_array(['--requests-dir', REQUESTS_DIR, '--requested-by', REQUESTED_BY])}\n",
        "\n[plugins.ha-approval]\n"
        f"args = {_array(['--hook-url', HOOK_URL, '--listen', listen])}\n"
        f"secrets = {_inline(APPROVAL_SECRETS)}\n",
    ]

    for server in servers:
        entry = f'\n[plugins.{server.name}]\nuser = "{USER_PREFIX}{server.name}"\n'
        if server.secrets:
            entry += f'secret_delivery = "env"\nsecrets = {_inline(server.secrets)}\n'
        parts.append(entry)

    parts.append('\n[slots]\napproval_channel = "ha-approval"\n')

    return "".join(parts)


def render_manifest(server: Server) -> str:
    words = [ENV_PROGRAM, *(f"{key}={value}" for key, value in server.env), *server.program]
    lines = [
        f"name = {_text(server.name)}",
        f"version = {_text(MANIFEST_VERSION)}",
        'kind = "effect"',
        'hooks = ["upstream"]',
    ]
    if server.secrets:
        lines.append(f"secrets = {_array([key for key, _ in server.secrets])}")

    lines += ["command = [", *(f"    {_text(one)}," for one in words), "]"]

    return "\n".join(lines) + "\n"


def draft(servers: Sequence[Server], roots: Roots, listen: str) -> tuple[DraftFile, ...]:
    install = DraftFile(INSTALL_PATH, render_install(servers, roots, listen))
    manifests = (
        DraftFile(ADDED_DIR / one.name / "plugin.toml", render_manifest(one)) for one in servers
    )

    return (install, *manifests)


def emit(files: Sequence[DraftFile], out_dir: Path | None) -> None:
    if out_dir is None:
        sys.stdout.write("".join(f"{HEADER}{one.path}\n{one.text}" for one in files))
        return

    try:
        out_dir.mkdir()
    except OSError as error:
        raise Refused(f"--out-dir {out_dir}: {error.strerror}. It must not exist yet") from None

    for one in files:
        target = out_dir / one.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(one.text, encoding="utf-8")


def _listen(text: str) -> str:
    found = LISTEN_RE.fullmatch(text)
    if found is None or found["host"] in UNSPECIFIED:
        raise argparse.ArgumentTypeError("give host:port, with a host that is not unspecified")

    return text


def _root(text: str) -> PurePosixPath:
    path = PurePosixPath(text)
    # `/usr/bin/env` reads a word with `=` as a variable, not as the program.
    if not path.is_absolute() or "=" in text or CONTROL_RE.search(text) is not None:
        raise argparse.ArgumentTypeError("give an absolute path with no '='")

    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.split("\n")[0])
    parser.add_argument("registry", type=Path, help="a registry checkout that holds mcp/")
    parser.add_argument("--approval-listen", type=_listen, required=True, help="host:port")
    parser.add_argument("--out-dir", type=Path, help="a new directory for the files")
    parser.add_argument("--state-root", type=_root, default=_root(STATE_ROOT))
    parser.add_argument("--mcp-root", type=_root, default=_root(MCP_ROOT))
    args = parser.parse_args(argv)
    roots = Roots(state=args.state_root, mcp=args.mcp_root)

    try:
        servers = read_servers(args.registry, roots)
        emit(draft(servers, roots, args.approval_listen), args.out_dir)
    except Refused as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return EXIT_REFUSED

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""Upstream credential loading (`stage7-releases.md` §4.3).

Two layouts, both read, because each holds real credentials.

1. **The monolith**, the `PEP_SECRETS` file: one sops file holding every
   name. `load_sops_secrets` reads it. Every upstream that predates the
   intake has its credential here.
2. **One file per secret**, `/var/lib/creche-handover/secrets/<name>.enc`:
   what the release executor's intake writes when the operator pastes a value
   (§4.3). `load_secret_dir` reads it. Adding a secret is then an
   ENCRYPT-ONLY operation, so the host can write a secret it cannot read.

A reload that read only the monolith would leave a value the operator pasted in a
file the PEP never opens, and the new upstream would start with no
credential. The two halves of "one action" would not meet.

Both are decrypted with the `chaperone` user's age key, and values live only in
PEP memory. Tests pass a plain dict and never touch sops."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Final, cast

import yaml
from yaml.reader import ReaderError

from .bounded_yaml import AliasLimitError, BoundedLoader, MergeLimitError

log = logging.getLogger("chaperone.secrets")

#: `handover/src/handover/intake/store.py` writes `<name>.enc`. The
#: pattern is contract 01b §4.1's secret name, checked here too: the file
#: name is input to this process, whoever wrote it.
SECRET_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,62}")

SECRET_SUFFIX: Final = ".enc"

#: A sealed value root will hand back. The largest is an armoured block
#: around an 8 KiB value, which matches the intake's own cap.
MAX_SECRET_BYTES: Final = 128 * 1024

#: How many per-secret files this will open in one pass. The fleet has one
#: or two credentials per server and about a dozen servers.
MAX_SECRET_FILES: Final = 256

#: What repairs the monolith. It decrypts only where a recipient key is: the
#: operator's account on the host is not one of its recipients.
MONOLITH_FIX: Final = "Fix it with sops, on a machine that holds a recipient key"

#: What repairs one pasted value. The intake refuses a name that already
#: has a file (`O_EXCL`), so the file goes first.
PASTED_FIX: Final = "Remove it as root and paste the value again"

#: A YAML error with no mark. None is known to reach here.
NO_POSITION: Final = "a position YAML did not give"


class SecretsError(RuntimeError):
    pass


class SecretsFormatError(ValueError):
    """`sops` decrypted the file and the text will not parse: not UTF-8,
    not YAML, or not a mapping.

    The message names the file and a position, never the content. PyYAML's
    own message quotes the line it failed on, and a decrypted line is a
    credential (invariant 13). Every raise is `from None`, so a traceback
    never prints the error it replaced.

    A `ValueError`, as `json.JSONDecodeError` is, and not a `SecretsError`.
    `sops` failing is a `SecretsError`, and the PEP serves without those
    credentials. This is a file an operator must fix: at start the PEP
    stops, and at a reload `reload_wiring.FILE_FAILURES` keeps the
    credentials in memory.
    """


def load_sops_secrets(path: Path) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["sops", "-d", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretsError(f"sops invocation failed: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Its message quotes the byte it failed on.
        raise _unparsable(path, f"not UTF-8 at byte {exc.start}") from None
    if proc.returncode != 0:
        raise SecretsError(f"sops -d {path} failed: {proc.stderr.strip()}")
    return _string_map(path, proc.stdout)


def _unparsable(path: Path, where: str) -> SecretsFormatError:
    return SecretsFormatError(f"{path}: {where}. {MONOLITH_FIX}")


def _string_map(path: Path, text: str) -> dict[str, str]:
    """The decrypted monolith as `{name: value}`, or `SecretsFormatError`.

    `ReaderError` is a control character, raised before any token. A
    `ValueError` of YAML's own is a scalar it matched and cannot build.
    """
    try:
        return _parse(path, text)
    except MergeLimitError as exc:
        raise _unparsable(path, f"merge keys past a limit at {_where(text, exc)}") from None
    except AliasLimitError as exc:
        raise _unparsable(path, f"aliases past a limit at {_where(text, exc)}") from None
    except yaml.YAMLError as exc:
        raise _unparsable(path, f"not valid YAML at {_where(text, exc)}") from None
    except RecursionError:
        raise _unparsable(path, "nested deeper than YAML can parse") from None


def _parse(path: Path, text: str) -> dict[str, str]:
    """Node by node rather than `safe_load`: a node carries its line, and
    the line is all a message here may say about an entry.

    The loader holds the merge keys and the aliases of the file to its
    three limits (`bounded_yaml`)."""
    loader = BoundedLoader(text)
    try:
        root = loader.get_single_node()
        if not isinstance(root, yaml.MappingNode):
            raise _unparsable(path, f"{_shape(root)}, not a mapping of names to values")

        loader.flatten_mapping(root)

        return _strings(path, loader, root)
    finally:
        loader.dispose()


def _strings(path: Path, loader: yaml.SafeLoader, root: yaml.MappingNode) -> dict[str, str]:
    found: dict[str, str] = {}
    for key_node, value_node in cast("list[tuple[yaml.Node, yaml.Node]]", root.value):
        key = _build(path, loader, key_node)
        value = _build(path, loader, value_node)
        if isinstance(key, str) and isinstance(value, str):
            found[key] = value
            continue

        # A mistyped scalar (a bare number or bool) would otherwise vanish
        # and surface later as "missing secret". The line, never the
        # key: a value pasted with a stray colon on a line of its own IS a
        # key.
        log.warning(
            "%s: line %d: dropping an entry, key and value must both be strings (got %s: %s)",
            path,
            key_node.start_mark.line + 1,
            type(key).__name__,
            type(value).__name__,
        )

    return found


def _build(path: Path, loader: yaml.SafeLoader, node: yaml.Node) -> object:
    """One node as Python. `2001-02-30` matches the date pattern and has no
    day 30, and that `ValueError` names no position."""
    try:
        # The stubs leave `node` untyped.
        return loader.construct_object(node, deep=True)  # pyright: ignore[reportUnknownMemberType]
    except ValueError:
        raise _unparsable(path, f"a value YAML cannot build at {_at(node.start_mark)}") from None


def _shape(node: yaml.Node | None) -> str:
    if node is None:
        return "empty"

    return "a sequence" if isinstance(node, yaml.SequenceNode) else "a scalar"


def _where(text: str, exc: yaml.YAMLError) -> str:
    """Where `exc` points, from its marks and never its message."""
    if isinstance(exc, ReaderError):
        before = text[: exc.position]
        line = before.count("\n") + 1
        column = len(before) - before.rfind("\n")

        return f"line {line}, column {column}"

    if not isinstance(exc, yaml.MarkedYAMLError):
        return NO_POSITION

    problem, context = exc.problem_mark, exc.context_mark
    if problem is None:
        return _at(context) if context is not None else NO_POSITION

    if context is None:
        return _at(problem)

    # An unclosed quote fails at the end of the text. Where it opened is
    # the line to fix.
    return f"{_at(problem)} (in what starts at {_at(context)})"


def _at(mark: yaml.Mark) -> str:
    """PyYAML counts from 0, and an editor from 1."""
    return f"line {mark.line + 1}, column {mark.column + 1}"


def load_secret_dir(directory: Path) -> dict[str, str]:
    """`<name>.enc` per secret (§4.3), decrypted one file at a time.

    Each file holds ONE value as raw bytes, not a
    mapping: the intake seals the value itself, so `sops -d` prints the
    value and nothing around it.

    **One file's failure is one missing credential, never the whole set.**
    A name that fails to decrypt is logged and skipped, and every other
    server keeps its own — which is contract 01b §4.1 rule 4 applied to
    the loader rather than to the spawn.

    A missing directory is not an error. It is the state before the first
    paste, and every upstream that has a credential in the monolith keeps
    working.
    """
    if not directory.is_dir() or directory.is_symlink():
        return {}

    found: dict[str, str] = {}
    for path in sorted(directory.iterdir())[:MAX_SECRET_FILES]:
        name = _secret_name(path)
        if name is None:
            continue

        value = _decrypt_one(path)
        if value is not None:
            found[name] = value

    return found


def _secret_name(path: Path) -> str | None:
    """`<name>.enc` and a regular file, or nothing. A symlink here would
    let whoever wrote the directory point `sops` at another file."""
    if path.is_symlink() or not path.is_file():
        return None

    if not path.name.endswith(SECRET_SUFFIX):
        return None

    name = path.name.removesuffix(SECRET_SUFFIX)

    return name if SECRET_NAME_RE.fullmatch(name) else None


def _decrypt_one(path: Path) -> str | None:
    """One value, or None with the reason logged. The value itself never
    reaches a log line (invariant 13)."""
    if path.stat().st_size > MAX_SECRET_BYTES:
        log.warning("%s: larger than %d bytes; skipping", path.name, MAX_SECRET_BYTES)

        return None

    try:
        proc = subprocess.run(
            ["sops", "-d", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("%s: sops invocation failed (%s); skipping", path.name, exc)

        return None
    except UnicodeDecodeError as exc:
        # Raised, not skipped, as before: a reload keeps every credential
        # in memory. The intake seals UTF-8 only, so another writer made
        # this file. Its message quotes the byte it failed on.
        raise SecretsFormatError(f"{path}: not UTF-8 at byte {exc.start}. {PASTED_FIX}") from None

    if proc.returncode != 0:
        # `stderr` is sops's own message about a KEY, never about a value.
        log.warning("%s: sops -d failed (%s); skipping", path.name, proc.stderr.strip()[:200])

        return None

    # The intake seals the value as binary, so what comes back IS the
    # value. A trailing newline is the shell's, not the secret's.
    return proc.stdout.rstrip("\n")

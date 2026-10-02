"""`component.yaml`, parsed as hostile input (contract 06 §8, §10).

A manifest can come from a branch an agent wrote (stage7-releases.md §3.2), so
this module trusts nothing: a byte cap before the parse, `yaml.safe_load` only,
a closed key set, a strict pattern per scalar, and a refusal that names the
field rather than echoing its value.

Two rules are load-bearing and easy to lose in a refactor.

1. Every pattern is matched with `re.fullmatch`. A trailing `$` also matches
   before a trailing newline, and these values become argv words and systemd
   unit names (stage7-releases.md §3.2 rule 3).
2. `build` and `verify.command` are lists of argv lists. A bare string is
   refused, because a string is the shape that reaches a shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, TypeVar, cast

import yaml

from .catalog import (
    MANIFEST_CONTRACT_MAJOR,
    MANIFEST_CONTRACT_MINOR,
    ContractId,
    Kind,
    Releases,
    Repo,
    RestoreMode,
    RunsAs,
    VerifyUser,
)
from .errors import Refusal, RefusalCode, safe_token
from .site import operator_home, operator_user

#: A `component.yaml` is one page of declarations. Sixteen kibibytes is four
#: times the largest one in this repo, and small enough that a refusal costs
#: one read.
MAX_MANIFEST_BYTES = 16 * 1024

MAX_LIST_ITEMS = 64
MAX_ARGV_ITEMS = 32
MAX_STRING_CHARS = 512
MAX_PATH_CHARS = 256
MAX_CONTRACT_NUMBER = 999

TIMEOUT_MIN_S = 1
TIMEOUT_MAX_S = 300
KEEP_MIN = 1
KEEP_MAX = 10

NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
CONTRACT_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
UNIT_RE = re.compile(r"[A-Za-z0-9@_.-]{1,64}")
SECRET_RE = re.compile(r"[a-z][a-z0-9_]{1,62}")
PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
ANY_TEXT_RE = re.compile(r".{1,512}", re.DOTALL)

MANIFEST_FILENAME = "component.yaml"

REPO_ROOT_PATH = "."

#: The path segment that climbs out of a directory.
PARENT_SEGMENT = ".."

#: The word a manifest writes for the site's operator account.
OPERATOR_ACCOUNT = "operator"

#: Contract 06 §8's field table, exactly. The key set is closed: an unknown
#: key is a refusal, not a warning.
REQUIRED_FIELDS = frozenset(
    {
        "manifest_version",
        "name",
        "repo",
        "path",
        "kind",
        "unit",
        "runs_as",
        "install",
        "verify",
        "restore",
        "release",
    }
)
OPTIONAL_FIELDS = frozenset({"build", "provides", "requires", "depends_on", "secrets"})
KNOWN_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS

StrEnumT = TypeVar("StrEnumT", bound=StrEnum)


@dataclass(frozen=True)
class Provided:
    """Contract 06 §3.1: answers everything valid in `major.0` to `major.minor`."""

    contract: ContractId
    major: int
    minor: int


@dataclass(frozen=True)
class Required:
    """Contract 06 §3.1: calls only what is valid in `major.min_minor`."""

    contract: ContractId
    major: int
    min_minor: int


@dataclass(frozen=True)
class Verify:
    command: tuple[str, ...]
    user: VerifyUser
    timeout_s: int


@dataclass(frozen=True)
class Restore:
    mode: RestoreMode
    keep: int


@dataclass(frozen=True)
class Install:
    to: str
    prev: str


@dataclass(frozen=True)
class ComponentManifest:
    """One parsed `component.yaml`. Every field has passed its own pattern."""

    manifest_version: str
    name: str
    repo: Repo
    path: str
    kind: Kind
    unit: str | None
    runs_as: RunsAs
    build: tuple[tuple[str, ...], ...]
    install: Install
    provides: tuple[Provided, ...]
    requires: tuple[Required, ...]
    depends_on: tuple[str, ...]
    verify: Verify
    restore: Restore
    secrets: tuple[str, ...]
    release: Releases


class _Reader:
    """Field access that refuses instead of raising a type error.

    `subject` is the label a refusal names: a caller-chosen path such as
    `pep/component.yaml`, never a value read out of the document.
    """

    def __init__(self, subject: str, body: dict[str, Any]) -> None:
        self.subject = subject
        self._body = body

    def refuse(self, detail: str) -> Refusal:
        return Refusal(RefusalCode.MANIFEST, self.subject, detail)

    def raw(self, field: str) -> Any:
        return self._body.get(field)

    def text(self, field: str, pattern: re.Pattern[str]) -> str:
        value = self._body.get(field)
        if not isinstance(value, str):
            raise self.refuse(f"field '{field}' must be a string")

        if len(value) > MAX_STRING_CHARS:
            raise self.refuse(f"field '{field}' is longer than {MAX_STRING_CHARS} characters")

        if not pattern.fullmatch(value):
            raise self.refuse(f"field '{field}' does not match its pattern: {safe_token(value)}")

        return value

    def whole(self, field: str, allowed: type[StrEnumT]) -> StrEnumT:
        value = self._body.get(field)
        if not isinstance(value, str):
            raise self.refuse(f"field '{field}' must be a string")

        try:
            return allowed(value)
        except ValueError:
            detail = f"field '{field}' is not one of its values: {safe_token(value)}"
            raise self.refuse(detail) from None

    def integer(self, field: str, low: int, high: int) -> int:
        value = self._body.get(field)
        # A YAML bool is a Python int. Refuse it before the range check.
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.refuse(f"field '{field}' must be a whole number")

        if value < low or value > high:
            raise self.refuse(f"field '{field}' must be between {low} and {high}")

        return value

    def mapping(self, field: str, keys: frozenset[str]) -> _Reader:
        value = self._body.get(field)
        if not isinstance(value, dict):
            raise self.refuse(f"field '{field}' must be a mapping")

        nested = f"{self.subject}: {field}"
        body = _as_string_keyed(cast(dict[Any, Any], value), nested, field)
        _refuse_unknown_keys(body, keys, nested)

        return _Reader(nested, body)

    def items(self, field: str) -> list[Any]:
        """A list field. Absent means empty. A string is never a list here."""
        value = self._body.get(field)
        if value is None:
            return []

        if not isinstance(value, list):
            raise self.refuse(f"field '{field}' must be a list")

        entries = cast(list[Any], value)
        if len(entries) > MAX_LIST_ITEMS:
            raise self.refuse(f"field '{field}' holds more than {MAX_LIST_ITEMS} items")

        return entries


def _as_string_keyed(value: dict[Any, Any], subject: str, field: str) -> dict[str, Any]:
    """YAML allows a non-string key. The schema does not."""
    body: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise Refusal(RefusalCode.MANIFEST, subject, f"field '{field}' has a non-string key")

        body[key] = item

    return body


def _refuse_unknown_keys(body: dict[str, Any], known: frozenset[str], subject: str) -> None:
    unknown = sorted(set(body) - known)
    if not unknown:
        return

    raise Refusal(RefusalCode.MANIFEST, subject, f"unknown field: {safe_token(unknown[0])}")


def check_repo_path(value: str, subject: str) -> str:
    """A repo-relative path that cannot leave the repository.

    `.` means the repository root, which is how a whole-repo component such as
    `mcp-servers` names itself (contract 06 §1).
    """
    if value == REPO_ROOT_PATH:
        return value

    if len(value) > MAX_PATH_CHARS:
        raise Refusal(RefusalCode.MANIFEST, subject, "path is too long")

    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise Refusal(RefusalCode.MANIFEST, subject, "path must be relative and plain")

    for segment in value.split("/"):
        if segment in {".", ".."} or not PATH_SEGMENT_RE.fullmatch(segment):
            detail = f"path segment is not usable: {safe_token(segment)}"
            raise Refusal(RefusalCode.MANIFEST, subject, detail)

    return value


def check_absolute_path(value: str, subject: str, field: str) -> str:
    """An install target or an `argv[0]`: absolute, normalized, no traversal."""
    if len(value) > MAX_PATH_CHARS:
        raise Refusal(RefusalCode.MANIFEST, subject, f"field '{field}' path is too long")

    if not value.startswith("/") or "\x00" in value or value.endswith("/"):
        raise Refusal(RefusalCode.MANIFEST, subject, f"field '{field}' must be an absolute path")

    if str(PurePosixPath(value)) != value:
        raise Refusal(RefusalCode.MANIFEST, subject, f"field '{field}' must be a normalized path")

    # `PurePosixPath` keeps `..`, and every containment check after this one
    # reads the text of the path: `/opt/components/../../etc` starts inside.
    if PARENT_SEGMENT in PurePosixPath(value).parts:
        raise Refusal(RefusalCode.MANIFEST, subject, f"field '{field}' must not hold '..'")

    return value


#: A path that starts here is under the operator's home (`site.py`), e.g.
#: `~/.local/components/sessiond`. A manifest names no host's account, so it
#: cannot spell that home out. `~name/` is not this and stays refused.
OPERATOR_HOME_PREFIX = "~/"


def _from_home(value: str) -> str:
    """`~/x` as `<the operator's home>/x`. Any other value comes back as it
    is, for `check_absolute_path` to judge."""
    if not value.startswith(OPERATOR_HOME_PREFIX):
        return value

    return f"{operator_home()}/{value[len(OPERATOR_HOME_PREFIX) :]}"


def _names_the_operator(value: object) -> bool:
    """Whether `value` is the operator's own account name on this host."""
    if not isinstance(value, str):
        return False

    try:
        return value == operator_user()
    except Refusal:
        return False


def _read_account[AccountT: StrEnum](
    reader: _Reader, field: str, allowed: type[AccountT]
) -> AccountT:
    """`runs_as` or `verify.user`: one of the enum's words.

    `operator` is the site's operator account. A manifest stamped into a
    live tree before that word existed names the account itself, and reads
    as `operator` on the host whose operator it is. Root reads those stamps
    to learn what is live, so refusing them would refuse every release.
    """
    try:
        return reader.whole(field, allowed)
    except Refusal:
        if not _names_the_operator(reader.raw(field)):
            raise

        return allowed(OPERATOR_ACCOUNT)


def _read_argv(raw: Any, reader: _Reader, field: str) -> tuple[str, ...]:
    """One argv list. A string is refused: a string is what reaches a shell."""
    if not isinstance(raw, list):
        raise reader.refuse(f"field '{field}' must be a list of argv words")

    items = cast(list[Any], raw)
    if not items or len(items) > MAX_ARGV_ITEMS:
        raise reader.refuse(f"field '{field}' argv holds 1 to {MAX_ARGV_ITEMS} words")

    argv: list[str] = []
    for item in items:
        if not isinstance(item, str) or len(item) > MAX_STRING_CHARS:
            raise reader.refuse(f"field '{field}' argv words are short strings")

        if "\x00" in item or "\n" in item:
            raise reader.refuse(f"field '{field}' argv words hold no NUL and no newline")

        argv.append(item)

    argv[0] = check_absolute_path(_from_home(argv[0]), reader.subject, f"{field}[0]")

    return tuple(argv)


def _read_build(reader: _Reader) -> tuple[tuple[str, ...], ...]:
    return tuple(_read_argv(entry, reader, "build") for entry in reader.items("build"))


ContractRefs = list[tuple[ContractId, int, int]]


def _read_contract_refs(reader: _Reader, field: str, minor_key: str) -> ContractRefs:
    """`provides` and `requires` share a shape and differ only in the minor key."""
    keys = frozenset({"contract", "major", minor_key})
    nested = f"{reader.subject}: {field}"

    refs: list[tuple[ContractId, int, int]] = []
    for entry in reader.items(field):
        if not isinstance(entry, dict):
            raise reader.refuse(f"field '{field}' holds mappings")

        body = _as_string_keyed(cast(dict[Any, Any], entry), nested, field)
        _refuse_unknown_keys(body, keys, nested)
        item = _Reader(nested, body)
        refs.append(
            (
                item.whole("contract", ContractId),
                item.integer("major", 0, MAX_CONTRACT_NUMBER),
                item.integer(minor_key, 0, MAX_CONTRACT_NUMBER),
            )
        )

    return refs


def _read_depends_on(reader: _Reader) -> tuple[str, ...]:
    names: list[str] = []
    for entry in reader.items("depends_on"):
        if not isinstance(entry, str) or not NAME_RE.fullmatch(entry):
            raise reader.refuse("depends_on holds component names")

        if entry in names:
            raise reader.refuse(f"depends_on repeats {safe_token(entry)}")

        names.append(entry)

    return tuple(names)


def _read_secrets(reader: _Reader) -> tuple[str, ...]:
    names: list[str] = []
    for entry in reader.items("secrets"):
        if not isinstance(entry, str) or not SECRET_RE.fullmatch(entry):
            raise reader.refuse("secrets holds names, never values")

        names.append(entry)

    return tuple(names)


def _read_unit(reader: _Reader) -> str | None:
    if reader.raw("unit") is None:
        return None

    return reader.text("unit", UNIT_RE)


def _read_release(reader: _Reader) -> Releases:
    """`release: yes` is a YAML bool. `release: "yes"` is a string. Take both."""
    value = reader.raw("release")
    if isinstance(value, bool):
        return Releases.YES if value else Releases.NO

    return reader.whole("release", Releases)


def _check_manifest_version(value: str, subject: str) -> str:
    """This contract's major, at or below its minor. §3.1's `provides` rule."""
    matched = CONTRACT_VERSION_RE.fullmatch(value)
    if not matched:
        raise Refusal(RefusalCode.MANIFEST, subject, "manifest_version is MAJOR.MINOR")

    major, minor = int(matched.group(1)), int(matched.group(2))
    if major != MANIFEST_CONTRACT_MAJOR or minor > MANIFEST_CONTRACT_MINOR:
        ceiling = f"{MANIFEST_CONTRACT_MAJOR}.{MANIFEST_CONTRACT_MINOR}"
        detail = f"manifest_version {value} is outside {MANIFEST_CONTRACT_MAJOR}.0 to {ceiling}"
        raise Refusal(RefusalCode.MANIFEST, subject, detail)

    return value


def _read_install(reader: _Reader, subject: str) -> Install:
    install = reader.mapping("install", frozenset({"to", "prev"}))
    to = check_absolute_path(_from_home(install.text("to", ANY_TEXT_RE)), subject, "install.to")
    prev = _from_home(install.text("prev", ANY_TEXT_RE))
    prev = check_absolute_path(prev, subject, "install.prev")
    if to == prev:
        raise Refusal(RefusalCode.MANIFEST, subject, "install.prev must differ from install.to")

    return Install(to=to, prev=prev)


def _read_verify(reader: _Reader) -> Verify:
    verify = reader.mapping("verify", frozenset({"command", "user", "timeout_s"}))

    return Verify(
        command=_read_argv(verify.raw("command"), verify, "command"),
        user=_read_account(verify, "user", VerifyUser),
        timeout_s=verify.integer("timeout_s", TIMEOUT_MIN_S, TIMEOUT_MAX_S),
    )


def _read_restore(reader: _Reader) -> Restore:
    restore = reader.mapping("restore", frozenset({"mode", "keep"}))

    return Restore(
        mode=restore.whole("mode", RestoreMode),
        keep=restore.integer("keep", KEEP_MIN, KEEP_MAX),
    )


def parse_manifest(text: str, subject: str) -> ComponentManifest:
    """Parse one `component.yaml`. `subject` is the label a refusal names."""
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise Refusal(RefusalCode.MANIFEST, subject, f"larger than {MAX_MANIFEST_BYTES} bytes")

    try:
        loaded: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        detail = f"does not parse: {_yaml_where(error)}"
        raise Refusal(RefusalCode.MANIFEST, subject, detail) from None

    if not isinstance(loaded, dict):
        raise Refusal(RefusalCode.MANIFEST, subject, "is not a mapping")

    body = _as_string_keyed(cast(dict[Any, Any], loaded), subject, "<top level>")
    _refuse_unknown_keys(body, KNOWN_FIELDS, subject)

    missing = sorted(REQUIRED_FIELDS - set(body))
    if missing:
        raise Refusal(RefusalCode.MANIFEST, subject, f"missing required field: {missing[0]}")

    reader = _Reader(subject, body)
    provides = _read_contract_refs(reader, "provides", "minor")
    requires = _read_contract_refs(reader, "requires", "min_minor")
    declared = reader.text("manifest_version", CONTRACT_VERSION_RE)

    return ComponentManifest(
        manifest_version=_check_manifest_version(declared, subject),
        name=reader.text("name", NAME_RE),
        repo=reader.whole("repo", Repo),
        path=check_repo_path(reader.text("path", ANY_TEXT_RE), subject),
        kind=reader.whole("kind", Kind),
        unit=_read_unit(reader),
        runs_as=_read_account(reader, "runs_as", RunsAs),
        build=_read_build(reader),
        install=_read_install(reader, subject),
        provides=tuple(Provided(item, major, minor) for item, major, minor in provides),
        requires=tuple(Required(item, major, floor) for item, major, floor in requires),
        depends_on=_read_depends_on(reader),
        verify=_read_verify(reader),
        restore=_read_restore(reader),
        secrets=_read_secrets(reader),
        release=_read_release(reader),
    )


def _yaml_where(error: yaml.YAMLError) -> str:
    """The line number, never the offending text: the text is hostile input."""
    mark = getattr(error, "problem_mark", None)
    if mark is None:
        return "unreadable YAML"

    return f"line {getattr(mark, 'line', 0) + 1}"

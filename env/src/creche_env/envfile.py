"""The one reader of the env files: `default.env`, `example.env` and a `.env`.

A product ships a pair. `default.env` gives each key its default, and
`example.env` names each key that has no default. The host gives one
`.env`, which overrides a default key by key and sets each required key.
Each program of the product reads the files through this module, or through
the command `creche-env`, so one text has one meaning for each reader. A
Rust reader holds the same rules, and the vectors of the surfaces
`envfile.*` hold the two equal.

`env/AGENTS.md` has the grammar, the order of the layers and the meaning of
each fault. Each check stops at its first fault. Two checks name each key
that fails: the check of the required keys and the check of the
environment. Layers 4 and 5 visit the keys in the order of the catalog, and
the references of one value from left to right.
"""

from __future__ import annotations

import errno
import os
import pwd
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

#: The most bytes of one file, and of one value after the references.
MAX_BYTES: Final = 64 * 1024

#: The most references in one chain. `A=${B}` is a chain of one.
MAX_DEPTH: Final = 8

#: The `.env` of the product on a host, and the variable that names another
#: one. A test sets the variable, and nothing on a host does.
SITE_FILE: Final = Path("/etc/creche/site.env")
SITE_FILE_ENV: Final = "AGENT_SITE_FILE"

#: The three files of the package data.
DEFAULT_FILE: Final = "default.env"
EXAMPLE_FILE: Final = "example.env"
ROOT_KEYS_FILE: Final = "root-keys.txt"

#: Root's uid. For root, only a file of root passes the trust check.
ROOT_UID: Final = 0

#: The write bits of group and other.
OTHERS_WRITE_BITS: Final = stat.S_IWGRP | stat.S_IWOTH

COMMENT: Final = "#"
ASSIGN: Final = "="
LINE_END: Final = "\n"
SPACE: Final = " "
REFERENCE_SIGN: Final = "$"
BYTE_ORDER_MARK: Final = "\ufeff"
CARRIAGE_RETURN: Final = "\r"

#: One key.
_KEY_TEXT: Final = r"[A-Z][A-Z0-9_]{0,63}"
_KEY: Final = re.compile(_KEY_TEXT)

#: The characters of a value: printable ASCII and the space, but `"`, `#`,
#: `'` and `\`.
_VALUE: Final = re.compile(r"[\x20\x21\x24-\x26\x28-\x5b\x5d-\x7e]*")

#: The two forms of a reference, and each form alone.
_PLACE: Final = re.compile(rf"\$\{{(?:home:)?{_KEY_TEXT}\}}")
_HOME_PLACE: Final = re.compile(rf"\$\{{home:({_KEY_TEXT})\}}")
_REFERENCE: Final = re.compile(rf"\$\{{({_KEY_TEXT})\}}")


class Fault(StrEnum):
    """Each fault of the reader. The Rust reader has the same names."""

    TOO_LARGE = "too-large"
    NOT_UTF8 = "not-utf8"
    BYTE_ORDER_MARK = "byte-order-mark"
    CARRIAGE_RETURN = "carriage-return"
    NO_FINAL_NEWLINE = "no-final-newline"
    NOT_KEY_VALUE = "not-key-value"
    BAD_KEY = "bad-key"
    BAD_VALUE = "bad-value"
    EMPTY_VALUE = "empty-value"
    DUPLICATE_KEY = "duplicate-key"
    UNKNOWN_KEY = "unknown-key"
    REFERENCE_IN_ENV = "reference-in-env"
    UNKNOWN_REFERENCE = "unknown-reference"
    UNSET_REFERENCE = "unset-reference"
    REFERENCE_CYCLE = "reference-cycle"
    REFERENCE_TOO_DEEP = "reference-too-deep"
    UNKNOWN_ACCOUNT = "unknown-account"
    REQUIRED_UNSET = "required-unset"
    ENVIRONMENT_DIFFERS = "environment-differs"
    WRONG_OWNER = "wrong-owner"
    OTHERS_WRITE = "others-write"


class FileKind(Enum):
    """The three files, each with its rules for a value."""

    DEFAULT = DEFAULT_FILE
    EXAMPLE = EXAMPLE_FILE
    ENV = ".env"


@dataclass(frozen=True, slots=True)
class Problem:
    """One fault: the file, the line (0 for the whole file) and the key
    (empty for no key)."""

    fault: Fault
    file: str
    line: int
    key: str

    def text(self) -> str:
        """The fault as one line, for example `site.env:3: unknown-key AGENT_X`."""
        where = f"{self.file}:{self.line}" if self.line else self.file
        name = f" {self.key}" if self.key else ""

        return f"{where}: {self.fault}{name}"


class EnvError(Exception):
    """The files give no settings. `problems` holds one fault or more."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems: tuple[Problem, ...] = tuple(problems)
        super().__init__("; ".join(one.text() for one in self.problems))


class ReadError(Exception):
    """A file that this process cannot read. It is no fault of a text.

    CONTRACT-QUESTION: the fault set has no name for it. Reading taken: no
    `Fault`, and the command exits with 78. A change costs one name.
    """

    def __init__(self, file: str, reason: str) -> None:
        self.file = file
        self.reason = reason
        super().__init__(f"{file}: cannot be read: {reason}")


def _fault(fault: Fault, file: str, line: int = 0, key: str = "") -> EnvError:
    return EnvError((Problem(fault, file, line, key),))


@dataclass(frozen=True, slots=True)
class Text:
    """The bytes of one file, and the name that a fault gives the file."""

    name: str
    data: bytes


class _Made:
    """No module but this one names this type. `Line`, `Pair` and `Settings`
    take a value of it first, so only the readers of this module make one:
    pyright refuses the private name in each other module, and each type
    refuses each other value."""


_MADE: Final = _Made()


def _require_made(made: _Made, kind: str) -> None:
    if made is not _MADE:
        raise TypeError(f"only the readers of creche_env.envfile make a {kind}")


@dataclass(frozen=True, slots=True)
class Line:
    """One assignment: the key, the value and the number of its line.
    `parse` is the one maker, and the accessors read the fields."""

    made: InitVar[_Made]
    _key: str
    _value: str
    _number: int

    def __post_init__(self, made: _Made) -> None:
        _require_made(made, "Line")

    @property
    def key(self) -> str:
        return self._key

    @property
    def value(self) -> str:
        return self._value

    @property
    def number(self) -> int:
        return self._number


def _lines(source: Text) -> list[str]:
    """The lines of one file, each without its LF, after the checks of the
    whole file."""
    if len(source.data) > MAX_BYTES:
        raise _fault(Fault.TOO_LARGE, source.name)

    try:
        text = source.data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fault(Fault.NOT_UTF8, source.name) from None

    if text.startswith(BYTE_ORDER_MARK):
        raise _fault(Fault.BYTE_ORDER_MARK, source.name)

    at = text.find(CARRIAGE_RETURN)
    if at >= 0:
        raise _fault(Fault.CARRIAGE_RETURN, source.name, text.count(LINE_END, 0, at) + 1)

    if text and not text.endswith(LINE_END):
        raise _fault(Fault.NO_FINAL_NEWLINE, source.name)

    return text.split(LINE_END)[:-1]


def parse(source: Text, kind: FileKind) -> tuple[Line, ...]:
    """Each assignment of one file, in the order of its lines. For
    `example.env`, each declared required key."""
    found: list[Line] = []
    seen: set[str] = set()
    for number, line in enumerate(_lines(source), start=1):
        one = _line(source.name, number, line, kind)
        if one is None:
            continue

        if one.key in seen:
            raise _fault(Fault.DUPLICATE_KEY, source.name, number, one.key)

        seen.add(one.key)
        found.append(one)

    return tuple(found)


def _line(name: str, number: int, line: str, kind: FileKind) -> Line | None:
    if not line:
        return None

    if line.startswith(COMMENT):
        return _declaration(name, number, line) if kind is FileKind.EXAMPLE else None

    if kind is FileKind.EXAMPLE:
        # Each line of `example.env` is a comment: a copy of the file sets
        # nothing.
        raise _fault(Fault.NOT_KEY_VALUE, name, number)

    return _assignment(name, number, line, kind)


def _declaration(name: str, number: int, line: str) -> Line | None:
    """`#KEY=VALUE` declares a required key. `#` alone and `# text` are
    prose. The value to show has the rules of a `.env` value."""
    body = line.removeprefix(COMMENT)
    if not body or body.startswith(SPACE):
        return None

    # CONTRACT-QUESTION: no contract text says what `#text` with no space
    # is in `example.env`. Reading taken: a declaration, so each such line
    # must be one. A change costs this branch and its vectors.

    return _assignment(name, number, body, FileKind.ENV)


def _assignment(name: str, number: int, line: str, kind: FileKind) -> Line:
    key, sign, value = line.partition(ASSIGN)
    if not sign:
        raise _fault(Fault.NOT_KEY_VALUE, name, number)

    if _KEY.fullmatch(key) is None:
        raise _fault(Fault.BAD_KEY, name, number)

    _check_value(name, number, key, value, kind)

    return Line(_MADE, key, value, number)


def _check_value(name: str, number: int, key: str, value: str, kind: FileKind) -> None:
    if _VALUE.fullmatch(value) is None or value != value.strip(SPACE):
        raise _fault(Fault.BAD_VALUE, name, number, key)

    if not value:
        if kind is FileKind.DEFAULT:
            return

        raise _fault(Fault.EMPTY_VALUE, name, number, key)

    if REFERENCE_SIGN not in value:
        return

    if kind is not FileKind.DEFAULT:
        raise _fault(Fault.REFERENCE_IN_ENV, name, number, key)

    if REFERENCE_SIGN in _PLACE.sub("", value):
        raise _fault(Fault.BAD_VALUE, name, number, key)


def _is_plain(value: str) -> bool:
    """Whether `value` is a value that a `.env` can hold."""
    return (
        bool(value)
        and _VALUE.fullmatch(value) is not None
        and value == value.strip(SPACE)
        and REFERENCE_SIGN not in value
    )


@dataclass(frozen=True, slots=True)
class Pair:
    """The catalog: the defaults of `default.env` and the required keys of
    `example.env`, with the names of the two files. `read_pair` is the one
    maker, and the accessors read the fields."""

    made: InitVar[_Made]
    _defaults: tuple[Line, ...]
    _required: tuple[Line, ...]
    _default_file: str
    _example_file: str

    def __post_init__(self, made: _Made) -> None:
        _require_made(made, "Pair")

    @property
    def defaults(self) -> tuple[Line, ...]:
        return self._defaults

    @property
    def required(self) -> tuple[Line, ...]:
        return self._required

    @property
    def default_file(self) -> str:
        return self._default_file

    @property
    def example_file(self) -> str:
        return self._example_file

    def catalog(self) -> tuple[str, ...]:
        """Each catalog key: the defaults, then the required keys."""
        return tuple(one.key for one in (*self._defaults, *self._required))


def read_pair(default: Text, example: Text) -> Pair:
    """The catalog of one pair. A required key with a default is refused."""
    defaults = parse(default, FileKind.DEFAULT)
    required = parse(example, FileKind.EXAMPLE)
    known = {one.key for one in defaults}
    for one in required:
        if one.key in known:
            raise _fault(Fault.DUPLICATE_KEY, example.name, one.number, one.key)

    return Pair(_MADE, defaults, required, default.name, example.name)


@dataclass(frozen=True, slots=True)
class Settings:
    """What the layers give. `values`: each catalog key that has a value.
    `sources`: each catalog key in catalog order, with the file and the line
    of the text that it took. `load` is the one maker. The two maps are
    read-only views."""

    made: InitVar[_Made]
    _values: Mapping[str, str]
    _sources: Mapping[str, tuple[str, int]]

    def __post_init__(self, made: _Made) -> None:
        _require_made(made, "Settings")

    @property
    def values(self) -> Mapping[str, str]:
        return self._values

    @property
    def sources(self) -> Mapping[str, tuple[str, int]]:
        return self._sources


#: The home of an account, or None for an account that the database lacks.
type HomeLookup = Callable[[str], str | None]


def system_home(account: str) -> str | None:
    """The home of `account` in the password database of this host."""
    try:
        return pwd.getpwnam(account).pw_dir
    except KeyError:
        return None


def load(pair: Pair, env: Text, homes: HomeLookup | None = None) -> Settings:
    """The settings of one pair and one `.env`: each layer of the module
    docstring. `homes` is the password database, `system_home` when None.

    CONTRACT-QUESTION: no contract text orders the check of the required
    keys and the references. Reading taken: the required keys come first, so
    that a copy of `example.env` names each required key and no reference.
    A change costs the order here, its vectors and the Rust reader.
    """
    texts = {one.key: one.value for one in pair.defaults}
    where = {one.key: (pair.default_file, one.number) for one in pair.defaults}
    catalog = set(pair.catalog())
    for one in parse(env, FileKind.ENV):
        if one.key not in catalog:
            raise _fault(Fault.UNKNOWN_KEY, env.name, one.number, one.key)

        texts[one.key] = one.value
        where[one.key] = (env.name, one.number)

    missing = [
        Problem(Fault.REQUIRED_UNSET, pair.example_file, one.number, one.key)
        for one in pair.required
        if one.key not in texts
    ]
    if missing:
        raise EnvError(missing)

    sources = {key: where[key] for key in pair.catalog()}
    with_homes = _with_homes(texts, sources, homes or system_home)

    values = _resolve(with_homes, sources)

    return Settings(_MADE, MappingProxyType(values), MappingProxyType(sources))


def _with_homes(
    texts: Mapping[str, str], sources: Mapping[str, tuple[str, int]], homes: HomeLookup
) -> dict[str, str]:
    """Layer 4: each `${home:KEY}` as the home of the account in KEY."""
    out = dict(texts)
    for key, (file, line) in sources.items():
        parts: list[str] = []
        last = 0
        text = texts[key]
        for match in _HOME_PLACE.finditer(text):
            account = texts.get(match.group(1))
            if account is None:
                raise _fault(Fault.UNKNOWN_REFERENCE, file, line, key)

            if not account:
                raise _fault(Fault.UNSET_REFERENCE, file, line, key)

            home = homes(account)
            if home is None:
                raise _fault(Fault.UNKNOWN_ACCOUNT, file, line, key)

            # CONTRACT-QUESTION: no contract text gives a home a grammar.
            # Reading taken: a home is a value that a `.env` can hold.
            if not _is_plain(home):
                raise _fault(Fault.BAD_VALUE, file, line, key)

            parts.extend((text[last : match.start()], home))
            last = match.end()

        parts.append(text[last:])
        out[key] = "".join(parts)

    return out


def _resolve(texts: Mapping[str, str], sources: Mapping[str, tuple[str, int]]) -> dict[str, str]:
    """Layer 5: each `${KEY}` as the value of KEY.

    A fault names the key whose value holds the reference that fails. A
    chain that is too long is the one exception: the fault names the first
    key of the catalog whose chain has more than `MAX_DEPTH` references.

    CONTRACT-QUESTION: no contract text limits a value after its
    references. Reading taken: `MAX_BYTES`, the limit of a file, and a
    longer value is `too-large`. A change costs this check and its vector.
    """
    done: dict[str, tuple[str, int]] = {}

    def visit(path: tuple[str, ...]) -> tuple[str, int]:
        """The value of the last key of `path`, and its longest chain."""
        key = path[-1]
        known = done.get(key)
        if known is not None:
            return known

        file, line = sources[key]
        top_file, top_line = sources[path[0]]
        too_deep = _fault(Fault.REFERENCE_TOO_DEEP, top_file, top_line, path[0])
        text = texts[key]
        parts: list[str] = []
        size = 0
        height = 0
        last = 0
        for match in _REFERENCE.finditer(text):
            name = match.group(1)
            if name not in texts:
                raise _fault(Fault.UNKNOWN_REFERENCE, file, line, key)

            if name in path:
                raise _fault(Fault.REFERENCE_CYCLE, file, line, key)

            if not texts[name]:
                raise _fault(Fault.UNSET_REFERENCE, file, line, key)

            if len(path) > MAX_DEPTH:
                raise too_deep

            value, below = visit((*path, name))
            height = max(height, below + 1)
            if height > MAX_DEPTH:
                raise too_deep

            for piece in (text[last : match.start()], value):
                size += len(piece)
                if size > MAX_BYTES:
                    raise _fault(Fault.TOO_LARGE, file, line, key)

                parts.append(piece)

            last = match.end()

        if size + len(text) - last > MAX_BYTES:
            raise _fault(Fault.TOO_LARGE, file, line, key)

        parts.append(text[last:])
        result = ("".join(parts), height)
        done[key] = result

        return result

    values: dict[str, str] = {}
    for key in sources:
        value, _ = visit((key,))
        if value:
            values[key] = value

    return values


def check_environment(settings: Settings, environ: Mapping[str, str]) -> None:
    """Refuse each catalog key of `environ` whose value is not the value of
    the files. An optional key with no value has the empty value. A
    variable outside the catalog is free. The fault names the line that
    gave the key its text. A root program calls this at its start."""
    problems = [
        Problem(Fault.ENVIRONMENT_DIFFERS, file, line, key)
        for key, (file, line) in settings.sources.items()
        if key in environ and environ[key] != settings.values.get(key, "")
    ]
    if problems:
        raise EnvError(problems)


def raw(source: Text) -> dict[str, str]:
    """The grammar-only mode: each assignment of one file with the rules of
    a `.env`, with no catalog and no reference. For a file that is not a
    part of the catalog.

    CONTRACT-QUESTION: no contract text gives this mode the rules of a
    value. Reading taken: the rules of a `.env`, so no empty value and no
    `$`. A change costs the kind of file here and its vectors.
    """
    return {one.key: one.value for one in parse(source, FileKind.ENV)}


def read_root_keys(source: Text, pair: Pair) -> tuple[str, ...]:
    """`root-keys.txt`: one catalog key on each line, each one time."""
    catalog = set(pair.catalog())
    names: list[str] = []
    for number, line in enumerate(_lines(source), start=1):
        if _KEY.fullmatch(line) is None:
            raise _fault(Fault.BAD_KEY, source.name, number)

        if line in names:
            raise _fault(Fault.DUPLICATE_KEY, source.name, number, line)

        if line not in catalog:
            raise _fault(Fault.UNKNOWN_KEY, source.name, number, line)

        names.append(line)

    return tuple(names)


def is_key(text: str) -> bool:
    """Whether `text` has the grammar of a key."""
    return _KEY.fullmatch(text) is not None


def check_trust(name: str, owner: int, mode: int, reader: int) -> None:
    """Refuse a file that another account can change. The owner is root or
    `reader`, and group and other cannot write the file. For root, only a
    file of root passes."""
    if owner not in (ROOT_UID, reader):
        raise _fault(Fault.WRONG_OWNER, name)

    if mode & OTHERS_WRITE_BITS:
        raise _fault(Fault.OTHERS_WRITE, name)


def _reason(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, type(error).__name__)


class Owner(Enum):
    """Whose file a read takes. The owner of a tree answers for its pair."""

    CHECKED = "checked"
    ANY = "any"


def _read(path: Path, owner: Owner) -> Text:
    """At most `MAX_BYTES` and one byte more of the regular file at `path`,
    through one open. The trust check judges the file that the open gave,
    whatever `path` names after the open."""
    name = str(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise ReadError(name, _reason(error)) from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReadError(name, "no regular file")

        if owner is Owner.CHECKED:
            check_trust(name, info.st_uid, info.st_mode, os.getuid())

        return Text(name, _bytes_of(fd))
    except OSError as error:
        raise ReadError(name, _reason(error)) from None
    finally:
        os.close(fd)


def _bytes_of(fd: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size <= MAX_BYTES:
        chunk = os.read(fd, MAX_BYTES + 1 - size)
        if not chunk:
            break

        chunks.append(chunk)
        size += len(chunk)

    return b"".join(chunks)


def read_trusted(path: Path) -> Text:
    """The bytes of a `.env`, after the trust check of the opened file. A
    link is followed: its target is the file that the check judges."""
    return _read(path, Owner.CHECKED)


def site_file(environ: Mapping[str, str]) -> Path:
    """The `.env` of the product: `AGENT_SITE_FILE`, else `SITE_FILE`."""
    named = environ.get(SITE_FILE_ENV, "")

    return Path(named) if named else SITE_FILE


def pair_in(directory: Path) -> Pair:
    """The pair in `directory`, for example the package data of a tree."""
    return read_pair(
        _read(directory / DEFAULT_FILE, Owner.ANY), _read(directory / EXAMPLE_FILE, Owner.ANY)
    )


def root_keys_in(directory: Path, pair: Pair) -> tuple[str, ...]:
    """The keys of `root-keys.txt` in `directory`."""
    return read_root_keys(_read(directory / ROOT_KEYS_FILE, Owner.ANY), pair)


def package_dir() -> Path:
    """The package data, in a checkout and in an installed tree."""
    return Path(__file__).resolve().parent

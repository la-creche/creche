"""The command `creche-env`: what a script asks of the env files.

    creche-env get KEY [--pair DIR] [--env FILE]
    creche-env check [--pair DIR | --catalog DIR ...] [--env FILE | --site FILE]
    creche-env changed-trusted OLD NEW [--pair DIR]
    creche-env raw FILE KEY

`get` prints the value of KEY, or an empty line for a key with no value.
`check` prints nothing. Its `--catalog` names each pair to read the `.env`
against, and `--site` is `--env`. `changed-trusted` names each key of
`root-keys.txt` whose value differs between two `.env` files. `raw` prints
the value of KEY in one file of the grammar-only mode, or nothing.

The pair and `root-keys.txt` are the package data, or the files in `--pair
DIR`. The `.env` is `--env FILE`, else `envfile.site_file`. Each `.env` and
the file of `raw` pass the trust check. A fault prints one line on stderr
and the status is 78, `os.EX_CONFIG`, also when stderr is closed. A command
line that the parser refuses gives 2. A closed stdout gives 74,
`os.EX_IOERR`, when the command has a value to print, and the command then
prints nothing more. The command reads no catalog key from its environment.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, TextIO

from .envfile import (
    EnvError,
    Fault,
    Pair,
    Problem,
    ReadError,
    is_key,
    load,
    package_dir,
    pair_in,
    raw,
    read_trusted,
    root_keys_in,
    site_file,
)

PROGRAM: Final = "creche-env"

type Command = Callable[[argparse.Namespace], int]


class _NoOutput(Exception):
    """Stdout is closed: the caller gave no fd 1, or its reader went away."""


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point of `creche-env`."""
    parser = _parser()
    args = parser.parse_args(argv)
    command: Command = args.command
    try:
        status = _status(command, args)
        _flush()
    except _NoOutput:
        return os.EX_IOERR

    return status


def _status(command: Command, args: argparse.Namespace) -> int:
    try:
        return command(args)
    except EnvError as error:
        for one in error.problems:
            _say(one.text())
    except ReadError as error:
        _say(str(error))

    return os.EX_CONFIG


def _drop(stream: TextIO) -> None:
    """Point `stream` at the null device, so that the flush at the exit of
    the interpreter writes to no closed pipe."""
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, stream.fileno())
    os.close(null)


def _say(text: str) -> None:
    """One line on stderr. A closed stderr changes no status."""
    stream = sys.stderr
    if stream is None:
        return

    try:
        stream.write(f"{PROGRAM}: {text}\n")
    except BrokenPipeError:
        _drop(stream)


def _out(text: str) -> None:
    stream = sys.stdout
    if stream is None:
        raise _NoOutput

    try:
        stream.write(f"{text}\n")
    except BrokenPipeError:
        _drop(stream)
        raise _NoOutput from None


def _flush() -> None:
    stream = sys.stdout
    if stream is None:
        return

    try:
        stream.flush()
    except BrokenPipeError:
        _drop(stream)
        raise _NoOutput from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description="Read the env files.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    get = verbs.add_parser("get", help="print the value of one catalog key")
    get.add_argument("key")
    _layer_options(get)
    get.set_defaults(command=_get)

    check = verbs.add_parser("check", help="read the files and print nothing")
    pairs = check.add_mutually_exclusive_group()
    pairs.add_argument("--pair", type=Path, metavar="DIR")
    pairs.add_argument("--catalog", action="extend", nargs="+", default=[], metavar="DIR")
    envs = check.add_mutually_exclusive_group()
    envs.add_argument("--env", type=Path, metavar="FILE")
    envs.add_argument("--site", type=Path, metavar="FILE", dest="env")
    check.set_defaults(command=_check)

    changed = verbs.add_parser("changed-trusted", help="name each root key that changed")
    changed.add_argument("old", type=Path)
    changed.add_argument("new", type=Path)
    changed.add_argument("--pair", type=Path, metavar="DIR")
    changed.set_defaults(command=_changed)

    plain = verbs.add_parser("raw", help="print one value of a file of no catalog")
    plain.add_argument("file", type=Path)
    plain.add_argument("key")
    plain.set_defaults(command=_raw)

    return parser


def _layer_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pair", type=Path, metavar="DIR")
    parser.add_argument("--env", type=Path, metavar="FILE")


def _pair_dir(given: Path | None) -> Path:
    return given if given is not None else package_dir()


def _env_file(given: Path | None) -> Path:
    return given if given is not None else site_file(os.environ)


def _key_of(text: str, pair: Pair) -> str:
    """`text` when it is a catalog key. A fault names the file of the
    defaults, with no line."""
    if not is_key(text):
        raise EnvError((Problem(Fault.BAD_KEY, pair.default_file, 0, ""),))

    if text not in pair.catalog():
        raise EnvError((Problem(Fault.UNKNOWN_KEY, pair.default_file, 0, text),))

    return text


def _get(args: argparse.Namespace) -> int:
    pair = pair_in(_pair_dir(args.pair))
    key = _key_of(args.key, pair)
    settings = load(pair, read_trusted(_env_file(args.env)))
    _out(settings.values.get(key, ""))

    return os.EX_OK


def _check(args: argparse.Namespace) -> int:
    directories = [Path(one) for one in args.catalog] or [_pair_dir(args.pair)]
    pairs = [pair_in(one) for one in directories]
    env = read_trusted(_env_file(args.env))
    for pair in pairs:
        load(pair, env)

    return os.EX_OK


def _changed(args: argparse.Namespace) -> int:
    # CONTRACT-QUESTION: no contract text says which value of a root key
    # this compares. Reading taken: the value after each layer, so a changed
    # root names each root key below it. A change costs this comparison.
    directory = _pair_dir(args.pair)
    pair = pair_in(directory)
    keys = root_keys_in(directory, pair)
    old = load(pair, read_trusted(args.old)).values
    new = load(pair, read_trusted(args.new)).values
    for key in keys:
        if old.get(key) != new.get(key):
            _out(key)

    return os.EX_OK


def _raw(args: argparse.Namespace) -> int:
    source = read_trusted(args.file)
    if not is_key(args.key):
        raise EnvError((Problem(Fault.BAD_KEY, source.name, 0, ""),))

    value = raw(source).get(args.key)
    if value is not None:
        _out(value)

    return os.EX_OK

"""The pinned build of the Rust chaperone: the commit, the cache and the build.

The row `chaperone-rs` of `proc_services.py` starts a `chaperone` binary.
`CRECHE_PROC_CHAPERONE_RS` names one. With no variable, the suite builds
the binary from the chaperone repository at the commit of `chaperone.pin`,
and keeps it in a cache outside the checkout:

    ~/.cache/creche-proc/chaperone-rs/<commit>/bin/chaperone

A build has these steps, under one lock, so workers of one run build once:

1. Fetch the one commit into a new directory beside the cache entry.
2. Install the toolchain that the `rust-toolchain.toml` of the commit names.
3. Run `cargo install --locked --no-track --bin chaperone` into a new root.
4. Rename the new root to the cache entry. A failed build leaves no entry.

The repository is `chaperone` beside this checkout on GitHub: the owner
comes from the `origin` remote of this checkout, so no file here names it.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

PIN_FILE: Final = Path(__file__).resolve().with_name("chaperone.pin")

#: The name of the repository of the Rust chaperone, beside this one.
REPOSITORY: Final = "chaperone"

#: The program that the row starts, and the one binary that a build installs.
PROGRAM: Final = "chaperone"

_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
_COMMENT: Final = "#"
_ORIGIN: Final = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/[A-Za-z0-9._-]+?(?:\.git)?/?"
)

#: A cold build takes about two minutes on a laptop.
_BUILD_TIMEOUT_S: Final = 1800
_GIT_TIMEOUT_S: Final = 300


class BuildError(Exception):
    """The pinned build failed. The message holds the output of the step."""


class NoToolchain(BuildError):
    """`git` or `cargo` is not on PATH. A test then skips, or fails with no skip."""


def pinned_commit(text: str | None = None) -> str:
    """The one commit of the pin file. A comment line starts with `#`."""
    source = PIN_FILE.read_text(encoding="utf-8") if text is None else text
    lines = [line.strip() for line in source.splitlines()]
    commits = [line for line in lines if line and not line.startswith(_COMMENT)]

    if len(commits) != 1 or _COMMIT.fullmatch(commits[0]) is None:
        raise BuildError(f"{PIN_FILE.name} holds no one commit of 40 lower-case hex characters")

    return commits[0]


def cache_root() -> Path:
    """The directory of every pinned build. CI keeps it between runs."""
    return Path.home() / ".cache" / "creche-proc" / "chaperone-rs"


def pinned_binary(commit: str | None = None) -> Path:
    """Where the build of one commit puts the program. It can be missing."""
    return cache_root() / (commit or pinned_commit()) / "bin" / PROGRAM


def repository_url(origin: str) -> str:
    """The HTTPS address of the chaperone repository, beside the origin of this checkout."""
    found = _ORIGIN.fullmatch(origin.strip())

    if found is None:
        raise BuildError(
            "the origin of this checkout is no GitHub repository. "
            "Build chaperone yourself and set CRECHE_PROC_CHAPERONE_RS."
        )

    return f"https://github.com/{found['owner']}/{REPOSITORY}"


def build_pinned() -> Path:
    """The program of the pinned commit. Builds it when the cache holds none."""
    commit = pinned_commit()
    binary = pinned_binary(commit)

    if binary.is_file():
        return binary

    for tool in ("git", "cargo"):
        if shutil.which(tool) is None:
            raise NoToolchain(f"the pinned build of chaperone needs `{tool}` on PATH")

    with _locked(cache_root() / f"{commit}.lock"):
        if not binary.is_file():
            _build(commit, binary.parent.parent)

    return binary


def _build(commit: str, entry: Path) -> None:
    source = entry.with_name(f"{commit}.src")
    staged = entry.with_name(f"{commit}.new")

    for leftover in (source, staged):
        shutil.rmtree(leftover, ignore_errors=True)

    url = repository_url(_git(Path(__file__).resolve().parent, "remote", "get-url", "origin"))
    source.mkdir(parents=True)
    _git(source, "init", "--quiet")
    _git(source, "fetch", "--quiet", "--depth", "1", url, commit)
    _git(source, "checkout", "--quiet", "--detach", "FETCH_HEAD")

    if shutil.which("rustup") is not None:
        _run(["rustup", "toolchain", "install", "--no-self-update"], source, _GIT_TIMEOUT_S)

    install = ["cargo", "install", "--locked", "--no-track", "--bin", PROGRAM]
    _run([*install, "--path", "crates/chaperone", "--root", str(staged)], source, _BUILD_TIMEOUT_S)
    staged.rename(entry)
    shutil.rmtree(source, ignore_errors=True)


def _git(cwd: Path, *args: str) -> str:
    """One `git` command with no config file of a person, as `proc_registry.py` runs it."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(Path.home()),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    return _run(["git", *args], cwd, _GIT_TIMEOUT_S, env)


def _run(words: list[str], cwd: Path, timeout_s: int, env: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(
            words,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BuildError(f"cannot run {' '.join(words)}: {error}") from error

    if done.returncode != 0:
        raise BuildError(f"{' '.join(words)} exited {done.returncode}\n{done.stderr[-4000:]}")

    return done.stdout.strip()


@contextmanager
def _locked(path: Path) -> Generator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

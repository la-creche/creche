"""The one `policy.toml` of the fleet (chaperone design section 6).

The Rust chaperone reads one policy file with an `[apps.<id>]` table for
each family. A file that `chaperone check` refuses denies every family, so
this module writes no file that the check did not pass.

1. Each family has one fragment in this service's state:
   `grants/<family>.toml`, the text of `render_app`.
2. `assemble_policy` joins the fragments under one `flock`: `version = 1`
   once, then each fragment in family order.
3. A `PolicyChecker` checks the joined text. When the check refuses it,
   each fragment is checked alone. A fragment that fails alone is left
   out, with one error for its family. The rest is checked again.
4. The checked text replaces `policy.toml` with an atomic rename. The file
   it replaces stays as `policy.toml.prev`.

The configuration directory of chaperone also holds the operator's
`install.toml`. This module writes the two names of `TargetFile` there and
nothing else."""

from __future__ import annotations

import fcntl
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast

from caregiver import paths
from caregiver.atomic import atomic_write
from caregiver.policy_file import POLICY_VERSION, App, is_app_id, render_app

#: `POLICY_FILE_CAP` of the Rust chaperone. A larger file does not load.
POLICY_FILE_MAX_BYTES: Final = 1024 * 1024

#: The file of the operator, beside `policy.toml`. This module only reads it.
INSTALL_FILE: Final = "install.toml"

#: The policy holds token digests and no secret.
_POLICY_MODE: Final = 0o644
_FRAGMENT_MODE: Final = 0o600

#: `chaperone check` reads two small files. It takes well under a second.
CHECK_TIMEOUT_S: Final = 30.0

#: The longest reason that a refusal keeps from the output of the check.
_REASON_MAX: Final = 500

_HEADER: Final = f"version = {POLICY_VERSION}\n"
_APPS_KEY: Final = "apps"


class TargetFile(StrEnum):
    """The only names this module writes in the configuration directory."""

    POLICY = "policy.toml"
    PREV = "policy.toml.prev"


@dataclass(frozen=True)
class PolicyTarget:
    """The configuration directory of chaperone: `/etc/chaperone` on the
    host, `root:agents 1775`. The sticky bit lets this service replace its
    own `policy.toml` and not the `install.toml` of root."""

    config_dir: Path

    def path(self, name: TargetFile) -> Path:
        return self.config_dir / name

    @property
    def install(self) -> Path:
        return self.config_dir / INSTALL_FILE


@dataclass(frozen=True)
class Passed:
    """`chaperone check` passed the text."""


@dataclass(frozen=True)
class Refused:
    """`chaperone check` refused the text. The reason is its first error
    line."""

    reason: str


Verdict = Passed | Refused


class CheckerError(Exception):
    """The check did not run, so it gives no verdict."""


class PolicyChecker(Protocol):
    def check(self, policy: str) -> Verdict: ...


@dataclass(frozen=True)
class ChaperoneChecker:
    """Runs `chaperone check` on a candidate `policy.toml` in a new temp
    directory that holds a copy of `install.toml`, because the check reads
    the install file beside the policy file."""

    binary: Path
    config_dir: Path
    plugins_dir: Path
    timeout_s: float = CHECK_TIMEOUT_S

    def check(self, policy: str) -> Verdict:
        with tempfile.TemporaryDirectory(prefix="caregiver-policy-") as scratch:
            directory = Path(scratch)
            candidate = directory / TargetFile.POLICY
            try:
                shutil.copyfile(self.config_dir / INSTALL_FILE, directory / INSTALL_FILE)
                candidate.write_text(policy, encoding="utf-8")
            except OSError as error:
                raise CheckerError(f"cannot stage the check: {error}") from error

            argv = [
                str(self.binary),
                "check",
                str(candidate),
                "--plugins-dir",
                str(self.plugins_dir),
            ]
            try:
                done = subprocess.run(
                    argv, capture_output=True, timeout=self.timeout_s, check=False
                )
            except subprocess.TimeoutExpired as error:
                raise CheckerError(
                    f"chaperone check did not finish in {self.timeout_s} s"
                ) from error
            except OSError as error:
                raise CheckerError(f"chaperone check did not start: {error}") from error

        if done.returncode == 0:
            return Passed()

        lines = done.stderr.decode("utf-8", errors="replace").strip().splitlines()
        reason = lines[0] if lines else f"chaperone check exited with {done.returncode}"
        return Refused(reason[:_REASON_MAX])


@dataclass
class FakeChecker:
    """Reads the app IDs of the text and answers as these fields say. Tests
    read `checked` for each text it saw."""

    broken: str | None = None
    refused_apps: frozenset[str] = frozenset()
    clashes: tuple[frozenset[str], ...] = ()
    unavailable: bool = False
    checked: list[str] = field(default_factory=list[str])

    def check(self, policy: str) -> Verdict:
        if self.unavailable:
            raise CheckerError("the fake checker does not run")

        self.checked.append(policy)
        if self.broken is not None:
            return Refused(self.broken)

        apps = set(tomllib.loads(policy).get(_APPS_KEY, {}))
        refused = sorted(apps & self.refused_apps)
        if refused:
            return Refused(f"the app {refused[0]} is refused")

        for clash in self.clashes:
            if clash <= apps:
                return Refused(f"the apps {' and '.join(sorted(clash))} clash")

        return Passed()


class FleetOutcome(StrEnum):
    WRITTEN = "written"
    UNCHANGED = "unchanged"
    REFUSED = "refused"


@dataclass(frozen=True)
class Assembly:
    """The result of one assembly. `left_out` names each family whose
    fragment is not in the file, with one error. With `REFUSED`, nothing
    was written, the last `policy.toml` stays and `error` says why."""

    outcome: FleetOutcome
    left_out: Mapping[str, str]
    error: str | None = None


def write_fragment(root: Path, family: str, app: App) -> None:
    """Write the app table of one family. The next assembly takes it."""
    text = render_app(family, app)
    atomic_write(
        paths.policy_fragment_path(root, family), text.encode("utf-8"), mode=_FRAGMENT_MODE
    )


def remove_fragment(root: Path, family: str) -> None:
    """Remove the app table of one family. The next assembly leaves the
    family out of `policy.toml`."""
    paths.policy_fragment_path(root, family).unlink(missing_ok=True)


def assemble_policy(root: Path, target: PolicyTarget, checker: PolicyChecker) -> Assembly:
    """Join every fragment into one checked `policy.toml`."""
    with _locked(paths.policy_lock_path(root)):
        fragments, left_out = _read_fragments(root)
        try:
            return _assemble(fragments, left_out, target, checker)
        except CheckerError as error:
            return Assembly(FleetOutcome.REFUSED, left_out, str(error))


def _assemble(
    fragments: dict[str, str],
    left_out: dict[str, str],
    target: PolicyTarget,
    checker: PolicyChecker,
) -> Assembly:
    text = _join(fragments)
    verdict = _check(text, checker)
    if isinstance(verdict, Refused):
        base = _check(_HEADER, checker)
        if isinstance(base, Refused):
            return Assembly(FleetOutcome.REFUSED, left_out, base.reason)

        for family, fragment in fragments.items():
            alone = _check(_join({family: fragment}), checker)
            if isinstance(alone, Refused):
                left_out[family] = alone.reason

        text = _join({name: body for name, body in fragments.items() if name not in left_out})
        verdict = _check(text, checker)

    if isinstance(verdict, Refused):
        return Assembly(FleetOutcome.REFUSED, left_out, verdict.reason)

    return Assembly(_install(target, text), left_out)


def _check(text: str, checker: PolicyChecker) -> Verdict:
    size = len(text.encode("utf-8"))
    if size > POLICY_FILE_MAX_BYTES:
        return Refused(
            f"policy.toml would have {size} bytes, and the cap is {POLICY_FILE_MAX_BYTES}"
        )

    return checker.check(text)


def _join(fragments: Mapping[str, str]) -> str:
    """The text of `render_policy` for the same apps."""
    return _HEADER + "".join("\n" + fragments[family] for family in sorted(fragments))


def _install(target: PolicyTarget, text: str) -> FleetOutcome:
    """Replace `policy.toml` with checked text. The old file becomes
    `policy.toml.prev`."""
    data = text.encode("utf-8")
    current = target.path(TargetFile.POLICY)
    try:
        old = current.read_bytes()
    except FileNotFoundError:
        old = None

    if old == data:
        return FleetOutcome.UNCHANGED

    if old is not None:
        atomic_write(target.path(TargetFile.PREV), old, mode=_POLICY_MODE)

    atomic_write(current, data, mode=_POLICY_MODE)
    return FleetOutcome.WRITTEN


def _read_fragments(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Each fragment by family, and each family whose fragment does not
    read, with one error. A file whose name is not an app ID is not a
    fragment."""
    directory = paths.policy_fragments_dir(root)
    fragments: dict[str, str] = {}
    errors: dict[str, str] = {}
    for path in sorted(directory.glob(f"*{paths.POLICY_FRAGMENT_SUFFIX}")):
        family = path.name.removesuffix(paths.POLICY_FRAGMENT_SUFFIX)
        if not is_app_id(family):
            continue

        try:
            text = _read_fragment(path, family)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            errors[family] = str(error)
            continue

        fragments[family] = text

    return fragments, errors


def _read_fragment(path: Path, family: str) -> str:
    """The text of one fragment. It must hold the table of its own family
    and nothing else."""
    with path.open("rb") as handle:
        data = handle.read(POLICY_FILE_MAX_BYTES + 1)

    if len(data) > POLICY_FILE_MAX_BYTES:
        raise ValueError(f"the fragment of {family} has more than {POLICY_FILE_MAX_BYTES} bytes")

    try:
        text = data.decode("utf-8")
        table = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"the fragment of {family} does not read: {error}") from error

    apps = table.get(_APPS_KEY)
    if set(table) != {_APPS_KEY} or not isinstance(apps, dict):
        raise ValueError(f"the fragment of {family} holds more than [apps.{family}]")

    if set(cast("dict[str, object]", apps)) != {family}:
        raise ValueError(f"the fragment of {family} holds more than [apps.{family}]")

    return text


@contextmanager
def _locked(lock: Path) -> Generator[None]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

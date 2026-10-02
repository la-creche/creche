"""The sandbox driver (contract 05 §4.3, §4.4).

`SandboxDriver` is the seam. `SbxDriver` shells to the real `sbx` CLI;
`FakeDriver` records calls for a test. Every command `SbxDriver` issues is a
proven fact about the host, not a design choice: the root README §6 records
them."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Final, Protocol

log = logging.getLogger("agent_managerd.driver")


class DriverError(RuntimeError):
    pass


#: README §6: every sandbox is created from sbx's built-in `shell` kit,
#: which attaches its own non-editable `openrouter.ai` allow on every `sbx
#: create`, invisible to the global policy check. Deny it explicitly at
#: create and again in `set_egress` (belt and braces: `set_egress` also
#: narrows an already-created sandbox), and `assert_egress` probes it as
#: denied. A failed probe is a real hole in deny-by-default (invariant 11).
KIT_EXTRA_EGRESS: Final = ("openrouter.ai",)

#: sbx's own CLI init dominates every call's latency; its telemetry adds a
#: further delay on top and buys `managerd` nothing (`sbx --help` vs `sbx
#: version`). Every `sbx` call this driver makes sets it off.
SBX_ENV: Final = {"SBX_NO_TELEMETRY": "1"}

CREATE_TIMEOUT_S: Final = 300
POLICY_TIMEOUT_S: Final = 30
DESTROY_TIMEOUT_S: Final = 60
LIST_TIMEOUT_S: Final = 30


@dataclass(frozen=True)
class Mount:
    path: str
    readonly: bool


@dataclass(frozen=True)
class SandboxSpec:
    """What `sbx create` needs for one family sandbox.

    `mounts[0]` is the primary workspace. sbx requires the primary to be
    read-write and allows `:ro` only on secondaries (README §6), so this
    driver never checks `mounts[0].readonly` — the caller orders the list."""

    name: str
    image: str
    mounts: tuple[Mount, ...]
    cpus: int
    memory: str


class SandboxDriver(Protocol):
    """No `exec`, no `publish_ingress`: a family sandbox takes no ingress
    and publishes no port. Whatever runs turns
    inside it is a different package's channel (contract 03), not this
    one's concern."""

    def create(self, spec: SandboxSpec) -> None: ...
    def set_egress(self, name: str, allow: tuple[str, ...]) -> None: ...
    def remove_egress(self, name: str, hosts: tuple[str, ...]) -> None: ...
    def assert_egress(
        self, name: str, allowed: tuple[str, ...], denied: tuple[str, ...]
    ) -> None: ...
    def destroy(self, name: str, allow: tuple[str, ...]) -> None: ...
    def list_names(self) -> set[str]: ...


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **SBX_ENV}
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise DriverError(
            f"{' '.join(args[:3])}… timed out after {timeout}s — if this repeats, "
            "run 'sbx daemon restart'"
        ) from exc


class SbxDriver:
    """Shells to `sbx`. Every command is the exact sequence README §6
    proves works; this class adds no policy of its own."""

    def create(self, spec: SandboxSpec) -> None:
        args = ["sbx", "create", "shell"]
        for mount in spec.mounts:
            args.append(f"{mount.path}:ro" if mount.readonly else mount.path)

        args += ["-t", spec.image, "--name", spec.name, "--cpus", str(spec.cpus), "-m", spec.memory]
        for host in KIT_EXTRA_EGRESS:
            args += ["--deny-network", host]

        args.append("-q")
        proc = _run(args, CREATE_TIMEOUT_S)
        if proc.returncode != 0:
            raise DriverError(f"sbx create {spec.name} failed: {proc.stderr.strip()[:300]}")

    def set_egress(self, name: str, allow: tuple[str, ...]) -> None:
        for dest in allow:
            proc = _run(
                ["sbx", "policy", "allow", "network", "--sandbox", name, dest], POLICY_TIMEOUT_S
            )
            if proc.returncode != 0:
                raise DriverError(f"policy allow {dest} for {name}: {proc.stderr.strip()[:200]}")

        # Deny beats allow: narrows the kit hole on THIS sandbox regardless
        # of whether `create`'s --deny-network took (older sbx, or a
        # sandbox created without the flag and reapplied now).
        for host in KIT_EXTRA_EGRESS:
            proc = _run(
                ["sbx", "policy", "deny", "network", "--sandbox", name, host], POLICY_TIMEOUT_S
            )
            if proc.returncode != 0:
                raise DriverError(f"policy deny {host} for {name}: {proc.stderr.strip()[:200]}")

    def remove_egress(self, name: str, hosts: tuple[str, ...]) -> None:
        """Drop a policy row from a RUNNING sandbox. A per-sandbox
        `policy rm` takes effect mid-session, in the same VM boot (README
        §6, grants-probe), which is what lets an egress removal apply at
        once without replacing the sandbox (contract 05 §3.5)."""
        for dest in hosts:
            proc = _run(
                ["sbx", "policy", "rm", "network", "--sandbox", name, "--resource", dest],
                POLICY_TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise DriverError(f"policy rm {dest} for {name}: {proc.stderr.strip()[:200]}")

    def assert_egress(self, name: str, allowed: tuple[str, ...], denied: tuple[str, ...]) -> None:
        """Fail closed: a reachable set wider than granted refuses the apply
        (invariant 11). Catches global local-policy drift and the sbx kit's
        own network allow, before any process starts."""
        for dest in allowed:
            proc = _run(
                ["sbx", "policy", "check", "network", "--sandbox", name, dest], POLICY_TIMEOUT_S
            )
            if "Allowed" not in proc.stdout:
                raise DriverError(f"expected {dest} allowed for {name}; got: {proc.stdout[:150]}")

        # Deduplicated, order kept: the caller's canary list may already
        # name the kit host, and every probe costs one sbx call.
        for dest in dict.fromkeys((*denied, *KIT_EXTRA_EGRESS)):
            proc = _run(
                ["sbx", "policy", "check", "network", "--sandbox", name, dest], POLICY_TIMEOUT_S
            )
            if "Denied" not in proc.stdout:
                raise DriverError(
                    f"{dest} is REACHABLE from {name} — deny-by-default is broken. "
                    f"check said: {proc.stdout[:150]}"
                )

    def destroy(self, name: str, allow: tuple[str, ...]) -> None:
        """Order: policy rows first, then the VM (contract 05 §4.4). `sbx rm
        -f` does not remove a sandbox's policy rows as a side effect
        (README §6), so a caller that skips this leaks them."""
        for dest in allow:
            _run(
                ["sbx", "policy", "rm", "network", "--sandbox", name, "--resource", dest],
                POLICY_TIMEOUT_S,
            )

        proc = _run(["sbx", "rm", "-f", name], DESTROY_TIMEOUT_S)
        if proc.returncode != 0 and "not found" not in (proc.stderr + proc.stdout).lower():
            raise DriverError(f"sbx rm {name} failed: {proc.stderr.strip()[:200]}")

    def list_names(self) -> set[str]:
        """Every sandbox sbx knows, header row dropped. An unreadable
        listing answers empty: a sweep that cannot see is a sweep that does
        nothing, never one that assumes and deletes."""
        try:
            proc = _run(["sbx", "ls"], LIST_TIMEOUT_S)
        except DriverError as exc:
            log.error("sbx ls failed: %s", exc)
            return set()

        return {line.split()[0] for line in proc.stdout.splitlines()[1:] if line.split()}


@dataclass(frozen=True)
class FakeCall:
    """One recorded driver call, for a test to assert an exact sequence
    (credentials die before the sandbox)."""

    op: str
    args: tuple[object, ...]


@dataclass
class FakeDriver:
    """Never touches a real sandbox. Tests read `.calls` for order and
    arguments, and `.ops()` for a short sequence assertion.

    `SbxDriver` keeps nothing between calls, so it is safe to share
    between the threads that converge families side by side.
    This one keeps a list and a set, so it takes a lock."""

    calls: list[FakeCall] = field(default_factory=list[FakeCall])
    _existing: set[str] = field(default_factory=set[str])
    _mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def create(self, spec: SandboxSpec) -> None:
        with self._mutex:
            self.calls.append(FakeCall("create", (spec,)))
            self._existing.add(spec.name)

    def set_egress(self, name: str, allow: tuple[str, ...]) -> None:
        with self._mutex:
            self.calls.append(FakeCall("set_egress", (name, allow)))

    def remove_egress(self, name: str, hosts: tuple[str, ...]) -> None:
        with self._mutex:
            self.calls.append(FakeCall("remove_egress", (name, hosts)))

    def assert_egress(self, name: str, allowed: tuple[str, ...], denied: tuple[str, ...]) -> None:
        with self._mutex:
            self.calls.append(FakeCall("assert_egress", (name, allowed, denied)))

    def destroy(self, name: str, allow: tuple[str, ...]) -> None:
        with self._mutex:
            self.calls.append(FakeCall("destroy", (name, allow)))
            self._existing.discard(name)

    def list_names(self) -> set[str]:
        with self._mutex:
            return set(self._existing)

    def ops(self) -> tuple[str, ...]:
        with self._mutex:
            return tuple(call.op for call in self.calls)

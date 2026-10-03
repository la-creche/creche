"""The intake's unit and its root wrapper, read as configuration.

`systemd/agent-rework-intake.service` and `bin/agent-rework-intake` are the
two files nothing in `release/tests/` can reach: they are what actually
decides which uid runs the listener, what it may touch, and which binary
root execs. They are also the two files a future editor is most likely to
loosen without noticing, so the properties are asserted here rather than
described in a comment.

Nothing here runs as root and nothing starts systemd. Every assertion is a
read of the file's own text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
UNIT: Final = ROOT / "systemd" / "agent-rework-intake.service"
WRAPPER: Final = ROOT / "bin" / "agent-rework-intake"

#: The three paths root either executes or decrypts. Each one that the
#: operator could write is a path that decides what root runs.
GUARDED: Final = ("INTAKE", "SOPS", "SECRETS")

#: What `ProtectHome=` hides, per directive value. `read-only` leaves the
#: three readable, which is why it is not here.
HIDDEN_BY_PROTECT_HOME: Final = {"yes", "tmpfs"}

#: The directories `ProtectHome=` covers.
HOME_DIRECTORIES: Final = ("/home", "/root", "/run/user")

#: The directives that give one path back inside a sandbox that hid it.
BIND_DIRECTIVES: Final = ("BindReadOnlyPaths=", "BindPaths=")


def _unit() -> str:
    """The unit's DIRECTIVES, with every comment dropped.

    The file explains each directive it sets and each one it deliberately
    leaves out, so a plain substring search over the whole text would find
    `PrivateDevices` in a paragraph saying why it is absent.
    """
    lines = UNIT.read_text(encoding="utf-8").splitlines()

    return "\n".join(line for line in lines if line and not line.startswith("#"))


def _wrapper() -> str:
    return WRAPPER.read_text(encoding="utf-8")


# -- the unit ------------------------------------------------------------


def test_the_unit_runs_the_wrapper_and_not_the_intake_directly() -> None:
    """The wrapper is what proves the three paths and decrypts the push
    hook. A unit that execed the intake would skip both."""
    assert "ExecStart=/usr/local/sbin/agent-rework-intake" in _unit()


def test_the_unit_names_no_environment_file() -> None:
    """Invariant 13: the bearer is in no env file, no argv and no URL."""
    assert "EnvironmentFile" not in _unit()


def test_the_unit_writes_only_the_two_directories_it_must() -> None:
    """`ProtectSystem=strict` plus the gap directory it unlinks from and
    the secrets directory it writes. Nothing else on the host."""
    unit = _unit()

    assert "ProtectSystem=strict" in unit
    # Root's own root. The gaps stay the operator's.
    assert (
        "ReadWritePaths=/var/lib/agent-release/secrets /srv/agents/state/rework/secret-gaps" in unit
    )


def test_the_unit_keeps_the_hardening_the_executor_carries() -> None:
    """The floor: every directive the executor's unit carries that
    still lets this one do its job."""
    unit = _unit()
    for directive in (
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "SystemCallArchitectures=native",
        "ProtectClock=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelLogs=yes",
        "ProtectHostname=yes",
        "RestrictSUIDSGID=yes",
        "RestrictAddressFamilies=",
    ):
        assert directive in unit, directive


def test_the_unit_adds_what_the_executor_could_not_take() -> None:
    """The executor keeps `runuser`, `systemctl` and a compose build, so it
    cannot take these. The intake starts one `sops` child and binds one
    port, so it can."""
    unit = _unit()
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectHome=tmpfs",
        "PrivateTmp=yes",
        "RestrictNamespaces=yes",
        "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH",
    ):
        assert directive in unit, directive


def test_the_unit_does_not_hide_the_key_its_wrapper_reads() -> None:
    """The sandbox and the wrapper have to agree.

    `ProtectHome=yes` makes `/home`, `/root` and `/run/user` inaccessible
    and empty, and the wrapper decrypts `secrets.enc.env` with an age key
    under `/root`. The wrapper then exits 1 by design, and the rework
    restart policy restarts it FOR EVER rather than letting it reach
    `failed`: with no bind-back that is an unbounded loop
    that never once decrypts, the journal saying only "cannot decrypt"
    every few minutes for as long as nobody looks. Nothing else in this
    repository can catch that: there is no systemd here.
    """
    key = _age_key_file()
    covered = any(key.startswith(one) for one in HOME_DIRECTORIES)
    if not covered:
        return

    unit = _unit()
    hidden = any(f"ProtectHome={one}" in unit for one in HIDDEN_BY_PROTECT_HOME)
    if not hidden:
        return

    given_back = [
        value
        for line in unit.splitlines()
        for directive in BIND_DIRECTIVES
        if line.startswith(directive)
        for value in line.removeprefix(directive).split()
    ]

    assert any(key.startswith(one) for one in given_back), (
        f"ProtectHome hides {key}, which bin/agent-rework-intake decrypts with"
    )


def _age_key_file() -> str:
    """The age key the wrapper hands `sops`, read out of the wrapper."""
    marker = "SOPS_AGE_KEY_FILE="
    for line in _wrapper().splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip()

    raise AssertionError(f"{WRAPPER} names no {marker}")


def test_the_unit_sits_in_a_directory_root_owns() -> None:
    """`sops` walks up from the working directory for a `.sops.yaml`, and
    one with no matching rule refuses every seal."""
    assert "WorkingDirectory=/etc/agent-intake" in _unit()


def test_the_unit_restarts_and_cannot_burn_its_start_limit() -> None:
    """A listener that died with a gap open leaves the operator unable to answer
    it, and the wrapper's refusal is permanent until a human fixes it.

    A `StartLimitIntervalSec` in `[Service]` is ignored by this systemd
    version with a logged warning, so a crash loop hits systemd's DEFAULT
    start limit and the unit stays down. The rule is the shared rework policy
    (`creche-caregiver.service`'s `[Unit]` comment): no start limit at all,
    `StartLimitIntervalSec=0` in `[Unit]`, so `StartLimitBurst` is gone —
    a burst value means nothing beside an interval of zero."""
    unit = _unit()

    assert "Restart=always" in unit
    assert "RestartSec=5s" in unit
    assert "RestartSteps=5" in unit
    assert "RestartMaxDelaySec=120s" in unit
    assert "StartLimitIntervalSec=0" in unit
    assert "StartLimitBurst=" not in unit


# -- the wrapper ---------------------------------------------------------


def test_the_wrapper_guards_every_path_root_runs_or_decrypts() -> None:
    wrapper = _wrapper()

    assert "require_root_owned" in wrapper
    for name in GUARDED:
        assert f'"${name}"' in wrapper, name


def test_the_wrapper_has_no_fallback_that_starts_without_the_hook() -> None:
    """The release wrapper execs its executor anyway, because an undrained
    spool loops its path unit into the start limit. Nothing loops here, and
    an intake with no push path would mint tokens nobody is ever told
    about. There is exactly one exec, and it is under `sops exec-env`.
    """
    wrapper = _wrapper()
    execs = [line.strip() for line in wrapper.splitlines() if line.strip().startswith("exec ")]

    assert execs == ['exec "$SOPS" exec-env "$SECRETS" "$INTAKE"']


def test_the_wrapper_refuses_to_run_as_anything_but_root() -> None:
    assert "id -u" in _wrapper()


def test_the_wrapper_execs_the_intake_console_script() -> None:
    """The name in `release/pyproject.toml`'s `[project.scripts]`. A
    mismatch here is a unit that starts and does nothing."""
    assert "/opt/components/releasectl/bin/agent-release-intake" in _wrapper()

    scripts = (ROOT / "release" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'agent-release-intake = "agent_release.intake.run:main"' in scripts

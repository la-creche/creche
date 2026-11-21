"""The decided lines of the two units of the release signer ci-signer.

`systemd/creche-ci-signer.socket` is the socket of the attach interface of
the runner design at commit 131ce43, section 6.4. `systemd/creche-ci-
signer.service` is the service of section 4.3 of
`docs/design/forgejo-release-path.md`. `systemd-analyze verify` checks the
syntax only, so each decided line gets a row here. Each path that the
program names in its code must be the path of the unit, so the test reads
the constants of the Rust source too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
SOCKET: Final = ROOT / "systemd" / "creche-ci-signer.socket"
SERVICE: Final = ROOT / "systemd" / "creche-ci-signer.service"
PROGRAM: Final = ROOT / "rust" / "crates" / "ci-signer" / "src" / "bin" / "ci-signer"

#: The start line of the service: the program of the tree and the config.
EXEC_START: Final = (
    "/opt/components/ci-signer/bin/ci-signer serve --config /etc/creche/ci-signer/signer.conf"
)

#: Each row: the key and every value of the socket unit, in order.
SOCKET_DECIDED: Final = [
    ("ListenStream", ["/run/ci-signer/attach.sock"]),
    ("SocketUser", ["ci-signer"]),
    ("SocketGroup", ["ci-runner"]),
    ("SocketMode", ["0660"]),
    ("RemoveOnStop", ["yes"]),
    ("RuntimeDirectory", ["ci-signer"]),
    ("Accept", ["no"]),
    ("Service", ["creche-ci-signer.service"]),
]

#: Each row: the key and every value of the service unit, in order.
SERVICE_DECIDED: Final = [
    ("User", ["ci-signer"]),
    ("Group", ["ci-signer"]),
    ("ExecStart", [EXEC_START]),
    ("StandardInput", ["socket"]),
    ("RestartPreventExitStatus", ["78"]),
    (
        "LoadCredential",
        [
            "release-signing.key:/etc/creche/ci-signer/release-signing.key",
            "release-reader.token:/etc/creche/ci-signer/release-reader.token",
            "ci-signer.token:/etc/creche/ci-signer/ci-signer.token",
        ],
    ),
    ("StateDirectory", ["ci-signer"]),
    ("StateDirectoryMode", ["0700"]),
    ("RuntimeDirectory", ["ci-signer-work"]),
    ("RuntimeDirectoryMode", ["0700"]),
    ("ProtectControlGroups", ["no"]),
    ("ProtectProc", ["invisible"]),
    ("ProcSubset", ["pid"]),
    ("PrivateUsers", []),
    ("PrivatePIDs", []),
    ("DynamicUser", []),
    ("NoNewPrivileges", ["yes"]),
    ("CapabilityBoundingSet", [""]),
]


def _values(unit: Path) -> dict[str, list[str]]:
    """Each key of the unit with its values in order. Comments skipped."""
    found: dict[str, list[str]] = {}
    for line in unit.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        found.setdefault(key, []).append(value)
    return found


def _constant(file: str, name: str) -> str:
    """The text of the `&str` constant `name` of a module of the program."""
    text = (PROGRAM / file).read_text(encoding="utf-8")
    match = re.search(rf'const {name}: &str = "([^"]*)";', text)
    assert match is not None, name
    return match.group(1)


def test_the_socket_unit_holds_the_decided_lines() -> None:
    values = _values(SOCKET)
    for key, expected in SOCKET_DECIDED:
        assert values.get(key, []) == expected, key


def test_the_service_unit_holds_the_decided_lines() -> None:
    values = _values(SERVICE)
    for key, expected in SERVICE_DECIDED:
        assert values.get(key, []) == expected, key


def test_the_credentials_are_the_files_that_verify_checks() -> None:
    sources = [one.split(":", 1)[1] for one in _values(SERVICE)["LoadCredential"]]
    names = [one.split(":", 1)[0] for one in _values(SERVICE)["LoadCredential"]]
    assert sources == [
        _constant("verify.rs", "KEY_SOURCE"),
        _constant("verify.rs", "READER_SOURCE"),
        _constant("verify.rs", "TAGGER_SOURCE"),
    ]
    assert names == [
        _constant("credentials.rs", "KEY"),
        _constant("credentials.rs", "READER_TOKEN"),
        _constant("credentials.rs", "TAGGER_TOKEN"),
    ]


def test_the_folders_are_the_folders_of_the_program() -> None:
    values = _values(SERVICE)
    journal = _constant("ledger.rs", "JOURNAL_PATH")
    assert journal == "/var/lib/ci-signer/issued.tsv"
    assert journal.startswith(f"/var/lib/{values['StateDirectory'][0]}/")
    assert _constant("main.rs", "WORK_FOLDER") == f"/run/{values['RuntimeDirectory'][0]}"
    assert _constant("verify.rs", "SIGNER_USER") == values["User"][0]
    assert _constant("verify.rs", "RUNNER_GROUP") == _values(SOCKET)["SocketGroup"][0]


def test_the_socket_is_the_socket_of_the_policy_row() -> None:
    """Section 4.4 of the note: `socket = "/run/ci-signer/attach.sock"`."""
    note = (ROOT / "docs" / "design" / "forgejo-release-path.md").read_text(encoding="utf-8")
    assert f'socket = "{_values(SOCKET)["ListenStream"][0]}"' in note

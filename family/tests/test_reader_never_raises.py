"""A name or a value that a codec refuses never raises out of the package.

The reader answers a report, and the program ends with an exit code."""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

import pytest
from agent_family import cli, registry


def test_revision_reads_a_name_that_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revision holds the bytes of the file name. Not each file system
    can make such a name, so the test gives the path to the function."""
    named = tmp_path / registry.FAMILIES_DIR / "bad\udce9name" / registry.FAMILY_FILE
    monkeypatch.setattr(registry, "_registry_files", lambda _root: [named])
    monkeypatch.setattr(registry, "_read", lambda _path: ("text", None))

    revision = registry.revision_of(tmp_path)

    digest = hashlib.sha256(b"families/bad\xe9name/family.yaml\0text\0").hexdigest()
    assert revision == registry.REVISION_PREFIX + digest[: registry.REVISION_CHARS]


#: A family file with one egress host. The egress rule refuses the host and
#: quotes it, so the report line holds each character of the host.
_FAMILY = (
    "name: odd-host\n"
    "kind: thin\n"
    "description: One written input.\n"
    "model: {{ router: fast, budget_usd_per_day: 1 }}\n"
    'egress: ["{host}"]\n'
)

#: The escape that the YAML reader turns into one lone surrogate.
_SURROGATE = "\\ud800"


def _text_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, codec: str) -> bytes:
    """The bytes that `agent-family validate` writes for a family with the
    egress host `host`, on a terminal that refuses each character that
    `codec` cannot encode. Asserts the exit code of an invalid registry."""
    directory = tmp_path / registry.FAMILIES_DIR / "odd-host"
    directory.mkdir(parents=True)
    (directory / registry.FAMILY_FILE).write_text(_FAMILY.format(host=host), encoding="utf-8")
    written = io.BytesIO()
    terminal = io.TextIOWrapper(written, encoding=codec, errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", terminal)

    code = cli.main(["validate", str(tmp_path), "--family", "odd-host"])
    terminal.flush()

    assert code == cli.EXIT_INVALID
    return written.getvalue()


@pytest.mark.parametrize(
    ("host", "codec", "printed"),
    [
        (f"a{_SURROGATE}.example", "utf-8", b"'a\\ud800.example' is not a hostname"),
        (f"caf\u00e9{_SURROGATE}.example", "utf-8", b"'caf\xc3\xa9\\ud800.example' is not a"),
        ("caf\u00e9.example", "ascii", b"'caf\\xe9.example' is not a hostname"),
    ],
    ids=["surrogate", "surrogate-after-a-letter", "letter-on-ascii"],
)
def test_text_report_escapes_what_the_terminal_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, codec: str, printed: bytes
) -> None:
    assert printed in _text_report(tmp_path, monkeypatch, host, codec)


def test_text_report_keeps_a_line_that_the_terminal_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _text_report(tmp_path, monkeypatch, "caf\u00e9.example", "utf-8")

    assert b"'caf\xc3\xa9.example' is not a hostname" in report

"""A document that the reader library cannot build is a refusal.

Each parser of this package answers `Refusal` for a document it does not
take (contract 06 §10, contract 01b). A reader library raises more than
its own error type: PyYAML raises `ValueError`, `KeyError` and
`AttributeError` for a scalar it cannot build, and both libraries raise
`RecursionError` for a text that nests too deep. Each of those left the
parser as it was, and a caller that catches `Refusal` did not catch it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.intake.store import recipients_of
from handover.manifest import MAX_MANIFEST_BYTES, parse_manifest
from handover.mcpserver import parse_server
from handover.state import MAX_STATE_BYTES, parse_state
from handover_fixtures import manifest_text
from handover_mcp_fixtures import KAGI_LOCK_PATH, KAGI_YAML

SUBJECT = "chaperone/component.yaml"
STATE_SUBJECT = "live-state.json"

#: Deeper than each supported Python reads, and under the byte cap.
YAML_DEPTH = 4000
JSON_DEPTH = 30000

#: More digits than Python reads as one integer.
DIGITS = "1" * 5000

#: One half of a surrogate pair. No UTF-8 text holds it.
LONE_SURROGATE = "\\ud800".encode("ascii").decode("unicode_escape")


def _manifest_refusal(text: str) -> Refusal:
    assert len(text.encode("utf-8", "surrogatepass")) <= MAX_MANIFEST_BYTES
    with pytest.raises(Refusal) as caught:
        parse_manifest(text, SUBJECT)

    assert caught.value.code is RefusalCode.MANIFEST

    return caught.value


def _with_unit(value: str) -> str:
    """A valid manifest with one value in place of its unit."""
    text = manifest_text("chaperone")
    assert "unit: null" in text

    return text.replace("unit: null", f"unit: {value}")


# -- component.yaml -----------------------------------------------------------


def test_a_manifest_that_nests_too_deep_is_refused() -> None:
    text = _with_unit("[" * YAML_DEPTH + "]" * YAML_DEPTH)

    assert _manifest_refusal(text).detail == "does not parse: unreadable YAML"


def test_a_manifest_of_deep_mappings_is_refused() -> None:
    depth = 2000
    text = "unit: " + "{a: " * depth + "1" + "}" * depth

    assert _manifest_refusal(text).detail == "does not parse: unreadable YAML"


@pytest.mark.parametrize(
    "value",
    [
        "!!int abc",
        "!!int 0x",
        "!!float x",
        "!!bool x",
        "!!timestamp x",
        "2001-99-99",
        "2001-01-01 99:99:99",
        pytest.param(DIGITS, id="an-integer-too-long-to-read"),
    ],
)
def test_a_scalar_that_the_reader_cannot_build_is_refused(value: str) -> None:
    refusal = _manifest_refusal(_with_unit(value))

    assert refusal.detail == "does not parse: unreadable YAML"
    assert value[:16] not in refusal.as_line()


def test_a_manifest_that_is_no_utf8_text_is_refused() -> None:
    refusal = _manifest_refusal(_with_unit(f"a{LONE_SURROGATE}b"))

    assert refusal.detail == "is not UTF-8"


def test_a_yaml_error_still_names_its_line() -> None:
    """The refusal of a text that is not YAML does not change."""
    assert _manifest_refusal("unit: [\n").detail.startswith("does not parse: line ")


# -- the live-state document --------------------------------------------------


def _state_refusal(text: str) -> Refusal:
    assert len(text.encode("utf-8", "surrogatepass")) <= MAX_STATE_BYTES
    with pytest.raises(Refusal) as caught:
        parse_state(text, STATE_SUBJECT)

    assert caught.value.code is RefusalCode.STATE

    return caught.value


def test_a_state_document_that_nests_too_deep_is_refused() -> None:
    """Python 3.14 reads this depth. The value is then no object, which is
    a refusal too, so the test names no detail."""
    _state_refusal('{"live": ' + "[" * JSON_DEPTH + "]" * JSON_DEPTH + "}")


@pytest.mark.parametrize(
    "version",
    [pytest.param(f"{DIGITS}.0", id="major"), pytest.param(f"0.{DIGITS}", id="minor")],
)
def test_a_contract_version_too_long_to_read_is_refused(version: str) -> None:
    refusal = _state_refusal(f'{{"provided": {{"pep-grant": "{version}"}}}}')

    assert refusal.detail == "'provided.pep-grant' must be MAJOR.MINOR"


def test_a_state_document_that_is_no_utf8_text_is_refused() -> None:
    refusal = _state_refusal(f'{{"live": "{LONE_SURROGATE}"}}')

    assert refusal.detail == "is not UTF-8"


# -- server.yaml and the sops file --------------------------------------------


@pytest.mark.parametrize("value", ["!!int abc", "!!bool x", "!!timestamp x", "2001-99-99"])
def test_a_server_file_that_the_reader_cannot_build_is_refused(value: str) -> None:
    text = f"{KAGI_YAML.format(lock=KAGI_LOCK_PATH)}extra: {value}\n"
    with pytest.raises(Refusal) as caught:
        parse_server(text.encode("utf-8"), "kagi")

    assert caught.value.code is RefusalCode.SERVER
    assert caught.value.detail == "server.yaml is not readable YAML"


@pytest.mark.parametrize("value", ["!!int abc", "!!bool x", "2001-99-99"])
def test_a_sops_file_that_the_reader_cannot_build_names_no_recipient(
    tmp_path: Path, value: str
) -> None:
    """The intake reads this file before its loop starts. An error here
    ended the process, and the unit started it again."""
    path = tmp_path / ".sops.yaml"
    path.write_text(f"creation_rules: {value}\n", encoding="utf-8")
    path.chmod(0o600)

    assert recipients_of(path, "secrets/mcp/kagi.env", owner_uid=os.getuid()) == ()

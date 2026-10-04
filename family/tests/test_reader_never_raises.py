"""A bad name and a bad value never raise out of the reader or the program.

Two readers can meet a character that is not UTF-8 or is a lone surrogate.
The registry revision reads a file name. The text report prints a message.
Neither may raise: the reader answers a report, and the program ends with an
exit code."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from agent_family import cli, registry

#: A family file whose egress host holds a lone surrogate. The value rule
#: echoes the host into its message, so the report message holds the
#: surrogate. The escape is ASCII text in the file, which PyYAML reads into a
#: string with one lone surrogate.
_SURROGATE_FAMILY = (
    "name: surrogate\n"
    "kind: thin\n"
    "description: One written input.\n"
    "model: { router: fast, budget_usd_per_day: 1 }\n"
    'egress: ["a\\ud800.example"]\n'
)


def test_revision_reads_a_name_that_is_not_utf8(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    non_utf8 = root / registry.FAMILIES_DIR / "bad\udce9name" / registry.FAMILY_FILE
    monkeypatch.setattr(registry, "_registry_files", lambda _root: [non_utf8])
    monkeypatch.setattr(registry, "_read", lambda _path: ("text", None))

    revision = registry.revision_of(root)

    assert revision.startswith(registry.REVISION_PREFIX)


def test_text_report_prints_a_message_with_a_lone_surrogate(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / registry.FAMILIES_DIR / "surrogate"
    directory.mkdir(parents=True)
    (directory / registry.FAMILY_FILE).write_text(_SURROGATE_FAMILY, encoding="utf-8")

    # A terminal that refuses a character it cannot encode, so a print of the
    # surrogate raises without the fix.
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)

    code = cli.main(["validate", str(tmp_path), "--family", "surrogate"])
    stream.flush()

    assert code == cli.EXIT_INVALID
    assert "\\ud800" in buffer.getvalue().decode("utf-8")

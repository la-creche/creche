"""The gap drain, read as an attacker.

`caregiver` writes `/srv/agents/state/rework/secret-gaps/<name>.json` as
the operator, and root reads it. The directory is operator-writable, so every byte
of it is hostile: §3.2's rules are the same ones `spool.py` applies to
`requests/`.

Nothing here runs as root. `writer_uid` is this test's own uid, which is
what makes the ownership rule testable at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agent_release.intake.gaps import MAX_GAPS_PER_PASS, GapDirectory

SERVER: str = "weather"
NAME: str = "weather_token"


@pytest.fixture
def gaps_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secret-gaps"
    directory.mkdir()

    return directory


def _write(directory: Path, name: str, body: object) -> Path:
    target = directory / f"{name}.json"
    target.write_text(json.dumps(body), encoding="utf-8")

    return target


def _drain(directory: Path) -> GapDirectory:
    return GapDirectory(directory, os.getuid())


def test_one_well_formed_gap_is_read(gaps_dir: Path) -> None:
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 100.0})

    found = _drain(gaps_dir).open_gaps()

    assert [(one.server, one.secret) for one in found] == [(SERVER, NAME)]


def test_a_missing_directory_is_no_gap_and_no_crash(tmp_path: Path) -> None:
    assert GapDirectory(tmp_path / "never-made", os.getuid()).open_gaps() == ()


def test_a_name_outside_the_grammar_is_never_read(gaps_dir: Path) -> None:
    """Rule: names from the grammar only. The stem becomes a file name
    under root's secrets directory two steps later."""
    for name in ("../escape", "Weather_Token", "weather-token", "_lead", "x" * 70):
        (gaps_dir / f"{name}.json").parent.mkdir(parents=True, exist_ok=True)
        try:
            (gaps_dir / f"{name}.json").write_text("{}", encoding="utf-8")
        except OSError:
            continue

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_body_naming_another_secret_than_its_file_is_refused(gaps_dir: Path) -> None:
    """The file name and the body must agree. Otherwise the gap the operator is
    shown and the gap root fills are two different names."""
    _write(gaps_dir, NAME, {"server": SERVER, "secret": "github_token", "at": 1.0})

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_server_name_outside_the_grammar_is_refused(gaps_dir: Path) -> None:
    """The server name is copied out of
    `mcp/<name>/server.yaml`, which the attacker writes, and it reaches
    the operator's browser."""
    _write(gaps_dir, NAME, {"server": "<script>x</script>", "secret": NAME, "at": 1.0})

    assert _drain(gaps_dir).open_gaps() == ()


def test_an_unknown_key_is_refused(gaps_dir: Path) -> None:
    """§3.2 rule 2: the key set is closed."""
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0, "value": "hunter2"})

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_file_that_is_not_json_is_refused(gaps_dir: Path) -> None:
    (gaps_dir / f"{NAME}.json").write_bytes(b"\xff\xfe not json at all")

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_gap_larger_than_the_cap_is_refused(gaps_dir: Path) -> None:
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0, "pad": "x" * 8192})

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_symlink_is_never_followed(gaps_dir: Path, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"server": SERVER, "secret": NAME, "at": 1.0}), encoding="utf-8")
    (gaps_dir / f"{NAME}.json").symlink_to(real)

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_directory_at_a_gap_name_is_refused(gaps_dir: Path) -> None:
    (gaps_dir / f"{NAME}.json").mkdir()

    assert _drain(gaps_dir).open_gaps() == ()


def test_a_file_another_uid_wrote_is_refused(gaps_dir: Path) -> None:
    """Ownership is the rule; this test proves the code READS it, by
    naming a uid nothing here can own."""
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0})

    assert GapDirectory(gaps_dir, os.getuid() + 1).open_gaps() == ()


def test_a_symlinked_gap_directory_is_refused(gaps_dir: Path, tmp_path: Path) -> None:
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0})
    link = tmp_path / "linked"
    link.symlink_to(gaps_dir)

    assert GapDirectory(link, os.getuid()).open_gaps() == ()


def test_a_flood_is_capped_and_still_answers(gaps_dir: Path) -> None:
    """The directory is operator-writable, so its size is the attacker's
    choice. Root reads a bounded prefix and never sorts the whole thing."""
    for index in range(MAX_GAPS_PER_PASS + 20):
        _write(gaps_dir, f"secret_{index:03d}", {"server": SERVER, "secret": f"secret_{index:03d}"})

    found = _drain(gaps_dir).open_gaps()

    assert len(found) == MAX_GAPS_PER_PASS


def test_one_bad_gap_does_not_hide_a_good_one(gaps_dir: Path) -> None:
    _write(gaps_dir, "broken_one", {"server": "..", "secret": "broken_one"})
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0})

    assert [one.secret for one in _drain(gaps_dir).open_gaps()] == [NAME]


def test_at_is_optional_and_decides_nothing(gaps_dir: Path) -> None:
    """`at` is an operator-written timestamp. Root records it and never lets
    it decide anything."""
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1e18})

    assert [one.secret for one in _drain(gaps_dir).open_gaps()] == [NAME]


def test_closing_a_gap_removes_its_file(gaps_dir: Path) -> None:
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0})
    drain = _drain(gaps_dir)
    (gap,) = drain.open_gaps()

    assert drain.close(gap) is True
    assert not (gaps_dir / f"{NAME}.json").exists()
    assert drain.open_gaps() == ()


def test_closing_a_gap_twice_is_quiet(gaps_dir: Path) -> None:
    _write(gaps_dir, NAME, {"server": SERVER, "secret": NAME, "at": 1.0})
    drain = _drain(gaps_dir)
    (gap,) = drain.open_gaps()
    drain.close(gap)

    assert drain.close(gap) is False

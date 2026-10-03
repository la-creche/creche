"""The fault-file reader: input from another process, so a bad entry is
dropped rather than trusted or raised (contract 05 section 3.3.1)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_managerd.faults import FaultEntry, drop_superseded, read_fault_file, rescope_by_fleet

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def write(path: Path, body: object) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert read_fault_file(tmp_path / "no-such-file.json", "pep") is None


def test_corrupt_json_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_fault_file(path, "pep") is None


def test_a_json_list_at_the_top_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(path, [1, 2, 3])
    assert read_fault_file(path, "pep") is None


def test_missing_family_or_written_at_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(path, {"source": "pep", "faults": []})
    assert read_fault_file(path, "pep") is None


def test_faults_not_a_list_is_none(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(path, {"family": "chat", "written_at": "2026-09-19T11:59:00Z", "faults": {}})
    assert read_fault_file(path, "pep") is None


def test_a_valid_pep_fault_parses_with_its_detail(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "source": "pep",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                {
                    "code": "grants_stale",
                    "blocks_turns": True,
                    "since": "2026-09-19T11:58:41Z",
                    "source": "pep",
                    "message": "grants/chat.json: unknown version 3",
                    "rev": "reg-8c01aa",
                }
            ],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.family == "chat"
    assert len(result.faults) == 1
    fault = result.faults[0]
    assert fault.code == "grants_stale"
    assert fault.detail == {"message": "grants/chat.json: unknown version 3", "rev": "reg-8c01aa"}


def test_sessiond_may_report_an_unreadable_audit(tmp_path: Path) -> None:
    """Contract 05 section 3.3's `audit_unreadable`.
    It does not stop turns: the PEP's audit is how sessiond LABELS a turn
    that waits for a phone tap, never how it decides to run one."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "source": "sessiond",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                {
                    "code": "audit_unreadable",
                    "blocks_turns": False,
                    "since": "2026-09-19T11:58:41Z",
                    "source": "sessiond",
                    "message": "cannot read the PEP's audit at /srv/.../2026-09-19.jsonl",
                }
            ],
        },
    )
    result = read_fault_file(path, "sessiond", now=NOW)
    assert result is not None
    assert len(result.faults) == 1
    assert result.faults[0].code == "audit_unreadable"
    assert result.faults[0].blocks_turns is False


def test_blocks_turns_is_forced_from_the_fixed_table_not_the_file(tmp_path: Path) -> None:
    """The table in contract 05 section 3.3 is authoritative; a fault file
    is untrusted input and must not be able to lie about it."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                {"code": "grants_stale", "blocks_turns": False, "since": "x", "source": "pep"}
            ],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].blocks_turns is True


def test_an_unknown_code_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [{"code": "not_a_real_code", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults == ()


def test_a_code_belonging_to_a_different_source_is_dropped(tmp_path: Path) -> None:
    """`grants_stale` is the PEP's code. A sessiond-written file claiming
    it is either a bug in sessiond or a forged file, and either way it is
    dropped, not trusted (rule 6)."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [{"code": "grants_stale", "since": "x", "source": "sessiond"}],
        },
    )
    result = read_fault_file(path, "sessiond", now=NOW)
    assert result is not None
    assert result.faults == ()


def test_one_malformed_entry_does_not_drop_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "2026-09-19T11:59:00Z",
            "faults": [
                "not even an object",
                {"code": "orphan_processes", "since": "2026-09-19T11:58:00Z", "source": "sessiond"},
                {"blocks_turns": True},  # no code
            ],
        },
    )
    result = read_fault_file(path, "sessiond", now=NOW)
    assert result is not None
    assert [f.code for f in result.faults] == ["orphan_processes"]


def test_a_fresh_file_is_not_stale(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": (NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "faults": [{"code": "grants_stale", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].stale is False


def test_a_file_older_than_90_seconds_marks_every_entry_stale(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": (NOW - timedelta(seconds=91)).isoformat().replace("+00:00", "Z"),
            "faults": [{"code": "grants_stale", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].stale is True


def test_an_unparseable_written_at_counts_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": "not a timestamp",
            "faults": [{"code": "grants_stale", "since": "x", "source": "pep"}],
        },
    )
    result = read_fault_file(path, "pep", now=NOW)
    assert result is not None
    assert result.faults[0].stale is True


# --- `sandbox_start_failed`, the code two services report (section 3.3.1) -----


def sessiond_file(path: Path, sandbox: str) -> None:
    write(
        path,
        {
            "family": "chat",
            "source": "sessiond",
            "written_at": (NOW - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "faults": [
                {
                    "code": "sandbox_start_failed",
                    "blocks_turns": True,
                    "since": "2026-09-19T11:58:41Z",
                    "source": "sessiond",
                    "message": "playpen answered fatal: mount_dir_unset",
                    "sandbox": sandbox,
                }
            ],
        },
    )


def test_sessiond_may_raise_sandbox_start_failed(tmp_path: Path) -> None:
    """Contract 05 section 3.3.1 names FOUR codes for this writer. The reader
    allowed three, so a playpen that answered `fatal` reached the status
    document through nothing at all."""
    path = tmp_path / "chat.json"
    sessiond_file(path, "chat-s1")

    result = read_fault_file(path, "sessiond", now=NOW)

    assert result is not None
    assert [one.code for one in result.faults] == ["sandbox_start_failed"]
    assert result.faults[0].detail["sandbox"] == "chat-s1"


def test_the_lone_sandbox_failing_blocks_turns(tmp_path: Path) -> None:
    """Section 3.3's own wording: "no sandbox reached `ready`". One live
    sandbox, and that one fatal, means the family cannot serve."""
    path = tmp_path / "chat.json"
    sessiond_file(path, "chat-s1")
    read = read_fault_file(path, "sessiond", now=NOW)
    assert read is not None

    rescoped = rescope_by_fleet(read.faults, ("chat-s1",))

    assert rescoped[0].blocks_turns is True


def test_a_replacement_failing_keeps_the_family_up(tmp_path: Path) -> None:
    """The reason the code was dropped rather than accepted. During a
    make-before-break replacement the fault names the INCOMING sandbox while
    the outgoing one serves every turn, and section 5.3 promises the family
    keeps serving. So `blocks_turns` comes from the fleet, not the code."""
    path = tmp_path / "chat.json"
    sessiond_file(path, "chat-s2")
    read = read_fault_file(path, "sessiond", now=NOW)
    assert read is not None

    rescoped = rescope_by_fleet(read.faults, ("chat-s1", "chat-s2"))

    assert rescoped[0].blocks_turns is False
    # Nothing is hidden: the entry, its message and its sandbox all survive.
    assert rescoped[0].code == "sandbox_start_failed"
    assert rescoped[0].detail["sandbox"] == "chat-s2"


def test_the_fleet_rule_touches_no_other_code(tmp_path: Path) -> None:
    """Every other code of section 3.3 is fixed per code, as the table says.
    Only this one is defined fleet-wide."""
    path = tmp_path / "chat.json"
    write(
        path,
        {
            "family": "chat",
            "written_at": (NOW - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "faults": [{"code": "protocol_mismatch", "since": "x", "source": "sessiond"}],
        },
    )
    read = read_fault_file(path, "sessiond", now=NOW)
    assert read is not None

    rescoped = rescope_by_fleet(read.faults, ("chat-s1", "chat-s2"))

    assert rescoped[0].blocks_turns is True


# --- a fault raised before the grant file this pass wrote --------------------


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _pep_fault(since: datetime, code: str = "grants_stale", source: str = "pep") -> FaultEntry:
    return FaultEntry(code=code, blocks_turns=True, since=_stamp(since), source=source)


def _grant_file(tmp_path: Path, written_at: datetime) -> Path:
    path = tmp_path / "chat-grants.json"
    write(path, {"version": 2, "family": "chat"})
    stamp = written_at.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_a_fault_older_than_the_grant_file_is_dropped(tmp_path: Path) -> None:
    """A preflight probe leaves `grants_stale` behind and `managerd` then
    writes the real grant file: publishing the fault would show `degraded`
    over a complaint about a file that no longer exists."""
    path = _grant_file(tmp_path, NOW)

    assert drop_superseded((_pep_fault(NOW - timedelta(seconds=30)),), path) == ()


def test_a_fault_after_the_write_still_holds(tmp_path: Path) -> None:
    """The PEP read the file this pass wrote and would not serve it. That is
    the failure the code is for."""
    path = _grant_file(tmp_path, NOW - timedelta(seconds=30))
    faults = (_pep_fault(NOW),)

    assert drop_superseded(faults, path) == faults


def test_a_fault_inside_the_same_second_is_kept(tmp_path: Path) -> None:
    """`since` is second-resolution (contract 05 section 3.3), so a fault
    raised 0.4 s AFTER the write can read as 0.6 s before it. Keeping it is
    the conservative answer: the PEP's sweep clears it a moment later."""
    path = _grant_file(tmp_path, NOW)
    faults = (_pep_fault(NOW - timedelta(milliseconds=600)),)

    assert drop_superseded(faults, path) == faults


def test_no_grant_file_keeps_every_fault(tmp_path: Path) -> None:
    """Nothing to compare against. A revoked family keeps its fault."""
    faults = (_pep_fault(NOW - timedelta(seconds=30)),)

    assert drop_superseded(faults, tmp_path / "gone.json") == faults


def test_an_unparseable_since_keeps_its_fault(tmp_path: Path) -> None:
    path = _grant_file(tmp_path, NOW)
    faults = (FaultEntry(code="grants_stale", blocks_turns=True, since="x", source="pep"),)

    assert drop_superseded(faults, path) == faults


def test_the_rule_touches_only_the_peps_own_code(tmp_path: Path) -> None:
    """A channel fault says nothing about a grant file, so a grant file's
    write time says nothing about it."""
    path = _grant_file(tmp_path, NOW)
    old = NOW - timedelta(seconds=30)
    faults = (
        _pep_fault(old, code="protocol_mismatch", source="sessiond"),
        _pep_fault(old, source="sessiond"),
    )

    assert drop_superseded(faults, path) == faults

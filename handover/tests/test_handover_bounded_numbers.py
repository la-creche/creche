"""A number that a file holds is read with a bound.

`stage7-releases.md` §2.2: `requests/` is drained on each run, with no
exception. A JSON integer has no largest value, and a float has one. A
reader that converts the number answers a refusal for a number it cannot
hold. It does not raise.

The same holds for each file that a later run reads back: the marker of
the follower, a gap file, a note in `running/` and a ledger entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor import spool as spool_module
from handover.executor.drain import drain
from handover.executor.request import Request, parse_request
from handover.executor.spool import (
    DONE_DIR,
    HOOK_TIMEOUT_MAX_S,
    HOOK_TIMEOUT_MIN_S,
    REJECTED_DIR,
    REQUESTS_DIR,
    RUNNING_DIR,
    SELF_SUFFIX,
    SWITCH_SUFFIX,
    Spool,
)
from handover.executor.steps import Wiring
from handover.follow import Answer, Marker, read_answer, read_marker
from handover.intake import run as intake_run
from handover.intake.gaps import GapDirectory, OpenGap
from handover.intake.run import Watch
from handover.intake.store import SecretStore
from handover.intake.token import Tokens
from handover_executor_fixtures import (
    REQUEST_ID,
    FakeRun,
    fake_host,
    git_host_answers,
    grant_transport,
    make_spool_dirs,
    request_body,
    this_uid,
    write_request,
)

from handover import manifest

COMPONENT = "chaperone"
VERSION = "2.1.0"
OTHER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DF"

#: The largest power of ten that a float holds, and the next one.
LARGEST_POWER = 10**308
PAST_A_FLOAT = 10**309

TS_REFUSAL = "field 'ts' is malformed"

#: A text that only the error holds. No line of the journal may carry it.
ERROR_TEXT = "the-text-of-the-error"

#: Deeper than each supported Python reads, and under the cap of each file.
JSON_DEPTH = 30000

SERVER = "weather"
SECRET = "weather_token"
HOOK_USER = "root"


def _body(ts: object, request_id: str = REQUEST_ID) -> dict[str, object]:
    return request_body({COMPONENT: VERSION}, request_id=request_id) | {"ts": ts}


def _raw(body: dict[str, object]) -> bytes:
    return json.dumps(body).encode("utf-8")


def _entry(spool_root: Path, request_id: str) -> dict[str, object]:
    path = spool_root / DONE_DIR / f"{request_id}.json"
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


# -- the request ---------------------------------------------------------------


@pytest.mark.parametrize(
    "ts",
    [
        pytest.param(PAST_A_FLOAT, id="past-a-float"),
        pytest.param(-PAST_A_FLOAT, id="below-zero"),
        pytest.param(10**400, id="more-digits"),
    ],
)
def test_a_time_too_large_for_a_float_is_refused(ts: int) -> None:
    with pytest.raises(Refusal) as caught:
        parse_request(_raw(_body(ts)), REQUEST_ID)

    assert caught.value.code is RefusalCode.REQUEST
    assert caught.value.detail == TS_REFUSAL


def test_the_largest_power_of_ten_is_still_a_time() -> None:
    request = parse_request(_raw(_body(LARGEST_POWER)), REQUEST_ID)

    assert request.ts == float(LARGEST_POWER)


def test_a_request_with_such_a_time_leaves_requests(tmp_path: Path) -> None:
    """The entry is under the id of the file name, the file is gone, and
    the pass reads the next file."""
    spool_root = make_spool_dirs(tmp_path)
    write_request(spool_root, REQUEST_ID, _body(PAST_A_FLOAT))
    write_request(spool_root, OTHER_ID, _body(PAST_A_FLOAT, OTHER_ID))
    wiring = Wiring(
        host=fake_host(tmp_path, FakeRun(answers=git_host_answers())),
        transport=grant_transport(),
    )
    spool = Spool(str(spool_root), this_uid())
    try:
        handled = drain(spool, wiring)
    finally:
        spool.close()

    assert handled == 2
    assert not list((spool_root / REQUESTS_DIR).iterdir())
    assert not list((spool_root / REJECTED_DIR).iterdir())
    for request_id in (REQUEST_ID, OTHER_ID):
        entry = _entry(spool_root, request_id)
        assert entry["status"] == "refused"
        assert entry["refused_check"] == "request"
        assert entry["reason"] == TS_REFUSAL


def test_a_read_that_raises_moves_the_file_and_the_pass_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§2.4 row 1: a file that is no parseable request goes to `rejected/`
    whole. An error that the reader does not name is such a file: the pass
    writes the type of the error, and the next file is read."""

    def read(raw: bytes, request_id: str) -> Request:
        if request_id == REQUEST_ID:
            raise RuntimeError(ERROR_TEXT)

        return parse_request(raw, request_id)

    monkeypatch.setattr(spool_module, "parse_request", read)
    spool_root = make_spool_dirs(tmp_path)
    write_request(spool_root, REQUEST_ID, _body(1.0))
    write_request(spool_root, OTHER_ID, _body(PAST_A_FLOAT, OTHER_ID))
    wiring = Wiring(
        host=fake_host(tmp_path, FakeRun(answers=git_host_answers())),
        transport=grant_transport(),
    )
    spool = Spool(str(spool_root), this_uid())
    try:
        handled = drain(spool, wiring)
    finally:
        spool.close()

    assert handled == 2
    assert not list((spool_root / REQUESTS_DIR).iterdir())
    assert len(list((spool_root / REJECTED_DIR).iterdir())) == 1
    assert not (spool_root / DONE_DIR / f"{REQUEST_ID}.json").exists()
    assert _entry(spool_root, OTHER_ID)["status"] == "refused"
    said = capsys.readouterr().out
    assert "RuntimeError" in said
    assert ERROR_TEXT not in said


# -- the marker of the follower ------------------------------------------------


def _marker_file(tmp_path: Path, at: object) -> Path:
    moving = {COMPONENT: VERSION}
    body = Marker(seen=moving, asked=moving, request_id=REQUEST_ID, asks=1).as_dict() | {"at": at}
    path = tmp_path / "follow.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return path


def test_a_marker_reads_back(tmp_path: Path) -> None:
    assert read_marker(_marker_file(tmp_path, 5)).asked_at == 5.0


def test_a_marker_with_a_time_that_no_float_holds_is_no_marker(tmp_path: Path) -> None:
    assert read_marker(_marker_file(tmp_path, PAST_A_FLOAT)) == Marker()


def test_an_entry_that_nests_too_deep_holds_the_set(tmp_path: Path) -> None:
    """The follower cannot read the entry, which is the quiet answer."""
    entry = tmp_path / f"{REQUEST_ID}.json"
    entry.write_text('{"reason": ' + "[" * JSON_DEPTH + "]" * JSON_DEPTH + "}", encoding="utf-8")

    assert read_answer(tmp_path, REQUEST_ID) is Answer.FINAL


# -- a gap file ----------------------------------------------------------------


def _gaps(tmp_path: Path) -> Path:
    directory = tmp_path / "secret-gaps"
    directory.mkdir()

    return directory


def test_a_gap_with_a_time_that_no_float_holds_is_still_a_gap(tmp_path: Path) -> None:
    """`at` decides nothing, so the gap stays and the time is zero. A
    second gap in the same directory is read too."""
    directory = _gaps(tmp_path)
    for secret, at in ((SECRET, PAST_A_FLOAT), ("other_token", 7)):
        body = {"server": SERVER, "secret": secret, "at": at}
        (directory / f"{secret}.json").write_text(json.dumps(body), encoding="utf-8")

    found = GapDirectory(directory, os.getuid()).open_gaps()

    assert [(one.secret, one.at) for one in found] == [("other_token", 7.0), (SECRET, 0.0)]


class _Stop(Exception):
    """Ends the loop of the intake in a test."""


class _Listener:
    bound = False

    def open(self) -> bool:
        return False

    def close(self) -> None:
        return None


class _RaisesOnce(GapDirectory):
    """A gap directory whose first read raises an error that is no `OSError`."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory, os.getuid())
        self.reads = 0

    def open_gaps(self) -> tuple[OpenGap, ...]:
        self.reads += 1
        if self.reads == 1:
            raise RuntimeError(ERROR_TEXT)

        return super().open_gaps()


def test_a_pass_that_raises_does_not_end_the_intake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unit starts a process that ended again. A file that made one
    pass raise then made each later process raise."""
    gaps = _RaisesOnce(_gaps(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    wiring = intake_run.Wiring(
        gaps=gaps,
        store=SecretStore(secrets, lambda _: None, owner_uid=os.getuid()),
        tokens=Tokens(lambda: 0.0),
        push=lambda _gap, _token: False,
        watch=Watch(lambda: 0.0),
        listener=_Listener(),
    )
    sleeps = {"n": 0}

    def sleep(_: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 2:
            raise _Stop

    with pytest.raises(_Stop):
        intake_run._loop(wiring, sleep)

    assert gaps.reads == 2
    said = capsys.readouterr().out
    assert "RuntimeError" in said
    assert ERROR_TEXT not in said


# -- a note in `running/` ------------------------------------------------------


def _hook(timeout: object) -> dict[str, object]:
    return {"command": ["/bin/true"], "user": HOOK_USER, "timeout_s": timeout}


def _switch_file(spool_root: Path, hook: object) -> None:
    body = {"component": COMPONENT, "to": "/opt/x", "prev": "/opt/x.prev", "verify": hook}
    path = spool_root / RUNNING_DIR / f"{REQUEST_ID}-{COMPONENT}{SWITCH_SUFFIX}"
    path.write_text(json.dumps(body), encoding="utf-8")


def _unfinished(spool_root: Path) -> list[spool_module.Unfinished]:
    spool = Spool(str(spool_root), this_uid())
    try:
        return spool.unfinished()
    finally:
        spool.close()


def test_the_bounds_of_a_hook_time_are_those_of_a_manifest() -> None:
    assert (HOOK_TIMEOUT_MIN_S, HOOK_TIMEOUT_MAX_S) == (
        manifest.TIMEOUT_MIN_S,
        manifest.TIMEOUT_MAX_S,
    )


@pytest.mark.parametrize("timeout", [HOOK_TIMEOUT_MIN_S, HOOK_TIMEOUT_MAX_S])
def test_a_note_keeps_a_hook_with_a_time_limit_in_its_bounds(tmp_path: Path, timeout: int) -> None:
    spool_root = make_spool_dirs(tmp_path)
    _switch_file(spool_root, _hook(timeout))

    (found,) = _unfinished(spool_root)

    assert found.note.verify is not None
    assert found.note.verify.timeout_s == timeout


@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(HOOK_TIMEOUT_MIN_S - 1, id="below"),
        pytest.param(HOOK_TIMEOUT_MAX_S + 1, id="above"),
        pytest.param(PAST_A_FLOAT, id="past-a-float"),
        pytest.param(True, id="a-boolean"),
    ],
)
def test_a_hook_with_a_time_limit_outside_its_bounds_is_no_hook(
    tmp_path: Path, timeout: object
) -> None:
    """The note stays a note: the repair puts the tree back and says that
    it ran no hook."""
    spool_root = make_spool_dirs(tmp_path)
    _switch_file(spool_root, _hook(timeout))

    (found,) = _unfinished(spool_root)

    assert found.note.component == COMPONENT
    assert found.note.verify is None


def test_a_self_note_with_such_a_hook_is_no_note(tmp_path: Path) -> None:
    spool_root = make_spool_dirs(tmp_path)
    body = {
        "component": "handover",
        "to": "/opt/x",
        "prev": "/opt/x.prev",
        "to_version": VERSION,
        "from_version": None,
        "requested_by": "human",
        "verify": _hook(PAST_A_FLOAT),
    }
    path = spool_root / RUNNING_DIR / f"{REQUEST_ID}-handover{SELF_SUFFIX}"
    path.write_text(json.dumps(body), encoding="utf-8")
    spool = Spool(str(spool_root), this_uid())
    try:
        assert spool.unfinished_self() == []
    finally:
        spool.close()


def test_a_note_that_nests_too_deep_is_no_note(tmp_path: Path) -> None:
    spool_root = make_spool_dirs(tmp_path)
    _switch_file(spool_root, None)
    path = spool_root / RUNNING_DIR / f"{OTHER_ID}-{COMPONENT}{SWITCH_SUFFIX}"
    path.write_text('{"component": ' + "[" * JSON_DEPTH + "]" * JSON_DEPTH + "}", encoding="utf-8")

    assert [one.request_id for one in _unfinished(spool_root)] == [REQUEST_ID]

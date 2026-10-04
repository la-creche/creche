"""A number that a file holds is read with a bound.

`stage7-releases.md` §2.2: `requests/` is drained on each run, with no
exception. A JSON integer has no largest value, and a float has one. A
reader that converts the number answers a refusal for a number it cannot
hold. It does not raise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor import spool as spool_module
from handover.executor.drain import drain
from handover.executor.request import Request, parse_request
from handover.executor.spool import DONE_DIR, REJECTED_DIR, REQUESTS_DIR, Spool
from handover.executor.steps import Wiring
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

COMPONENT = "chaperone"
VERSION = "2.1.0"
OTHER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DF"

#: The largest power of ten that a float holds, and the next one.
LARGEST_POWER = 10**308
PAST_A_FLOAT = 10**309

TS_REFUSAL = "field 'ts' is malformed"

#: A text that only the error holds. No line of the journal may carry it.
ERROR_TEXT = "the-text-of-the-error"


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


def test_the_largest_whole_time_is_still_a_request() -> None:
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

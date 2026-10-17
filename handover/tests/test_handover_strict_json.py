"""Each JSON reader and each JSON writer of the release tool holds strict JSON.

A reader gives a text that is not strict JSON the answer that it gives a
text that is not JSON. It also records one notice with `notices.record`: the
surface word of the reader and the rule word. A writer writes strict JSON
only. For a value with no strict text it refuses in a stated way, and it
writes no file and sends no body.

`handover/src/handover/strictjson.py` holds the rules. The test of that file
is in the noticeboard, which holds the first copy
(`bin/tests/test_strictjson_copies.py`).
"""

from __future__ import annotations

import io
import json
import math
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor import drain, phone, provenance, quiet, spool
from handover.executor.approval import Verdict, not_granted
from handover.executor.quiesce import starting
from handover.executor.request import NOT_AN_OBJECT, parse_request
from handover.executor.selfcontained import UNREADABLE_DIRECT_URL, escapes
from handover.executor.spool import RUNNING_DIR, SWITCH_SUFFIX, Spool, SpoolError
from handover.follow import Answer, Marker, read_answer, read_marker, write_marker
from handover.intake import notify
from handover.intake import run as intake_run
from handover.intake.gaps import GapDirectory, OpenGap
from handover.requester.file import plan_request
from handover.resolve import Resolution, build_document
from handover.state import ReleaseState, parse_state
from handover.strictjson import NotStrictJson
from handover_executor_fixtures import REQUEST_ID, make_spool_dirs, request_body, this_uid

from handover import notices

#: One call of `notices.record`: the surface, the rule word, the family and
#: the proof.
type Notice = tuple[str, str, str | None, bool]


class Notices:
    """The calls of `notices.record` in one test."""

    def __init__(self) -> None:
        self.calls: list[Notice] = []

    def record(
        self, surface: str, rule: object, family: str | None = None, *, proven: bool = True
    ) -> None:
        self.calls.append((surface, str(rule), family, proven))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Notices:
    found = Notices()
    monkeypatch.setattr(notices, "record", found.record)

    return found


@pytest.fixture(autouse=True)
def _no_notice_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no `configure` call, and the test puts the
    settings of the module back at its end."""
    monkeypatch.setattr(notices, "_settings", None)


def _strict(body: object) -> bytes:
    return json.dumps(body).encode("utf-8")


# -- the release request (manifest.request) -----------------------------------

REQUEST_TEXT = json.dumps(request_body({"chaperone": "2.1.0"}))

#: A request text that `json.loads` reads and the strict reader refuses, with
#: the rule word of the refusal.
NOT_STRICT_REQUESTS = {
    "a-key-two-times": (
        REQUEST_TEXT.replace('"kind": ', '"kind": "release", "kind": '),
        "duplicate_key",
    ),
    "nan": (REQUEST_TEXT.replace('"ts": 1758153590.0', '"ts": NaN'), "constant"),
    "an-integer-past-64-bits": (
        REQUEST_TEXT.replace('"ts": 1758153590.0', '"ts": ' + "9" * 21),
        "integer_range",
    ),
    "a-lone-surrogate": (REQUEST_TEXT.replace('"agent-control"', '"\\ud800"'), "lone_surrogate"),
}


def test_the_request_text_of_this_file_is_a_request() -> None:
    assert '"ts": 1758153590.0' in REQUEST_TEXT
    assert parse_request(REQUEST_TEXT.encode("utf-8"), REQUEST_ID).id == REQUEST_ID


def test_a_request_that_is_not_json_is_not_a_json_object() -> None:
    assert _request_refusal(b"{not json").detail == NOT_AN_OBJECT


def _request_refusal(raw: bytes) -> Refusal:
    with pytest.raises(Refusal) as caught:
        parse_request(raw, REQUEST_ID)

    return caught.value


@pytest.mark.parametrize("name", NOT_STRICT_REQUESTS)
def test_a_request_that_is_not_strict_gets_the_refusal_of_a_text_that_is_not_json(
    name: str, recorded: Notices
) -> None:
    text, rule = NOT_STRICT_REQUESTS[name]

    refusal = _request_refusal(text.encode("utf-8"))

    assert (refusal.code, refusal.detail) == (RefusalCode.REQUEST, NOT_AN_OBJECT)
    assert recorded.calls == [("manifest.request", rule, None, True)]


@pytest.mark.parametrize(
    ("raw", "rule"),
    [
        (b"\xef\xbb\xbf" + REQUEST_TEXT.encode("utf-8"), "byte_order_mark"),
        (REQUEST_TEXT.encode("utf-16"), "not_utf8"),
    ],
    ids=["a-byte-order-mark", "utf-16"],
)
def test_a_request_in_another_encoding_records_its_rule(
    raw: bytes, rule: str, recorded: Notices
) -> None:
    assert _request_refusal(raw).detail == NOT_AN_OBJECT
    assert recorded.calls == [("manifest.request", rule, None, True)]


def test_a_request_past_its_cap_keeps_its_own_refusal(recorded: Notices) -> None:
    """The cap check comes before the read of the text."""
    refusal = _request_refusal(REQUEST_TEXT.encode("utf-8") + b" " * 4096)

    assert "larger than" in refusal.detail
    assert recorded.calls == []


# -- the live-state document (manifest.state) ---------------------------------


def test_a_live_state_with_a_key_two_times_does_not_parse(recorded: Notices) -> None:
    text = '{"live": {"chaperone": "bad"}, "live": {"chaperone": "1.0.0"}}'

    with pytest.raises(Refusal) as caught:
        parse_state(text, "live-state.json")

    assert caught.value.code is RefusalCode.STATE
    assert caught.value.detail == "does not parse as JSON"
    assert recorded.calls == [("manifest.state", "duplicate_key", None, True)]


def test_a_strict_live_state_reads_as_before(recorded: Notices) -> None:
    state = parse_state('{"live": {"chaperone": "1.0.0"}}', "live-state.json")

    assert state.live == {"chaperone": "1.0.0"}
    assert recorded.calls == []


# -- the marker and the ledger entry of the follower ---------------------------


def test_a_marker_that_is_not_strict_reads_as_no_marker(tmp_path: Path, recorded: Notices) -> None:
    path = tmp_path / "follow.json"
    path.write_text('{"seen": {}, "asked": {}, "id": null, "asks": 0, "at": NaN}', "utf-8")

    assert read_marker(path) == Marker()
    assert recorded.calls == [("release.marker", "constant", None, True)]


def test_a_marker_with_no_strict_text_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "state" / "follow.json"
    marker = Marker(seen={"noticeboard": "0.1.1"}, asked={}, asked_at=math.inf)

    with pytest.raises(NotStrictJson):
        write_marker(path, marker)

    assert list(tmp_path.rglob("*")) in ([], [tmp_path / "state"])


def test_a_strict_marker_has_the_bytes_of_before(tmp_path: Path) -> None:
    path = tmp_path / "follow.json"
    marker = Marker(seen={"noticeboard": "0.1.1"}, asked={}, asks=0, asked_at=1760000000.5)

    write_marker(path, marker)

    assert path.read_bytes() == json.dumps(marker.as_dict(), sort_keys=True).encode("utf-8")
    assert read_marker(path) == marker


def test_a_ledger_entry_that_is_not_strict_holds_the_set(tmp_path: Path, recorded: Notices) -> None:
    """The gate closed with no tap, and a strict entry asks again. The same
    entry with a key two times is an entry this side cannot read."""
    reason = not_granted(Verdict.TIMEOUT)
    strict = {"refused_check": "approval", "reason": reason}
    (tmp_path / f"{REQUEST_ID}.json").write_text(json.dumps(strict), "utf-8")
    assert read_answer(tmp_path, REQUEST_ID) is Answer.UNANSWERED

    twice = json.dumps(strict).replace('"reason"', '"reason": "x", "reason"')
    (tmp_path / f"{REQUEST_ID}.json").write_text(twice, "utf-8")

    assert read_answer(tmp_path, REQUEST_ID) is Answer.FINAL
    assert recorded.calls == [("release.spool_entry", "duplicate_key", None, True)]


# -- a gap file of the intake (release.gap_file) -------------------------------


def _gaps(tmp_path: Path, text: str) -> tuple[OpenGap, ...]:
    directory = tmp_path / "secret-gaps"
    directory.mkdir()
    (directory / "kagi_token.json").write_text(text, "utf-8")

    return GapDirectory(directory, os.getuid()).open_gaps()


def test_a_strict_gap_file_is_a_gap(tmp_path: Path, recorded: Notices) -> None:
    text = '{"server": "kagi", "secret": "kagi_token", "at": 1}'

    assert _gaps(tmp_path, text) == (OpenGap("kagi", "kagi_token", 1.0),)
    assert recorded.calls == []


def test_a_gap_file_that_is_not_strict_is_no_gap(tmp_path: Path, recorded: Notices) -> None:
    text = '{"server": "kagi", "secret": "other", "secret": "kagi_token", "at": 1}'

    assert _gaps(tmp_path, text) == ()
    assert recorded.calls == [("release.gap_file", "duplicate_key", None, True)]


# -- the phone hook (hook.answer) ----------------------------------------------


class _Reply:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self._body = io.BytesIO(body)

    def read(self, count: int) -> bytes:
        return self._body.read(count)

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Opener:
    """The opener of `urllib.request`, with one answer and no network."""

    def __init__(self, answer: bytes) -> None:
        self.answer = answer
        self.sent: list[bytes] = []

    def open(self, request: Any, timeout: float) -> _Reply:
        del timeout
        self.sent.append(request.data)

        return _Reply(self.answer)


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> _Opener:
    made = _Opener(b"")
    monkeypatch.setattr(phone.urllib.request, "build_opener", lambda *_handlers: made)

    return made


def test_a_strict_hook_answer_reads(opener: _Opener, recorded: Notices) -> None:
    opener.answer = b'{"gate": "g", "decision": "pending"}'
    body: dict[str, object] = {"kind": "approval-poll", "gate": "g"}

    assert phone._post("https://hook.invalid", "bearer", body) == {
        "gate": "g",
        "decision": "pending",
    }
    assert opener.sent == [json.dumps(body).encode("utf-8")]
    assert recorded.calls == []


@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        (b'{"gate": "g", "decision": "approve", "at": NaN}', "constant"),
        (b'{"decision": "deny", "decision": "approve"}', "duplicate_key"),
        ('{"decision": "approve"}'.encode("utf-16"), "not_utf8"),
    ],
    ids=["nan", "a-key-two-times", "utf-16"],
)
def test_a_hook_answer_that_is_not_strict_is_no_answer(
    opener: _Opener, recorded: Notices, answer: bytes, rule: str
) -> None:
    opener.answer = answer

    assert phone._post("https://hook.invalid", "bearer", {"kind": "approval-poll"}) is None
    assert recorded.calls == [("hook.answer", rule, None, True)]


def test_a_hook_body_with_no_strict_text_is_not_sent(opener: _Opener) -> None:
    body: dict[str, object] = {"kind": "approval-poll", "at": math.nan}

    assert phone._post("https://hook.invalid", "bearer", body) is None
    assert opener.sent == []


def test_a_push_of_the_intake_with_no_strict_text_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = _Opener(b"")
    monkeypatch.setattr(notify.urllib.request, "build_opener", lambda *_handlers: made)

    assert notify._post("https://hook.invalid", "bearer", {"kind": "secret-gap"}) is True
    assert made.sent == [json.dumps({"kind": "secret-gap"}).encode("utf-8")]

    assert notify._post("https://hook.invalid", "bearer", {"summary": "\ud800"}) is False
    assert len(made.sent) == 1


# -- the code host (code_host.answer) ------------------------------------------


class _Connection:
    """`http.client.HTTPSConnection`, with one answer and no network."""

    answer = b""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def request(self, *_args: object, **_kwargs: object) -> None:
        return None

    def getresponse(self) -> _Reply:
        return _Reply(self.answer)

    def close(self) -> None:
        return None


def _tag_commit(monkeypatch: pytest.MonkeyPatch, answer: bytes) -> str:
    monkeypatch.setattr(provenance.http.client, "HTTPSConnection", _Connection)
    monkeypatch.setattr(_Connection, "answer", answer)
    api = provenance.github_api("token")

    return provenance.tag_commit(api, "chaperone", "agent-control", "chaperone-v2.1.0")


SHA = "a" * 40
TAG_REF = {"ref": "refs/tags/chaperone-v2.1.0", "object": {"type": "commit", "sha": SHA}}


def test_a_strict_answer_of_the_code_host_reads(
    monkeypatch: pytest.MonkeyPatch, recorded: Notices
) -> None:
    assert _tag_commit(monkeypatch, _strict(TAG_REF)) == SHA
    assert recorded.calls == []


def test_an_answer_of_the_code_host_that_is_not_strict_stops_the_step(
    monkeypatch: pytest.MonkeyPatch, recorded: Notices
) -> None:
    with pytest.raises(Refusal) as not_json:
        _tag_commit(monkeypatch, b"{not json")

    twice = _strict(TAG_REF).replace(b'"ref"', b'"ref": "x", "ref"')
    with pytest.raises(Refusal) as caught:
        _tag_commit(monkeypatch, twice)

    assert caught.value.code is not_json.value.code is RefusalCode.P2
    assert caught.value.detail.startswith("api.github.com unreachable: ")
    assert recorded.calls == [
        ("code_host.answer", "syntax", None, True),
        ("code_host.answer", "duplicate_key", None, True),
    ]


# -- a note of the spool and a record of the ledger (release.spool_entry) ----


def _switch_note(spool_root: Path, text: str) -> list[spool.Unfinished]:
    path = spool_root / RUNNING_DIR / f"{REQUEST_ID}-chaperone{SWITCH_SUFFIX}"
    path.write_text(text, "utf-8")
    opened = Spool(str(spool_root), this_uid())
    try:
        return opened.unfinished()
    finally:
        opened.close()


NOTE = {"component": "chaperone", "to": "/opt/x", "prev": "/opt/x.prev", "unit": None}


def test_a_strict_note_is_a_note(tmp_path: Path, recorded: Notices) -> None:
    (found,) = _switch_note(make_spool_dirs(tmp_path), json.dumps(NOTE))

    assert found.note.component == "chaperone"
    assert recorded.calls == []


def test_a_note_that_is_not_strict_is_no_note(tmp_path: Path, recorded: Notices) -> None:
    text = json.dumps(NOTE).replace('"to"', '"to": "/opt/y", "to"')

    assert _switch_note(make_spool_dirs(tmp_path), text) == []
    assert recorded.calls == [("release.spool_entry", "duplicate_key", None, True)]


def test_a_record_with_no_strict_text_is_no_record(tmp_path: Path) -> None:
    """The spool writes neither the log nor the entry, and says so in the
    way of each other write that fails."""
    spool_root = make_spool_dirs(tmp_path)
    opened = Spool(str(spool_root), this_uid())
    try:
        with pytest.raises(SpoolError):
            opened.finish(REQUEST_ID, {"id": REQUEST_ID, "approved_at": math.nan}, ["a line"])
    finally:
        opened.close()

    assert list((spool_root / "done").iterdir()) == []


# -- the session answer of the quiet window (session.answer) ------------------


def test_a_strict_session_answer_reads(recorded: Notices) -> None:
    assert quiet._has_rows(b'{"sessions": [{"id": "s"}]}') is True
    assert quiet._has_rows(b'{"sessions": []}') is False
    assert recorded.calls == []


@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        (b'{"sessions": [], "sessions": [{"id": "s"}]}', "duplicate_key"),
        ('{"sessions": []}'.encode("utf-16"), "not_utf8"),
    ],
    ids=["a-key-two-times", "utf-16"],
)
def test_a_session_answer_that_is_not_strict_is_never_quiet(
    recorded: Notices, answer: bytes, rule: str
) -> None:
    assert quiet._has_rows(answer) is None
    assert recorded.calls == [("session.answer", rule, None, True)]


# -- the turn records of step 7 (release.turn_record) -------------------------


def _turn(tmp_path: Path, text: str) -> Path:
    turns = tmp_path / "sessions" / "family" / "session" / "turns"
    turns.mkdir(parents=True)
    (turns / "turn.json").write_text(text, "utf-8")

    return tmp_path / "sessions"


NOW = 1_760_000_000.0
STARTED = "2025-10-09T08:53:10Z"


def test_a_strict_turn_record_in_flight_is_mid_start(tmp_path: Path, recorded: Notices) -> None:
    text = json.dumps({"state": "running", "started_at": STARTED})

    assert starting(_turn(tmp_path, text), NOW) == ("family/session/turn",)
    assert recorded.calls == []


def test_a_turn_record_that_is_not_strict_informs_nothing(
    tmp_path: Path, recorded: Notices
) -> None:
    """Root proves no writer of a turn record, so the notice says so."""
    text = '{"state": "settled", "state": "running", "started_at": "' + STARTED + '"}'

    assert starting(_turn(tmp_path, text), NOW) == ()
    assert recorded.calls == [("release.turn_record", "duplicate_key", None, False)]


# -- the record of a staged venv (release.direct_url) -------------------------


def _venv(tmp_path: Path, direct_url: str) -> Path:
    tree = tmp_path / "tree"
    info = tree / "lib" / "python3.12" / "site-packages" / "example-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "direct_url.json").write_text(direct_url, "utf-8")

    return tree


def test_a_strict_record_of_an_editable_install_is_a_fault(
    tmp_path: Path, recorded: Notices
) -> None:
    text = '{"url": "file:///src", "dir_info": {"editable": true}}'

    (fault,) = escapes(_venv(tmp_path, text))

    assert fault.endswith("is installed editable")
    assert recorded.calls == []


def test_a_record_that_is_not_strict_is_a_record_root_cannot_read(
    tmp_path: Path, recorded: Notices
) -> None:
    text = '{"url": "file:///src", "dir_info": {"editable": true}, "dir_info": {}}'

    (fault,) = escapes(_venv(tmp_path, text))

    assert fault.startswith(UNREADABLE_DIRECT_URL)
    assert recorded.calls == [("release.direct_url", "duplicate_key", None, True)]


# -- the resolved manifest -----------------------------------------------------

EMPTY = Resolution(components=(), order=(), contracts=(), deploying=frozenset())


@pytest.mark.parametrize("resolved_at", [math.nan, math.inf, -math.inf], ids=str)
def test_a_resolved_manifest_with_a_time_that_is_not_finite_is_refused(
    resolved_at: float,
) -> None:
    with pytest.raises(Refusal) as caught:
        build_document(EMPTY, ReleaseState(), REQUEST_ID, "human", resolved_at)

    assert caught.value.code is RefusalCode.REQUEST
    assert caught.value.detail == "resolved_at must be a finite number"


# -- the requester ------------------------------------------------------------


def _plan(**fields: Any) -> Refusal:
    arguments: dict[str, Any] = {"requested_by": "human", "now": 1758153590.0} | fields
    with pytest.raises(Refusal) as caught:
        plan_request({"chaperone": "2.1.0"}, request_id=REQUEST_ID, **arguments)

    return caught.value


@pytest.mark.parametrize("now", [math.nan, math.inf])
def test_a_request_with_a_time_that_is_not_finite_is_refused_at_its_field(now: float) -> None:
    assert _plan(now=now).detail == "field 'ts' is malformed"


def test_a_request_with_no_strict_text_gets_the_refusal_of_root(recorded: Notices) -> None:
    """Root gives that refusal to a text that is not strict JSON. The
    requester writes no such text, so no notice follows."""
    refusal = _plan(requested_by="\ud800")

    assert refusal.detail == NOT_AN_OBJECT
    assert recorded.calls == []


# -- one rule for each writer -------------------------------------------------


def _writes() -> Iterator[str]:
    """Each module of the package that calls a writer of the `json` module."""
    package = Path(notices.__file__).parent
    for module in sorted(package.rglob("*.py")):
        if module.name == "strictjson.py":
            continue

        text = module.read_text(encoding="utf-8")
        for word in ("json.dumps(", "json.dump(", "json.loads(", "json.load("):
            for line in text.splitlines():
                if word in line and "strictjson." not in line:
                    yield f"{module.relative_to(package)}: {line.strip()}"


def test_no_module_reads_or_writes_a_json_text_past_the_strict_module() -> None:
    """The command line prints its own reports with `json.dumps`, and no
    reader reads them. Each other call goes through `strictjson`."""
    found = [line for line in _writes() if not line.startswith("cli.py: print(json.dumps(")]

    assert found == []


# -- the notice file of each program of root ---------------------------------


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path | None]]:
    calls: list[tuple[str, Path | None]] = []
    monkeypatch.setattr(notices, "configure", lambda *given: calls.append(given))

    return calls


@pytest.mark.usefixtures("operator_is_an_account")
def test_the_executor_has_no_notice_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: list[tuple[str, Path | None]],
) -> None:
    """Root creates no file in a directory that the operator account can
    write, so the notice of the executor is its log line."""
    monkeypatch.setattr(drain.os, "geteuid", lambda: 0)
    monkeypatch.setattr(drain, "keep_only_the_secrets", lambda _env: drain.Secrets("", "", ""))
    monkeypatch.setattr(drain, "SPOOL_ROOT", str(tmp_path / "absent"))

    assert drain.main() == 1

    assert configured == [("handover-exec", None)]
    assert list(tmp_path.iterdir()) == []


def test_the_intake_has_no_notice_file(
    monkeypatch: pytest.MonkeyPatch, configured: list[tuple[str, Path | None]]
) -> None:
    monkeypatch.setattr(intake_run.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        intake_run, "keep_only_the_push_secrets", lambda _env: intake_run.Secrets("", "")
    )
    monkeypatch.setattr(intake_run, "_certificate_is_roots", lambda: True)
    monkeypatch.setattr(
        intake_run.pwd, "getpwnam", lambda _name: intake_run.pwd.getpwuid(os.getuid())
    )
    monkeypatch.setattr(intake_run, "build_wiring", lambda *_args: None)
    monkeypatch.setattr(intake_run, "_loop", lambda *_args: None)

    assert intake_run.main() == 0

    assert configured == [("handover-intake", None)]

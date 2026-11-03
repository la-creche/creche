"""`creche_wire.read` is strict (decision CH9-1).

`SessionId`, the one wire type today, is a string, and a lax string refuses
a number too. So these tests read scratch models that are no wire type. They
show what each future wire type gets from the read.
"""

from datetime import datetime
from enum import Enum

import pytest
from creche_wire import NotStrictJson, read, strictjson
from pydantic import BaseModel, RootModel, ValidationError

CAP = 1 << 10


class _Kind(Enum):
    one = "one"


class _Scratch(BaseModel):
    count: int
    flag: bool
    at: datetime
    kind: _Kind


class _Count(RootModel[int]):
    root: int


GOOD = '{"count": 1, "flag": true, "at": "2026-11-02T00:00:00Z", "kind": "one"}'


def test_a_strict_text_of_the_model_reads() -> None:
    value = read(_Scratch, GOOD.encode(), cap=CAP)

    assert (value.count, value.flag, value.kind) == (1, True, _Kind.one)
    assert read(_Count, b"7", cap=CAP).root == 7


@pytest.mark.parametrize(
    "text",
    [
        GOOD.replace('"count": 1', '"count": "1"'),
        GOOD.replace('"count": 1', '"count": 1.0'),
        GOOD.replace('"count": 1', '"count": true'),
        GOOD.replace('"flag": true', '"flag": 1'),
        GOOD.replace('"flag": true', '"flag": "true"'),
        GOOD.replace('"2026-11-02T00:00:00Z"', "1793577600"),
    ],
)
def test_a_lax_value_is_refused(text: str) -> None:
    with pytest.raises(ValidationError):
        read(_Scratch, text.encode(), cap=CAP)


@pytest.mark.parametrize("text", ['"1"', "1.0", "true"])
def test_a_root_model_is_strict_too(text: str) -> None:
    with pytest.raises(ValidationError):
        read(_Count, text.encode(), cap=CAP)


NOT_STRICT = [
    (b'{"count": 1, "count": 2}', strictjson.Rule.DUPLICATE_KEY),
    (b'"\\ud800"', strictjson.Rule.LONE_SURROGATE),
    (b"NaN", strictjson.Rule.CONSTANT),
    (b"18446744073709551616", strictjson.Rule.INTEGER_RANGE),
    (b"7 7", strictjson.Rule.TRAILING_DATA),
    (b"7" * (CAP + 1), strictjson.Rule.TOO_LARGE),
]


@pytest.mark.parametrize(("data", "rule"), NOT_STRICT, ids=[rule.value for _, rule in NOT_STRICT])
def test_a_text_that_is_not_strict_json_is_refused(data: bytes, rule: strictjson.Rule) -> None:
    with pytest.raises(NotStrictJson) as refused:
        read(_Count, data, cap=CAP)

    assert refused.value.rule == rule

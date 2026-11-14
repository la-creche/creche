"""spec §9's cap against the catalog: one request may name every releasing row.

The cap is a constant (`catalog.MAX_REQUEST_COMPONENTS`) and the catalog is
a list, so a new row can pass the cap without a test that reads both. Ten
rows release since `playpen` left, one under the cap of eleven (CH35 (1)).
"""

from __future__ import annotations

import json

from handover.catalog import MAX_REQUEST_COMPONENTS, releasable_names
from handover.executor.request import parse_request as parse_request_file
from handover.resolve import parse_request
from handover_executor_fixtures import REQUEST_ID, request_body


def test_the_cap_holds_every_releasable_row() -> None:
    """A row past the cap fails here, in the PR that adds it."""
    assert len(releasable_names()) <= MAX_REQUEST_COMPONENTS


def test_the_cap_is_eleven_and_ten_rows_release() -> None:
    """spec §9 and CH35 (1): eleven, and ten releasing rows under it."""
    assert MAX_REQUEST_COMPONENTS == 11
    assert len(releasable_names()) == 10


def test_a_request_of_every_row_parses() -> None:
    """The CLI's request of every releasable row."""
    names = releasable_names()

    wanted = parse_request([f"{name}=latest" for name in names])

    assert {"vmm", "disks"} <= set(wanted)
    assert set(wanted) == set(names)


def test_a_request_file_of_every_row_parses() -> None:
    """Root's reader of the spool takes the same request (§2.3)."""
    body = request_body({name: "latest" for name in releasable_names()})

    request = parse_request_file(json.dumps(body).encode("utf-8"), REQUEST_ID)

    assert {name for name, _ in request.components} == set(releasable_names())

"""Shared test helpers.

There is deliberately no `conftest.py`: every test directory is prepended
to `sys.path`, so a second module named `conftest` shadows another
package's (`family/tests/family_helpers.py`'s own docstring). Helpers live
in a module with a unique basename and are imported by name."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from agent_family import FamilyFile, parse_family
from caregiver.credentials import read_creds, token_sha256, write_creds
from caregiver.grants import UNCOMPARED

from caregiver import paths

FAMILY_YAML: dict[str, Any] = {
    "name": "chat",
    "kind": "attended",
    "description": "Test family.",
    "model": {"router": "agent-router", "budget_usd_per_day": 15},
}

#: A grant the validator refuses, in a file that still parses: no
#: `mcp/ghost/server.yaml` declares the server. `registry.families` holds
#: the file all the same, beside a report that is not ok.
REFUSED_TOOLS: dict[str, list[str]] = {"ghost": ["merge_pull_request"]}

#: An edit the validator passes and the reconciler refuses: `kind` cannot
#: move (contract 01 §3.1), and only the applied snapshot proves it did.
#: The limit moves with it, so the edit shows in the grant file.
KIND_MOVED: dict[str, object] = {"kind": "thin", "max_inflight_delegations": 7}

#: Before any clock this suite runs under.
LONG_AGO: str = "2020-01-01T00:00:00Z"

#: The token of the epoch before a rotation, as `expire_overlap` leaves it.
OLD_TOKEN: str = "OLDTOKEN"

#: More levels than the JSON parser of each supported Python version reads.
VERY_DEEP: int = 400_000

#: More digits than the interpreter converts to an integer.
HUGE_DIGITS: int = 5_000

#: A value of this many levels reads under Python 3.14 on a 16 MiB stack,
#: and `str` of it raises RecursionError there. The parser of Python 3.12
#: and of Python 3.13 refuses it.
DEEPER_THAN_STR: int = 90_000

#: Content that no JSON reader of this package can use. Each one made a
#: reader raise before `atomic.read_json` was the one reader.
UNREADABLE_JSON: dict[str, bytes] = {
    "not-utf8": b'{"family": "\xff"}',
    "utf16": '{"family": "chat"}'.encode("utf-16"),
    "huge-integer": b'{"n": ' + b"9" * HUGE_DIGITS + b"}",
    "very-deep": b'{"x": ' + b"[" * VERY_DEEP + b"]" * VERY_DEEP + b"}",
}


class NoText:
    """A value that `str` cannot convert, under each Python version. A list
    of `DEEPER_THAN_STR` levels is such a value under Python 3.14 alone."""

    def __str__(self) -> str:
        raise RecursionError("maximum recursion depth exceeded")


def family_yaml(**overrides: object) -> str:
    """The default `chat` family as YAML text, with any field replaced."""
    body = dict(FAMILY_YAML)
    body.update(overrides)
    return yaml.safe_dump(body)


def chat_family(**overrides: object) -> FamilyFile:
    """The same file, parsed. Raises on a shape the parser refuses, so a
    test that mistypes an override fails at the fixture, not three
    assertions later."""
    family, issues = parse_family(family_yaml(**overrides))
    if family is None:
        raise AssertionError(f"the test's own family file does not parse: {issues}")

    return family


def write_registry(root: Path, **overrides: object) -> Path:
    """One family in a fresh (or already-populated) registry root. The
    default shape is a minimal, valid `chat` family; pass `name=...` for a
    second family, or any other field to make it invalid on purpose."""
    body = dict(FAMILY_YAML)
    body.update(overrides)
    name = str(body["name"])
    family_dir = root / "families" / name
    family_dir.mkdir(parents=True, exist_ok=True)
    (family_dir / "family.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (family_dir / "instructions.md").write_text("Be helpful.\n", encoding="utf-8")
    return root


def expire_overlap(state_root: Path, family: str = "chat") -> None:
    """`creds.json` and the grant file as a graceful rotation leaves them
    once the grace has run out: a previous token whose overlap is over, and
    a grant file that still accepts it. The next `settle` has a write to
    make."""
    path = paths.creds_path(state_root, family)
    creds = read_creds(path)
    if creds is None:
        raise AssertionError(f"{family} has no credentials to expire")

    write_creds(path, replace(creds, previous_pep_token=OLD_TOKEN, previous_expires_at=LONG_AGO))
    grant = paths.grant_path(state_root, family)
    body = json.loads(grant.read_text(encoding="utf-8"))
    body["token_sha256"] = [token_sha256(creds.pep_token), token_sha256(OLD_TOKEN)]
    grant.write_text(json.dumps(body), encoding="utf-8")


def accepted_digests(state_root: Path, family: str = "chat") -> list[str]:
    """Every token digest the chaperone accepts for this family."""
    body = json.loads(paths.grant_path(state_root, family).read_text(encoding="utf-8"))
    return list(body["token_sha256"])


def grants_alone(state_root: Path, family: str = "chat") -> dict[str, Any]:
    """The grant file with `rev` and the digests left out: what the family
    may do, and nothing about which token proves it."""
    body = json.loads(paths.grant_path(state_root, family).read_text(encoding="utf-8"))
    return {key: value for key, value in body.items() if key not in UNCOMPARED}


def published(state_root: Path, family: str = "chat") -> dict[str, Any]:
    """The status document, which is all `attendance` reads of a family."""
    return json.loads(paths.status_path(state_root, family).read_text(encoding="utf-8"))


def published_epoch(state_root: Path, family: str = "chat") -> int | None:
    """The epoch `attendance` puts on the channel. `None` when the document
    carries no credentials block, which `attendance` reads as epoch 1."""
    block = published(state_root, family)["credentials"]
    return block["epoch"] if block is not None else None


def current_digest(state_root: Path, family: str = "chat") -> str:
    creds = read_creds(paths.creds_path(state_root, family))
    if creds is None:
        raise AssertionError(f"{family} has no credentials")

    return token_sha256(creds.pep_token)

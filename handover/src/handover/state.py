"""The live-state document: what runs now, and the source facts for a tag.

The resolver is pure. It decides nothing by looking at a host, a git remote or
an OCI registry, because the pure half holds no root code (stage7-releases.md
§2.1: root is the only actor that touches the host). Everything the resolver
cannot compute arrives in this one document.

**Root BUILDS it** (`executor/live_state.py`), out of the install trees' own
stamps, the newest tag and the tag's commit. Nothing on the host writes it as
a file and nothing reads one: contract 06 §11 says "built by root at step 2
and consumed in the same step".

This module is the SHAPE and the parser, not the source. A parser is still
needed because `handover resolve --state <file>` hands the pure
resolver a document a test or a reader wrote, which is what "pure" buys: every
refusal in `resolve.py` is provable with a fixture and no host. A document
from that door is input, so it is parsed like one: a byte cap, a closed key
set, a pattern per scalar, and a refusal that names the field rather than its
value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, cast

from .catalog import CATALOG_BY_NAME, ContractId
from .errors import Refusal, RefusalCode, safe_token

#: Nine components, four maps. Sixty-four kibibytes is far above the largest
#: this can be and small enough that a refusal costs one read.
MAX_STATE_BYTES = 64 * 1024

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
CONTRACT_VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

STATE_FIELDS = frozenset({"live", "provided", "latest", "facts"})
FACT_FIELDS = frozenset({"sha", "input_digest", "artifact_digest"})


@dataclass(frozen=True)
class SourceFacts:
    """What a tag resolves to. Contract 06 §9's per-component source columns.

    All three are nullable, and the rule over them is one sentence: they are
    non-null exactly for the components root RESOLVED A TAG FOR in this
    release — the ones it deploys, plus the ones whose manifest it had to
    read at a SHA (contract 06 §10.2's fourth row). For every other
    component root fetched no source and ran no predicate, and a column it
    cannot vouch for is not a record.
    """

    sha: str | None
    input_digest: str | None
    artifact_digest: str | None


@dataclass(frozen=True)
class ReleaseState:
    """Everything the resolver needs and cannot compute for itself."""

    #: Component name to the version installed now. `None` is a first install.
    live: dict[str, str | None] = field(default_factory=dict[str, "str | None"])
    #: Contract id to the `major.minor` the live provider provides. C4 reads it.
    provided: dict[ContractId, tuple[int, int]] = field(
        default_factory=dict[ContractId, tuple[int, int]]
    )
    #: Component name to the newest released version, which `latest` means.
    latest: dict[str, str] = field(default_factory=dict[str, str])
    #: Component name to its source facts at the version it resolves to.
    facts: dict[str, SourceFacts] = field(default_factory=dict[str, SourceFacts])


def _refuse(subject: str, detail: str) -> Refusal:
    return Refusal(RefusalCode.STATE, subject, detail)


def _as_mapping(value: Any, subject: str, label: str) -> dict[str, Any]:
    """One JSON object with string keys. Anything else is refused."""
    if not isinstance(value, dict):
        raise _refuse(subject, f"'{label}' must be an object")

    body: dict[str, Any] = {}
    for key, item in cast(dict[Any, Any], value).items():
        if not isinstance(key, str):
            raise _refuse(subject, f"'{label}' has a non-string key")

        body[key] = item

    return body


def _known_name(name: str, subject: str, label: str) -> str:
    if name not in CATALOG_BY_NAME:
        raise _refuse(subject, f"'{label}' names {safe_token(name)}, which contract 06 §1 omits")

    return name


def _version(value: Any, subject: str, label: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise _refuse(subject, f"'{label}' must be MAJOR.MINOR.PATCH")

    return value


def _read_live(value: Any, subject: str) -> dict[str, str | None]:
    live: dict[str, str | None] = {}
    for name, item in _as_mapping(value, subject, "live").items():
        _known_name(name, subject, "live")
        live[name] = None if item is None else _version(item, subject, f"live.{name}")

    return live


def _read_latest(value: Any, subject: str) -> dict[str, str]:
    latest: dict[str, str] = {}
    for name, item in _as_mapping(value, subject, "latest").items():
        _known_name(name, subject, "latest")
        latest[name] = _version(item, subject, f"latest.{name}")

    return latest


def _read_provided(value: Any, subject: str) -> dict[ContractId, tuple[int, int]]:
    provided: dict[ContractId, tuple[int, int]] = {}
    for name, item in _as_mapping(value, subject, "provided").items():
        try:
            contract = ContractId(name)
        except ValueError:
            detail = f"'provided' names {safe_token(name)}, which is no contract id"
            raise _refuse(subject, detail) from None

        matched = CONTRACT_VERSION_RE.fullmatch(item) if isinstance(item, str) else None
        if matched is None:
            raise _refuse(subject, f"'provided.{contract}' must be MAJOR.MINOR")

        # Python reads no text of more than 4300 digits as an integer.
        try:
            provided[contract] = (int(matched.group(1)), int(matched.group(2)))
        except ValueError:
            raise _refuse(subject, f"'provided.{contract}' must be MAJOR.MINOR") from None

    return provided


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None


def _read_one_fact(value: Any, subject: str, label: str) -> SourceFacts:
    body = _as_mapping(value, subject, label)
    unknown = sorted(set(body) - FACT_FIELDS)
    if unknown:
        raise _refuse(subject, f"'{label}' has an unknown field: {safe_token(unknown[0])}")

    sha = body.get("sha")
    if sha is not None and (not isinstance(sha, str) or not SHA_RE.fullmatch(sha)):
        raise _refuse(subject, f"'{label}.sha' must be 40 lower-case hex or null")

    digest = body.get("input_digest")
    if digest is not None and not _is_digest(digest):
        raise _refuse(subject, f"'{label}.input_digest' must be sha256:<64 hex> or null")

    artifact = body.get("artifact_digest")
    if artifact is not None and not _is_digest(artifact):
        raise _refuse(subject, f"'{label}.artifact_digest' must be sha256:<64 hex> or null")

    return SourceFacts(sha=sha, input_digest=digest, artifact_digest=artifact)


def _read_facts(value: Any, subject: str) -> dict[str, SourceFacts]:
    facts: dict[str, SourceFacts] = {}
    for name, item in _as_mapping(value, subject, "facts").items():
        _known_name(name, subject, "facts")
        facts[name] = _read_one_fact(item, subject, f"facts.{name}")

    return facts


def parse_state(text: str, subject: str) -> ReleaseState:
    """Parse one live-state document. Every key is optional and defaults empty."""
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise _refuse(subject, "is not UTF-8") from None

    if size > MAX_STATE_BYTES:
        raise _refuse(subject, f"larger than {MAX_STATE_BYTES} bytes")

    try:
        loaded: Any = json.loads(text)
    except (ValueError, RecursionError):
        raise _refuse(subject, "does not parse as JSON") from None

    body = _as_mapping(loaded, subject, "<top level>")
    unknown = sorted(set(body) - STATE_FIELDS)
    if unknown:
        raise _refuse(subject, f"unknown field: {safe_token(unknown[0])}")

    return ReleaseState(
        live=_read_live(body.get("live", {}), subject),
        provided=_read_provided(body.get("provided", {}), subject),
        latest=_read_latest(body.get("latest", {}), subject),
        facts=_read_facts(body.get("facts", {}), subject),
    )

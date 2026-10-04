"""Id grammars: every copy of every identifier pattern, on the same inputs.

One grammar has many copies in this repository. A copy is one entry point
of one package. Every copy of a grammar reads the same inputs, so a
difference between two copies is a row of `ids/disagreements.json` and not
a thing somebody has to notice.

An entry point is a public function where the package has one. Where the
package holds only a public pattern, the entry point is that pattern with
the method the package's own code calls on it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from re import Pattern
from typing import Final

from agent_door_owui import headers as owui_headers
from agent_door_owui import openai_api as owui_api
from agent_door_owui.errors import DoorError
from agent_door_trigger import ulid as trigger_ulid
from agent_door_tui import ids as tui_ids
from agent_family import grammar as family_grammar
from chaperone.family_grants import parse_grants
from handover.executor import host as executor_host
from handover.executor import provenance, source
from handover.executor import request as executor_request
from handover.intake import store as intake_store
from handover.intake import token as intake_token

from attendance import ids as attendance_ids
from caregiver import mcp_release
from chaperone import family_ids as chaperone_ids
from chaperone import gates as chaperone_gates
from chaperone import headers as chaperone_headers
from chaperone import run_as
from chaperone import secrets as chaperone_secrets
from handover import allocate, manifest, mcpserver, resolve, state
from noticeboard import registrywrite
from vectors.core import Json, Surface, Vector, accepted, compact, refused, run, text_input

#: An outcome of one copy on one input: whether it took the input, and what
#: it made of it (a normalized value, or a refusal code), when it says.
type Outcome = tuple[bool, object]
type Check = Callable[[str], Outcome]

#: Non-ASCII digits that every supported Python calls a decimal digit.
ARABIC_ONE: Final = "\u0661"
FULLWIDTH_ONE: Final = "\uff11"

#: Longer than every length cap of every grammar here.
OVERLONG_CHARS: Final = 300

NOTE_PATTERN: Final = (
    "The entry point is a pattern. The package's own code calls this method on it. "
    "A caller can add a length cap of its own."
)


@dataclass(frozen=True)
class Copy:
    """One entry point of one package for one grammar."""

    #: The last part of the surface name, e.g. `attendance`.
    owner: str
    #: The surface's `entry`, e.g. `attendance.ids.is_family`.
    entry: str
    check: Check
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Concept:
    """One identifier, e.g. the family name, and every copy of it."""

    name: str
    contract: str
    copies: tuple[Copy, ...]


@dataclass(frozen=True)
class Grammar:
    """One grammar: its inputs, and every identifier that is meant to follow it."""

    name: str
    #: A value every copy accepts. The fixed probes are built from it.
    sample: str
    #: One character every copy accepts at any place but the first.
    filler: str
    cases: tuple[tuple[str, str], ...]
    concepts: tuple[Concept, ...]


@contextmanager
def _quiet_logs() -> Generator[None]:
    """No log line from a copy that logs a refusal, and the level put back."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _predicate(function: Callable[[str], bool]) -> Check:
    return lambda text: (bool(function(text)), None)


def _fullmatch(pattern: Pattern[str]) -> Check:
    return lambda text: (pattern.fullmatch(text) is not None, None)


def _match(pattern: Pattern[str]) -> Check:
    return lambda text: (pattern.match(text) is not None, None)


def _pattern_copy(owner: str, entry: str, pattern: Pattern[str]) -> Copy:
    """A public pattern the package calls `fullmatch` on."""
    return Copy(owner, f"{entry}.fullmatch", _fullmatch(pattern), (NOTE_PATTERN,))


# --- chaperone: the grant file fields, through the public parser ---

_GRANT_FAMILY: Final = "chat"
_GRANT_DIGEST: Final = "0" * 64
NOTE_GRANT: Final = (
    "The entry point is the grant file parser. The input is one field of a grant file "
    "that is valid in every other field."
)


def _grant_accepts(family: str, **fields: object) -> Outcome:
    document: dict[str, object] = {
        "version": 2,
        "family": family,
        "rev": "reg-000000",
        "token_sha256": [_GRANT_DIGEST],
        "model_alias": "fast",
        **fields,
    }
    grants, _ = parse_grants(json.dumps(document).encode("utf-8"), family)

    return grants is not None, None


def _grant_copy(field: str, check: Check) -> Copy:
    return Copy(
        "chaperone_grants",
        f"chaperone.family_grants.parse_grants, field {field}",
        check,
        (NOTE_GRANT,),
    )


# --- chaperone: the advisory headers ---


def _claimed_session(text: str) -> Outcome:
    with _quiet_logs():
        claimed = chaperone_headers.read_claimed({chaperone_headers.SESSION_ID_HEADER: text})

    return claimed.session_id is not None, None


def _claimed_turn(text: str) -> Outcome:
    with _quiet_logs():
        claimed = chaperone_headers.read_claimed({chaperone_headers.TURN_ID_HEADER: text})

    return claimed.turn_id is not None, None


NOTE_CLAIMED: Final = (
    "The entry point reads one advisory header. A refused value is dropped and reads as "
    "absent. It denies no call."
)

# --- door-owui: an id from a request header, and a family from a model name ---

_OWUI_MESSAGE_ID: Final = "m1"


def _owui_chat_id(text: str) -> Outcome:
    given = {owui_headers.CHAT_ID_HEADER: text, owui_headers.MESSAGE_ID_HEADER: _OWUI_MESSAGE_ID}
    try:
        ids = owui_headers.read_ids(given)
    except DoorError as exc:
        return False, {"code": exc.code}

    return True, {"chat_id": ids.chat_id, "session": ids.session}


def _owui_family_of(text: str) -> Outcome:
    try:
        return True, owui_api.family_of(text)
    except DoorError as exc:
        return False, {"code": exc.code}


# --- handover: a tag, through the function that reads the newest version ---


def _newest_tagged(text: str) -> Outcome:
    component, _, _ = text.rpartition("-v")
    version = source.newest_tagged_version([text], component)

    return version is not None, version


# --- the grammars ---

_A31: Final = "a" * 31
_A32: Final = "a" * 32

NAME_CASES: Final = (
    ("two-chars", "ab"),
    ("one-char", "a"),
    ("word", "chat"),
    ("hyphen-inside", "vault-oracle"),
    ("letter-digit", "a1"),
    ("trailing-hyphen", "a-"),
    ("double-hyphen", "a--b"),
    ("probe-family", "gate-probe"),
    ("digits-and-hyphen", "x9-9"),
    ("max-31-chars", _A31),
    ("over-32-chars", _A32),
    ("leading-digit", "1chat"),
    ("leading-hyphen", "-chat"),
    ("upper-first", "Chat"),
    ("upper-last", "chaT"),
    ("underscore", "agent_control"),
    ("double-underscore", "a__b"),
    ("dot", "a.b"),
    ("slash", "a/b"),
    ("dot-dot", ".."),
    ("parent-path", "../etc"),
    ("space-inside", "a b"),
    ("tab-inside", "a\tb"),
    ("latin-letter", "caf\u00e9"),
    ("fullwidth-letter", "\uff41b"),
    ("leading-newline", "\nchat"),
    ("two-newlines", "chat\n\n"),
    ("crlf", "chat\r\n"),
    ("percent", "a%00"),
)

SESSION_CASES: Final = (
    ("one-letter", "a"),
    ("one-digit", "0"),
    ("every-class", "A.b_c-d"),
    ("owui-uuid", "owui-3f2b1c9e-8a55-4c1e-9f0a-2b6d7e8f9a10"),
    ("job-ulid", "job-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"),
    ("auto-ulid", "auto-01J9ZQ5V7Y8X4W3T2S1R0QPNMK"),
    ("no-prefix", "01J9ZQ5V7Y8X4W3T2S1R0QPNMK"),
    ("dot", "."),
    ("dot-dot", ".."),
    ("leading-dot", ".hidden"),
    ("leading-hyphen", "-x"),
    ("leading-underscore", "_x"),
    ("dot-dot-inside", "a..b"),
    ("trailing-dot", "a."),
    ("slash", "a/b"),
    ("backslash", "a\\b"),
    ("space-inside", "a b"),
    ("colon", "a:b"),
    ("percent", "a%2Fb"),
    ("tab-inside", "a\tb"),
    ("latin-letter", "\u00e4"),
    ("line-separator", "a\u2028b"),
    ("max-128-chars", "a" * 128),
    ("over-129-chars", "a" * 129),
    ("owui-prefix-133-chars", "owui-" + "a" * 128),
    ("leading-newline", "\na"),
    ("crlf", "a\r\n"),
)

_ULID: Final = "01J9ZQ5V7Y8X4W3T2S1R0QPNMK"

ULID_CASES: Final = (
    ("all-zero", "0" * 26),
    ("largest-in-time", "7" + "Z" * 25),
    ("past-48-bits", "Z" * 26),
    ("lower-case", _ULID.lower()),
    ("one-lower-case", _ULID[:-1] + "k"),
    ("short-25-chars", _ULID[:-1]),
    ("long-27-chars", _ULID + "0"),
    ("letter-i", "I" + _ULID[1:]),
    ("letter-l", "L" + _ULID[1:]),
    ("letter-o", "O" + _ULID[1:]),
    ("letter-u", "U" + _ULID[1:]),
    ("hyphen", _ULID[:-1] + "-"),
    ("space-inside", _ULID[:12] + " " + _ULID[13:]),
    ("two-newlines", _ULID + "\n\n"),
    ("crlf", _ULID + "\r\n"),
    ("leading-newline", "\n" + _ULID),
)

SANDBOX_CASES: Final = (
    ("zero", "chat-s0"),
    ("nine-digits", "chat-s123456789"),
    ("ten-digits", "chat-s1234567890"),
    ("leading-zero", "chat-s01"),
    ("no-number", "chat-s"),
    ("no-s", "chat-1"),
    ("family-only", "chat"),
    ("short-family", "ab-s1"),
    ("one-char-family", "a-s1"),
    ("upper-s", "chat-S1"),
    ("family-ends-like-a-sandbox", "chat-s1-s2"),
    ("max-family", _A31 + "-s1"),
    ("over-family", _A32 + "-s1"),
    ("negative", "chat-s-1"),
    ("upper-family", "Chat-s1"),
    ("underscore", "chat_s1"),
    ("plus", "chat-s+1"),
    ("crlf", "chat-s1\r\n"),
)

TOOL_CASES: Final = (
    ("one-lower", "a"),
    ("one-upper", "A"),
    ("underscore", "get_weather"),
    ("hyphen", "list-items"),
    ("upper-first", "Search"),
    ("double-underscore", "a__b"),
    ("leading-digit", "1tool"),
    ("leading-underscore", "_tool"),
    ("leading-hyphen", "-tool"),
    ("dot", "tool.name"),
    ("space-inside", "tool name"),
    ("slash", "a/b"),
    ("star", "*"),
    ("all", "all"),
    ("invoke-agent", "invoke_agent"),
    ("latin-letter", "t\u00f6\u00f6l"),
    ("len-64", "a" * 64),
    ("len-65", "a" * 65),
    ("len-128", "a" * 128),
    ("len-129", "a" * 129),
    ("crlf", "a\r\n"),
)

ENV_CASES: Final = (
    ("one-letter", "A"),
    ("word", "HOME"),
    ("trailing-underscore", "A_"),
    ("letter-digit", "A1"),
    ("lower", "a"),
    ("mixed", "Api"),
    ("leading-digit", "1A"),
    ("leading-underscore", "_A"),
    ("hyphen", "A-B"),
    ("space-inside", "A B"),
    ("equals", "A=B"),
    ("latin-letter", "\u00c0B"),
    ("len-64", "A" * 64),
    ("len-65", "A" * 65),
    ("crlf", "A\r\n"),
)

SECRET_CASES: Final = (
    ("two-chars", "ab"),
    ("one-char", "a"),
    ("trailing-underscore", "a_"),
    ("letter-digit", "a1"),
    ("upper", "A_b"),
    ("leading-digit", "1ab"),
    ("leading-underscore", "_ab"),
    ("hyphen", "a-b"),
    ("dot", "a.b"),
    ("slash", "a/b"),
    ("space-inside", "a b"),
    ("max-63-chars", "a" * 63),
    ("over-64-chars", "a" * 64),
    ("dot-dot", ".."),
    ("crlf", "ab\r\n"),
)

VERSION_CASES: Final = (
    ("zero", "0.0.0"),
    ("two-digits", "10.20.30"),
    ("leading-zero", "01.2.3"),
    ("two-parts", "1.2"),
    ("four-parts", "1.2.3.4"),
    ("v-prefix", "v1.2.3"),
    ("pre-release", "1.2.3-rc1"),
    ("build", "1.2.3+build"),
    ("empty-part", "1..3"),
    ("leading-dot", ".1.2"),
    ("trailing-dot", "1.2."),
    ("letter-part", "1.2.x"),
    ("latest", "latest"),
    ("minus", "-1.2.3"),
    ("plus", "+1.2.3"),
    ("commas", "1,2,3"),
    ("past-32-bits", "4294967296.0.0"),
    ("past-64-bits", "99999999999999999999.0.0"),
    ("all-arabic-digits", "\u0661.\u0662.\u0663"),
    ("crlf", "1.2.3\r\n"),
)

CONTRACT_VERSION_CASES: Final = (
    ("one-zero", "1.0"),
    ("leading-zero", "0.06"),
    ("one-part", "1"),
    ("three-parts", "1.0.0"),
    ("letters", "a.b"),
    ("leading-dot", ".1"),
    ("trailing-dot", "1."),
    ("space-inside", "1 .0"),
    ("past-64-bits", "99999999999999999999.0"),
    ("all-arabic-digits", "\u0661.\u0662"),
)

TAG_CASES: Final = (
    ("hyphen-name", "mcp-servers-v0.0.1"),
    ("short-name", "ab-v0.0.0"),
    ("one-char-name", "a-v1.0.0"),
    ("two-parts", "chaperone-v1.2"),
    ("no-v", "chaperone-1.2.3"),
    ("upper-v", "chaperone-V1.2.3"),
    ("no-name", "v1.2.3"),
    ("old-schema-tag", "schema-v3"),
    ("pre-release", "chaperone-v1.2.3-rc1"),
    ("leading-zeros", "chaperone-v01.02.03"),
    ("upper-name", "Chaperone-v1.2.3"),
    ("underscore-name", "chaperone_x-v1.2.3"),
    ("name-ends-in-v", "chaperone-v-v1.2.3"),
    ("max-name", _A31 + "-v1.0.0"),
    ("over-name", _A32 + "-v1.0.0"),
    ("past-64-bits", "chaperone-v99999999999999999999.0.0"),
    ("all-arabic-digits", "chaperone-v\u0661.\u0662.\u0663"),
    ("crlf", "chaperone-v1.2.3\r\n"),
)

PACKAGE_VERSION_CASES: Final = (
    ("one-digit", "1"),
    ("date", "2024.1.1"),
    ("pre-release", "1.0.0rc1"),
    ("hyphen", "1.0.0-rc1"),
    ("plus", "1.0.0+local"),
    ("v-prefix", "v1.0"),
    ("caret", "^1.0"),
    ("tilde", "~1.0"),
    ("at-least", ">=1.0"),
    ("star", "1.*"),
    ("two-versions", "1.0, 2.0"),
    ("space-inside", "1 .0"),
    ("latest", "latest"),
    ("len-64", "1" + "0" * 63),
    ("len-65", "1" + "0" * 64),
)

_HEX64: Final = "0123456789abcdef" * 4

SHA256_CASES: Final = (
    ("all-zero", "0" * 64),
    ("upper", _HEX64.upper()),
    ("short-63-chars", _HEX64[:-1]),
    ("long-65-chars", _HEX64 + "0"),
    ("letter-g", "g" + _HEX64[1:]),
    ("prefix", "sha256:" + _HEX64),
    ("crlf", _HEX64 + "\r\n"),
)

_HEX40: Final = _HEX64[:40]

OBJECT_ID_CASES: Final = (
    ("all-zero", "0" * 40),
    ("upper", _HEX40.upper()),
    ("short-39-chars", _HEX40[:-1]),
    ("long-41-chars", _HEX40 + "0"),
    ("sha256-length", _HEX64),
    ("short-sha", _HEX40[:7]),
    ("letter-g", "g" + _HEX40[1:]),
)

MCP_USER_CASES: Final = (
    ("two-chars", "mcp-ab"),
    ("one-char", "mcp-a"),
    ("no-name", "mcp-"),
    ("no-hyphen", "mcp"),
    ("upper-prefix", "MCP-kagi"),
    ("underscore", "mcp_kagi"),
    ("leading-digit", "mcp-1x"),
    ("max-name", "mcp-" + _A31),
    ("over-name", "mcp-" + _A32),
    ("root", "root"),
    ("hyphen-name", "mcp-ha-read"),
    ("crlf", "mcp-kagi\r\n"),
)

GATE_ID_CASES: Final = (
    ("all-zero", "0" * 16),
    ("upper", "0123456789ABCDEF"),
    ("short-15-chars", "0123456789abcde"),
    ("long-17-chars", "0123456789abcdef0"),
    ("letter-g", "g123456789abcdef"),
    ("crlf", "0123456789abcdef\r\n"),
)

ATTACHMENT_CASES: Final = (
    ("one-char", "a"),
    ("file-name", "report-2.final_v1.pdf"),
    ("dot", "."),
    ("dot-dot", ".."),
    ("leading-dot", ".hidden"),
    ("three-dots", "..."),
    ("leading-hyphen", "-rf"),
    ("slash", "a/b"),
    ("backslash", "a\\b"),
    ("space-inside", "a b"),
    ("latin-letter", "caf\u00e9.txt"),
    ("max-120-chars", "a" * 120),
    ("over-121-chars", "a" * 121),
    ("crlf", "a.txt\r\n"),
)

_NAME_CONTRACT: Final = "contract 01 §2, contract 02 §2"

GRAMMARS: Final = (
    Grammar(
        "name",
        "vault-2",
        "a",
        NAME_CASES,
        (
            Concept(
                "family_name",
                _NAME_CONTRACT,
                (
                    Copy(
                        "attendance",
                        "attendance.ids.is_family",
                        _predicate(attendance_ids.is_family),
                    ),
                    Copy(
                        "chaperone",
                        "chaperone.family_ids.is_family_name",
                        _predicate(chaperone_ids.is_family_name),
                    ),
                    _grant_copy("family", lambda text: _grant_accepts(text)),
                    Copy("door_tui", "agent_door_tui.ids.is_family", _predicate(tui_ids.is_family)),
                    Copy(
                        "door_owui",
                        "agent_door_owui.openai_api.is_family_name",
                        _predicate(owui_api.is_family_name),
                    ),
                    _pattern_copy(
                        "family_file",
                        "agent_family.grammar.FAMILY_NAME",
                        family_grammar.FAMILY_NAME,
                    ),
                    Copy(
                        "noticeboard",
                        "noticeboard.registrywrite.NAME_RE.match",
                        _match(registrywrite.NAME_RE),
                        (
                            "The entry point is a pattern. The package's own code calls "
                            "`match` on it.",
                        ),
                    ),
                    _pattern_copy(
                        "handover_requester",
                        "handover.resolve.REQUESTED_BY_RE",
                        resolve.REQUESTED_BY_RE,
                    ),
                    _pattern_copy(
                        "handover_executor",
                        "handover.executor.request.REQUESTED_BY_RE",
                        executor_request.REQUESTED_BY_RE,
                    ),
                ),
            ),
            Concept(
                "server_name",
                "contract 01b §1",
                (
                    _pattern_copy(
                        "family_file",
                        "agent_family.grammar.SERVER_NAME",
                        family_grammar.SERVER_NAME,
                    ),
                    _pattern_copy(
                        "caregiver",
                        "caregiver.mcp_release.SERVER_NAME_RE",
                        mcp_release.SERVER_NAME_RE,
                    ),
                    _grant_copy(
                        "tools, a key",
                        lambda text: _grant_accepts(_GRANT_FAMILY, tools={text: []}),
                    ),
                    _pattern_copy(
                        "handover_mcpserver",
                        "handover.mcpserver.SERVER_NAME_RE",
                        mcpserver.SERVER_NAME_RE,
                    ),
                    _pattern_copy(
                        "handover_intake",
                        "handover.intake.token.SERVER_NAME_RE",
                        intake_token.SERVER_NAME_RE,
                    ),
                ),
            ),
            Concept(
                "webhook_name",
                "contract 01 §3.13",
                (
                    _pattern_copy(
                        "family_file",
                        "agent_family.grammar.WEBHOOK_NAME",
                        family_grammar.WEBHOOK_NAME,
                    ),
                ),
            ),
            Concept(
                "skill_name",
                "contract 01 §3.10",
                (
                    _pattern_copy(
                        "family_file", "agent_family.grammar.SKILL_NAME", family_grammar.SKILL_NAME
                    ),
                ),
            ),
            Concept(
                "component_name",
                "contract 06 §1",
                (
                    _pattern_copy(
                        "handover_manifest", "handover.manifest.NAME_RE", manifest.NAME_RE
                    ),
                    _pattern_copy(
                        "handover_executor",
                        "handover.executor.request.COMPONENT_RE",
                        executor_request.COMPONENT_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "mcp_user",
        "mcp-kagi2",
        "a",
        MCP_USER_CASES,
        (
            Concept(
                "mcp_user",
                "contract 01b §9",
                (
                    _pattern_copy("chaperone", "chaperone.run_as.USER_RE", run_as.USER_RE),
                    _pattern_copy(
                        "handover_executor",
                        "handover.executor.host.MCP_USER_RE",
                        executor_host.MCP_USER_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "session_id",
        "tui-" + _ULID[:-1] + "2",
        "a",
        SESSION_CASES,
        (
            Concept(
                "session_id",
                "contract 02 §2",
                (
                    Copy(
                        "attendance",
                        "attendance.ids.is_session",
                        _predicate(attendance_ids.is_session),
                    ),
                    Copy(
                        "chaperone",
                        "chaperone.headers.read_claimed, header x-session-id",
                        _claimed_session,
                        (NOTE_CLAIMED,),
                    ),
                    Copy(
                        "door_tui", "agent_door_tui.ids.is_session", _predicate(tui_ids.is_session)
                    ),
                    _pattern_copy(
                        "handover_executor",
                        "handover.executor.request.SESSION_ID_RE",
                        executor_request.SESSION_ID_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "owui_chat_id",
        "3f2b1c9e-8a55-4c1e-9f0a-2b6d7e8f9a12",
        "a",
        SESSION_CASES,
        (
            Concept(
                "owui_chat_id",
                "contract 02 §2",
                (
                    Copy(
                        "door_owui",
                        "agent_door_owui.headers.read_ids, header x-owui-chat-id",
                        _owui_chat_id,
                        (
                            "The input is the header value. The entry point strips white space "
                            "from both ends before it validates.",
                            "The value holds the chat id and the session id that the door makes "
                            "from it. The session id is 5 characters longer than the chat id.",
                        ),
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "owui_model",
        "agent:vault-2",
        "a",
        (
            *((slug, f"agent:{text}") for slug, text in NAME_CASES),
            ("no-prefix", "chat"),
            ("upper-prefix", "Agent:chat"),
            ("prefix-only", "agent:"),
        ),
        (
            Concept(
                "owui_model",
                "contract 02 §2",
                (
                    Copy(
                        "door_owui",
                        "agent_door_owui.openai_api.family_of",
                        _owui_family_of,
                        ("The input is a model name. The value is the family name inside it.",),
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "ulid",
        _ULID[:-1] + "2",
        "0",
        ULID_CASES,
        (
            Concept(
                "ulid",
                "contract 02 §2",
                (
                    Copy(
                        "attendance", "attendance.ids.is_ulid", _predicate(attendance_ids.is_ulid)
                    ),
                    Copy(
                        "chaperone",
                        "chaperone.headers.read_claimed, header x-turn-id",
                        _claimed_turn,
                        (NOTE_CLAIMED,),
                    ),
                    Copy("door_tui", "agent_door_tui.ids.is_ulid", _predicate(tui_ids.is_ulid)),
                    Copy(
                        "door_trigger",
                        "agent_door_trigger.ulid.ULID_PATTERN.match",
                        _match(trigger_ulid.ULID_PATTERN),
                        (
                            "The entry point is a pattern. No product code calls it. The "
                            "package's tests call `match` on it.",
                        ),
                    ),
                    _pattern_copy(
                        "caregiver", "caregiver.mcp_release.ULID_RE", mcp_release.ULID_RE
                    ),
                    _pattern_copy(
                        "handover_requester", "handover.resolve.ULID_RE", resolve.ULID_RE
                    ),
                    _pattern_copy(
                        "handover_executor",
                        "handover.executor.request.ULID_RE",
                        executor_request.ULID_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "sandbox_name",
        "chat-s12",
        "1",
        SANDBOX_CASES,
        (
            Concept(
                "sandbox_name",
                "contract 05 §4.1",
                (
                    Copy(
                        "attendance",
                        "attendance.ids.is_sandbox",
                        _predicate(attendance_ids.is_sandbox),
                    ),
                    Copy(
                        "door_tui", "agent_door_tui.ids.is_sandbox", _predicate(tui_ids.is_sandbox)
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "tool_name",
        "search2",
        "a",
        TOOL_CASES,
        (
            Concept(
                "tool_name",
                "contract 01 §3.4, contract 01b §5",
                (
                    _pattern_copy(
                        "family_file", "agent_family.grammar.TOOL_NAME", family_grammar.TOOL_NAME
                    ),
                    _grant_copy(
                        "tools, a list entry",
                        lambda text: _grant_accepts(_GRANT_FAMILY, tools={"kagi": [text]}),
                    ),
                    _pattern_copy(
                        "handover_mcpserver",
                        "handover.mcpserver.TOOL_NAME_RE",
                        mcpserver.TOOL_NAME_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "env_name",
        "API_KEY2",
        "A",
        ENV_CASES,
        (
            Concept(
                "env_name",
                "contract 01b §4.1",
                (
                    _pattern_copy(
                        "family_file",
                        "agent_family.grammar.ENV_VAR_NAME",
                        family_grammar.ENV_VAR_NAME,
                    ),
                    _pattern_copy(
                        "handover_mcpserver",
                        "handover.mcpserver.ENV_NAME_RE",
                        mcpserver.ENV_NAME_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "secret_name",
        "kagi_api_key2",
        "a",
        SECRET_CASES,
        (
            Concept(
                "secret_name",
                "contract 01b §4.1, contract 06 §8",
                (
                    _pattern_copy(
                        "caregiver",
                        "caregiver.mcp_release.SECRET_NAME_RE",
                        mcp_release.SECRET_NAME_RE,
                    ),
                    _pattern_copy(
                        "chaperone",
                        "chaperone.secrets.SECRET_NAME_RE",
                        chaperone_secrets.SECRET_NAME_RE,
                    ),
                    _pattern_copy(
                        "handover_manifest", "handover.manifest.SECRET_RE", manifest.SECRET_RE
                    ),
                    _pattern_copy(
                        "handover_mcpserver",
                        "handover.mcpserver.SECRET_NAME_RE",
                        mcpserver.SECRET_NAME_RE,
                    ),
                    _pattern_copy(
                        "handover_intake_store",
                        "handover.intake.store.SECRET_NAME_RE",
                        intake_store.SECRET_NAME_RE,
                    ),
                    _pattern_copy(
                        "handover_intake_token",
                        "handover.intake.token.SECRET_NAME_RE",
                        intake_token.SECRET_NAME_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "version",
        "1.2.3",
        "1",
        VERSION_CASES,
        (
            Concept(
                "version",
                "contract 06 §2",
                (
                    _pattern_copy(
                        "handover_manifest", "handover.manifest.VERSION_RE", manifest.VERSION_RE
                    ),
                    _pattern_copy("handover_state", "handover.state.VERSION_RE", state.VERSION_RE),
                    _pattern_copy(
                        "handover_executor",
                        "handover.executor.request.VERSION_RE",
                        executor_request.VERSION_RE,
                    ),
                    _pattern_copy(
                        "handover_provenance",
                        "handover.executor.provenance.VERSION_RE",
                        provenance.VERSION_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "contract_version",
        "0.6",
        "1",
        CONTRACT_VERSION_CASES,
        (
            Concept(
                "contract_version",
                "contract 06 §3",
                (
                    _pattern_copy(
                        "handover_manifest",
                        "handover.manifest.CONTRACT_VERSION_RE",
                        manifest.CONTRACT_VERSION_RE,
                    ),
                    _pattern_copy(
                        "handover_state",
                        "handover.state.CONTRACT_VERSION_RE",
                        state.CONTRACT_VERSION_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "tag",
        "chaperone-v1.2.3",
        "1",
        TAG_CASES,
        (
            Concept(
                "tag",
                "contract 06 §2",
                (
                    _pattern_copy("handover_allocate", "handover.allocate.TAG_RE", allocate.TAG_RE),
                    Copy(
                        "handover_source",
                        "handover.executor.source.newest_tagged_version",
                        _newest_tagged,
                        (
                            "The entry point takes a list of tags and a component. The generator "
                            "gives it one tag, and the text before the last `-v` as the component.",
                            "The value is the version that the entry point returns.",
                        ),
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "package_version",
        "1.2.3",
        "1",
        PACKAGE_VERSION_CASES,
        (
            Concept(
                "package_version",
                "contract 01b §3.1",
                (
                    _pattern_copy(
                        "family_file",
                        "agent_family.grammar.EXACT_VERSION",
                        family_grammar.EXACT_VERSION,
                    ),
                    _pattern_copy(
                        "handover_mcpserver", "handover.mcpserver.VERSION_RE", mcpserver.VERSION_RE
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "sha256_hex",
        _HEX64[:-1] + "2",
        "0",
        SHA256_CASES,
        (
            Concept(
                "sha256_hex",
                "contract 01b §3.1, contract 04 §2.2",
                (
                    _pattern_copy(
                        "family_file", "agent_family.grammar.SHA256_HEX", family_grammar.SHA256_HEX
                    ),
                    _grant_copy(
                        "token_sha256, a list entry",
                        lambda text: _grant_accepts(_GRANT_FAMILY, token_sha256=[text]),
                    ),
                    _pattern_copy(
                        "handover_mcpserver", "handover.mcpserver.SHA256_RE", mcpserver.SHA256_RE
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "git_object_id",
        _HEX40[:-1] + "2",
        "0",
        OBJECT_ID_CASES,
        (
            Concept(
                "git_object_id",
                "contract 06 §4",
                (
                    _pattern_copy("handover_state", "handover.state.SHA_RE", state.SHA_RE),
                    _pattern_copy(
                        "handover_provenance",
                        "handover.executor.provenance.SHA_RE",
                        provenance.SHA_RE,
                    ),
                    _pattern_copy(
                        "handover_source",
                        "handover.executor.source.OBJECT_ID_RE",
                        source.OBJECT_ID_RE,
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "gate_id",
        "0123456789abcde2",
        "0",
        GATE_ID_CASES,
        (
            Concept(
                "gate_id",
                "contract 04 §8.4",
                (
                    Copy(
                        "chaperone",
                        "chaperone.gates.GATE_ID_RE.match",
                        _match(chaperone_gates.GATE_ID_RE),
                        (
                            "The entry point is a pattern. The package's own code calls "
                            "`match` on it.",
                        ),
                    ),
                ),
            ),
        ),
    ),
    Grammar(
        "attachment_name",
        "notes-2",
        "a",
        ATTACHMENT_CASES,
        (
            Concept(
                "attachment_name",
                "contract 02 §5.4.1",
                (
                    Copy(
                        "attendance",
                        "attendance.ids.is_attachment",
                        _predicate(attendance_ids.is_attachment),
                    ),
                ),
            ),
        ),
    ),
)


def _probes(grammar: Grammar) -> tuple[tuple[str, str], ...]:
    """The fixed probes every grammar gets, built from its sample."""
    sample = grammar.sample

    return (
        ("probe-sample", sample),
        ("probe-empty", ""),
        ("probe-trailing-newline", sample + "\n"),
        ("probe-trailing-cr", sample + "\r"),
        ("probe-leading-space", " " + sample),
        ("probe-trailing-space", sample + " "),
        ("probe-nul-at-end", sample + "\x00"),
        ("probe-nul-inside", sample[:1] + "\x00" + sample[1:]),
        ("probe-arabic-digit", sample[:-1] + ARABIC_ONE),
        ("probe-fullwidth-digit", sample[:-1] + FULLWIDTH_ONE),
        ("probe-overlong", sample + grammar.filler * (OVERLONG_CHARS - len(sample))),
    )


def _inputs(grammar: Grammar) -> tuple[tuple[str, str], ...]:
    return _probes(grammar) + grammar.cases


def _vector(vector_id: str, text: str, check: Check) -> Vector:
    given = text_input(text)

    def call() -> Vector:
        took, detail = check(text)
        if took:
            return accepted(vector_id, given, detail)

        return refused(vector_id, given, detail)

    return run(vector_id, given, call)


def _surface_name(concept: Concept, copy: Copy) -> str:
    return f"id.{concept.name}.{copy.owner}"


def _surface(grammar: Grammar, concept: Concept, copy: Copy) -> Surface:
    return Surface(
        name=_surface_name(concept, copy),
        path=f"ids/{concept.name}.{copy.owner}.json",
        entry=copy.entry,
        contract=concept.contract,
        notes=copy.notes,
        context={"grammar": grammar.name},
        vectors=tuple(_vector(slug, text, copy.check) for slug, text in _inputs(grammar)),
    )


def surfaces() -> tuple[Surface, ...]:
    """One surface per copy, in the order of `GRAMMARS`."""
    return tuple(
        _surface(grammar, concept, copy)
        for grammar in GRAMMARS
        for concept in grammar.concepts
        for copy in concept.copies
    )


def _results(grammar: Grammar, built: dict[str, Surface]) -> dict[str, dict[str, str]]:
    """Vector id to surface name to result, for every copy of one grammar."""
    table: dict[str, dict[str, str]] = {}
    for concept in grammar.concepts:
        for copy in concept.copies:
            name = _surface_name(concept, copy)
            for vector in built[name].vectors:
                table.setdefault(vector.id, {})[name] = str(vector.body["result"])

    return table


def disagreements(built: tuple[Surface, ...]) -> list[Json]:
    """Every input that two copies of one grammar end differently on."""
    by_name = {surface.name: surface for surface in built}
    rows: list[Json] = []
    for grammar in GRAMMARS:
        texts = dict(_inputs(grammar))
        for vector_id, results in _results(grammar, by_name).items():
            if len(set(results.values())) < 2:
                continue

            by_result: dict[str, Json] = {}
            for result in sorted(set(results.values())):
                names = sorted(name for name, one in results.items() if one == result)
                by_result[result] = [*names]

            rows.append(
                {
                    "grammar": grammar.name,
                    "id": vector_id,
                    "input": text_input(texts[vector_id]),
                    "results": by_result,
                }
            )

    return rows


def render_disagreements(built: tuple[Surface, ...]) -> str:
    """The bytes of `ids/disagreements.json`. One row per line."""
    rows = ",\n".join(f"  {compact(row)}" for row in disagreements(built))
    body = f"[\n{rows}\n ]" if rows else "[]"

    return '{\n "format": 1,\n "kind": "disagreements",\n "rows": ' + body + "\n}\n"

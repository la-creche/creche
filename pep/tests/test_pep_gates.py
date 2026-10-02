"""The gate id and the phone summary (contract 04 §8.2, §8.3).

Both are pure functions of `(family, tool, args)`. No model writes either one
(invariant 14), so every case here is an exact string.
"""

from __future__ import annotations

import hashlib

from agent_pep.gates import (
    FIELD_MAX_CHARS,
    GATE_ID_HEX_LEN,
    GATE_ID_RE,
    SUMMARY_MAX_CHARS,
    approval_summary,
    canonical_args,
    gate_id,
)

FAMILY = "chat"
OTHER = "code-sandbox"
TOOL = "ha_call"
ARGS: dict[str, object] = {"domain": "notify", "service": "mobile_app_example_phone"}


# ---- the gate id (§8.2) ----------------------------------------------------


def test_the_gate_id_is_16_hex_characters() -> None:
    gate = gate_id(FAMILY, TOOL, ARGS)
    assert len(gate) == GATE_ID_HEX_LEN
    assert GATE_ID_RE.match(gate)


def test_the_same_call_hashes_the_same_gate() -> None:
    assert gate_id(FAMILY, TOOL, ARGS) == gate_id(FAMILY, TOOL, dict(ARGS))


def test_key_order_does_not_change_the_gate() -> None:
    """Canonical means sorted: a client reordering its JSON keys re-issues the
    same call, so it must land on the same gate."""
    reordered = {"service": "mobile_app_example_phone", "domain": "notify"}
    assert gate_id(FAMILY, TOOL, reordered) == gate_id(FAMILY, TOOL, ARGS)


def test_a_changed_argument_hashes_a_new_gate() -> None:
    """§8.2: a re-issue with different arguments opens a fresh approval."""
    changed = {**ARGS, "entity_id": "light.office"}
    assert gate_id(FAMILY, TOOL, changed) != gate_id(FAMILY, TOOL, ARGS)


def test_another_family_hashes_a_new_gate() -> None:
    assert gate_id(OTHER, TOOL, ARGS) != gate_id(FAMILY, TOOL, ARGS)


def test_another_tool_hashes_a_new_gate() -> None:
    assert gate_id(FAMILY, "enqueue", ARGS) != gate_id(FAMILY, TOOL, ARGS)


def test_the_separator_cannot_be_forged_from_a_name() -> None:
    """The three fields are joined on NUL, which no family name, tool name or
    JSON text can carry. Without that, `chat` + `ha_call` and `chat_ha` +
    `call` could hash alike."""
    assert gate_id("chat", "ha_call", {}) != gate_id("chatha_call", "", {})


def test_the_gate_id_is_the_documented_digest() -> None:
    whole = f"{FAMILY}\x00{TOOL}\x00{canonical_args(ARGS)}".encode()
    assert gate_id(FAMILY, TOOL, ARGS) == hashlib.sha256(whole).hexdigest()[:GATE_ID_HEX_LEN]


def test_canonical_args_is_compact_and_sorted() -> None:
    assert canonical_args({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'


def test_canonical_args_keeps_non_ascii_whole() -> None:
    """A hash over escaped text and a hash over the text itself differ, so the
    encoding is fixed rather than left to a default."""
    assert canonical_args({"m": "über"}) == '{"m":"über"}'


# ---- the summary (§8.3) ----------------------------------------------------


def test_the_summary_leads_with_the_tool() -> None:
    assert approval_summary(TOOL, ARGS).startswith(f"{TOOL} ")


def test_the_summary_lists_arguments_in_key_order() -> None:
    assert approval_summary(TOOL, ARGS) == (
        'ha_call domain="notify" service="mobile_app_example_phone"'
    )


def test_a_tool_with_no_arguments_is_its_own_summary() -> None:
    assert approval_summary("job_status", {}) == "job_status"


def test_a_long_field_is_cut_with_its_length_and_a_hash() -> None:
    """§8.3: 120 characters, then an ellipsis, the full length and a sha256
    prefix. The cut is visible and the whole stays checkable in the audit."""
    body = "x" * 500
    summary = approval_summary("enqueue", {"message": body})
    assert "…(500 chars, sha256:" in summary
    assert summary.count("x") == FIELD_MAX_CHARS - 1  # one character is the opening quote


def test_a_short_field_is_not_cut() -> None:
    assert approval_summary("enqueue", {"message": "hi"}) == 'enqueue message="hi"'


def test_the_whole_summary_is_capped() -> None:
    args: dict[str, object] = {f"k{n:02d}": "y" * 100 for n in range(10)}
    assert len(approval_summary("enqueue", args)) <= SUMMARY_MAX_CHARS


def test_a_cut_summary_says_how_many_fields_it_hides() -> None:
    """The cap can hide an argument that says where a call lands. The human
    reading the push must see that something is hidden, not a clean sentence
    that happens to be short."""
    args: dict[str, object] = {f"k{n:02d}": "y" * 100 for n in range(10)}
    assert approval_summary("enqueue", args).endswith(" more)")


def test_a_summary_that_just_fits_hides_nothing() -> None:
    args: dict[str, object] = {"a": "1", "b": "2"}
    assert approval_summary("enqueue", args) == 'enqueue a="1" b="2"'


def test_a_tool_name_alone_over_the_cap_is_cut() -> None:
    """`CallBody` caps a tool name at 200, which is the summary cap too, so a
    maximal name plus one argument cannot fit."""
    summary = approval_summary("t" * SUMMARY_MAX_CHARS, {"a": "1"})
    assert len(summary) <= SUMMARY_MAX_CHARS


def test_a_summary_never_carries_a_newline() -> None:
    """The push renders as one line. A newline in an argument would let a
    caller draw a second, fake line under the real one."""
    assert "\n" not in approval_summary("enqueue", {"message": "one\ntwo"})


def test_the_summary_is_deterministic() -> None:
    assert approval_summary(TOOL, ARGS) == approval_summary(TOOL, dict(ARGS))

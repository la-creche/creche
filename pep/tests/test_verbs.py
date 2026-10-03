"""The verb catalog: contract 04 §4.1's six schemas and their fences."""

from __future__ import annotations

from agent_pep.family_grants import VerbFence
from agent_pep.verbs import VERB_CATALOG, check_fence, check_schema, manifest_schema


def test_the_catalog_holds_the_six_names() -> None:
    assert set(VERB_CATALOG) == {
        "embed",
        "ha_call",
        "enqueue",
        "job_status",
        "release",
        "invoke_agent",
    }


def test_every_schema_forbids_unknown_arguments() -> None:
    """§4.1: an unknown argument key is arg_validation, never ignored."""
    for name, spec in VERB_CATALOG.items():
        assert spec.schema["additionalProperties"] is False, name
        denial = check_schema(name, {"surprise": 1})
        assert denial is not None
        assert denial.reason == "arg_validation"


def test_embed_schema() -> None:
    assert check_schema("embed", {"input": "hello"}) is None
    assert check_schema("embed", {}) is not None
    assert check_schema("embed", {"input": ""}) is not None
    assert check_schema("embed", {"input": "x" * 32769}) is not None


def test_embed_takes_no_model_argument() -> None:
    """The PEP serves one pinned model (§4.1), so naming one is an unknown
    argument, not a choice the caller gets to make."""
    denial = check_schema("embed", {"input": "hello", "model": "bge-m3"})
    assert denial is not None
    assert denial.reason == "arg_validation"
    assert manifest_schema("embed")["properties"] == {
        "input": {"type": "string", "minLength": 1, "maxLength": 32768}
    }


def test_ha_call_schema_and_fence() -> None:
    fence = VerbFence.model_validate(
        {"allow": [{"domain": "notify", "service": "mobile_app_example_phone"}]}
    )
    args = {"domain": "notify", "service": "mobile_app_example_phone"}
    assert check_schema("ha_call", args) is None
    assert check_fence("ha_call", args, fence) is None

    other = {"domain": "lock", "service": "unlock"}
    denial = check_fence("ha_call", other, fence)
    assert denial is not None
    assert denial.reason == "arg_validation"

    # entity_id must match on both sides, not on one.
    with_entity = {**args, "entity_id": "light.kitchen"}
    assert check_fence("ha_call", with_entity, fence) is not None


def test_ha_call_without_a_fence_matches_nothing() -> None:
    """Deny by default: a grant with no allow list reaches no service."""
    args = {"domain": "notify", "service": "x"}
    assert check_fence("ha_call", args, VerbFence()) is not None
    assert check_fence("ha_call", args, None) is not None


def test_ha_call_schema_rejects_bad_shapes() -> None:
    assert check_schema("ha_call", {"domain": "notify"}) is not None
    assert check_schema("ha_call", {"domain": "Notify", "service": "x"}) is not None
    assert check_schema("ha_call", {"domain": "n", "service": "x", "entity_id": "bad"}) is not None
    big = {f"k{i}": i for i in range(33)}
    assert check_schema("ha_call", {"domain": "n", "service": "x", "data": big}) is not None


def test_enqueue_target_outside_the_fence_is_not_granted() -> None:
    """§4.1: the verb was granted and the target was not."""
    fence = VerbFence.model_validate({"targets": ["agent-control-worker"]})
    inside = {"family": "agent-control-worker", "message": "go"}
    assert check_schema("enqueue", inside) is None
    assert check_fence("enqueue", inside, fence) is None

    outside = {"family": "finance-worker", "message": "go"}
    denial = check_fence("enqueue", outside, fence)
    assert denial is not None
    assert denial.reason == "tool_not_granted"

    missing = check_fence("enqueue", inside, VerbFence())
    assert missing is not None
    assert missing.reason == "tool_not_granted"


def test_job_status_takes_no_arguments_at_all() -> None:
    assert check_schema("job_status", {}) is None
    assert check_fence("job_status", {}, VerbFence()) is None
    assert check_schema("job_status", {"limit": 200}) is None
    assert check_schema("job_status", {"limit": 201}) is not None
    assert check_schema("job_status", {"since": "not a time"}) is not None
    # No `family` argument exists, so the scope cannot be widened.
    assert check_schema("job_status", {"family": "chat"}) is not None


def test_release_schema_and_fence() -> None:
    fence = VerbFence.model_validate({"components": ["pep", "managerd"]})
    args: dict[str, object] = {"components": {"pep": "0.2.0"}}
    assert check_schema("release", args) is None
    assert check_fence("release", args, fence) is None

    outside: dict[str, object] = {"components": {"attendance": "latest"}}
    denial = check_fence("release", outside, fence)
    assert denial is not None
    assert denial.reason == "arg_validation"

    assert check_schema("release", {"components": {"pep": "v0.2"}}) is not None
    assert check_schema("release", {"components": {}}) is not None


def test_release_kind_and_rollback_of_must_agree() -> None:
    fence = VerbFence.model_validate({"components": ["pep"]})
    rollback: dict[str, object] = {"kind": "rollback", "components": {"pep": "0.1.0"}}
    assert check_fence("release", rollback, fence) is not None

    ulid = "01K5J9QW3R7T0ZP4YB2H6N8M1D"
    rollback["rollback_of"] = ulid
    assert check_schema("release", rollback) is None
    assert check_fence("release", rollback, fence) is None

    both: dict[str, object] = {"components": {"pep": "0.1.0"}, "rollback_of": ulid}
    assert check_fence("release", both, fence) is not None


def test_release_without_a_components_fence_reaches_nothing() -> None:
    args: dict[str, object] = {"components": {"pep": "0.2.0"}}
    assert check_fence("release", args, VerbFence()) is not None
    assert check_fence("release", args, None) is not None


def test_release_components_must_be_an_object() -> None:
    fence = VerbFence.model_validate({"components": ["pep"]})
    assert check_fence("release", {"components": ["pep"]}, fence) is not None


def test_invoke_agent_schema() -> None:
    assert check_schema("invoke_agent", {"family": "vault-oracle", "message": "hi"}) is None
    assert check_schema("invoke_agent", {"family": "Vault", "message": "hi"}) is not None
    over = {"family": "v", "message": "hi", "attachments": ["a"] * 17}
    assert check_schema("invoke_agent", over) is not None
    # invoke_agent's fence is `delegates`, which the decision core reads.
    assert check_fence("invoke_agent", {"family": "v", "message": "x"}, None) is None


def test_an_unknown_verb_is_not_granted() -> None:
    schema_denial = check_schema("teleport", {})
    assert schema_denial is not None
    assert schema_denial.reason == "tool_not_granted"

    fence_denial = check_fence("teleport", {}, None)
    assert fence_denial is not None
    assert fence_denial.reason == "tool_not_granted"


def test_manifest_schema_is_a_copy() -> None:
    served = manifest_schema("embed")
    assert served == VERB_CATALOG["embed"].schema
    served["type"] = "tampered"
    assert VERB_CATALOG["embed"].schema["type"] == "object"

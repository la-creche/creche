"""The rules P1 to P6 of `executor/forge_provenance.py` on typed facts, and
the whole chain `verify_forge_release` on recorded replies
(`docs/design/forgejo-release-path.md`, section 5).

The chain needs replies that no recording holds as recorded: a Release, a
ref and a tag object of a release tag, a statement in the tag message and
a status of the head of the pull request. `_forge` builds each one from a
recording and says in its docstring what test code changed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from handover.artifacts import ArtifactRefusal, disk_files
from handover.catalog import Repo
from handover.errors import Refusal, RefusalCode
from handover.executor.forge_api import (
    ForgePath,
    ForgeReason,
    ForgeReply,
    ForgeRepo,
    ForgeValueError,
    ObjectId,
    TagName,
)
from handover.executor.forge_provenance import (
    ExpectedOrigin,
    ForgeVerified,
    ProvenanceReason,
    check_digests,
    check_statement,
    expected_origin,
    forge_repo,
    signed_names,
    verify_forge_commit,
    verify_forge_release,
)
from handover.executor.provenance import Verified
from handover.executor.release_statement import (
    ReleaseStatement,
    StatementReason,
    StatementTag,
    UnverifiedMessage,
    check_signature,
    read_statement,
)
from handover_executor_fixtures import FakeRun
from handover_frp_fixtures import (
    MERGE_COMMIT,
    PR_HEAD,
    SIGNER,
    TAG_OBJECT,
    KeyStandIn,
    changed,
    reply,
    signed_bundle,
    tag_message,
    use_verifier_site,
)

OWNER = "example-owner"
NAME = "creche"
FORGE = ForgeRepo(OWNER, Repo.CRECHE)
COMPONENT = "vmm"
TAG = "vmm-v0.1.24"
ASSET = "vmm-x86_64-unknown-linux-musl.tar.gz"
DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
GATE = "gate / gate (pull_request)"


def _example() -> dict[str, Any]:
    """The first statement vector of the signer: the example of section
    4.5 for `example-owner/creche` at the recorded merge commit, with the
    tags `handover-v0.1.96` and `vmm-v0.1.24`."""
    text = json.loads((SIGNER / "vectors" / "statement.json").read_text(encoding="utf-8"))
    value: dict[str, Any] = json.loads(text["Statement"][0]["json"])
    assert value["commit"] == MERGE_COMMIT

    return value


def _canonical(value: dict[str, Any]) -> bytes:
    """A statement in canonical form: sorted tags, no whitespace. The keys
    keep the order of the vector."""
    value["tags"] = sorted(value["tags"], key=lambda one: one["tag"])

    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _statement(tmp_path: Path, value: dict[str, Any]) -> ReleaseStatement:
    """A `ReleaseStatement`, through the real check and the real reader.
    The runner passes each verifier child."""
    work = tmp_path / "parse"
    work.mkdir(exist_ok=True)
    message = UnverifiedMessage(statement=_canonical(value), bundle=b"{}")

    return read_statement(check_signature(FakeRun(), COMPONENT, message, work))


def _origin() -> ExpectedOrigin:
    return expected_origin(FORGE, ObjectId(MERGE_COMMIT))


def _refused(call: Any, *args: Any) -> Refusal:
    with pytest.raises(Refusal) as refused:
        call(*args)

    return refused.value


# -- P1b: the statement and the catalog -------------------------------------------


def test_the_example_statement_names_the_tag(tmp_path: Path) -> None:
    statement = _statement(tmp_path, _example())
    entry = check_statement(COMPONENT, statement, _origin(), TagName(TAG))

    assert (entry.tag, [(one.name, one.sha256) for one in entry.assets]) == (TAG, [(ASSET, DIGEST)])


@pytest.mark.parametrize(
    ("origin", "reason"),
    [
        (
            ExpectedOrigin(ForgeRepo(OWNER, Repo.MCP), "release.yml", MERGE_COMMIT),
            "OTHER_REPOSITORY",
        ),
        (
            ExpectedOrigin(ForgeRepo("other-owner", Repo.CRECHE), "release.yml", MERGE_COMMIT),
            "OTHER_REPOSITORY",
        ),
        (ExpectedOrigin(FORGE, "tags.yml", MERGE_COMMIT), "OTHER_WORKFLOW"),
        (ExpectedOrigin(FORGE, "release.yml", PR_HEAD), "OTHER_COMMIT"),
    ],
)
def test_p1_refuses_another_origin(tmp_path: Path, origin: ExpectedOrigin, reason: str) -> None:
    statement = _statement(tmp_path, _example())
    refused = _refused(check_statement, COMPONENT, statement, origin, TagName(TAG))

    assert (refused.code, refused.detail) == (RefusalCode.P1, ProvenanceReason[reason].value)


def test_p1_refuses_a_tag_that_is_not_signed(tmp_path: Path) -> None:
    statement = _statement(tmp_path, _example())
    later = TagName("vmm-v0.1.25")
    refused = _refused(check_statement, COMPONENT, statement, _origin(), later)

    assert refused.detail == ProvenanceReason.TAG_NOT_SIGNED.value


@pytest.mark.parametrize(
    ("tag", "assets", "reason"),
    [
        ("chaperone-v1.0.0", [], ProvenanceReason.FOREIGN_TAG),
        ("registry-data-v0.1.0", [], ProvenanceReason.FOREIGN_TAG),
        ("nosuch-v1.0.0", [], ProvenanceReason.FOREIGN_TAG),
        ("memory-v0.1.0", [{"name": "memory.tar.gz", "sha256": DIGEST}], "ASSET_ON_SOURCE"),
    ],
)
def test_p1_refuses_each_tag_outside_the_rules(
    tmp_path: Path, tag: str, assets: list[dict[str, str]], reason: ProvenanceReason | str
) -> None:
    """The example statement with one more tag, added in test code: a
    component of another repository, a component that takes no release, a
    name of no component, and a venv component with an asset."""
    value = _example()
    value["tags"].append({"tag": tag, "assets": assets})
    statement = _statement(tmp_path, value)
    refused = _refused(check_statement, COMPONENT, statement, _origin(), TagName(TAG))

    expected = reason if isinstance(reason, ProvenanceReason) else ProvenanceReason[reason]
    assert refused.detail == expected.value


@pytest.mark.parametrize("repo", [Repo.MCP, Repo.CRECHE_RELEASE, Repo.REGISTRY])
def test_a_repository_with_no_release_workflow_refuses(repo: Repo) -> None:
    """mcp and creche-release wait for FR9. registry has no release."""
    refused = _refused(expected_origin, ForgeRepo(OWNER, repo), ObjectId(MERGE_COMMIT))

    assert (refused.code, refused.detail) == (
        RefusalCode.CATALOG,
        ProvenanceReason.NO_RELEASE_WORKFLOW.value,
    )


# -- the repository on the forge ---------------------------------------------------


def test_a_forge_repository_takes_its_name_from_the_catalog() -> None:
    """The name on the forge is the value of the catalog `Repo`, so no
    value pairs `Repo.CRECHE` with the forge name of another repository."""
    assert [ForgeRepo(OWNER, repo).full_name for repo in Repo] == [
        f"{OWNER}/{repo.value}" for repo in Repo
    ]

    with pytest.raises(ForgeValueError):
        ForgeRepo(OWNER, "chaperone")  # pyright: ignore[reportArgumentType]


def test_a_bad_owner_refuses_with_the_site_code() -> None:
    refused = _refused(forge_repo, COMPONENT, Repo.CRECHE, "bad/owner")

    assert (refused.code, refused.detail) == (
        RefusalCode.SITE,
        ProvenanceReason.BAD_FORGE_NAME.value,
    )
    assert forge_repo(COMPONENT, Repo.CRECHE, OWNER) == FORGE


def test_only_the_reader_makes_a_statement_value() -> None:
    with pytest.raises(TypeError):
        StatementTag(object(), TAG, ())


# -- the whole chain on recorded replies ------------------------------------------


@dataclass
class FakeForge:
    """The API reader of a test. Each path that it does not hold raises
    `ConnectionRefusedError`, as a read that does not reach the Forgejo
    instance."""

    replies: dict[str, ForgeReply]
    seen: list[str] = field(default_factory=list[str])

    def __call__(self, path: ForgePath) -> ForgeReply:
        self.seen.append(path.text)
        found = self.replies.get(path.text)
        if found is None:
            raise ConnectionRefusedError(path.text)

        return found


Change = Callable[[Any], None]


def _forge(
    message: str, forge: ForgeRepo = FORGE, extra: Mapping[str, Change] | None = None
) -> FakeForge:
    """The replies of one release of `vmm-v0.1.24` at the recorded merge
    commit. Test code changes these recorded replies:

    1. `p3-release-by-tag.json`: `tag_name` is `vmm-v0.1.24`.
    2. `p2-git-refs-tag.json`: the `ref` is `refs/tags/vmm-v0.1.24`.
    3. `p2-git-tags-sha.json`: `tag` is `vmm-v0.1.24` and `message` is the
       statement message of the test.
    4. `p4-commit-status-main.json`, served for the head of the pull
       request: one more entry `gate / gate (pull_request)` with the status
       `success`, and `total_count` one higher. No recording holds a status
       of that head or a `pull_request` context.

    `extra` maps a file to one more change of a test, after the change
    above.
    """
    base = f"/repos/{forge.full_name}"
    more = extra or {}

    def both(name: str, first: Change) -> ForgeReply:
        def change(body: Any) -> None:
            first(body)
            more.get(name, lambda _: None)(body)

        return changed(name, change)

    def release(body: dict[str, Any]) -> None:
        body["tag_name"] = TAG

    def refs(body: list[Any]) -> None:
        body[0]["ref"] = f"refs/tags/{TAG}"

    def tag_object(body: dict[str, Any]) -> None:
        body["tag"] = TAG
        body["message"] = message

    def statuses(body: dict[str, Any]) -> None:
        body["statuses"].append({"context": GATE, "status": "success"})
        body["total_count"] += 1

    return FakeForge(
        {
            f"{base}/releases/tags/{TAG}": both("p3-release-by-tag.json", release),
            f"{base}/git/refs/tags/{TAG}": both("p2-git-refs-tag.json", refs),
            f"{base}/git/tags/{TAG_OBJECT}": both("p2-git-tags-sha.json", tag_object),
            f"{base}/compare/main...{MERGE_COMMIT}": reply("p5-compare-main-itself.json"),
            f"{base}/commits/{MERGE_COMMIT}/pull": reply("p1-commit-pull-merge-sha.json"),
            f"{base}/commits/{PR_HEAD}/status": changed("p4-commit-status-main.json", statuses),
        }
    )


def _signed_message(value: dict[str, Any] | None = None) -> str:
    statement = _canonical(_example() if value is None else value)

    return tag_message(statement, signed_bundle(statement))


def _verify(api: FakeForge, run: Any, tmp_path: Path, forge: ForgeRepo = FORGE) -> ForgeVerified:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)

    return verify_forge_release(api, run, COMPONENT, forge, TAG, MERGE_COMMIT, work)


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KeyStandIn:
    use_verifier_site(tmp_path, monkeypatch)

    return KeyStandIn()


def test_a_signed_release_passes_p1_to_p5(tmp_path: Path, verifier: KeyStandIn) -> None:
    api = _forge(_signed_message())
    found = _verify(api, verifier, tmp_path)

    assert found.verified == Verified(component=COMPONENT, pr=2, head_sha=PR_HEAD)
    assert found.entry.tag == TAG
    assert len(verifier.words) == 1
    assert api.seen == list(api.replies)


def test_a_refused_signature_stops_before_the_parse(tmp_path: Path, verifier: KeyStandIn) -> None:
    """The statement is no JSON text, and its bundle signs other bytes. The
    refusal is the signature, so root parsed nothing, and no later read
    ran."""
    statement = b"no json"
    api = _forge(tag_message(statement, signed_bundle(b"other bytes")))
    refused = _refused(_verify, api, verifier, tmp_path)

    assert (refused.code, refused.detail) == (
        RefusalCode.P1,
        StatementReason.SIGNATURE_REFUSED.value,
    )
    assert not any("compare" in one for one in api.seen)


def test_a_passed_signature_of_no_json_refuses_as_no_json(
    tmp_path: Path, verifier: KeyStandIn
) -> None:
    statement = b"no json"
    api = _forge(tag_message(statement, signed_bundle(statement)))
    refused = _refused(_verify, api, verifier, tmp_path)

    assert refused.detail == StatementReason.NOT_JSON.value


def test_a_release_of_no_statement_refuses(tmp_path: Path, verifier: KeyStandIn) -> None:
    """The recorded probe message of the tag, with its LF: no statement."""
    api = _forge("annotated probe\nline two\n")
    refused = _refused(_verify, api, verifier, tmp_path)

    assert (refused.code, refused.detail) == (RefusalCode.P1, StatementReason.LINES.value)
    assert verifier.words == []


def test_a_read_that_does_not_reach_the_forge_refuses(tmp_path: Path, verifier: KeyStandIn) -> None:
    api = FakeForge({})
    refused = _refused(_verify, api, verifier, tmp_path)

    assert (refused.code, refused.detail) == (RefusalCode.P1, ForgeReason.NOT_REACHED.value)


def test_a_fault_of_the_reader_is_no_refusal(tmp_path: Path, verifier: KeyStandIn) -> None:
    """An error that is no `OSError` is a fault of the step, and the ledger
    gets its type (executor rule 9). It is never a refusal."""

    def broken(_: ForgePath) -> ForgeReply:
        raise KeyError("a fault of the client")

    with pytest.raises(KeyError):
        _verify(broken, verifier, tmp_path)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_p1_refuses_a_draft_and_a_prerelease(
    tmp_path: Path, verifier: KeyStandIn, flag: str
) -> None:
    """`_forge` with `draft` or `prerelease` of the Release set in test
    code."""

    def set_flag(body: dict[str, Any]) -> None:
        body[flag] = True

    api = _forge(_signed_message(), extra={"p3-release-by-tag.json": set_flag})
    refused = _refused(_verify, api, verifier, tmp_path)

    assert (refused.code, refused.detail) == (
        RefusalCode.P1,
        ForgeReason.DRAFT_OR_PRERELEASE.value,
    )
    assert verifier.words == []


def test_p1_refuses_a_release_of_another_tag(tmp_path: Path, verifier: KeyStandIn) -> None:
    """`_forge` with another `tag_name` of the Release, set in test code."""

    def other(body: dict[str, Any]) -> None:
        body["tag_name"] = "vmm-v0.1.23"

    api = _forge(_signed_message(), extra={"p3-release-by-tag.json": other})
    refused = _refused(_verify, api, verifier, tmp_path)

    assert (refused.code, refused.detail) == (RefusalCode.P1, ForgeReason.OTHER_TAG.value)
    assert verifier.words == []


def test_p2_refuses_a_tag_of_another_commit(tmp_path: Path, verifier: KeyStandIn) -> None:
    """`_forge` with `object.sha` of the tag object set to the head of the
    pull request in test code. No verifier child runs."""

    def head(body: dict[str, Any]) -> None:
        body["object"]["sha"] = PR_HEAD

    api = _forge(_signed_message(), extra={"p2-git-tags-sha.json": head})
    refused = _refused(_verify, api, verifier, tmp_path)

    assert (refused.code, refused.detail) == (RefusalCode.P2, ForgeReason.OTHER_COMMIT.value)
    assert verifier.words == []
    assert verifier.seen == []


def test_a_component_of_another_repository_refuses(tmp_path: Path, verifier: KeyStandIn) -> None:
    """A merged change to chaperone's `release.yml` gets `vmm-v0.1.24`
    signed in the chaperone repository, and a caller pairs `vmm` with that
    repository. The statement names `example-owner/chaperone`, set in test
    code. P1 refuses the tag, because `vmm` is no component of chaperone."""
    chaperone = ForgeRepo(OWNER, Repo.CHAPERONE)
    value = _example()
    value["repository"] = chaperone.full_name
    api = _forge(_signed_message(value), forge=chaperone)
    refused = _refused(_verify, api, verifier, tmp_path, chaperone)

    assert (refused.code, refused.detail) == (RefusalCode.P1, ProvenanceReason.FOREIGN_TAG.value)
    assert all(one.startswith(f"/repos/{chaperone.full_name}/") for one in api.seen)


@pytest.mark.parametrize(
    ("tag", "sha", "code", "reason"),
    [
        ("handover-v0.1.96", MERGE_COMMIT, RefusalCode.P1, ProvenanceReason.BAD_TAG),
        ("vmm-v0.1.024", MERGE_COMMIT, RefusalCode.P1, ProvenanceReason.BAD_TAG),
        (TAG, MERGE_COMMIT.upper(), RefusalCode.P2, ProvenanceReason.BAD_COMMIT),
    ],
)
def test_a_bad_input_refuses_before_any_read(
    tmp_path: Path, tag: str, sha: str, code: RefusalCode, reason: ProvenanceReason
) -> None:
    api = FakeForge({})
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Refusal) as refused:
        verify_forge_release(api, FakeRun(), COMPONENT, FORGE, tag, sha, work)

    assert (refused.value.code, refused.value.detail) == (code, reason.value)
    assert api.seen == []


def test_the_registry_commit_passes_p3_to_p5() -> None:
    """The replies of `_forge` for P3 to P5, with the two required contexts
    of registry added to the status in test code."""

    def statuses(body: dict[str, Any]) -> None:
        for context in ("validate / validate (pull_request)", "validate / reach (pull_request)"):
            body["statuses"].append({"context": context, "status": "success"})
        body["total_count"] += 2

    base = f"/repos/{OWNER}/registry"
    api = FakeForge(
        {
            f"{base}/compare/main...{MERGE_COMMIT}": reply("p5-compare-main-itself.json"),
            f"{base}/commits/{MERGE_COMMIT}/pull": reply("p1-commit-pull-merge-sha.json"),
            f"{base}/commits/{PR_HEAD}/status": changed("p4-commit-status-main.json", statuses),
        }
    )
    registry = ForgeRepo(OWNER, Repo.REGISTRY)
    found = verify_forge_commit(api, "mcp-servers", registry, MERGE_COMMIT)

    assert found == Verified(component="mcp-servers", pr=2, head_sha=PR_HEAD)


# -- P6 -------------------------------------------------------------------------------


@pytest.fixture
def release(tmp_path: Path, verifier: KeyStandIn) -> ForgeVerified:
    return _verify(_forge(_signed_message()), verifier, tmp_path)


def test_p6_passes_equal_digests(release: ForgeVerified) -> None:
    check_digests(release, {ASSET: DIGEST})


@pytest.mark.parametrize(
    ("digests", "reason"),
    [
        ({}, ArtifactRefusal.NO_ARTIFACT),
        ({ASSET: "f" * 64}, ArtifactRefusal.SIGNATURE_REFUSED),
        ({ASSET: f"sha256:{DIGEST}"}, ArtifactRefusal.SIGNATURE_REFUSED),
    ],
)
def test_p6_refuses_each_mismatch(
    release: ForgeVerified, digests: dict[str, str], reason: ArtifactRefusal
) -> None:
    refused = _refused(check_digests, release, digests)

    assert (refused.code, refused.subject, refused.detail) == (
        RefusalCode.P6,
        COMPONENT,
        str(reason),
    )


@pytest.mark.parametrize(
    "assets",
    [
        [{"name": ASSET, "sha256": DIGEST}, {"name": f"{ASSET}.extra", "sha256": DIGEST}],
        [{"name": ASSET, "sha256": DIGEST}, {"name": f"{ASSET}.sigstore.json", "sha256": DIGEST}],
        [],
    ],
)
def test_p6_refuses_a_statement_of_other_assets(
    tmp_path: Path, verifier: KeyStandIn, assets: list[dict[str, str]]
) -> None:
    """The example statement with the assets of `vmm-v0.1.24` changed in
    test code: one more name, the retired bundle (FR8), and none. P6 takes
    the expected names from the component, so a copy of each signed asset
    does not help."""
    value = _example()
    for one in value["tags"]:
        if one["tag"] == TAG:
            one["assets"] = assets
    release = _verify(_forge(_signed_message(value)), verifier, tmp_path)
    digests = {one["name"]: one["sha256"] for one in assets}
    refused = _refused(check_digests, release, digests)

    assert (refused.code, refused.detail) == (
        RefusalCode.P6,
        str(ArtifactRefusal.SIGNATURE_REFUSED),
    )


def test_the_signed_names_come_from_the_component() -> None:
    """A binary component signs its archive, a disk-image component each
    file of `disk_files`, and no bundle is signed (FR8). A venv component
    and a name of no row sign none."""
    assert signed_names(COMPONENT) == frozenset({ASSET})
    assert signed_names("disks") == frozenset(one.name for one in disk_files())
    assert signed_names("memory") == frozenset()
    assert signed_names("nosuch") == frozenset()


def test_only_the_chain_makes_a_forge_verified(release: ForgeVerified) -> None:
    with pytest.raises(TypeError):
        ForgeVerified(object(), release.verified, release.entry)

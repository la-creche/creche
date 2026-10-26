"""What CI asks the pure half about a binary component.

Three commands print it: `handover artifacts` (the rows, one crate row and
the words of the signature check), `handover check-tree` (the walk of a
built tree) and `handover allocate-tags --assets` (the assets of a
Release). The catalog holds no binary component, so a test that needs one
swaps one row for a binary copy of itself (`handover_bin_fixtures.py`).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from handover.artifacts import (
    CODE_HOST,
    TOKEN_ISSUER,
    ArtifactRefusal,
    ArtifactRow,
    archive_expected,
    asset_names,
    crate_row,
    download_url,
    install_words,
    rows,
    signer_policy,
    verify_words,
)
from handover.catalog import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_MEMBERS,
    ARCHIVE_MAX_UNPACKED_BYTES,
    BINARY_INSTALL_ENV,
    BINARY_TARGET,
    BUNDLE_MAX_BYTES,
    CATALOG,
    CATALOG_BY_NAME,
    MAIN_BRANCH,
    RELEASE_WORKFLOW,
    Kind,
    Repo,
)
from handover.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from handover.errors import Refusal, RefusalCode
from handover.manifest import Markup, parse_manifest
from handover.mcpserver import ServerPin, Source
from handover.site import SITE_FILE_ENV
from handover.treewalk import ABSOLUTE_LINK, NAMES_WORK_TREE
from handover_bin_fixtures import (
    BINARY_NAME,
    BUILD_LINE,
    binary_manifest_text,
    staged_binary_tree,
    use_binary_catalog,
    write_program,
)
from handover_fixtures import manifest_text, write_manifest

from handover import artifacts

REPO_ROOT = Path(__file__).resolve().parents[2]

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OWNER = "example-owner"
HOSTED = "creche"
ARCHIVE = f"/srv/assets/{BINARY_NAME}-{BINARY_TARGET}.tar.gz"
BUNDLE = f"{ARCHIVE}.sigstore.json"
TRUSTED_ROOT = "/var/lib/verify/trusted_root.json"

#: The words of the check, as `verify_words` gives them with no trust root.
WORDS = (
    "verify-blob",
    "--bundle",
    BUNDLE,
    "--certificate-oidc-issuer",
    "https://token.actions.githubusercontent.com",
    "--certificate-identity",
    f"https://github.com/{OWNER}/{HOSTED}/.github/workflows/release.yml@refs/heads/main",
    "--certificate-github-workflow-repository",
    f"{OWNER}/{HOSTED}",
    "--certificate-github-workflow-ref",
    "refs/heads/main",
    "--certificate-github-workflow-sha",
    COMMIT,
    ARCHIVE,
)

#: The four reasons of check P6, as the ledger writes them.
P6_REASONS = (
    "no artifact in the store",
    "the artifact signature was refused",
    "the verifier was not available",
    "the archive shape was refused",
)


def _out(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, list[str]]:
    code = main(list(argv))

    return code, capsys.readouterr().out.splitlines()


def _refusal(call: Callable[[], object]) -> Refusal:
    with pytest.raises(Refusal) as caught:
        call()

    return caught.value


def _binary_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None = None) -> Path:
    """A root that holds the manifest of the binary component alone, with a
    catalog that holds its binary row."""
    use_binary_catalog(monkeypatch)
    root = tmp_path / "root"
    write_manifest(root, BINARY_NAME, text or binary_manifest_text(tmp_path / "components"))

    return root


# ---- the constants and the names --------------------------------------------


def test_only_a_binary_component_ships_an_archive() -> None:
    assert {kind: archive_expected(kind) for kind in Kind} == {
        Kind.VENV: False,
        Kind.BINARY: True,
        Kind.OCI_IMAGE: False,
        Kind.COMPOSE: False,
        Kind.DATA: False,
    }


def test_the_asset_names_end_with_the_target_and_the_two_suffixes() -> None:
    names = asset_names("caregiver")

    assert names.archive == "caregiver-x86_64-unknown-linux-musl.tar.gz"
    assert names.bundle == "caregiver-x86_64-unknown-linux-musl.tar.gz.sigstore.json"


def test_the_limits_and_the_names_of_the_catalog() -> None:
    assert BINARY_TARGET == "x86_64-unknown-linux-musl"
    assert BINARY_INSTALL_ENV == "CARGO_INSTALL_ROOT"
    assert MAIN_BRANCH == "main"
    assert RELEASE_WORKFLOW == {Repo.AGENT_CONTROL: "release.yml", Repo.CHAPERONE: "release.yml"}
    assert ARCHIVE_MAX_BYTES == 256 * 1024 * 1024
    assert BUNDLE_MAX_BYTES == 1024 * 1024
    assert ARCHIVE_MAX_MEMBERS == 64
    assert ARCHIVE_MAX_UNPACKED_BYTES == 512 * 1024 * 1024


def test_check_p6_has_one_code_and_four_reasons() -> None:
    assert str(RefusalCode.P6) == "P6"
    assert [str(reason) for reason in ArtifactRefusal] == list(P6_REASONS)


def test_only_the_module_of_the_archive_holds_a_reason_of_check_p6() -> None:
    """Each other module imports the enum. A second copy of a text can
    drift, and the follower then reads a reason that no refusal gives."""
    source = REPO_ROOT / "handover" / "src" / "handover"
    texts = {path: path.read_text(encoding="utf-8") for path in source.rglob("*.py")}
    holders = {
        reason: sorted(
            str(path.relative_to(source)) for path, text in texts.items() if reason in text
        )
        for reason in P6_REASONS
    }

    assert holders == {reason: ["artifacts.py"] for reason in P6_REASONS}


def test_each_download_url_starts_with_the_one_code_host() -> None:
    """The download URL of an artifact and of an MCP server asset read one
    constant for the host of the code service."""
    url = download_url(OWNER, HOSTED, "caregiver-v1.2.3", asset_names("caregiver").archive)
    pin = ServerPin(
        source=Source.GITHUB_RELEASE,
        python="3.12",
        version="1.12.0",
        repo="github/github-mcp-server",
        asset="github-mcp-server_Linux_x86_64.tar.gz",
    )
    start = f"https://{CODE_HOST}/"

    assert url == (
        f"{start}{OWNER}/{HOSTED}/releases/download/caregiver-v1.2.3/"
        "caregiver-x86_64-unknown-linux-musl.tar.gz"
    )
    assert pin.asset_url().startswith(start)
    assert CODE_HOST == "github.com"
    assert TOKEN_ISSUER == "https://token.actions.githubusercontent.com"


@pytest.mark.parametrize(
    ("owner", "repo", "tag", "asset", "subject"),
    [
        ("-owner", HOSTED, "caregiver-v1.2.3", "a.tar.gz", "owner"),
        (OWNER, ".hidden", "caregiver-v1.2.3", "a.tar.gz", "repository"),
        (OWNER, HOSTED, "caregiver-1.2.3", "a.tar.gz", "tag"),
        (OWNER, HOSTED, "v1.2.3/../x", "a.tar.gz", "tag"),
        (OWNER, HOSTED, "caregiver-v1.2.3", "..", "asset"),
        (OWNER, HOSTED, "caregiver-v1.2.3", "a/b.tar.gz", "asset"),
        (OWNER, HOSTED, "caregiver-v1.2.3", "", "asset"),
    ],
)
def test_a_download_url_refuses_each_part_that_is_not_plain(
    owner: str, repo: str, tag: str, asset: str, subject: str
) -> None:
    refusal = _refusal(lambda: download_url(owner, repo, tag, asset))

    assert refusal.code is RefusalCode.REQUEST
    assert refusal.subject == subject


# ---- the signer and the words of the check ----------------------------------


def test_the_policy_names_the_workflow_of_the_map_and_the_main_ref() -> None:
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)

    assert (policy.owner, policy.repo, policy.workflow, policy.commit) == (
        OWNER,
        HOSTED,
        "release.yml",
        COMMIT,
    )
    assert policy.repository() == f"{OWNER}/{HOSTED}"
    assert policy.identity() == (
        f"https://github.com/{OWNER}/{HOSTED}/.github/workflows/release.yml@refs/heads/main"
    )


@pytest.mark.parametrize("repo", [Repo.AGENT_MCP, Repo.AGENT_REGISTRY])
def test_a_repository_with_no_release_workflow_has_no_policy(repo: Repo) -> None:
    refusal = _refusal(lambda: signer_policy(repo, OWNER, HOSTED, COMMIT))

    assert refusal.code is RefusalCode.CATALOG
    assert refusal.subject == str(repo)


@pytest.mark.parametrize(
    ("owner", "name", "commit", "subject"),
    [
        ("", HOSTED, COMMIT, "owner"),
        ("-owner", HOSTED, COMMIT, "owner"),
        ("owner-", HOSTED, COMMIT, "owner"),
        ("own--er", HOSTED, COMMIT, "owner"),
        ("own_er", HOSTED, COMMIT, "owner"),
        ("o" * 40, HOSTED, COMMIT, "owner"),
        ("owner\n", HOSTED, COMMIT, "owner"),
        (OWNER, "", COMMIT, "repository"),
        (OWNER, ".creche", COMMIT, "repository"),
        (OWNER, "-creche", COMMIT, "repository"),
        (OWNER, "cre/che", COMMIT, "repository"),
        (OWNER, "c" * 101, COMMIT, "repository"),
        (OWNER, "creche\n", COMMIT, "repository"),
        (OWNER, HOSTED, "", "commit"),
        (OWNER, HOSTED, COMMIT.upper(), "commit"),
        (OWNER, HOSTED, COMMIT[:-1], "commit"),
        (OWNER, HOSTED, f"{COMMIT}0", "commit"),
        (OWNER, HOSTED, f"{COMMIT[:-1]}g", "commit"),
        (OWNER, HOSTED, f"{COMMIT}\n", "commit"),
    ],
)
def test_each_refusal_of_a_signer_policy(owner: str, name: str, commit: str, subject: str) -> None:
    refusal = _refusal(lambda: signer_policy(Repo.AGENT_CONTROL, owner, name, commit))

    assert refusal.code is RefusalCode.REQUEST
    assert refusal.subject == subject


def test_the_longest_owner_and_name_make_a_policy() -> None:
    policy = signer_policy(Repo.AGENT_CONTROL, "o" * 39, "c" * 100, COMMIT)

    assert policy.repository() == f"{'o' * 39}/{'c' * 100}"


@pytest.mark.parametrize(
    ("change", "code", "subject"),
    [
        ({"_owner": "-bad"}, RefusalCode.REQUEST, "owner"),
        ({"_repo": ".creche"}, RefusalCode.REQUEST, "repository"),
        ({"_commit": "--x"}, RefusalCode.REQUEST, "commit"),
        ({"_workflow": "other.yml"}, RefusalCode.CATALOG, "workflow"),
    ],
)
def test_a_copy_of_a_policy_holds_the_rules_of_the_maker(
    change: dict[str, str], code: RefusalCode, subject: str
) -> None:
    """`dataclasses.replace` makes a policy through the type, and the type
    checks each field."""
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)

    refusal = _refusal(lambda: replace(policy, made=artifacts._MADE, **change))

    assert refusal.code is code
    assert refusal.subject == subject


def test_a_valid_copy_of_a_policy_is_a_policy() -> None:
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)
    other = "f" * 40

    assert replace(policy, made=artifacts._MADE, _commit=other).commit == other


@pytest.mark.parametrize("made", [None, artifacts._Made()])
def test_only_the_maker_makes_a_policy(made: object) -> None:
    """The private value of the module is the one key to the type."""
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)

    with pytest.raises(TypeError):
        replace(policy, made=made)


def test_the_words_without_a_trust_root() -> None:
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)

    assert verify_words(policy, ARCHIVE, BUNDLE, None) == WORDS


def test_the_words_with_a_trust_root() -> None:
    """The two words of the trust root follow the bundle, as the probe ran
    them."""
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)

    assert verify_words(policy, ARCHIVE, BUNDLE, TRUSTED_ROOT) == (
        *WORDS[:3],
        "--trusted-root",
        TRUSTED_ROOT,
        *WORDS[3:],
    )


@pytest.mark.parametrize(
    ("archive", "bundle", "trusted_root", "subject"),
    [
        ("assets/a.tar.gz", BUNDLE, None, "archive"),
        ("-a.tar.gz", BUNDLE, None, "archive"),
        ("", BUNDLE, None, "archive"),
        (f"{ARCHIVE}\n", BUNDLE, None, "archive"),
        (f"{ARCHIVE}\x00", BUNDLE, None, "archive"),
        (ARCHIVE, "a.tar.gz.sigstore.json", None, "bundle"),
        (ARCHIVE, "--bundle", None, "bundle"),
        (ARCHIVE, f"/srv/a\n{BUNDLE}", None, "bundle"),
        (ARCHIVE, f"/srv/a\x00{BUNDLE}", None, "bundle"),
        (ARCHIVE, BUNDLE, "trusted_root.json", "trusted root"),
        (ARCHIVE, BUNDLE, "", "trusted root"),
        (ARCHIVE, BUNDLE, f"{TRUSTED_ROOT}\x00", "trusted root"),
    ],
)
def test_the_words_refuse_a_path_that_is_not_absolute_or_not_one_line(
    archive: str, bundle: str, trusted_root: str | None, subject: str
) -> None:
    policy = signer_policy(Repo.AGENT_CONTROL, OWNER, HOSTED, COMMIT)

    refusal = _refusal(lambda: verify_words(policy, archive, bundle, trusted_root))

    assert refusal.code is RefusalCode.REQUEST
    assert refusal.subject == subject


def _words_argv(*extra: str) -> list[str]:
    return [
        "artifacts",
        "--verify-words",
        "--repo",
        "agent-control",
        "--github-repository",
        f"{OWNER}/{HOSTED}",
        "--sha",
        COMMIT,
        "--archive",
        ARCHIVE,
        "--bundle",
        BUNDLE,
        *extra,
    ]


def test_the_command_prints_one_word_on_each_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert _out(capsys, *_words_argv()) == (EXIT_OK, list(WORDS))

    code, lines = _out(capsys, *_words_argv("--trusted-root", TRUSTED_ROOT))

    assert code == EXIT_OK
    assert lines[3:5] == ["--trusted-root", TRUSTED_ROOT]


@pytest.mark.parametrize(
    ("argv", "check"),
    [
        (["--repo", "agent-mcp"], "catalog"),
        (["--github-repository", OWNER], "request"),
        (["--github-repository", f"{OWNER}/{HOSTED}/x"], "request"),
        (["--sha", COMMIT.upper()], "request"),
        (["--archive", "a.tar.gz"], "request"),
    ],
)
def test_the_command_refuses_what_the_policy_and_the_words_refuse(
    capsys: pytest.CaptureFixture[str], argv: list[str], check: str
) -> None:
    """A later value of an option replaces the first one."""
    code = main([*_words_argv(), *argv])

    assert code == EXIT_REFUSED
    assert capsys.readouterr().err.startswith(f"refused [{check}]")


# ---- the rows ---------------------------------------------------------------


def test_no_row_with_the_catalog_of_today(capsys: pytest.CaptureFixture[str]) -> None:
    assert _out(capsys, "artifacts", "--repo", "agent-control", "--root", str(REPO_ROOT)) == (
        EXIT_OK,
        [],
    )


def test_with_no_binary_row_no_manifest_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CI runner has no site file, and a manifest read needs one. With no
    binary component the command reads no manifest, so it needs no site."""
    monkeypatch.setenv(SITE_FILE_ENV, str(tmp_path / "absent.env"))

    for repo in (one for one in Repo if not artifacts.archive_components(one)):
        assert _out(capsys, "artifacts", "--repo", str(repo), "--root", str(REPO_ROOT)) == (
            EXIT_OK,
            [],
        )


#: The lines of the binary component of the fixture manifest.
ROW_HEAD = [
    f"component {BINARY_NAME}",
    f"target {BINARY_TARGET}",
    f"archive {BINARY_NAME}-{BINARY_TARGET}.tar.gz",
    f"bundle {BINARY_NAME}-{BINARY_TARGET}.tar.gz.sigstore.json",
    "install-env CARGO_INSTALL_ROOT",
    "max-archive-bytes 268435456",
    "max-members 64",
    "max-unpacked-bytes 536870912",
]


def test_one_row_with_a_catalog_that_holds_one_binary_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _binary_root(tmp_path, monkeypatch)

    code, lines = _out(capsys, "artifacts", "--repo", "agent-control", "--root", str(root))

    assert code == EXIT_OK
    assert lines == [
        *ROW_HEAD,
        "argv 6",
        "cargo",
        "install",
        "--locked",
        "--no-track",
        "--path",
        f"rust/crates/{BINARY_NAME}",
        "end",
    ]


def test_each_command_is_a_count_and_one_word_on_each_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A word can be empty or hold a space. The count says where a command
    ends, so a `read` loop needs no other mark."""
    second = '  - ["cargo", "", "a word"]\ninstall:'
    text = binary_manifest_text(tmp_path / "components").replace("install:", second, 1)
    root = _binary_root(tmp_path, monkeypatch, text)

    code, lines = _out(capsys, "artifacts", "--repo", "agent-control", "--root", str(root))

    assert code == EXIT_OK
    assert lines[len(ROW_HEAD) :] == [
        "argv 6",
        "cargo",
        "install",
        "--locked",
        "--no-track",
        "--path",
        f"rust/crates/{BINARY_NAME}",
        "argv 3",
        "cargo",
        "",
        "a word",
        "end",
    ]


def test_another_repository_has_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _binary_root(tmp_path, monkeypatch)

    assert _out(capsys, "artifacts", "--repo", "agent-mcp", "--root", str(root)) == (EXIT_OK, [])


def test_a_binary_row_with_no_manifest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_binary_catalog(monkeypatch)
    root = tmp_path / "root"
    write_manifest(root, "caregiver", manifest_text("caregiver"))

    code = main(["artifacts", "--repo", "agent-control", "--root", str(root)])

    assert code == EXIT_REFUSED
    assert capsys.readouterr().err.startswith(f"refused [catalog] {BINARY_NAME}:")


def test_a_binary_manifest_with_no_build_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = binary_manifest_text(tmp_path / "components").replace(BUILD_LINE, "install:")
    root = _binary_root(tmp_path, monkeypatch, text)

    code = main(["artifacts", "--repo", "agent-control", "--root", str(root)])

    assert code == EXIT_REFUSED
    assert "no build command" in capsys.readouterr().err


def test_a_manifest_of_another_kind_makes_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery refuses such a manifest first. `rows` holds the rule too,
    for a caller that reads the manifests another way."""
    use_binary_catalog(monkeypatch)
    text = binary_manifest_text(tmp_path / "components", kind=Kind.VENV)
    manifest = parse_manifest(text, "noticeboard/component.yaml", Markup.YAML)

    refusal = _refusal(lambda: rows({BINARY_NAME: manifest}, Repo.AGENT_CONTROL))

    assert refusal.code is RefusalCode.CATALOG
    assert refusal.subject == BINARY_NAME


@pytest.mark.parametrize("moving", ["RETIRING", "ARRIVING"])
def test_a_component_that_leaves_or_comes_into_the_catalog_has_no_row(
    monkeypatch: pytest.MonkeyPatch, moving: str
) -> None:
    use_binary_catalog(monkeypatch)
    monkeypatch.setattr(artifacts, moving, frozenset({BINARY_NAME}))

    assert artifacts.archive_components(Repo.AGENT_CONTROL) == ()
    assert rows({}, Repo.AGENT_CONTROL) == ()


# ---- one crate row ----------------------------------------------------------


def test_the_crate_row_has_the_shape_of_a_row(capsys: pytest.CaptureFixture[str]) -> None:
    code, lines = _out(
        capsys,
        "artifacts",
        "--crate-row",
        "agent-family",
        "--crate-dir",
        "rust/crates/agent-family",
    )

    assert code == EXIT_OK
    assert lines == [
        "component agent-family",
        f"target {BINARY_TARGET}",
        f"archive agent-family-{BINARY_TARGET}.tar.gz",
        f"bundle agent-family-{BINARY_TARGET}.tar.gz.sigstore.json",
        "install-env CARGO_INSTALL_ROOT",
        "max-archive-bytes 268435456",
        "max-members 64",
        "max-unpacked-bytes 536870912",
        "argv 6",
        "cargo",
        "install",
        "--locked",
        "--no-track",
        "--path",
        "rust/crates/agent-family",
        "end",
    ]


def test_a_crate_row_reads_no_manifest_and_no_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SITE_FILE_ENV, str(tmp_path / "absent.env"))

    row = crate_row("creche-probe", "rust/crates/creche-testkit")

    assert row.commands == (
        ("cargo", "install", "--locked", "--no-track", "--path", "rust/crates/creche-testkit"),
    )


def test_the_install_words_of_a_deeper_crate() -> None:
    assert install_words("rust/crates/group/one")[-1] == "rust/crates/group/one"


@pytest.mark.parametrize(
    "crate_dir",
    [
        "",
        "rust",
        "rust/crates",
        "rust/crates/",
        "rust/crates//x",
        "rust/crates/x/",
        "/rust/crates/x",
        "./rust/crates/x",
        "rust/crates/.",
        "rust/crates/..",
        "rust/crates/../crates/x",
        "rust/crates/a b",
        "rust/crates/x\n",
        "rust/crate/x",
        "crates/x",
        "Rust/crates/x",
        f"rust/crates/{'x' * 65}",
        "rust/crates/" + "/".join(["x"] * 200),
    ],
)
def test_each_refusal_of_the_install_words(crate_dir: str) -> None:
    refusal = _refusal(lambda: install_words(crate_dir))

    assert refusal.code is RefusalCode.REQUEST
    assert refusal.subject == "crate directory"


@pytest.mark.parametrize("name", ["", "a", "Agent", "-agent", "agent_family", "x" * 32, "agent\n"])
def test_a_crate_row_refuses_a_name_that_is_no_component_name(name: str) -> None:
    refusal = _refusal(lambda: crate_row(name, "rust/crates/agent-family"))

    assert refusal.code is RefusalCode.REQUEST
    assert refusal.subject == "crate row"


def test_a_row_reads_each_constant_of_the_catalog() -> None:
    """The target, the names, the variable and the limits are no field of
    a row, so no caller gives a row other values."""
    row = ArtifactRow(BINARY_NAME, (("cargo", "build"),))

    assert (row.target, row.names, row.install_env) == (
        BINARY_TARGET,
        asset_names(BINARY_NAME),
        BINARY_INSTALL_ENV,
    )
    assert (row.max_archive_bytes, row.max_members, row.max_unpacked_bytes) == (
        ARCHIVE_MAX_BYTES,
        ARCHIVE_MAX_MEMBERS,
        ARCHIVE_MAX_UNPACKED_BYTES,
    )


@pytest.mark.parametrize(
    ("component", "commands"),
    [
        ("a\nb", (("cargo",),)),
        ("Agent", (("cargo",),)),
        ("", (("cargo",),)),
        (BINARY_NAME, ()),
        (BINARY_NAME, ((),)),
        (BINARY_NAME, (("cargo",), ())),
        (BINARY_NAME, (("cargo", "a\nb"),)),
        (BINARY_NAME, (("cargo", "a\x00b"),)),
        (BINARY_NAME, (("cargo",), ("cargo", "x\n"))),
    ],
)
def test_a_row_refuses_what_its_line_form_cannot_hold(
    component: str, commands: tuple[tuple[str, ...], ...]
) -> None:
    refusal = _refusal(lambda: ArtifactRow(component, commands))

    assert refusal.code is RefusalCode.REQUEST
    assert refusal.subject == "artifact row"


def test_a_copy_of_a_row_holds_the_same_rules() -> None:
    row = crate_row("agent-family", "rust/crates/agent-family")

    refusal = _refusal(lambda: replace(row, component="a\nb"))

    assert refusal.subject == "artifact row"


def test_the_command_refuses_a_bad_crate_directory(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["artifacts", "--crate-row", "agent-family", "--crate-dir", "family"])

    assert code == EXIT_REFUSED
    assert capsys.readouterr().err.startswith("refused [request] crate directory:")


# ---- the forms of the command -----------------------------------------------


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["artifacts"], "needs --repo"),
        (["artifacts", "--repo", "agent-control", "--sha", COMMIT], "takes no --sha"),
        (["artifacts", "--crate-dir", "rust/crates/x"], "takes no --crate-dir"),
        (["artifacts", "--crate-row", "x-y"], "needs --crate-dir"),
        (
            [
                "artifacts",
                "--crate-row",
                "x-y",
                "--crate-dir",
                "rust/crates/x",
                "--repo",
                "agent-control",
            ],
            "takes no --repo",
        ),
        (["artifacts", "--verify-words", "--repo", "agent-control"], "needs --archive"),
        ([*_words_argv(), "--root", "."], "takes no --root"),
        ([*_words_argv(), "--crate-row", "x-y"], "takes no --crate-row"),
    ],
)
def test_each_form_takes_its_own_options_only(
    capsys: pytest.CaptureFixture[str], argv: list[str], said: str
) -> None:
    code = main(argv)

    assert code == EXIT_USAGE
    assert said in capsys.readouterr().err


def test_an_unknown_repository_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["artifacts", "--repo", "agent-elsewhere"])

    assert caught.value.code == EXIT_USAGE


# ---- the assets of a Release ------------------------------------------------


def test_no_component_of_today_but_chaperone_has_an_asset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for row in (one for one in CATALOG if one.name != "chaperone"):
        argv = ["allocate-tags", "--repo", str(row.repo), "--assets", row.name]

        assert _out(capsys, *argv) == (EXIT_OK, []), row.name


def test_a_binary_component_has_two_assets_with_their_limits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_binary_catalog(monkeypatch)
    names = asset_names(BINARY_NAME)

    code, lines = _out(capsys, "allocate-tags", "--repo", "agent-control", "--assets", BINARY_NAME)

    assert code == EXIT_OK
    assert lines == [f"{names.archive} 268435456", f"{names.bundle} 1048576"]


@pytest.mark.parametrize(
    ("repo", "name"),
    [("agent-control", "nobody"), ("agent-control", "mcp-servers"), ("agent-mcp", "chaperone")],
)
def test_a_name_that_the_catalog_of_the_repository_does_not_hold_is_refused(
    capsys: pytest.CaptureFixture[str], repo: str, name: str
) -> None:
    code = main(["allocate-tags", "--repo", repo, "--assets", name])

    assert code == EXIT_REFUSED
    assert capsys.readouterr().err.startswith("refused [catalog]")


@pytest.mark.parametrize(
    "extra",
    [
        ["--paths", "x"],
        ["--tags", "x"],
        ["--at-sha", "x"],
        ["--levels", "x"],
        ["--cut", "x"],
        ["--json"],
    ],
)
def test_the_assets_take_no_input_of_a_plan(
    capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    code = main(["allocate-tags", "--repo", "agent-control", "--assets", "chaperone", *extra])

    assert code == EXIT_USAGE
    assert "--assets takes no" in capsys.readouterr().err


# ---- the walk of a built tree -----------------------------------------------


def _check_tree(tree: Path, *needles: str) -> list[str]:
    argv = ["check-tree", "--kind", "binary", "--tree", str(tree)]
    for needle in needles:
        argv.extend(["--needle", needle])

    return argv


def test_a_clean_tree_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree = staged_binary_tree(tmp_path / "tree")

    assert _out(capsys, *_check_tree(tree, "/home/runner/work")) == (EXIT_OK, [])


def test_each_fault_is_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each needle counts. A link with an absolute target is a fault too."""
    tree = staged_binary_tree(tmp_path / "tree")
    write_program(tree / "bin" / "first", body=b"\x7fELF /home/runner/work/x\n")
    write_program(tree / "bin" / "second", body=b"\x7fELF /tmp/cargo-home/y\n")
    os.symlink("/usr/bin/true", tree / "bin" / "third")

    code, lines = _out(capsys, *_check_tree(tree, "/home/runner/work", "/tmp/cargo-home"))

    assert code == EXIT_REFUSED
    assert lines == [
        f"bin/first {NAMES_WORK_TREE}",
        f"bin/second {NAMES_WORK_TREE}",
        f"bin/third {ABSOLUTE_LINK}",
    ]


def test_no_program_list_is_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CI names no program: a tree with no program of the component passes."""
    tree = tmp_path / "tree"
    tree.mkdir()

    assert _out(capsys, *_check_tree(tree, "/home/runner/work")) == (EXIT_OK, [])


def test_a_tree_that_is_no_directory_is_one_fault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines = _out(capsys, *_check_tree(tmp_path / "absent", "/home/runner/work"))

    assert code == EXIT_REFUSED
    assert lines == ["absent is not a directory of its own"]


@pytest.mark.parametrize(
    "argv",
    [
        ["check-tree", "--kind", "binary", "--tree", "t"],
        ["check-tree", "--kind", "venv", "--tree", "t", "--needle", "/x"],
        ["check-tree", "--tree", "t", "--needle", "/x"],
        ["check-tree", "--kind", "binary", "--needle", "/x"],
        ["check-tree", "--kind", "binary", "--tree", "t", "--needle", "x"],
        ["check-tree", "--kind", "binary", "--tree", "t", "--needle", "/"],
        ["check-tree", "--kind", "binary", "--tree", "t", "--needle", "//"],
        ["check-tree", "--kind", "binary", "--tree", "t", "--needle", ""],
    ],
)
def test_a_walk_with_no_kind_no_tree_or_a_bad_needle_is_a_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(argv)

    assert caught.value.code == EXIT_USAGE


def test_the_catalog_of_the_fixture_is_restored() -> None:
    """`use_binary_catalog` lasts one test."""
    assert CATALOG_BY_NAME[BINARY_NAME].kind is Kind.VENV

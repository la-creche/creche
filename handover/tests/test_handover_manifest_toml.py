"""`component.toml`, the TOML form of a component manifest, as the release
tool reads it.

A directory holds `component.toml` or `component.yaml`. The tool reads
`component.toml` when the directory holds that name, and `component.yaml`
when it does not. A directory that holds both names is a refusal. The TOML
text goes through `handover.tomlfile` with the file kind `MANIFEST`. Each
check after that read is the check of the YAML form, with two rules of
TOML: `unit` is optional, and `release` is a boolean.

A live tree holds the stamp `.component.toml` or `.component.yaml`. The tool
reads the TOML stamp first, and it writes the stamp under the name of the
markup of the manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from handover.catalog import (
    CATALOG,
    CATALOG_BY_NAME,
    MANIFEST_CONTRACT_MAJOR,
    MANIFEST_CONTRACT_MINOR,
    Releases,
)
from handover.cli import EXIT_OK, EXIT_REFUSED, main
from handover.discovery import MAX_MANIFEST_FILES, discover, read_one
from handover.errors import Refusal, RefusalCode
from handover.executor.drain import Counter, handle
from handover.executor.live_state import (
    MANIFEST_STAMP,
    MANIFEST_STAMP_YAML,
    LiveTree,
    StampedText,
    installed_manifest,
    read_trees,
    write_manifest_stamp,
)
from handover.executor.spool import Spool
from handover.executor.steps import Wiring
from handover.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_FILENAME_YAML,
    ComponentManifest,
    Markup,
    parse_manifest,
)
from handover.silent import silent_manifest
from handover.tomlfile import BYTE_ORDER_MARK, DEPTH, NOT_TOML, NOT_UTF8, TOO_LARGE, FileKind
from handover_executor_fixtures import (
    REQUEST_ID,
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    grant_transport,
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from handover_fixtures import (
    manifest_text,
    manifest_toml,
    provides_entry,
    requires_entry,
    write_manifest,
)

#: Each set of arguments that the suite gives `manifest_text`. A run of the
#: suite that recorded each call gave this list. An argument with an empty
#: text is the default, so it is not in a set.
ARGUMENT_SETS: tuple[tuple[str, dict[str, str]], ...] = (
    ("attendance", {}),
    ("attendance", {"depends_on": "chaperone"}),
    (
        "attendance",
        {
            "depends_on": "chaperone",
            "provides": provides_entry("session-api", 1, 4),
            "requires": requires_entry("pep-grant", 2, 0),
        },
    ),
    ("attendance", {"depends_on": "chaperone", "requires": requires_entry("pep-grant", 2, 0)}),
    ("attendance", {"depends_on": "chaperone", "requires": requires_entry("pep-grant", 3, 0)}),
    ("attendance", {"provides": provides_entry("session-api", 1, 4)}),
    (
        "attendance",
        {
            "provides": provides_entry("session-api", 1, 4),
            "requires": requires_entry("pep-grant", 2, 0),
        },
    ),
    ("attendance", {"requires": requires_entry("pep-grant", 2, 0)}),
    ("attendance", {"requires": requires_entry("pep-grant", 2, 4)}),
    ("attendance", {"requires": requires_entry("pep-grant", 2, 9)}),
    ("attendance", {"requires": requires_entry("pep-grant", 3, 0)}),
    ("caregiver", {}),
    ("caregiver", {"depends_on": "chaperone"}),
    ("caregiver", {"provides": provides_entry("pep-grant", 2, 1)}),
    ("chaperone", {}),
    ("chaperone", {"depends_on": "attendance, infra"}),
    ("chaperone", {"depends_on": "chaperone"}),
    ("chaperone", {"depends_on": "noticeboard"}),
    ("chaperone", {"path": "attendance"}),
    ("chaperone", {"provides": provides_entry("pep-grant", 2, 1)}),
    ("chaperone", {"provides": provides_entry("pep-grant", 3, 0)}),
    ("handover", {}),
    ("infra", {}),
    ("mcp-servers", {}),
    ("noticeboard", {}),
    ("noticeboard", {"depends_on": "attendance"}),
    ("noticeboard", {"depends_on": "attendance", "requires": requires_entry("pep-grant", 2, 0)}),
    ("noticeboard", {"depends_on": "not-a-component"}),
    ("noticeboard", {"provides": provides_entry("pep-grant", 2, 1)}),
    ("noticeboard", {"provides": provides_entry("session-api", 9, 9)}),
    ("noticeboard", {"requires": requires_entry("manager-status", 1, 1)}),
    ("noticeboard", {"requires": requires_entry("pep-grant", 2, 0)}),
    ("noticeboard", {"requires": requires_entry("pep-grant", 3, 0)}),
    ("playpen", {}),
    ("registry-data", {}),
    ("registry-data", {"release": "yes"}),
)

SUBJECT = "chaperone/component.toml"

#: A text that the TOML reader refuses as no TOML 1.0: the escape `\e` of
#: TOML 1.1.
TOML_11_ESCAPE = 'name = "\\e"\n'


def _parse_toml(text: str, subject: str = SUBJECT) -> ComponentManifest:
    return parse_manifest(text, subject, Markup.TOML)


def _refusal(text: str) -> Refusal:
    with pytest.raises(Refusal) as caught:
        _parse_toml(text)

    return caught.value


def _chaperone(edits: dict[str, str]) -> str:
    """The TOML form of the `chaperone` fixture, with each line of `edits`
    replaced."""
    text = manifest_toml("chaperone")
    for old, new in edits.items():
        assert old in text
        text = text.replace(old, new, 1)

    return text


# -- the two forms of one manifest -------------------------------------------


@pytest.mark.parametrize(
    "argument_set",
    ARGUMENT_SETS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(ARGUMENT_SETS)],
)
def test_each_fixture_reads_to_one_manifest_in_both_forms(
    argument_set: tuple[str, dict[str, str]],
) -> None:
    name, arguments = argument_set

    from_yaml = parse_manifest(
        manifest_text(name, **arguments), f"{name}/component.yaml", Markup.YAML
    )
    from_toml = parse_manifest(
        manifest_toml(name, **arguments), f"{name}/component.toml", Markup.TOML
    )

    assert from_toml == from_yaml


def test_the_argument_sets_cover_each_component() -> None:
    assert {name for name, _ in ARGUMENT_SETS} == set(CATALOG_BY_NAME)


# -- the two rules of TOML ----------------------------------------------------


def test_an_absent_unit_means_no_unit() -> None:
    assert "unit" not in manifest_toml("chaperone")

    assert _parse_toml(manifest_toml("chaperone")).unit is None


def test_a_unit_reads_as_its_text() -> None:
    unit = 'unit = "creche-chaperone.service"\nruns_as = "operator"'
    text = _chaperone({'runs_as = "operator"': unit})

    assert _parse_toml(text).unit == "creche-chaperone.service"


def test_an_empty_unit_is_refused() -> None:
    refusal = _refusal(_chaperone({'runs_as = "operator"': 'unit = ""\nruns_as = "operator"'}))

    assert refusal.code is RefusalCode.MANIFEST
    assert "field 'unit'" in refusal.detail


def test_a_yaml_manifest_still_needs_its_unit_key() -> None:
    text = manifest_text("chaperone").replace("unit: null\n", "")

    with pytest.raises(Refusal) as caught:
        parse_manifest(text, "chaperone/component.yaml", Markup.YAML)

    assert caught.value.detail == "missing required field: unit"


@pytest.mark.parametrize(("word", "releases"), [("true", Releases.YES), ("false", Releases.NO)])
def test_release_is_a_boolean(word: str, releases: Releases) -> None:
    text = manifest_toml("chaperone").replace("release = true", f"release = {word}")

    assert _parse_toml(text).release is releases


@pytest.mark.parametrize("value", ['"yes"', '"no"', '"true"', "1"])
def test_a_release_that_is_no_boolean_is_refused(value: str) -> None:
    refusal = _refusal(_chaperone({"release = true": f"release = {value}"}))

    assert refusal.code is RefusalCode.MANIFEST
    assert refusal.detail == "field 'release' must be true or false"


def test_a_yaml_manifest_still_takes_a_release_word() -> None:
    for word in ('"yes"', "yes"):
        text = manifest_text("chaperone").replace('release: "yes"', f"release: {word}")

        assert parse_manifest(text, "c", Markup.YAML).release is Releases.YES


def test_a_missing_field_is_named() -> None:
    refusal = _refusal(_chaperone({'name = "chaperone"\n': ""}))

    assert refusal.detail == "missing required field: name"


def test_an_unknown_field_is_refused() -> None:
    refusal = _refusal(_chaperone({"secrets = []": "secrets = []\nunit_file = 'x'"}))

    assert refusal.detail == "unknown field: unit_file"


def test_a_date_time_is_no_text() -> None:
    refusal = _refusal(_chaperone({'manifest_version = "0.4"': "manifest_version = 1979-05-27"}))

    assert refusal.detail == "field 'manifest_version' must be a string"


# -- the TOML reader ----------------------------------------------------------


def test_the_byte_limit_is_the_limit_of_the_reader() -> None:
    """The TOML branch names no limit of its own: a text at the limit of
    `FileKind.MANIFEST` reads, and one byte more is the refusal of the
    reader."""
    base = manifest_toml("chaperone")
    limit = FileKind.MANIFEST.byte_limit
    padding = "#" * (limit - len(base.encode("utf-8")) - 1) + "\n"

    assert len((padding + base).encode("utf-8")) == limit
    assert _parse_toml(padding + base).name == "chaperone"

    refusal = _refusal("#" + padding + base)
    assert refusal.code is RefusalCode.MANIFEST
    assert refusal.subject == SUBJECT
    assert refusal.detail == TOO_LARGE.format(limit=limit)


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("\ufeff" + manifest_toml("chaperone"), BYTE_ORDER_MARK),
        ('name = "\ud800"\n', NOT_UTF8),
        ("name = [\n", NOT_TOML),
        (TOML_11_ESCAPE, NOT_TOML),
        ("a = " + "[" * (DEPTH + 1) + "]" * (DEPTH + 1) + "\n", NOT_TOML),
        ("a = 9223372036854775808\n", NOT_TOML),
    ],
    ids=["byte-order-mark", "lone-surrogate", "no-toml", "toml-1.1", "too-deep", "integer"],
)
def test_a_refusal_of_the_reader_is_one_refusal_with_its_text(text: str, detail: str) -> None:
    refusal = _refusal(text)

    assert refusal.code is RefusalCode.MANIFEST
    assert refusal.subject == SUBJECT
    assert refusal.detail == detail


# -- discovery ----------------------------------------------------------------


def _both_names(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_FILENAME).write_text(manifest_toml("chaperone"), encoding="utf-8")
    (directory / MANIFEST_FILENAME_YAML).write_text(manifest_text("chaperone"), encoding="utf-8")


def test_the_two_names() -> None:
    assert MANIFEST_FILENAME == "component.toml"
    assert MANIFEST_FILENAME_YAML == "component.yaml"
    assert Markup.TOML.file_name == MANIFEST_FILENAME
    assert Markup.YAML.file_name == MANIFEST_FILENAME_YAML


def test_a_walk_finds_a_toml_and_a_yaml_manifest(tmp_path: Path) -> None:
    write_manifest(tmp_path, "chaperone", manifest_toml("chaperone"), Markup.TOML)
    write_manifest(tmp_path, "attendance", manifest_text("attendance"))

    found = discover([tmp_path]).found

    assert found["chaperone"].markup is Markup.TOML
    assert found["chaperone"].subject == "chaperone/component.toml"
    assert found["chaperone"].text == manifest_toml("chaperone")
    assert found["attendance"].markup is Markup.YAML
    assert found["attendance"].subject == "attendance/component.yaml"


def test_both_names_in_one_directory_refuse_the_walk(tmp_path: Path) -> None:
    _both_names(tmp_path / "chaperone")

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert caught.value.code is RefusalCode.MANIFEST
    assert caught.value.subject == "chaperone"
    assert caught.value.detail == "holds component.toml and component.yaml; keep one"


def test_both_names_at_a_repository_root_refuse_the_walk(tmp_path: Path) -> None:
    _both_names(tmp_path)

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert caught.value.subject == "."


def test_a_link_counts_as_a_name(tmp_path: Path) -> None:
    """Each entry of a name counts, also a link to no file."""
    directory = tmp_path / "chaperone"
    directory.mkdir()
    (directory / MANIFEST_FILENAME).symlink_to(tmp_path / "nothing")
    (directory / MANIFEST_FILENAME_YAML).write_text(manifest_text("chaperone"), encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert "keep one" in caught.value.detail

    with pytest.raises(Refusal) as again:
        read_one(tmp_path, "chaperone")

    assert "keep one" in again.value.detail


@pytest.mark.parametrize("kind", ["directory", "link-to-a-directory"])
def test_a_directory_counts_as_a_name(tmp_path: Path, kind: str) -> None:
    directory = tmp_path / "chaperone"
    directory.mkdir()
    (directory / MANIFEST_FILENAME_YAML).write_text(manifest_text("chaperone"), encoding="utf-8")
    if kind == "directory":
        (directory / MANIFEST_FILENAME).mkdir()
    else:
        (tmp_path / "elsewhere").mkdir()
        (directory / MANIFEST_FILENAME).symlink_to(tmp_path / "elsewhere")

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert "keep one" in caught.value.detail

    with pytest.raises(Refusal) as again:
        read_one(tmp_path, "chaperone")

    assert "keep one" in again.value.detail


def test_both_names_refuse_read_one(tmp_path: Path) -> None:
    _both_names(tmp_path / "chaperone")

    with pytest.raises(Refusal) as caught:
        read_one(tmp_path, "chaperone")

    assert caught.value.code is RefusalCode.MANIFEST
    assert caught.value.detail == "holds component.toml and component.yaml; keep one"


@pytest.mark.parametrize("markup", list(Markup))
def test_read_one_reads_each_name(tmp_path: Path, markup: Markup) -> None:
    text = manifest_toml("chaperone") if markup is Markup.TOML else manifest_text("chaperone")
    write_manifest(tmp_path, "chaperone", text, markup)

    found = read_one(tmp_path, "chaperone")

    assert found.markup is markup
    assert found.subject == f"chaperone/{markup.file_name}"
    assert found.text == text
    assert found.manifest == parse_manifest(manifest_text("chaperone"), "c", Markup.YAML)


def test_read_one_names_both_names_when_neither_is_there(tmp_path: Path) -> None:
    (tmp_path / "chaperone").mkdir()

    with pytest.raises(Refusal) as caught:
        read_one(tmp_path, "chaperone")

    assert caught.value.code is RefusalCode.CATALOG
    assert "no component.toml or component.yaml under any root" in caught.value.detail


def test_a_symlinked_toml_manifest_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.toml"
    real.write_text(manifest_toml("chaperone"), encoding="utf-8")
    (tmp_path / "chaperone").mkdir()
    (tmp_path / "chaperone" / MANIFEST_FILENAME).symlink_to(real)

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert caught.value.code is RefusalCode.MANIFEST
    assert "is a symlink" in caught.value.detail


def test_a_large_toml_file_gets_the_text_of_the_reader(tmp_path: Path) -> None:
    padding = "#" * (FileKind.MANIFEST.byte_limit * 4) + "\n"
    write_manifest(tmp_path, "chaperone", padding + manifest_toml("chaperone"), Markup.TOML)

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert caught.value.subject == "chaperone/component.toml"
    assert caught.value.detail == TOO_LARGE.format(limit=FileKind.MANIFEST.byte_limit)


def test_the_reader_gets_the_bytes_of_the_file(tmp_path: Path) -> None:
    """A byte that is no UTF-8 and a CR with no LF each reach the reader as
    they are in the file."""
    directory = tmp_path / "chaperone"
    directory.mkdir()
    path = directory / MANIFEST_FILENAME
    path.write_bytes(b'name = "\xff"\n')

    with pytest.raises(Refusal) as caught:
        read_one(tmp_path, "chaperone")

    assert caught.value.detail == NOT_UTF8

    path.write_bytes(manifest_toml("chaperone").encode("utf-8").replace(b"\n", b"\r"))

    with pytest.raises(Refusal) as again:
        read_one(tmp_path, "chaperone")

    assert again.value.detail == NOT_TOML


def test_a_toml_file_with_crlf_line_ends_reads(tmp_path: Path) -> None:
    directory = tmp_path / "chaperone"
    directory.mkdir()
    text = manifest_toml("chaperone")
    (directory / MANIFEST_FILENAME).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    assert read_one(tmp_path, "chaperone").manifest == _parse_toml(text)


def test_the_cap_counts_both_names(tmp_path: Path) -> None:
    for index in range(MAX_MANIFEST_FILES + 2):
        directory = tmp_path / f"c{index:03d}"
        directory.mkdir()
        name = MANIFEST_FILENAME if index % 2 else MANIFEST_FILENAME_YAML
        (directory / name).write_text("name = 'chaperone'\n", encoding="utf-8")

    with pytest.raises(Refusal) as caught:
        discover([tmp_path])

    assert caught.value.code is RefusalCode.CATALOG
    assert caught.value.detail == "more than 32 component.toml and component.yaml files"


# -- the stamps of a live tree ------------------------------------------------


def test_the_two_stamp_names() -> None:
    assert MANIFEST_STAMP == ".component.toml"
    assert MANIFEST_STAMP_YAML == ".component.yaml"


@pytest.mark.parametrize("markup", list(Markup))
def test_the_stamp_has_the_name_of_its_markup(tmp_path: Path, markup: Markup) -> None:
    write_manifest_stamp(tmp_path, "the text\n", markup)

    names = {path.name for path in tmp_path.iterdir()}
    assert names == {f".{markup.file_name}"}
    assert installed_manifest(tmp_path) == StampedText("the text\n", markup)


def test_a_tree_with_both_stamps_reads_the_toml_one(tmp_path: Path) -> None:
    write_manifest_stamp(tmp_path, "the yaml text\n", Markup.YAML)
    write_manifest_stamp(tmp_path, "the toml text\n", Markup.TOML)

    assert installed_manifest(tmp_path) == StampedText("the toml text\n", Markup.TOML)


def test_a_toml_stamp_that_is_a_link_reads_as_absent(tmp_path: Path) -> None:
    """The TOML name decides when a tree holds it. A link there is no stamp,
    and the YAML stamp beside it is not read."""
    write_manifest_stamp(tmp_path, "the yaml text\n", Markup.YAML)
    real = tmp_path / "elsewhere"
    real.write_text("the toml text\n", encoding="utf-8")
    (tmp_path / MANIFEST_STAMP).symlink_to(real)

    assert installed_manifest(tmp_path) is None


def test_the_read_of_a_toml_stamp_stops_one_byte_past_the_limit(tmp_path: Path) -> None:
    write_manifest_stamp(tmp_path, "#" * (FileKind.MANIFEST.byte_limit * 4), Markup.TOML)

    stamp = installed_manifest(tmp_path)

    assert stamp is not None
    assert len(stamp.text.encode("utf-8")) == FileKind.MANIFEST.byte_limit + 1


def _trees(tmp_path: Path, name: str, text: str, markup: Markup) -> LiveTree:
    tree = tmp_path / name
    tree.mkdir(parents=True)
    write_manifest_stamp(tree, text, markup)

    return read_trees((tmp_path,))[name]


@pytest.mark.parametrize("markup", list(Markup))
def test_a_live_tree_reads_the_stamp_of_each_markup(tmp_path: Path, markup: Markup) -> None:
    text = manifest_toml("chaperone") if markup is Markup.TOML else manifest_text("chaperone")

    tree = _trees(tmp_path, "chaperone", text, markup)

    assert tree.manifest == parse_manifest(manifest_text("chaperone"), "c", Markup.YAML)


@pytest.mark.parametrize(
    "text",
    [
        "name = [\n",
        manifest_text("chaperone"),
        "#" * FileKind.MANIFEST.byte_limit + "\n" + manifest_toml("chaperone"),
        manifest_toml("attendance"),
    ],
    ids=["no-toml", "yaml-text", "too-large", "another-component"],
)
def test_a_toml_stamp_that_does_not_parse_reads_as_absent(tmp_path: Path, text: str) -> None:
    tree = _trees(tmp_path, "chaperone", text, Markup.TOML)

    assert tree.present
    assert tree.manifest is None


# -- the stamp of a release ---------------------------------------------------

LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"

#: The `build` entry of each markup, before `install`, so the release stages
#: something.
BUILD_LINES = {
    Markup.TOML: ("install.to", 'build = [["/usr/local/bin/uv", "sync", "--frozen"]]\ninstall.to'),
    Markup.YAML: ("install:", 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'),
}


def _staged_manifest(name: str, components: Path, markup: Markup) -> str:
    """A manifest whose install paths sit under `components` and whose
    `build` the fake child runs."""
    text = manifest_toml(name) if markup is Markup.TOML else manifest_text(name)
    text = text.replace("/opt/components", str(components))
    old, new = BUILD_LINES[markup]

    return text.replace(old, new, 1)


def _release(tmp_path: Path, markup: Markup) -> Path:
    """One release of `chaperone` from 2.0.3 to 2.1.0, whose manifest at the
    new tag has `markup`. Gives the live tree after the release."""
    components = tmp_path / "components"
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=grant_transport(),
        readers=fake_readers(latest={"chaperone": NEW_VERSION}),
        api=green_api("agent-control", f"chaperone-v{NEW_VERSION}", SHA_OF["chaperone"]),
    )
    stamp_tree(components, "chaperone", LIVE_VERSION)

    def write_tree(destination: Path, component: str) -> None:
        text = _staged_manifest(component, components, markup)
        write_manifest(destination, component, text, markup)

    def build(_: object) -> None:
        stamp_tree(components, "chaperone.new", NEW_VERSION)

    run.dynamic = GitFake(write_tree)
    run.hooks["sync"] = build
    write_request(spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))
    spool = Spool(str(spool_root), this_uid())
    try:
        assert "succeeded" in handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
    finally:
        spool.close()

    return components / "chaperone"


@pytest.mark.parametrize("markup", list(Markup))
def test_the_stamp_of_a_release_has_the_name_of_its_markup(tmp_path: Path, markup: Markup) -> None:
    tree = _release(tmp_path, markup)

    stamps = {path.name for path in tree.iterdir() if path.name.startswith(".component.")}
    assert stamps == {f".{markup.file_name}"}
    stamp = installed_manifest(tree)
    assert stamp is not None
    assert stamp.markup is markup
    assert stamp.text == _staged_manifest("chaperone", tmp_path / "components", markup)
    assert read_trees((tmp_path / "components",))["chaperone"].manifest is not None


# -- the commands that walk a checkout -----------------------------------------


def _checkout(tmp_path: Path, markup: Markup) -> list[str]:
    """One root with each component of the catalog, in one markup."""
    root = tmp_path / "repo"
    for row in CATALOG:
        text = manifest_toml(row.name) if markup is Markup.TOML else manifest_text(row.name)
        write_manifest(root, row.name, text, markup)

    return ["--root", str(root), "--partial"]


@pytest.mark.parametrize("markup", list(Markup))
def test_check_reads_a_checkout_of_each_markup(
    tmp_path: Path, markup: Markup, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["check", *_checkout(tmp_path, markup)])

    assert code == EXIT_OK
    assert "check: PASS" in capsys.readouterr().out


@pytest.mark.parametrize("markup", list(Markup))
def test_a_request_reads_a_checkout_of_each_markup(tmp_path: Path, markup: Markup) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    state = tmp_path / "live-state.json"
    state.write_text(
        '{"live": {"chaperone": "2.0.3"}, "provided": {}, "latest": {"chaperone": "2.1.0"},'
        ' "facts": {}}',
        encoding="utf-8",
    )
    argv = [*_checkout(tmp_path, markup), "--state", str(state), "--spool", str(spool)]

    assert main(["request", "chaperone", *argv, "--dry-run"]) == EXIT_OK


def test_check_refuses_a_checkout_with_both_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _checkout(tmp_path, Markup.TOML)
    write_manifest(tmp_path / "repo", "chaperone", manifest_text("chaperone"))

    assert main(["check", *argv]) == EXIT_REFUSED
    assert "keep one" in capsys.readouterr().err


# -- the manifest of a component that is not installed ------------------------

#: The YAML text that `silent.py` built before it built a TOML text. The two
#: texts give one manifest for each component.
SILENT_YAML = """
manifest_version: "{version}"
name: {name}
repo: {repo}
path: {path}
kind: {kind}
unit: null
runs_as: none
install:
  to: /opt/components/{name}
  prev: /opt/components/{name}.prev
provides: []
requires: []
depends_on: []
verify:
  command: ["/opt/components/{name}/bin/{name}-verify", "--json"]
  user: root
  timeout_s: 60
restore:
  mode: automatic
  keep: 1
secrets: []
release: no
"""


@pytest.mark.parametrize("name", sorted(CATALOG_BY_NAME))
def test_a_silent_manifest_is_the_manifest_of_its_yaml_text(name: str) -> None:
    row = CATALOG_BY_NAME[name]
    kind = "data" if str(row.kind) == "data" else "venv"
    fields: dict[str, Any] = {
        "version": f"{MANIFEST_CONTRACT_MAJOR}.{MANIFEST_CONTRACT_MINOR}",
        "name": name,
        "repo": row.repo,
        "path": row.path,
        "kind": kind,
    }
    text = SILENT_YAML.format(**fields)

    assert silent_manifest(name) == parse_manifest(text, f"{name} (not installed)", Markup.YAML)

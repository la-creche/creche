"""The frozen files: what the generator does with a file that no group writes.

A frozen file is a data file whose Python origin left the repository. The
index holds its SHA-256. Each test here gives the generator a directory of
its own and made surfaces. No test here builds the vectors.
`test_vectors_current.py` holds the map of the committed index.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from vectors import generate
from vectors.core import Surface, accepted, refused, render, text_input

#: A made surface that stands for a surface whose Python origin leaves.
LEAVES = Surface(
    name="made.leaves",
    path="made/leaves.json",
    entry="made.package.leaves",
    contract="no contract",
    vectors=(
        accepted("one", text_input("a"), "a"),
        refused("two", text_input(""), "empty"),
        refused("three", text_input(" "), "blank"),
    ),
)
#: A made surface whose Python origin stays.
STAYS = Surface(
    name="made.stays",
    path="made/stays.json",
    entry="made.package.stays",
    contract="no contract",
    vectors=(accepted("one", text_input("b"), "b"),),
)
#: A made surface whose path sorts after each other made path.
LAST = Surface(
    name="made.last",
    path="z.json",
    entry="made.package.last",
    contract="no contract",
    vectors=(refused("one", text_input("c"), "last"),),
)
#: A file of the generator that is no vector file. It has a `kind`.
OTHER_KIND = '{\n "format": 1,\n "kind": "registries",\n "files": []\n}\n'

WRONG_DIGEST = "0" * 64


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _groups(*surfaces: Surface) -> dict[str, Callable[[], tuple[Surface, ...]]]:
    return {surface.name: lambda surface=surface: (surface,) for surface in surfaces}


def _index(root: Path) -> dict[str, object]:
    return json.loads((root / generate.INDEX_FILE).read_text(encoding="utf-8"))


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory that holds both made surfaces, as the generator wrote them."""
    monkeypatch.setattr(generate, "GROUPS", _groups(LEAVES, STAYS))

    assert generate.main([], tmp_path) == generate.EXIT_OK

    return tmp_path


def test_a_frozen_file_with_its_digest_is_current() -> None:
    text = render(LEAVES)
    frozen = {LEAVES.path: _sha256(text)}

    assert generate.stale({}, {LEAVES.path: text}, (), frozen) == []
    assert generate.damaged(frozen, {LEAVES.path: text}) == []


def test_a_frozen_file_with_a_wrong_digest_differs() -> None:
    """The digest of the index is the one proof that nobody changed the file."""
    text = render(LEAVES)
    edited = text.replace('"blank"', '"empty"')

    assert edited != text

    for frozen, on_disk in (
        ({LEAVES.path: WRONG_DIGEST}, {LEAVES.path: text}),
        ({LEAVES.path: _sha256(text)}, {LEAVES.path: edited}),
        ({LEAVES.path: _sha256(text)}, {LEAVES.path: text + "\n"}),
        ({LEAVES.path: _sha256(text).upper()}, {LEAVES.path: text}),
    ):
        assert generate.stale({}, on_disk, (), frozen) == [f"differs: {LEAVES.path}"]
        assert generate.damaged(frozen, on_disk) == [f"differs: {LEAVES.path}"]


def test_a_frozen_file_that_is_absent_is_missing() -> None:
    frozen = {LEAVES.path: _sha256(render(LEAVES))}
    files = {STAYS.path: render(STAYS)}

    assert generate.stale(files, dict(files), (), frozen) == [f"missing: {LEAVES.path}"]
    assert generate.damaged(frozen, files) == [f"missing: {LEAVES.path}"]


def test_a_frozen_file_is_not_left_over_and_a_write_keeps_it(tmp_path: Path) -> None:
    text = render(LEAVES)
    files = {STAYS.path: render(STAYS)}
    frozen = {LEAVES.path: _sha256(text)}
    (tmp_path / "made").mkdir()
    (tmp_path / LEAVES.path).write_text(text, encoding="utf-8")
    (tmp_path / "made" / "old.json").write_text("{}\n", encoding="utf-8")

    removed = generate.write(files, tmp_path, frozen)

    on_disk = generate.committed(tmp_path)

    assert removed == ["made/old.json"]

    assert on_disk == {**files, LEAVES.path: text}
    assert generate.stale(files, on_disk, generate.strays(tmp_path), frozen) == []
    assert generate.stale(files, on_disk, generate.strays(tmp_path)) == [
        f"left over: {LEAVES.path}"
    ]


def test_a_write_refuses_a_frozen_file(tmp_path: Path) -> None:
    text = render(LEAVES)

    with pytest.raises(ValueError, match="is frozen"):
        generate.write({LEAVES.path: text}, tmp_path, {LEAVES.path: _sha256(text)})

    assert generate.committed(tmp_path) == {}


def test_the_index_holds_one_line_per_frozen_file_and_a_row_per_frozen_surface() -> None:
    on_disk = {LEAVES.path: render(LEAVES), "made/registries.json": OTHER_KIND}
    frozen = {path: _sha256(text) for path, text in on_disk.items()}
    files = {STAYS.path: render(STAYS)}

    rows = generate.index_rows((STAYS,), files, frozen, on_disk)
    text = generate.render_index(rows, frozen)

    assert rows == [
        generate.index_row(STAYS),
        {
            "surface": "made.leaves",
            "path": "made/leaves.json",
            "entry": "made.package.leaves",
            "vectors": 3,
            "accepted": 1,
            "refused": 2,
            "raised": 0,
        },
    ]
    assert json.loads(text) == {
        "format": 1,
        "frozen": frozen,
        "kind": "index",
        "surfaces": rows,
    }
    assert [line for line in text.splitlines() if '.json": "' in line] == [
        f'  "made/leaves.json": "{frozen["made/leaves.json"]}",',
        f'  "made/registries.json": "{frozen["made/registries.json"]}"',
    ]
    assert generate.frozen_of({generate.INDEX_FILE: text}) == frozen


def test_an_index_with_no_frozen_file_holds_an_empty_map() -> None:
    text = generate.render_index([generate.index_row(STAYS)], {})

    assert text.startswith('{\n "format": 1,\n "frozen": {},\n "kind": "index",\n "surfaces": [\n')
    assert generate.frozen_of({generate.INDEX_FILE: text}) == {}
    assert generate.frozen_of({}) == {}


def test_a_damaged_frozen_file_gets_no_row() -> None:
    """`stale` reports such a file. A row from changed bytes says nothing true."""
    text = render(LEAVES)

    for frozen, on_disk in (
        ({LEAVES.path: WRONG_DIGEST}, {LEAVES.path: text}),
        ({LEAVES.path: _sha256(text)}, {}),
    ):
        assert generate.index_rows((), {}, frozen, on_disk) == []


def test_a_group_that_writes_a_frozen_file_stops_the_build() -> None:
    text = render(LEAVES)
    frozen = {LEAVES.path: _sha256(text)}

    with pytest.raises(ValueError, match="a group writes the frozen files"):
        generate.index_rows((LEAVES,), {LEAVES.path: text}, frozen, {LEAVES.path: text})


def test_a_frozen_surface_with_the_name_of_a_built_surface_stops_the_build() -> None:
    text = render(LEAVES)
    twin = Surface(LEAVES.name, "made/twin.json", LEAVES.entry, LEAVES.contract, LEAVES.vectors)
    frozen = {LEAVES.path: _sha256(text)}

    with pytest.raises(ValueError, match="share one name"):
        generate.index_rows((twin,), {twin.path: render(twin)}, frozen, {LEAVES.path: text})


def _index_with(**members: object) -> str:
    """The JSON text of an index with no surface, with some members replaced."""
    return json.dumps({"format": 1, "frozen": {}, "kind": "index", "surfaces": []} | members)


def _index_text(frozen: object) -> str:
    return _index_with(frozen=frozen)


#: An index with two maps of frozen files, as a merge by hand can leave it.
TWO_MAPS = '{"format": 1, "frozen": {}, "frozen": {}, "kind": "index", "surfaces": []}'
#: An index whose map names one path two times.
TWO_LINES = (
    f'{{"format": 1, "frozen": {{"a.json": "{WRONG_DIGEST}", "a.json": "{WRONG_DIGEST}"}},'
    ' "kind": "index", "surfaces": []}'
)


#: Each path that the map can hold, by its form. The Rust reader of
#: `creche-vectors` has the same two tables in its tests.
FROZEN_PATHS = (
    "old.json",
    "runtime/old.json",
    "runtime/index.json",
    "runtime/a b.json",
    "runtime/.json",
    "runtime/..json",
)
#: Each form of a path that the map cannot hold.
NO_FROZEN_PATHS = (
    "",
    ".",
    "..",
    "index.json",
    "/etc/hosts.json",
    "//old.json",
    "../../Cargo.json",
    "runtime/../old.json",
    "./old.json",
    "runtime/./old.json",
    "runtime//old.json",
    "old.json/",
    "old.json/.",
    "runtime/old.txt",
    "runtime/old.JSON",
    "runtime/old.json\n",
    "runtime/old.json ",
    "runtime",
    "json",
)


def test_the_map_reads_each_path_of_a_json_file_below_the_root() -> None:
    frozen = dict.fromkeys(FROZEN_PATHS, WRONG_DIGEST)

    assert generate.frozen_of({"index.json": _index_text(frozen)}) == frozen


def test_the_map_refuses_a_path_in_another_form() -> None:
    for path in NO_FROZEN_PATHS:
        with pytest.raises(ValueError) as refused_read:
            generate.frozen_of({"index.json": _index_text({path: WRONG_DIGEST})})

        assert str(refused_read.value) == (
            f"index.json: {path!r} is no path of a frozen file; {generate.RESTORE_INDEX}"
        )


def test_the_map_refuses_a_path_with_a_lone_surrogate() -> None:
    """A strict JSON reader refuses the text of such an index. The Rust readers are strict."""
    for path in ("\ud800.json", "made/\udcff.json"):
        text = _index_text({path: WRONG_DIGEST})

        assert text.isascii()

        with pytest.raises(ValueError, match="is no path of a frozen file"):
            generate.frozen_of({"index.json": text})


@pytest.mark.parametrize(
    ("on_disk", "reason"),
    [
        ({"made/leaves.json": "{}\n"}, "index.json is absent"),
        ({"index.json": "{"}, "index.json is no JSON"),
        ({"index.json": "[]"}, "index.json is no JSON object"),
        ({"index.json": '{"format": 1, "kind": "index", "surfaces": []}'}, "has no `frozen` map"),
        ({"index.json": TWO_MAPS}, "index.json holds the key 'frozen' two times"),
        ({"index.json": TWO_LINES}, "index.json holds the key 'a.json' two times"),
        ({"index.json": _index_with(format=2)}, "index.json is no index of format 1"),
        ({"index.json": _index_with(format=True)}, "index.json is no index of format 1"),
        ({"index.json": _index_with(format=1.0)}, "index.json is no index of format 1"),
        ({"index.json": '{"frozen": {}, "kind": "index"}'}, "index.json is no index of format 1"),
        ({"index.json": _index_with(kind="registries")}, "index.json is no index of format 1"),
        ({"index.json": '{"format": 1, "frozen": {}}'}, "index.json is no index of format 1"),
        ({"index.json": _index_with(FROZEN={})}, "index.json holds another set of keys"),
        ({"index.json": _index_with(extra=1)}, "index.json holds another set of keys"),
        (
            {"index.json": '{"format": 1, "frozen": {}, "kind": "index"}'},
            "index.json holds another set of keys",
        ),
        ({"index.json": _index_text([])}, "`frozen` of index.json is no JSON object"),
        ({"index.json": _index_text({"a.json": "0" * 63})}, "is not 64 hexadecimal digits"),
        ({"index.json": _index_text({"a.json": "0" * 65})}, "is not 64 hexadecimal digits"),
        ({"index.json": _index_text({"a.json": "A" * 64})}, "is not 64 hexadecimal digits"),
        ({"index.json": _index_text({"a.json": "0" * 64 + "\n"})}, "is not 64 hexadecimal digits"),
        ({"index.json": _index_text({"a.json": 7})}, "is not 64 hexadecimal digits"),
    ],
)
def test_an_index_that_does_not_give_the_frozen_files_stops_the_generator(
    on_disk: dict[str, str], reason: str
) -> None:
    """With a wrong map, a write removes a frozen file as a left over file."""
    with pytest.raises(ValueError) as refused_read:
        generate.frozen_of(on_disk)

    message = str(refused_read.value)

    assert reason in message
    assert message.endswith(generate.RESTORE_INDEX)


def test_a_removal_freezes_a_file_and_the_check_then_passes(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The steps of a pull request that removes a Python package."""
    written = (tree / LEAVES.path).read_bytes()
    digest = hashlib.sha256(written).hexdigest()
    row = generate.index_row(LEAVES)

    assert _index(tree)["frozen"] == {}
    assert _index(tree)["surfaces"] == [row, generate.index_row(STAYS)]

    # The surface module of the package is gone.
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))
    capsys.readouterr()

    assert generate.main(["--check"], tree) == generate.EXIT_STALE
    assert capsys.readouterr().err.splitlines() == [
        f"left over: {LEAVES.path}",
        "differs: index.json",
    ]

    assert generate.main(["--freeze", LEAVES.path], tree) == generate.EXIT_OK
    assert (tree / LEAVES.path).read_bytes() == written
    assert _index(tree)["frozen"] == {LEAVES.path: digest}
    assert _index(tree)["surfaces"] == [generate.index_row(STAYS), row]

    capsys.readouterr()

    assert generate.main(["--check"], tree) == generate.EXIT_OK

    checked = capsys.readouterr()

    assert checked.out.splitlines() == [f"frozen: {LEAVES.path}"]
    assert checked.err == ""

    # A later run of the generator keeps the file and its line of the index.
    assert generate.main([], tree) == generate.EXIT_OK
    assert (tree / LEAVES.path).read_bytes() == written
    assert _index(tree)["frozen"] == {LEAVES.path: digest}
    assert generate.main(["--check"], tree) == generate.EXIT_OK

    capsys.readouterr()

    assert generate.main(["--counts"], tree) == generate.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        "made.stays: 1 (1 accepted)",
        "made.leaves: 3 (1 accepted, 2 refused)",
        "total: 4",
    ]


def test_the_index_holds_the_frozen_files_in_the_order_of_their_paths(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call names its files in each order. The index has one order."""
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))
    (tree / LAST.path).write_text(render(LAST), encoding="utf-8")

    assert LAST.path > LEAVES.path
    assert generate.main(["--freeze", LAST.path, LEAVES.path], tree) == generate.EXIT_OK

    text = (tree / generate.INDEX_FILE).read_text(encoding="utf-8")

    assert [line for line in text.splitlines() if '.json": "' in line] == [
        f'  "{LEAVES.path}": "{_sha256(render(LEAVES))}",',
        f'  "{LAST.path}": "{_sha256(render(LAST))}"',
    ]
    assert _index(tree)["surfaces"] == [
        generate.index_row(STAYS),
        generate.index_row(LEAVES),
        generate.index_row(LAST),
    ]


def test_a_run_with_no_flag_removes_and_names_a_file_with_no_group(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A removal must not make this run before it freezes its files."""
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))
    capsys.readouterr()

    assert generate.main([], tree) == generate.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        f"removed: {LEAVES.path}",
        "wrote 2 files, 1 vectors",
    ]
    assert not (tree / LEAVES.path).exists()
    assert _index(tree)["surfaces"] == [generate.index_row(STAYS)]


def test_a_freeze_writes_nothing_while_a_file_with_no_group_has_no_name(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A freeze of one file must not remove a second file of the package that leaves."""
    other = "made/other.json"
    (tree / other).write_text(OTHER_KIND, encoding="utf-8")
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))
    before = generate.committed(tree)
    capsys.readouterr()

    assert generate.main(["--freeze", LEAVES.path], tree) == generate.EXIT_STALE

    refused_freeze = capsys.readouterr()

    assert refused_freeze.err.splitlines() == [f"left over: {other}"]
    assert refused_freeze.out == ""
    assert generate.committed(tree) == before

    # With a name for each such file, the freeze writes and removes nothing.
    assert generate.main(["--freeze", LEAVES.path, other], tree) == generate.EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["wrote 2 files, 1 vectors"]
    assert generate.committed(tree).keys() == before.keys()
    assert _index(tree)["frozen"] == {
        LEAVES.path: _sha256(before[LEAVES.path]),
        other: _sha256(OTHER_KIND),
    }


def test_a_changed_frozen_file_fails_the_check_and_stops_a_write(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))

    assert generate.main(["--freeze", LEAVES.path], tree) == generate.EXIT_OK

    written = (tree / LEAVES.path).read_text(encoding="utf-8")
    index = (tree / generate.INDEX_FILE).read_bytes()
    (tree / LEAVES.path).write_text(written.replace('"blank"', '"empty"'), encoding="utf-8")
    (tree / STAYS.path).unlink()
    capsys.readouterr()

    assert generate.main(["--check"], tree) == generate.EXIT_STALE
    assert capsys.readouterr().err.splitlines() == [
        f"missing: {STAYS.path}",
        "differs: index.json",
        f"differs: {LEAVES.path}",
    ]

    # No group can write the frozen file again, so the generator writes nothing.
    assert generate.main([], tree) == generate.EXIT_STALE
    assert capsys.readouterr().err.splitlines() == [f"differs: {LEAVES.path}"]
    assert not (tree / STAYS.path).exists()
    assert (tree / generate.INDEX_FILE).read_bytes() == index

    (tree / LEAVES.path).unlink()

    assert generate.main([], tree) == generate.EXIT_STALE
    assert capsys.readouterr().err.splitlines() == [f"missing: {LEAVES.path}"]
    assert not (tree / STAYS.path).exists()

    (tree / LEAVES.path).write_text(written, encoding="utf-8")

    assert generate.main([], tree) == generate.EXIT_OK
    assert (tree / STAYS.path).exists()
    assert generate.main(["--check"], tree) == generate.EXIT_OK


def test_a_frozen_file_of_another_kind_gets_a_line_and_no_row(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tree / "made" / "registries.json").write_text(OTHER_KIND, encoding="utf-8")

    assert generate.main(["--freeze", "made/registries.json"], tree) == generate.EXIT_OK
    assert _index(tree)["frozen"] == {"made/registries.json": _sha256(OTHER_KIND)}
    assert _index(tree)["surfaces"] == [generate.index_row(LEAVES), generate.index_row(STAYS)]

    capsys.readouterr()

    assert generate.main(["--check"], tree) == generate.EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["frozen: made/registries.json"]


@pytest.mark.parametrize("name", [".json", "..json", "a.b.json", "a b.json"])
def test_the_map_can_name_each_json_file_that_the_generator_reads(tree: Path, name: str) -> None:
    """The generator reads each `*.json` file. Its next run must read the index of a freeze."""
    path = f"made/{name}"
    (tree / path).write_text(OTHER_KIND, encoding="utf-8")

    assert generate.main(["--freeze", path], tree) == generate.EXIT_OK
    assert _index(tree)["frozen"] == {path: _sha256(OTHER_KIND)}
    assert generate.main(["--check"], tree) == generate.EXIT_OK
    assert generate.main([], tree) == generate.EXIT_OK
    assert (tree / path).read_text(encoding="utf-8") == OTHER_KIND


def test_freeze_refuses_a_file_with_a_name_that_is_not_utf8(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python reads such a name as a text with a lone surrogate. No index can hold that path."""
    path = "made/\udcff.json"
    on_disk = {**generate.committed(tree), path: OTHER_KIND}
    index = (tree / generate.INDEX_FILE).read_bytes()

    def with_that_file(_root: Path) -> dict[str, str]:
        return on_disk

    monkeypatch.setattr(generate, "committed", with_that_file)

    with pytest.raises(ValueError, match="is no path of a frozen file"):
        generate.main(["--freeze", path], tree)

    assert (tree / generate.INDEX_FILE).read_bytes() == index


@pytest.mark.parametrize(
    ("paths", "reason"),
    [
        ([STAYS.path], "the generator writes made/stays.json"),
        (["index.json"], "the generator writes index.json"),
        (["made/absent.json"], "no JSON file made/absent.json"),
        (["vectors/data/made/leaves.json"], "give the path from vectors/data/"),
        ([LEAVES.path, LEAVES.path], "made/leaves.json is frozen already"),
        (["made/not-ascii.json"], "made/not-ascii.json is not ASCII"),
        (["made/list.json"], "made/list.json is no JSON object"),
        (["made/text.json"], "made/text.json is no JSON"),
        (["made/no-vectors.json"], "made/no-vectors.json is no vector file of format 1"),
        (["made/format-2.json"], "made/format-2.json is no vector file of format 1"),
        (["made/format-true.json"], "made/format-true.json is no vector file of format 1"),
        (["made/key-twice.json"], "made/key-twice.json holds the key 'kind' two times"),
        (["made/result.json"], "made/result.json: 'passed' is no result of a vector"),
    ],
)
def test_freeze_refuses_a_file_that_cannot_freeze(
    tree: Path, monkeypatch: pytest.MonkeyPatch, paths: list[str], reason: str
) -> None:
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))
    vector_file = json.loads(render(LEAVES))
    made = {
        "made/not-ascii.json": render(LEAVES).replace("blank", "é"),
        "made/list.json": "[]\n",
        "made/text.json": "frozen\n",
        "made/no-vectors.json": '{"format": 1, "surface": "made.x", "entry": "made.entry"}\n',
        "made/format-2.json": json.dumps({**vector_file, "format": 2}),
        "made/format-true.json": json.dumps({**vector_file, "format": True}),
        "made/key-twice.json": '{"format": 1, "kind": "registries", "kind": "registries"}\n',
        "made/result.json": json.dumps({**vector_file, "vectors": [{"result": "passed"}]}),
    }
    for path, text in made.items():
        (tree / path).write_text(text, encoding="utf-8")

    before = generate.committed(tree)

    with pytest.raises(ValueError) as refused_freeze:
        generate.main(["--freeze", *paths], tree)

    assert reason in str(refused_freeze.value)
    assert generate.committed(tree) == before


def test_a_frozen_file_does_not_freeze_a_second_time(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second freeze can give a changed file a new digest."""
    monkeypatch.setattr(generate, "GROUPS", _groups(STAYS))

    assert generate.main(["--freeze", LEAVES.path], tree) == generate.EXIT_OK

    with pytest.raises(ValueError, match="is frozen already"):
        generate.main(["--freeze", LEAVES.path], tree)


@pytest.mark.parametrize(
    "argv",
    [["--check", "--counts"], ["--check", "--freeze", "a.json"], ["--freeze"]],
)
def test_the_generator_has_one_mode_at_a_time(tree: Path, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as usage:
        generate.main(argv, tree)

    assert usage.value.code == 2

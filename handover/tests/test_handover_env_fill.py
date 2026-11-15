"""The env fill (`executor/envfill.py`, `handover/AGENTS.md` "The env fill").

Two halves. Step 4 reads the env file of a unit and plans what to add, and
each fault refuses the release before the push. Step 8, after the tap and
before any swap, appends each key that is still absent and changes no line.

The catalog is the real one of `env/`, copied into the clone the way a
clone of the release commit holds it. The site file is the example site of
the suite (`conftest.py`).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME
from handover.errors import Refusal, RefusalCode
from handover.executor import envfill
from handover.executor.approval import Decision, Summary, Verdict
from handover.executor.drain import Counter, handle
from handover.executor.envfill import (
    CHANGED,
    EMPTY_LINE,
    FLEET_UP,
    LINK,
    NO_CATALOG,
    NOT_OPERATORS,
    NOT_PARSED,
    NOT_REGULAR,
    OPEN_TO_OTHERS,
    UNRESOLVED,
    Fill,
    Verb,
    append_fill,
    plan_fill,
)
from handover.executor.live_state import installed_version
from handover.executor.notice import Notice
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover.manifest import EnvDecl
from handover_executor_fixtures import (
    REQUEST_ID,
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from handover_fixtures import manifest_text

COMPONENT = "caregiver"
KEY = "VMM_GIT_SPOOL_ROOT"
VALUE = "/var/lib/creche-git-platform"
PRESENT = "VMM_MIRROR_ROOT"

#: A key whose default refers to the LAN address of the site file.
SITE_KEY = "VMM_LITELLM_ADDR"
SITE_VALUE = "192.0.2.10:4000"

#: A catalog key with no default, and a key outside the catalog.
NO_DEFAULT = "AGENT_PLATFORM_FAMILIES"
NOT_IN_CATALOG = "VMM_NO_SUCH_KEY"

HEADER = "# Written by bin/fleet-up, mode 0600. fleet-up owns each key below.\n"
LIVE_LINES = f"{HEADER}{PRESENT}=/srv/other\n"

REPO = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO.joinpath(*envfill.CATALOG_PARTS)

#: A `build` entry, so the executor stages the tree.
BUILD = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]'

LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"


def _clone(root: Path) -> Path:
    """A clone of the release commit, with the catalog of `env/`."""
    target = root.joinpath(*envfill.CATALOG_PARTS)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("default.env", "example.env"):
        shutil.copyfile(CATALOG_DIR / name, target / name)

    return root


def _env_file(path: Path, text: str = LIVE_LINES, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    path.chmod(mode)

    return path


def _decl(path: Path, *keys: str) -> EnvDecl:
    return EnvDecl(file=str(path), keys=keys or (PRESENT, KEY))


def _refused(call: Callable[[], Fill]) -> Refusal:
    with pytest.raises(Refusal) as caught:
        call()

    assert caught.value.code is RefusalCode.ENV
    assert caught.value.subject == COMPONENT

    return caught.value


def _detail(template: str, path: Path, **more: str) -> str:
    words = {"component": COMPONENT, "file": str(path), "fix": FLEET_UP} | more

    return template.format_map(words)


# -- step 4: the plan -----------------------------------------------------


def test_a_file_that_holds_each_key_has_nothing_to_add(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env", f"{LIVE_LINES}{KEY}=/srv/x\n")

    fill = plan_fill(COMPONENT, _decl(path), _clone(tmp_path / "clone"), os.getuid())

    assert fill.adds == ()
    assert fill.line(Verb.ADDS) == f"env {COMPONENT}: nothing to add to {path}"


def test_an_absent_key_takes_its_value_from_the_catalog(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env")

    fill = plan_fill(COMPONENT, _decl(path), _clone(tmp_path / "clone"), os.getuid())

    assert fill.adds == ((KEY, VALUE),)
    assert fill.line(Verb.ADDS) == f"env {COMPONENT}: adds {KEY} to {path}"
    assert path.read_text(encoding="ascii") == LIVE_LINES


def test_a_value_resolves_against_the_site_file(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env")

    fill = plan_fill(COMPONENT, _decl(path, SITE_KEY), _clone(tmp_path / "clone"), os.getuid())

    assert fill.adds == ((SITE_KEY, SITE_VALUE),)


@pytest.mark.parametrize("key", [NO_DEFAULT, NOT_IN_CATALOG])
def test_a_key_with_no_value_refuses(tmp_path: Path, key: str) -> None:
    path = _env_file(tmp_path / "vmm.env")
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path, key), clone, os.getuid()))

    assert refusal.detail == _detail(UNRESOLVED, path, key=key)


def test_a_missing_file_refuses_and_names_fleet_up(tmp_path: Path) -> None:
    path = tmp_path / "vmm.env"
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == (
        f"{COMPONENT}: {path} does not exist. As the operator, run /opt/creche/bin/fleet-up up"
    )


def test_a_file_of_another_account_refuses(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env")
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid() + 1))

    assert refusal.detail == _detail(NOT_OPERATORS, path)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
def test_a_file_open_to_group_or_other_refuses(tmp_path: Path, mode: int) -> None:
    path = _env_file(tmp_path / "vmm.env", mode=mode)
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(OPEN_TO_OTHERS, path)


def test_a_link_refuses_and_its_target_stays(tmp_path: Path) -> None:
    target = _env_file(tmp_path / "real.env")
    path = tmp_path / "vmm.env"
    path.symlink_to(target)
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(LINK, path)
    assert target.read_text(encoding="ascii") == LIVE_LINES


def test_a_directory_refuses(tmp_path: Path) -> None:
    path = tmp_path / "vmm.env"
    path.mkdir(mode=0o700)
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(NOT_REGULAR, path)


def test_a_line_with_no_value_refuses(tmp_path: Path) -> None:
    """The fill changes no line, and a second line for one key makes the
    reader of the vmm refuse the file."""
    path = _env_file(tmp_path / "vmm.env", f"{LIVE_LINES}{KEY}=\n")
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(EMPTY_LINE, path, key=KEY)


def test_a_file_that_does_not_parse_refuses_and_names_no_value(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env", f"{LIVE_LINES}{PRESENT}=/srv/again\n")
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    problem = f"{path}:3: duplicate-key {PRESENT}"
    assert refusal.detail == _detail(NOT_PARSED, path, problem=problem)
    assert "/srv/again" not in refusal.detail


def test_a_catalog_behind_a_link_refuses(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env")
    real = _clone(tmp_path / "real")
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "env").symlink_to(real / "env")

    refusal = _refused(lambda: plan_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(NO_CATALOG, clone / "env")


# -- step 8: the append ---------------------------------------------------


def test_the_append_adds_only_the_absent_keys(tmp_path: Path) -> None:
    """The present line keeps its value, also one that the catalog would
    not give."""
    path = _env_file(tmp_path / "vmm.env")

    fill = append_fill(
        COMPONENT, _decl(path, PRESENT, KEY, SITE_KEY), _clone(tmp_path / "clone"), os.getuid()
    )

    assert fill.adds == ((KEY, VALUE), (SITE_KEY, SITE_VALUE))
    assert fill.line(Verb.WROTE) == f"env {COMPONENT}: wrote {KEY}, {SITE_KEY} to {path}"
    added = f"{KEY}={VALUE}\n{SITE_KEY}={SITE_VALUE}\n"
    assert path.read_text(encoding="ascii") == f"{LIVE_LINES}{added}"
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_second_append_writes_nothing(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env")
    clone = _clone(tmp_path / "clone")
    append_fill(COMPONENT, _decl(path), clone, os.getuid())
    after = path.read_bytes()

    fill = append_fill(COMPONENT, _decl(path), clone, os.getuid())

    assert fill.adds == ()
    assert path.read_bytes() == after


def test_a_file_with_no_last_line_end_gets_one_before_the_new_line(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env", f"{HEADER}{PRESENT}=/srv/other")

    append_fill(COMPONENT, _decl(path), _clone(tmp_path / "clone"), os.getuid())

    assert path.read_text(encoding="ascii") == f"{LIVE_LINES}{KEY}={VALUE}\n"


def test_an_empty_file_takes_the_new_lines(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env", "")

    append_fill(COMPONENT, _decl(path, KEY), _clone(tmp_path / "clone"), os.getuid())

    assert path.read_text(encoding="ascii") == f"{KEY}={VALUE}\n"


def test_the_append_has_the_checks_of_the_plan(tmp_path: Path) -> None:
    path = _env_file(tmp_path / "vmm.env", mode=0o644)
    clone = _clone(tmp_path / "clone")

    refusal = _refused(lambda: append_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(OPEN_TO_OTHERS, path)
    assert path.read_text(encoding="ascii") == LIVE_LINES


def test_a_file_that_grows_after_the_read_takes_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root checks the descriptor again just before the write."""
    path = _env_file(tmp_path / "vmm.env")
    clone = _clone(tmp_path / "clone")
    real = envfill._adds  # pyright: ignore[reportPrivateUsage]

    def and_another_writer(
        component: str, decl: EnvDecl, root: Path, data: bytes
    ) -> tuple[tuple[str, str], ...]:
        found = real(component, decl, root, data)
        with path.open("a", encoding="ascii") as file:
            file.write("VMM_GIT_PROGRAM=/usr/bin/git\n")

        return found

    monkeypatch.setattr(envfill, "_adds", and_another_writer)

    refusal = _refused(lambda: append_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(CHANGED, path)
    assert KEY not in path.read_text(encoding="ascii")


def test_a_mode_that_opens_after_the_read_takes_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _env_file(tmp_path / "vmm.env")
    clone = _clone(tmp_path / "clone")
    real = envfill._adds  # pyright: ignore[reportPrivateUsage]

    def and_a_chmod(
        component: str, decl: EnvDecl, root: Path, data: bytes
    ) -> tuple[tuple[str, str], ...]:
        found = real(component, decl, root, data)
        path.chmod(0o644)

        return found

    monkeypatch.setattr(envfill, "_adds", and_a_chmod)

    refusal = _refused(lambda: append_fill(COMPONENT, _decl(path), clone, os.getuid()))

    assert refusal.detail == _detail(OPEN_TO_OTHERS, path)
    assert KEY not in path.read_text(encoding="ascii")


# -- the fill inside a release --------------------------------------------


@dataclass
class Bench:
    """One wired executor that releases `caregiver`, whose manifest names
    an env file, and what the phone saw."""

    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path
    env: Path
    #: The bytes of the env file each time the phone was asked.
    asked: list[bytes]
    notices: list[Notice]

    def ledger(self) -> dict[str, object]:
        loaded: object = json.loads(
            (self.spool_root / DONE_DIR / f"{REQUEST_ID}.json").read_text(encoding="utf-8")
        )
        assert isinstance(loaded, dict)

        return cast("dict[str, object]", loaded)


def _manifest(components: Path, name: str, env: Path) -> str:
    text = manifest_text(name).replace("/opt/components", str(components))
    text = text.replace("install:", f"{BUILD}\ninstall:")
    if name != COMPONENT:
        return text

    return f"{text}env:\n  file: {env}\n  keys: [{PRESENT}, {KEY}]\n"


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    components = tmp_path / "components"
    env = _env_file(tmp_path / "state" / "vmm.env")
    asked: list[bytes] = []
    notices: list[Notice] = []

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, summary, wait_s
        asked.append(env.read_bytes())

        return Decision(Verdict.GRANTED, gate, 1758153712.0)

    def notify(notice: Notice) -> bool:
        notices.append(notice)

        return True

    wiring = Wiring(
        host=host,
        transport=transport,
        readers=fake_readers(latest={COMPONENT: NEW_VERSION}),
        api=green_api("creche", f"{COMPONENT}-v{NEW_VERSION}", SHA_OF[COMPONENT]),
        notify=notify,
    )
    stamp_tree(components, COMPONENT, LIVE_VERSION)
    manifests = {row.name: _manifest(components, row.name, env) for row in CATALOG}

    def write_tree(destination: Path, component: str) -> None:
        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(manifests[component], encoding="utf-8")
        _clone(destination)

    run.dynamic = GitFake(write_tree)
    run.hooks["sync"] = lambda _: stamp_tree(components, f"{COMPONENT}.new", NEW_VERSION)

    return Bench(spool_root, wiring, run, components, env, asked, notices)


def _release(bench: Bench) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body({COMPONENT: NEW_VERSION}))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


def _log(entry: dict[str, object]) -> list[str]:
    return [str(line) for line in cast("list[object]", entry["log_tail"])]


def test_a_release_adds_the_key_after_the_tap(bench: Bench) -> None:
    result = _release(bench)

    assert "succeeded" in result
    assert bench.asked == [LIVE_LINES.encode()]
    assert bench.env.read_text(encoding="ascii") == f"{LIVE_LINES}{KEY}={VALUE}\n"
    log = _log(bench.ledger())
    assert f"env {COMPONENT}: adds {KEY} to {bench.env}" in log
    assert f"env {COMPONENT}: wrote {KEY} to {bench.env}" in log
    assert installed_version(bench.components / COMPONENT) == NEW_VERSION


def test_a_missing_env_file_refuses_before_the_push(bench: Bench) -> None:
    bench.env.unlink()

    result = _release(bench)

    assert "refused" in result
    assert bench.asked == []
    entry = bench.ledger()
    assert entry["refused_check"] == "env"
    assert entry["reason"] == (
        f"{COMPONENT}: {bench.env} does not exist. As the operator, run {FLEET_UP}"
    )
    steps = cast("list[dict[str, object]]", entry["steps"])
    assert [step["name"] for step in steps][-1] == "contracts"
    assert bench.notices[0].message == (
        f"Release failed at {COMPONENT} release.\nRolled back components: none"
    )
    assert installed_version(bench.components / COMPONENT) == LIVE_VERSION


def test_a_denied_tap_writes_no_key(bench: Bench) -> None:
    def deny(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, summary, wait_s

        return Decision(Verdict.DENIED, gate, 1758153712.0)

    bench.wiring = replace(bench.wiring, transport=deny)

    _release(bench)

    assert bench.env.read_text(encoding="ascii") == LIVE_LINES
    assert bench.notices[0].message == (
        "Release failed at approve step.\nRolled back components: none"
    )


def test_a_failed_stage_writes_no_key(bench: Bench) -> None:
    bench.run.fails["sync"] = 1

    _release(bench)

    assert bench.env.read_text(encoding="ascii") == LIVE_LINES
    assert bench.notices[0].message == (
        f"Release failed at {COMPONENT} release.\nRolled back components: none"
    )


def test_a_restore_keeps_the_added_key(bench: Bench) -> None:
    """The previous program reads only the keys that it names."""
    calls = {"n": 0}
    bench.run.fails["caregiver-verify"] = 1

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop("caregiver-verify", None)

    bench.run.hooks["caregiver-verify"] = pass_after_the_first_call

    _release(bench)

    assert bench.ledger()["status"] == "restored"
    assert bench.env.read_text(encoding="ascii") == f"{LIVE_LINES}{KEY}={VALUE}\n"
    assert bench.notices[0].message == (
        f"Release failed at {COMPONENT} release.\nRolled back components: {COMPONENT}"
    )

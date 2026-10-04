"""One `kind: binary` component, through all ten steps.

The other `kind: binary` modules drive one function each. This one files a
request and lets the executor run: resolve, provenance, approval, stage,
switch, verify and the ledger. It holds four ends.

1. A good release lands: the tree is swapped, the unit restarts, the hook
   runs, and the ledger says `succeeded`.
2. A tree that is not self-contained is refused with the code `editable`.
3. A unit that starts a program outside the tree is refused with the code
   `unit`, and the build never runs.
4. Both refusals leave the live tree in service and no staged tree behind.

The catalog is not changed: the executor reads the kind from the manifest
that the fetched tree carries.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME
from handover.executor.approval import Decision, Summary
from handover.executor.drain import Counter, handle
from handover.executor.install import BINARY_ENV_NAME, RELOCATABLE_ARGV
from handover.executor.live_state import MANIFEST_STAMP, installed_version
from handover.executor.selfcontained import ABSOLUTE_LINK, NOT_STAGED
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover_bin_fixtures import (
    BINARY_NAME,
    CARGO,
    PROGRAM_BYTES,
    UNIT,
    binary_manifest_text,
    staged_binary_tree,
    unit_text,
    write_program,
)
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
from handover_fixtures import manifest_text

LIVE_VERSION = "0.4.0"
NEW_VERSION = "0.5.0"

#: What the live tree's program holds, so a test can say which tree is live.
OLD_PROGRAM = b"the previous program\n"

#: A program in no tree that a release installs.
ELSEWHERE = "/opt/creche/.venv/bin/noticeboard"


@dataclass
class Bench:
    """One wired executor, about to release the binary component."""

    tmp_path: Path
    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path

    def tree(self) -> Path:
        return self.components / BINARY_NAME

    def staged(self) -> Path:
        return self.components / f"{BINARY_NAME}.new"

    def program(self, name: str = BINARY_NAME) -> str:
        return str(self.tree() / "bin" / name)

    def ledger(self) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{REQUEST_ID}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def reason(self) -> str:
        return str(self.ledger().get("reason", ""))

    def install_unit(self, text: str) -> None:
        (self.tmp_path / "system-units" / UNIT).write_text(text, encoding="utf-8")

    def release(self) -> str:
        write_request(self.spool_root, REQUEST_ID, request_body({BINARY_NAME: NEW_VERSION}))
        spool = Spool(str(self.spool_root), this_uid())
        try:
            return handle(spool, f"{REQUEST_ID}.json", self.wiring, Counter())
        finally:
            spool.close()


def _serve(bench: Bench, carried_unit: str | None) -> None:
    """What the fake `git clone` leaves in the fetched tree: every
    manifest, with the binary component's saying `kind: binary`, and the
    unit file that the release carries."""

    def write_tree(destination: Path, name: str) -> None:
        # Every clone holds the whole repository, whichever component it
        # was fetched for.
        del name
        for row in CATALOG:
            text = manifest_text(row.name).replace("/opt/components", str(bench.components))
            if row.name == BINARY_NAME:
                text = binary_manifest_text(bench.components, UNIT)

            sub = destination if row.path == "." else destination / row.path
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "component.yaml").write_text(text, encoding="utf-8")

        if carried_unit is not None:
            (destination / "systemd").mkdir(parents=True, exist_ok=True)
            (destination / "systemd" / UNIT).write_text(carried_unit, encoding="utf-8")

    bench.run.dynamic = GitFake(write_tree)


def _make_bench(
    tmp_path: Path,
    made: Callable[[Path], object] = staged_binary_tree,
    carried_unit: str | None = None,
) -> Bench:
    """The component live at 0.4.0 with 0.5.0 the latest tag. The build
    argv leaves what `made` writes at `<install.to>.new`."""
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=grant_transport(),
        readers=fake_readers(latest={BINARY_NAME: NEW_VERSION}),
        api=green_api(
            str(CATALOG_BY_NAME[BINARY_NAME].repo),
            f"{BINARY_NAME}-v{NEW_VERSION}",
            SHA_OF[BINARY_NAME],
        ),
    )
    bench = Bench(
        tmp_path=tmp_path,
        spool_root=make_spool_dirs(tmp_path),
        wiring=wiring,
        run=run,
        components=tmp_path / "components",
    )
    stamp_tree(bench.components, BINARY_NAME, LIVE_VERSION)
    write_program(bench.tree() / "bin" / BINARY_NAME, body=OLD_PROGRAM)
    run.hooks[CARGO] = lambda _: made(bench.staged())
    _serve(bench, carried_unit)

    return bench


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    """A release whose unit is installed and starts the component's tree."""
    made = _make_bench(tmp_path)
    made.install_unit(unit_text(made.program()))

    return made


# ---- a release that lands ----------------------------------------------------


def test_a_binary_component_releases(bench: Bench) -> None:
    assert "succeeded" in bench.release(), bench.reason()

    assert installed_version(bench.tree()) == NEW_VERSION
    assert (bench.tree() / "bin" / BINARY_NAME).read_bytes() == PROGRAM_BYTES
    assert not bench.staged().exists()


def test_the_previous_tree_is_kept(bench: Bench) -> None:
    """Contract 06 §5: one previous artifact stays, for a restore."""
    bench.release()
    previous = bench.components / f"{BINARY_NAME}.prev"

    assert installed_version(previous) == LIVE_VERSION
    assert (previous / "bin" / BINARY_NAME).read_bytes() == OLD_PROGRAM


def test_the_release_runs_the_build_then_the_unit_then_the_hook(bench: Bench) -> None:
    """The order of step 8 and step 9, for a tree of compiled programs."""
    bench.release()
    lines = bench.run.argv_lines()
    build = next(index for index, line in enumerate(lines) if CARGO in line)
    restart = next(index for index, line in enumerate(lines) if f"restart {UNIT}" in line)
    hook = next(index for index, line in enumerate(lines) if f"{BINARY_NAME}-verify" in line)

    assert build < restart < hook


def test_the_release_makes_no_python_environment(bench: Bench) -> None:
    bench.release()
    (build,) = [one for one in bench.run.seen if any(CARGO in word for word in one.argv)]

    assert build.env == ((BINARY_ENV_NAME, str(bench.staged())),)
    assert all(one.argv[:3] != RELOCATABLE_ARGV for one in bench.run.seen)


def test_the_live_tree_carries_both_stamps(bench: Bench) -> None:
    """The version and the manifest go into a binary tree as they go into
    a venv, so the next release reads `kind: binary` out of a tree root
    owns."""
    bench.release()

    assert "kind: binary" in (bench.tree() / MANIFEST_STAMP).read_text(encoding="utf-8")


def test_everyone_can_run_a_released_program(bench: Bench) -> None:
    """The mode pass runs on a binary tree. The unit runs as its own user,
    which is not root."""
    bench.release()
    mode = (bench.tree() / "bin" / BINARY_NAME).stat().st_mode

    assert mode & 0o005 == 0o005
    assert not mode & 0o022


def test_the_phone_is_told_the_kind(bench: Bench) -> None:
    """§2.5's `review` field names the kinds in the set."""
    seen: list[str] = []
    grant = bench.wiring.transport

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        seen.append(summary.review)

        return grant(action_id, gate, summary, wait_s)

    bench.wiring = replace(bench.wiring, transport=transport)
    bench.release()

    assert seen == ["safe: 1 component(s), binary"]


# ---- a tree that is not self-contained ----------------------------------------


def _linked_out(new: Path) -> None:
    staged_binary_tree(new)
    (new / "bin" / "extra").symlink_to("/usr/bin/env")


def test_a_tree_that_is_not_self_contained_is_refused(tmp_path: Path) -> None:
    bench = _make_bench(tmp_path, _linked_out)
    bench.install_unit(unit_text(bench.program()))

    assert "refused" in bench.release()

    assert bench.ledger()["refused_check"] == "editable"
    assert f"bin/extra {ABSOLUTE_LINK}" in bench.reason()


def test_that_refusal_leaves_the_live_tree_in_service(tmp_path: Path) -> None:
    """Nothing was swapped, nothing restarted, and the staged tree is gone."""
    bench = _make_bench(tmp_path, _linked_out)
    bench.install_unit(unit_text(bench.program()))

    bench.release()

    assert installed_version(bench.tree()) == LIVE_VERSION
    assert (bench.tree() / "bin" / BINARY_NAME).read_bytes() == OLD_PROGRAM
    assert not bench.staged().exists()
    assert not bench.run.ran("restart")


def test_a_unit_whose_program_the_build_did_not_make_is_refused(tmp_path: Path) -> None:
    """The release carries a unit that starts `noticeboard-next`. The
    build made no such program, so the restart would start nothing."""
    bench = _make_bench(tmp_path)
    bench.install_unit(unit_text(bench.program()))
    _serve(bench, unit_text(bench.program("noticeboard-next")))

    assert "refused" in bench.release()

    assert bench.ledger()["refused_check"] == "editable"
    assert f"bin/noticeboard-next {NOT_STAGED}" in bench.reason()
    assert installed_version(bench.tree()) == LIVE_VERSION


# ---- a unit that starts another tree -------------------------------------------


def test_a_unit_outside_the_tree_is_refused_before_the_build(tmp_path: Path) -> None:
    """Contract 06 §1 rule 8, for a binary component. The refusal comes
    before the build, so the host is exactly as it was."""
    bench = _make_bench(tmp_path)
    bench.install_unit(unit_text(ELSEWHERE))

    assert "refused" in bench.release()

    assert bench.ledger()["refused_check"] == "unit"
    assert not bench.run.ran(CARGO)
    assert installed_version(bench.tree()) == LIVE_VERSION

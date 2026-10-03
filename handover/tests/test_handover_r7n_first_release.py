"""A FIRST release, `noticeboard`, on a host where no release has run.

Every other test in this package stands up a host where a previous release
already ran: a stamped tree, a live version, a `latest` map somebody wrote.
A first release has none of that, and what breaks it is invisible to those
tests for exactly that reason.

So this module stands up that host and changes nothing else.

| What the host has | What this module does |
|---|---|
| trees the cutover made, none stamped | the directories, and no `.release-version` |
| no live-state document anywhere | writes none, and once writes a hostile one |
| seven `-v0.1.0` tags on ONE commit | one version and one SHA per component |
| no tag in agent-mcp or agent-registry | two components resolve to nothing |

The REAL `component.yaml` files of this repository are served, with their
install paths moved under `tmp_path`. A fixture manifest would hide what
decides it: `noticeboard/component.yaml` requires three contracts, and what the
other eight components declare decides whether `noticeboard` can be released at
all.

Fakes stand only at the edges the design puts them at: the GitHub API, the
phone, `uv` and `systemctl`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import ARRIVING, CATALOG, CATALOG_BY_NAME, RETIRING, Repo
from handover.executor.approval import Decision, Summary
from handover.executor.drain import Counter, drain, handle
from handover.executor.live_state import (
    OPERATOR_COMPONENTS,
    Readers,
    build_state,
    default_install_roots,
    installed_version,
    read_trees,
    select_manifests,
)
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
from handover.manifest import OPERATOR_HOME_PREFIX, ComponentManifest, parse_manifest
from handover.requester import preview_of
from handover.resolve import deploying_names, resolve
from handover.state import ReleaseState
from handover_executor_fixtures import (
    REQUEST_ID,
    FakeRun,
    GitFake,
    fake_host,
    git_host_answers,
    grant_transport,
    green_api,
    live_state_body,
    make_spool_dirs,
    request_body,
    this_uid,
    write_live_state,
    write_request,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: What `handover/bin/allocate-tags.sh` gives every component on its first
#: run: one version, one commit, seven tags in agent-control.
FIRST_VERSION = "0.1.0"
FIRST_SHA = "7c1b0d9a2e45f6837b0c4d1e9a53f2068d7be431"

COMPONENT = "noticeboard"
UNIT = "creche-noticeboard.service"

#: Every component that is a tree under an install root on this host.
#: `bin/rework-cutover.sh` made the three operator-owned ones and
#: `bin/rework-release-visit.sh` made `handover`. None is stamped.
#:
#: The other five are live or absent and are NOT trees: `chaperone` runs out
#: of the deployed checkout's venv, `playpen` is an image, `infra`
#: is a compose project, `mcp-servers` is `/opt/mcp/<name>` and
#: `registry-data` is a checkout. Root learns a component's manifest from
#: a tree under an install root, so it can see none of them.
INSTALLED_TREES = ("caregiver", "handover", "attendance", "noticeboard")

#: What `chaperone` provides, and nobody in the resolved set does. `caregiver`,
#: `attendance` and `noticeboard` all require it, so it is the requirement root cannot
#: check on this host.
UNPROVIDED = "pep-grant"

#: Every requirement root cannot check on the host, in the order
#: `contracts._check_floors` walks the set: by component name, then in the
#: order the manifest declares them. `chaperone` provides `pep-grant` and
#: `playpen` provides `channel`, and neither is a tree under an
#: install root. `handover` requires nothing, and `noticeboard` gets `session-api`
#: from `attendance` and `manager-status` from `caregiver`, which ARE trees.
#:
#: The FLOORS are read from the manifests and never written here. A contract
#: draft moves a `min_minor` in some other pull request, and a number typed
#: into this list then fails a test that has nothing to do with that change
#: (it did, the day this was written: `pep-grant` went 0.10 to 0.11 and
#: `channel` 0.11 to 0.12 while this file was on its branch).
_MANIFEST_DIR = {"caregiver": "caregiver", "attendance": "attendance", "noticeboard": "noticeboard"}


def _floor(component: str, contract: str) -> str:
    """`MAJOR.MIN_MINOR` as `<component>/component.yaml` requires it today."""
    path = REPO_ROOT / _MANIFEST_DIR[component] / "component.yaml"
    manifest = parse_manifest(path.read_text(encoding="utf-8"), str(path))
    for one in manifest.requires:
        if str(one.contract) == contract:
            return f"{one.major}.{one.min_minor}"

    raise AssertionError(f"{component} does not require {contract}")


def _not_verified(component: str, contract: str, owner: str) -> str:
    return (
        f"not verified: {component} requires {contract} {_floor(component, contract)}, "
        f"and {owner} is not a tree under an install root"
    )


UNVERIFIED = [
    _not_verified("attendance", "channel", "playpen"),
    _not_verified("attendance", "pep-grant", "chaperone"),
    _not_verified("caregiver", "pep-grant", "chaperone"),
    _not_verified("caregiver", "channel", "playpen"),
    _not_verified("noticeboard", "pep-grant", "chaperone"),
]

#: The two rows the operator reads on the phone, exactly. Four contracts
#: have a provider root can see: `family-file` and `manager-status` from
#: `caregiver`, `session-api` from `attendance`, `component-manifest` from
#: `handover`.
REVIEW_ROW = "suspect: 3 manifest(s) not verified (attendance, caregiver, handover)"
CONTRACTS_ROW = "4 satisfied, 5 not verified"

#: The second request the real spool held: `caregiver` asked for
#: `mcp-servers=latest` while agent-mcp carried no `mcp-servers-v*` tag,
#: so root refuses it. A later ULID than the operator's, so the drain reaches it
#: second.
CAREGIVER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DM"

#: What the noticeboard's unit really execs on the host — `bin/rework-cutover.sh`
#: installed it under `~operator/.config/systemd/user` pointing at the tree
#: `noticeboard` releases, which is why the first release was this component.
EXEC_START = "{tree}/bin/noticeboard"


@dataclass
class First:
    """The host before any release has ever run on it."""

    tmp_path: Path
    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path

    def tree(self) -> Path:
        return self.components / COMPONENT

    def ledger(self, request_id: str = REQUEST_ID) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{request_id}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def manifest_row(self, name: str) -> dict[str, object]:
        manifest = self.ledger()["manifest"]
        assert isinstance(manifest, dict)
        rows = cast("dict[str, object]", manifest)["components"]
        assert isinstance(rows, list)
        found = [one for one in cast("list[object]", rows) if isinstance(one, dict)]

        return next(cast("dict[str, object]", one) for one in found if one.get("name") == name)

    def step(self, name: str) -> dict[str, object]:
        rows = self.ledger()["steps"]
        assert isinstance(rows, list)
        listed = [one for one in cast("list[object]", rows) if isinstance(one, dict)]

        return next(cast("dict[str, object]", one) for one in listed if one.get("name") == name)


def _repo_manifest(components: Path, name: str) -> str | None:
    """This repository's own `component.yaml`, with its install paths moved
    under the bench. Only agent-control holds one, which is contract 06
    §10.1 and the reason two components resolve to nothing here."""
    row = CATALOG_BY_NAME[name]
    if row.repo is not Repo.AGENT_CONTROL:
        return None

    where = REPO_ROOT if row.path == "." else REPO_ROOT / row.path
    # A retiring component's directory has left this tree (`catalog.RETIRING`),
    # and an arriving one's has not reached it yet (`catalog.ARRIVING`).
    if name in RETIRING | ARRIVING and not (where / "component.yaml").is_file():
        return None

    text = (where / "component.yaml").read_text(encoding="utf-8")
    # A manifest writes the operator's root from the home, `~/…`, and root's
    # in full. Both move under the bench.
    text = text.replace(f"{OPERATOR_HOME_PREFIX}{OPERATOR_COMPONENTS}", str(components))
    for real in default_install_roots():
        text = text.replace(str(real), str(components))

    return text


#: A test that wants one manifest changed hands this in. It takes the
#: component name and the repository's own text, and answers the text the
#: clone should carry.
EditFn = Callable[[str, str], str]


def _serve_the_repository(bench: First, edit: EditFn | None = None) -> None:
    """What the fake `git clone` leaves behind: the whole agent-control
    tree's manifests at one commit, and the unit file step 9 refreshes."""

    def write_tree(destination: Path, name: str) -> None:
        for row in CATALOG:
            text = _repo_manifest(bench.components, row.name)
            if text is None or CATALOG_BY_NAME[name].repo is not row.repo:
                continue

            if edit is not None:
                text = edit(row.name, text)

            sub = destination if row.path == "." else destination / row.path
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "component.yaml").write_text(text, encoding="utf-8")

        units = destination / "systemd"
        units.mkdir(parents=True, exist_ok=True)
        (units / UNIT).write_text(_unit_text(bench), encoding="utf-8")

    bench.run.dynamic = GitFake(write_tree)


def _unit_text(bench: First) -> str:
    exec_start = EXEC_START.format(tree=bench.tree())

    return f"[Unit]\nDescription=the noticeboard\n\n[Service]\nExecStart={exec_start}\n"


def _first_readers() -> Readers:
    """Seven `-v0.1.0` tags on ONE commit, and nothing in the other two
    repositories. It is what `allocate-tags.sh`'s first run leaves."""

    def newest(component: str) -> str | None:
        if CATALOG_BY_NAME[component].repo is not Repo.AGENT_CONTROL:
            return None

        return FIRST_VERSION

    def tag_sha(component: str, version: str) -> str | None:
        del version
        if CATALOG_BY_NAME[component].repo is not Repo.AGENT_CONTROL:
            return None

        return FIRST_SHA

    def digest(component: str, sha: str) -> str | None:
        del component, sha

        return "sha256:" + "5c" * 32

    return Readers(newest_version=newest, tag_sha=tag_sha, input_digest=digest)


@pytest.fixture
def first(tmp_path: Path) -> First:
    """The host before the first release: trees, no stamps, no document."""
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    components = tmp_path / "components"
    # FOUR trees, and the other five absent. This is the line the first
    # round got wrong: it made nine, and on the real host only these four
    # exist. The cutover and the visit made them and stamped none, and a
    # bare directory is not the same thing as a component that is not
    # installed.
    for name in INSTALLED_TREES:
        (components / name).mkdir(parents=True, exist_ok=True)

    bench = First(
        tmp_path=tmp_path,
        spool_root=spool_root,
        wiring=Wiring(
            host=host,
            transport=grant_transport(),
            readers=_first_readers(),
            api=green_api("agent-control", f"{COMPONENT}-v{FIRST_VERSION}", FIRST_SHA),
        ),
        run=run,
        components=components,
    )
    (tmp_path / "user-units" / UNIT).write_text(_unit_text(bench), encoding="utf-8")
    _serve_the_repository(bench)
    run.hooks["sync"] = lambda _: _build_the_tree(bench)

    return bench


def _build_the_tree(bench: First) -> None:
    """What `uv sync` would leave at `<install.to>.new`: the console script
    the verify hook names, and a site-packages the self-contained walk
    needs."""
    staged = bench.components / f"{COMPONENT}.new"
    (staged / "bin").mkdir(parents=True, exist_ok=True)
    (staged / "lib" / "python3.12" / "site-packages").mkdir(parents=True, exist_ok=True)
    for script in ("noticeboard-verify", "noticeboard"):
        made = staged / "bin" / script
        made.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        made.chmod(0o755)


def _release(bench: First, wanted: dict[str, str]) -> str:
    write_request(bench.spool_root, REQUEST_ID, request_body(wanted, requested_by="human"))
    spool = Spool(str(bench.spool_root), this_uid())
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


def _manual_lines(bench: First) -> list[str]:
    manual = bench.ledger()["manual"]
    assert isinstance(manual, list)

    return [str(one) for one in cast("list[object]", manual)]


def _repo_manifests(bench: First) -> dict[str, ComponentManifest]:
    """Every agent-control manifest, parsed, as the requester's `--root`
    walk would hand them over. It is the same text the fake clone serves."""
    found: dict[str, ComponentManifest] = {}
    for row in CATALOG:
        text = _repo_manifest(bench.components, row.name)
        if text is not None:
            found[row.name] = parse_manifest(text, f"{row.name}/component.yaml")

    return found


def _raise_the_floor(bench: First, consumer: str, contract: str, floor: str) -> None:
    """Make one component's `requires` entry name a floor the live provider
    does not meet, in every tree the fake clone serves. The provider here
    IS under an install root, so C1 must still refuse."""
    major, minor = floor.split(".")
    wanted = f"  - {{ contract: {contract}, major: {major}, min_minor: {minor} }}"

    def edit(name: str, text: str) -> str:
        if name != consumer:
            return text

        lines = [wanted if f"contract: {contract}," in one else one for one in text.splitlines()]

        return "\n".join(lines) + "\n"

    _serve_the_repository(bench, edit)


# -- the release the operator types ---------------------------------------


def test_the_first_ui_release_reaches_done_with_a_named_version(first: First) -> None:
    """`handover request noticeboard@0.1.0`, then the tap, then `done`.

    This is the whole first release with fakes at the four edges.
    """
    result = _release(first, {COMPONENT: FIRST_VERSION})

    assert "succeeded" in result
    entry = first.ledger()
    assert entry["status"] == "succeeded"
    assert entry["previous"] == {COMPONENT: None}
    assert installed_version(first.tree()) == FIRST_VERSION


def test_the_first_ui_release_reaches_done_with_no_version_at_all(first: First) -> None:
    """`handover request noticeboard`, which means `latest`.

    Root reads the newest tag itself, so `latest` needs no document
    `caregiver` would have to publish.
    """
    result = _release(first, {COMPONENT: "latest"})

    assert "succeeded" in result
    assert first.ledger()["status"] == "succeeded"
    assert installed_version(first.tree()) == FIRST_VERSION


def test_a_planted_live_state_document_changes_nothing(first: First) -> None:
    """The same release, with a hostile live-state document planted.

    It claims `noticeboard` is already at 9.9.9 — which would make the request a
    downgrade the monotonic rule refuses — and that `latest` means 9.9.9,
    which GitHub has no Release for. Root opens no such file, so the
    release is byte for byte the release above.
    """
    planted = first.tmp_path / "srv-state-releases"
    planted.mkdir()
    write_live_state(planted, live_state_body({COMPONENT: "9.9.9"}, {COMPONENT: "9.9.9"}))

    result = _release(first, {COMPONENT: "latest"})

    assert "succeeded" in result
    assert first.ledger()["previous"] == {COMPONENT: None}
    assert installed_version(first.tree()) == FIRST_VERSION


# -- what the ledger says about the eight components that did not move -----


def test_only_the_released_component_carries_source_facts(first: First) -> None:
    """Contract 06 §9.

    Root fetched one source and ran the predicate over one tag, so one row
    carries a `sha` and an `input_digest`. `mcp-servers` has no tag in
    agent-mcp on this host, and the document still builds: only a
    component this release resolved a tag for carries facts.
    """
    _release(first, {COMPONENT: FIRST_VERSION})

    assert first.manifest_row(COMPONENT)["sha"] == FIRST_SHA
    assert str(first.manifest_row(COMPONENT)["input_digest"]).startswith("sha256:")
    assert first.manifest_row("mcp-servers")["sha"] is None
    assert first.manifest_row("mcp-servers")["input_digest"] is None


def test_the_first_release_resolves_against_an_empty_provided(first: First) -> None:
    """Contract 06 §11: a contract absent from `provided` is a first
    install and rule C4 does not apply.

    No tree on the host carries a stamped `component.yaml`, so `provided`
    is empty on the first release and C4 passes over every contract. The
    step still reports the contract table it built, which is what proves
    the rules ran rather than being skipped.
    """
    _release(first, {COMPONENT: FIRST_VERSION})

    assert first.step("contracts")["status"] == "ok"
    assert "0 refused" in str(first.step("contracts")["detail"])


# -- the contract check, and what the phone says --------------------------


def test_the_unprovided_requirement_is_reported_and_never_refused(first: First) -> None:
    """The refusal the operator's real first release got, and why it is gone.

    `chaperone` was live out of `/opt/creche/.venv`, which is no tree
    under an install root, so root saw no provider for `pep-grant` —
    which `caregiver`, `attendance` and `noticeboard` all
    require. Root cannot tell that from "nothing provides it", and
    refusing there refuses every release on this host for ever.
    """
    _release(first, {COMPONENT: FIRST_VERSION})

    entry = first.ledger()
    assert entry["status"] == "succeeded"
    assert [one for one in _manual_lines(first) if one.startswith("not verified:")] == UNVERIFIED
    assert "5 requirement(s) not verified" in str(first.step("contracts")["detail"])
    # And `pep-grant`, the one the real refusal named, is among them.
    assert sum(1 for one in UNVERIFIED if UNPROVIDED in one) == 3


def test_a_provider_root_can_see_is_still_checked(first: First) -> None:
    """The teeth stay where root can bite. `noticeboard` requires `session-api`
    from `attendance`, which IS a tree under an install root, so a floor the
    live `attendance` does not meet refuses exactly as before."""
    _raise_the_floor(first, "noticeboard", "session-api", "9.9")

    result = _release(first, {COMPONENT: FIRST_VERSION})

    assert "refused" in result
    assert first.ledger()["refused_check"] == "C1"
    assert "noticeboard requires session-api 9.9" in str(first.ledger()["reason"])


def test_the_phone_names_what_root_could_not_verify(first: First) -> None:
    """What the operator reads before the tap, on the real host, for `noticeboard`.

    Two rows carry it. `review` counts the manifests root read at a
    commit rather than out of a tree it owns, and `contracts` counts the
    requirements it could not check. Neither is a fault.
    """
    shown: list[Summary] = []

    def watching(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        shown.append(summary)

        return grant_transport()(action_id, gate, summary, wait_s)

    first.wiring = Wiring(
        host=first.wiring.host,
        transport=watching,
        readers=first.wiring.readers,
        api=first.wiring.api,
    )

    _release(first, {COMPONENT: FIRST_VERSION})

    assert len(shown) == 1
    assert shown[0].review == REVIEW_ROW
    assert shown[0].contracts == CONTRACTS_ROW


def test_the_preview_and_root_give_one_answer(first: First) -> None:
    """Both sides run `select_manifests` over the same install trees, so
    the preview's `contracts` row is root's, character for character.

    A requester that read all nine manifests out of the `--root` working
    trees would say `6 contracts satisfied` for a set root refuses.
    """
    shown: list[Summary] = []

    def watching(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        shown.append(summary)

        return grant_transport()(action_id, gate, summary, wait_s)

    first.wiring = Wiring(
        host=first.wiring.host,
        transport=watching,
        readers=first.wiring.readers,
        api=first.wiring.api,
    )
    _release(first, {COMPONENT: FIRST_VERSION})

    # The requester's half, over the SAME trees and the same manifests.
    roots = first.wiring.host.install_roots
    built = build_state(roots, {COMPONENT: FIRST_VERSION}, Readers()).state
    state = ReleaseState(live=built.live, provided=built.provided, latest=built.latest)
    chosen = select_manifests(
        deploying_names(state, {COMPONENT: FIRST_VERSION}),
        read_trees(roots),
        _repo_manifests(first).get,
    )
    resolution = resolve(chosen.manifests, state, {COMPONENT: FIRST_VERSION})
    preview = preview_of(resolution, chosen, "human")

    assert preview.summary.contracts == shown[0].contracts == CONTRACTS_ROW
    assert [one.line() for one in resolution.unprovided] == UNVERIFIED
    assert UNPROVIDED in preview.summary.contracts or "not verified" in preview.summary.contracts


def test_two_requests_in_one_pass_each_get_their_own_entry(first: First) -> None:
    """The real spool held two: the operator's `noticeboard`, and `caregiver`'s
    `mcp-servers=latest`, which root correctly refuses because agent-mcp
    carries no `mcp-servers-v*` tag.

    One drain, in ULID order, two ledger entries, two pushes. A pass that
    stopped at the refusal would leave the operator's release in `requests/` and
    the path unit re-firing on it.
    """
    pushed: list[str] = []
    first.wiring = Wiring(
        host=first.wiring.host,
        transport=grant_transport(),
        readers=first.wiring.readers,
        api=first.wiring.api,
        notify=lambda notice: bool(pushed.append(notice.line())) or True,
    )
    write_request(
        first.spool_root,
        CAREGIVER_ID,
        request_body({"mcp-servers": "latest"}, request_id=CAREGIVER_ID, requested_by="managerd"),
    )
    write_request(
        first.spool_root,
        REQUEST_ID,
        request_body({COMPONENT: FIRST_VERSION}, requested_by="human"),
    )

    spool = Spool(str(first.spool_root), this_uid())
    try:
        handled = drain(spool, first.wiring)
    finally:
        spool.close()

    assert handled == 2
    assert first.ledger()["status"] == "succeeded"
    other = first.ledger(CAREGIVER_ID)
    assert other["status"] == "refused"
    assert "names no released tag" in str(other["reason"])
    assert len(pushed) == 2
    assert sorted(first.spool_root.glob("requests/*.json")) == []


# -- the release that does NOT land ---------------------------------------


def test_a_switch_that_fails_leaves_no_new_tree(first: First) -> None:
    """A verify that fails: the restore puts the cutover's tree back and
    moves the new one aside to `<install.to>.new`, `root:root 750`. A tree
    kept there stops the next `rework-cutover.sh up`, which stages into
    that same name as the operator, at `rm: cannot remove 'noticeboard.new': Permission
    denied`, so the release removes it.
    """
    first.run.fails["noticeboard-verify"] = 1

    result = _release(first, {COMPONENT: FIRST_VERSION})

    assert "succeeded" not in result
    staged = first.components / f"{COMPONENT}.new"
    assert not staged.exists()
    assert f"removed {staged}" in cast("list[object]", first.ledger()["log_tail"])
    assert not [one for one in _manual_lines(first) if str(staged) in one]


def test_a_failed_release_leaves_no_switch_note(first: First) -> None:
    """The same release, the other leftover. When its restore fails its
    own verify too, a `<id>-noticeboard.switch` left in `running/` — root-only,
    invisible to operator-side tooling — makes
    `bin/rework-release-visit.sh --rebuild-executor` refuse with "an
    unfinished switch is in .../running. Drain it first." The note is
    there to survive a CRASH between the swap and the verify, not a run
    that recorded itself.
    """
    first.run.fails["noticeboard-verify"] = 1

    _release(first, {COMPONENT: FIRST_VERSION})

    assert first.ledger()["status"] in ("failed", "restored")
    assert sorted(first.spool_root.glob("running/*")) == []

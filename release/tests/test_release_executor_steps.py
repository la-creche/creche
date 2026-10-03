"""One release, end to end, through all ten steps (`stage7-releases.md` §2.4).

The whole executor runs here: a real `Spool` on real directories, the real
resolver, the real ten steps. Only the four things that leave the process are
fakes — the child, the GitHub API, the phone and the clock.

The scenarios are `bin/rework-release-gate.sh` section 1's list plus three more:
a crash between switch and verify that the next run repairs, two requests at
once that run one after the other, and a replayed approval for another hash.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from agent_release.catalog import CATALOG, CATALOG_BY_NAME
from agent_release.executor.approval import Decision, Summary, Verdict, gate_id
from agent_release.executor.drain import Counter, drain, handle, repair_unfinished
from agent_release.executor.host import Command
from agent_release.executor.live_state import STAMP_FILE, installed_version
from agent_release.executor.spool import DONE_DIR, REQUESTS_DIR, Spool, SwitchNote
from agent_release.executor.steps import NOTHING_TO_DO, Wiring
from release_executor_fixtures import (
    REQUEST_ID,
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    grant_transport,
    green_api,
    live_state_body,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_live_state,
    write_request,
)
from release_fixtures import manifest_text

LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"
OTHER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DF"


@dataclass
class Bench:
    """One fully wired executor, with the knobs a scenario turns."""

    tmp_path: Path
    spool_root: Path
    wiring: Wiring
    run: FakeRun
    components: Path

    def open_spool(self) -> Spool:
        return Spool(str(self.spool_root), this_uid())

    def ledger(self, request_id: str = REQUEST_ID) -> dict[str, object]:
        path = self.spool_root / DONE_DIR / f"{request_id}.json"
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}

    def step_status(self, entry: dict[str, object], name: str) -> str | None:
        rows = entry["steps"]
        assert isinstance(rows, list)
        for row in rows:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(row, dict) and row.get("name") == name:  # pyright: ignore[reportUnknownMemberType]
                return str(row.get("status"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

        return None

    def step_names(self, entry: dict[str, object]) -> list[str]:
        rows = entry["steps"]
        assert isinstance(rows, list)

        return [str(row.get("name")) for row in rows if isinstance(row, dict)]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]


def _serve_manifests(bench: Bench, manifests: dict[str, str]) -> None:
    """What the fake `git clone` leaves in each fetched tree.

    `fetch` removes the destination before it clones, so a tree written
    ahead of time would be deleted. The clone writes it instead, which is
    also what the real one does.
    """

    def write_tree(destination: Path, component: str) -> None:
        text = manifests.get(component)
        if text is None:
            return

        row = CATALOG_BY_NAME[component]
        sub = destination if row.path == "." else destination / row.path
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "component.yaml").write_text(text, encoding="utf-8")

    bench.run.dynamic = GitFake(write_tree)


#: A `build` entry the manifest needs for the executor to stage anything.
#: It is a list of argv lists with no interpolation, contract 06 §8's shape.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'


def _manifest_for(bench: Bench, name: str, edits: dict[str, str] | None = None) -> str:
    """A manifest whose install paths sit under the bench's components root
    and whose verify hook is the staged one the switch will run."""
    text = manifest_text(name).replace("/opt/components", str(bench.components))
    text = text.replace("install:", BUILD_LINE, 1)
    for old, new in (edits or {}).items():
        text = text.replace(old, new)

    return text


def _all_manifests(bench: Bench, edits: dict[str, str] | None = None) -> dict[str, str]:
    """Every component contract 06 §1 lists: §9 needs a row for each. The
    edits apply to the one component a scenario releases."""
    return {row.name: _manifest_for(bench, row.name, edits) for row in CATALOG}


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    """A host where `chaperone` is live at 2.0.3 and 2.1.0 is the latest tag."""
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    wiring = Wiring(
        host=host,
        transport=grant_transport(),
        readers=fake_readers(latest={"chaperone": NEW_VERSION}),
        api=green_api("agent-control", f"chaperone-v{NEW_VERSION}", SHA_OF["chaperone"]),
    )
    built = Bench(tmp_path, spool_root, wiring, run, tmp_path / "components")
    stamp_tree(built.components, "chaperone", LIVE_VERSION)

    return built


def _stage_builds(bench: Bench, version: str = NEW_VERSION) -> None:
    """`build` is a child, so the test writes what the child would have
    written: the staged tree with its hook, at `<install.to>.new`."""

    def write_new(_: object) -> None:
        stamp_tree(bench.components, "chaperone.new", version)

    bench.run.hooks["sync"] = write_new


def _run_one(bench: Bench, body: dict[str, object] | None = None) -> str:
    write_request(bench.spool_root, REQUEST_ID, body or request_body({"chaperone": NEW_VERSION}))
    _serve_manifests(bench, _all_manifests(bench))
    _stage_builds(bench)
    spool = bench.open_spool()
    try:
        return handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()


# -- the happy path -------------------------------------------------------


def test_one_component_walks_every_step_and_lands(bench: Bench) -> None:
    """§2.4's ten steps, in order, ending `succeeded`. This is the shape
    the operator reads after "release `chaperone` alone with one tap"."""
    result = _run_one(bench)

    assert "succeeded" in result
    entry = bench.ledger()
    assert entry["status"] == "succeeded"
    assert bench.step_names(entry) == [
        "intake",
        "resolve",
        "provenance",
        "contracts",
        "approve",
        "lock",
        "quiesce",
        "stage",
        "switch",
        "record",
    ]
    assert entry["previous"] == {"chaperone": LIVE_VERSION}
    assert installed_version(bench.components / "chaperone") == NEW_VERSION


def test_the_ledger_carries_the_whole_resolved_manifest(bench: Bench) -> None:
    """§2.6 and contract 06 §3.4: the ledger is where the resolved lock
    lives, because root never writes to a repository."""
    _run_one(bench)
    manifest = bench.ledger()["manifest"]

    assert isinstance(manifest, dict)
    assert str(manifest["manifest_sha256"]).startswith("sha256:")  # pyright: ignore[reportUnknownArgumentType]
    assert len(manifest["components"]) == 9  # pyright: ignore[reportUnknownArgumentType]


def test_the_request_file_always_leaves_requests(bench: Bench) -> None:
    """§2.2: `agent-release.path` is a `PathExistsGlob`. One file left
    behind re-fires the unit until systemd's start limit trips."""
    _run_one(bench)

    assert not list((bench.spool_root / REQUESTS_DIR).glob("*.json"))


# -- refusals that touch nothing ------------------------------------------


def test_a_forged_request_file_is_refused(bench: Bench) -> None:
    """A file that is not §2.3's shape never reaches step 2. The reason in
    the ledger is root's own words, never the file's."""
    body = request_body({"chaperone": NEW_VERSION}) | {"components": {"chaperone": "/etc/passwd"}}
    result = _run_one(bench, body)

    assert "refused" in result
    entry = bench.ledger()
    assert entry["refused_check"] == "request"
    assert installed_version(bench.components / "chaperone") == LIVE_VERSION


def test_a_replayed_id_is_quarantined_and_never_read(bench: Bench) -> None:
    """§6 row 5: an id is spent once it reaches `running/` or `done/`."""
    _run_one(bench)
    write_request(bench.spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))

    spool = bench.open_spool()
    try:
        result = handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()

    assert "rejected" in result
    assert bench.ledger()["status"] == "succeeded"
    assert list((bench.spool_root / "rejected").iterdir())


def test_a_component_may_not_go_backwards(bench: Bench) -> None:
    """§2.4 step 3's monotonic rule, resting on what is INSTALLED."""
    stamp_tree(bench.components, "chaperone", "9.9.9")
    body = request_body({"chaperone": NEW_VERSION})
    write_request(bench.spool_root, REQUEST_ID, body)
    write_live_state(
        bench.tmp_path, live_state_body({"chaperone": "9.9.9"}, {"chaperone": NEW_VERSION})
    )
    _serve_manifests(bench, _all_manifests(bench))
    _stage_builds(bench)

    spool = bench.open_spool()
    try:
        handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()

    assert bench.ledger()["refused_check"] == "monotonic"


def test_a_release_with_no_github_token_refuses_at_provenance(bench: Bench) -> None:
    """The predicate fails CLOSED: a transport it cannot use is a refusal,
    never a pass."""
    bench.wiring = Wiring(
        host=bench.wiring.host,
        transport=bench.wiring.transport,
        readers=bench.wiring.readers,
        api=None,
    )
    _run_one(bench)
    entry = bench.ledger()

    assert entry["refused_check"] == "P1"
    assert bench.step_status(entry, "approve") is None


def test_a_denied_tap_touches_nothing(bench: Bench) -> None:
    """§2.4 step 5: a deny, a timeout or an expiry is `refused`, check
    `approval`, and nothing on the host has changed."""
    bench.wiring = _with_transport(bench, lambda gate: Decision(Verdict.DENIED, gate, None))
    _run_one(bench)

    assert bench.ledger()["refused_check"] == "approval"
    assert installed_version(bench.components / "chaperone") == LIVE_VERSION
    assert not bench.run.ran("systemctl")


def test_an_approval_for_another_hash_is_refused(bench: Bench) -> None:
    """A replayed approval: the operator's tap for a DIFFERENT manifest, presented
    for this one. Step 5 compares the gate id before it believes it."""
    wrong = gate_id("sha256:" + "00" * 32)
    bench.wiring = _with_transport(bench, lambda _: Decision(Verdict.GRANTED, wrong, 1.0))
    _run_one(bench)

    assert bench.ledger()["refused_check"] == "approval"
    assert installed_version(bench.components / "chaperone") == LIVE_VERSION


def test_the_phone_summary_comes_from_roots_own_resolution(bench: Bench) -> None:
    """§2.5: the summary and the deployed set are one computation, so a
    requester cannot show the operator one thing and deploy another."""
    shown: list[Summary] = []

    def watching(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, wait_s
        shown.append(summary)

        return Decision(Verdict.GRANTED, gate, 1.0)

    bench.wiring = Wiring(
        host=bench.wiring.host,
        transport=watching,
        readers=bench.wiring.readers,
        api=bench.wiring.api,
    )
    _run_one(bench)

    summary = shown[0].as_dict()
    assert summary["components"] == f"chaperone {LIVE_VERSION} → {NEW_VERSION}"
    assert summary["restore"] == "automatic"
    assert summary["requested_by"] == "agent-control"
    manifest = bench.ledger()["manifest"]
    assert isinstance(manifest, dict)
    assert str(manifest["manifest_sha256"]).endswith(summary["manifest"]) or summary[  # pyright: ignore[reportUnknownArgumentType]
        "manifest"
    ] in str(manifest["manifest_sha256"])  # pyright: ignore[reportUnknownArgumentType]


def _with_transport(bench: Bench, answer: Callable[[str], Decision]) -> Wiring:
    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, summary, wait_s

        return answer(gate)

    return Wiring(
        host=bench.wiring.host,
        transport=transport,
        readers=bench.wiring.readers,
        api=bench.wiring.api,
    )


# -- step 9 fails, step 10 acts -------------------------------------------


def test_a_failed_verify_restores_and_the_previous_version_verifies(bench: Bench) -> None:
    """§2.4 row 10 and contract 06 §5.1. The invariant 17 test: the failed
    tree goes back, the previous version's own hook passes, and the ledger
    says all of it."""
    calls = {"n": 0}
    bench.run.fails["chaperone-verify"] = 1

    def pass_after_the_first_call(_: object) -> None:
        """The new version's hook fails once. The restored one passes, and
        that is the fact contract 06 §5.1 demands: the previous version
        verifies GREEN, not merely that the files went back."""
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop("chaperone-verify", None)

    bench.run.hooks["chaperone-verify"] = pass_after_the_first_call
    _run_one(bench)

    entry = bench.ledger()
    assert entry["status"] == "restored"
    assert entry["reason"] == "switch: chaperone verify failed"
    assert bench.step_status(entry, "switch") == "failed"
    assert bench.step_status(entry, "restore") == "ok"
    assert installed_version(bench.components / "chaperone") == LIVE_VERSION
    verify = entry["verify"]
    assert isinstance(verify, list)
    assert len(verify) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_a_failed_verifys_ledger_row_carries_its_report(bench: Bench) -> None:
    """Every `verify` row carries `detail`. Without it `verify noticeboard: exit 1:`
    is the whole line `done/<id>.json` holds — the exit code alone, with
    the report `noticeboard-verify --json` printed to stdout nowhere in it."""
    report = '{"ok": false, "checks": [{"name": "config", "ok": false}]}'
    calls = {"n": 0}
    bench.run.fails["chaperone-verify"] = 1
    bench.run.fail_stdout["chaperone-verify"] = report

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop("chaperone-verify", None)

    bench.run.hooks["chaperone-verify"] = pass_after_the_first_call
    _run_one(bench)

    entry = bench.ledger()
    assert entry["status"] == "restored"
    verify = entry["verify"]
    assert isinstance(verify, list)
    first = verify[0]
    assert isinstance(first, dict)  # pyright: ignore[reportUnknownArgumentType]
    assert first["status"] == "failed"
    assert report in str(first["detail"])


def test_a_restore_leaves_no_tree_behind(bench: Bench) -> None:
    """A restored release removes what it staged, and the ledger says it
    did.

    A failed tree kept at `<install.to>.new` — `noticeboard.new` in the operator's home,
    `root:root 750` — stops the next `rework-cutover.sh up`, which stages
    into the same name as the operator, at `rm: cannot remove 'noticeboard.new':
    Permission denied`."""
    calls = {"n": 0}
    bench.run.fails["chaperone-verify"] = 1

    def pass_after_the_first_call(_: object) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            bench.run.fails.pop("chaperone-verify", None)

    bench.run.hooks["chaperone-verify"] = pass_after_the_first_call
    _run_one(bench)

    entry = bench.ledger()
    assert entry["status"] == "restored"
    staged = bench.components / "chaperone.new"
    assert not staged.exists()
    log = entry["log_tail"]
    assert isinstance(log, list)
    assert f"removed {staged}" in log
    # What the sweep may not touch: the version the restore put back.
    assert installed_version(bench.components / "chaperone") == LIVE_VERSION


def test_a_refused_release_leaves_no_tree_behind(bench: Bench) -> None:
    """The other two ends of the same rule: a release that never swapped
    anything staged trees all the same, and the directory it staged into
    is the one the next release and the next cutover both need."""
    bench.run.fails["chaperone-verify"] = 1
    _run_one(bench)

    entry = bench.ledger()
    assert entry["status"] == "failed"
    assert not (bench.components / "chaperone.new").exists()


def test_the_manifests_env_file_reaches_the_hooks_own_argv(bench: Bench) -> None:
    """End to end: steps.py must not drop or mangle a
    manifest's `--env-file` words on their way to the child. The fake
    verify hook fails unless it sees the exact `--env-file` argv this
    manifest names, the same way the operator's real `noticeboard-verify` needed its own
    unit's `EnvironmentFile=` to pass rather than a hand-run one."""
    env_path = str(bench.components / "chaperone.env")
    edits = {'"--json"]': f'"--json", "--env-file", "{env_path}"]'}
    _serve_manifests(bench, _all_manifests(bench, edits))
    write_request(bench.spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))
    _stage_builds(bench)

    def check_env_file(command: Command) -> None:
        argv = list(command.argv)
        given = "--env-file" in argv and argv[argv.index("--env-file") + 1] == env_path
        if given:
            bench.run.fails.pop("chaperone-verify", None)
        else:
            bench.run.fails["chaperone-verify"] = 1

    bench.run.hooks["chaperone-verify"] = check_env_file

    spool = bench.open_spool()
    try:
        result = handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()

    assert "succeeded" in result
    assert bench.ledger()["status"] == "succeeded"


def test_a_manual_restore_component_stops_instead(bench: Bench) -> None:
    """Contract 06 §5.2 row 2: restore nothing, stop, ledger `failed`, push
    the phone. Blind reversal after a migration is the worse end."""
    _serve_manifests(bench, _all_manifests(bench, {"mode: automatic": "mode: manual"}))
    write_request(bench.spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))
    _stage_builds(bench)
    bench.run.fails["chaperone-verify"] = 1

    spool = bench.open_spool()
    try:
        handle(spool, f"{REQUEST_ID}.json", bench.wiring, Counter())
    finally:
        spool.close()

    entry = bench.ledger()
    assert entry["status"] == "failed"
    assert bench.step_status(entry, "restore") == "skipped"
    # The new tree is still live: stopping means stopping, and the operator reads
    # the phone push rather than finding a surprise reversal at 02:00.
    assert installed_version(bench.components / "chaperone") == NEW_VERSION


# -- the three more -------------------------------------------------------


def test_a_crash_between_switch_and_verify_is_repaired_by_the_next_run(bench: Bench) -> None:
    """The switch note is written BEFORE the move, so a crash in between
    leaves a record root owns. The next run puts the previous artifact back
    and does NOT retry the release."""
    stamp_tree(bench.components, "chaperone.prev", LIVE_VERSION)
    stamp_tree(bench.components, "chaperone", NEW_VERSION)
    spool = bench.open_spool()
    try:
        note = SwitchNote(
            "chaperone",
            str(bench.components / "chaperone"),
            str(bench.components / "chaperone.prev"),
            "creche-chaperone.service",
        )
        spool.note_switch(REQUEST_ID, note)
        assert spool.unfinished()
        lines = repair_unfinished(spool, bench.wiring)
        assert not spool.unfinished()
    finally:
        spool.close()

    # The repair is a step of its own, so the
    # line names the outcome it ledgered rather than only the swap.
    assert "repaired (restored)" in lines[0]
    assert installed_version(bench.components / "chaperone") == LIVE_VERSION


def test_two_requests_at_once_run_one_after_the_other(bench: Bench) -> None:
    """§2.4 row 6. Both are drained in one pass, in id order, and each gets
    its own ledger entry — the lock is what serializes them."""
    write_request(bench.spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))
    write_request(
        bench.spool_root, OTHER_ID, request_body({"chaperone": NEW_VERSION}, request_id=OTHER_ID)
    )
    _serve_manifests(bench, _all_manifests(bench))
    _stage_builds(bench)

    spool = bench.open_spool()
    try:
        handled = drain(spool, bench.wiring)
    finally:
        spool.close()

    assert handled == 2
    assert bench.ledger(REQUEST_ID)["status"] == "succeeded"
    # The second one finds 2.1.0 already live, so it resolves to
    # `unchanged` and refuses rather than restarting a healthy unit.
    # Serialization is what makes that TRUE: had
    # they overlapped, the second would have resolved against 2.0.3.
    second = bench.ledger(OTHER_ID)
    assert second["status"] == "refused"
    assert second["reason"] == NOTHING_TO_DO
    assert not list((bench.spool_root / REQUESTS_DIR).glob("*.json"))


def test_a_ninth_request_from_one_requester_is_refused_as_rate(bench: Bench) -> None:
    """§3.2 rule 8. The path unit still drains it, so the start limit is
    never reached."""
    counter = Counter()
    for _ in range(8):
        assert not counter.over_limit("agent-control")

    assert counter.over_limit("agent-control")
    assert not counter.over_limit("ci")


def test_the_rate_refusal_still_empties_requests(bench: Bench) -> None:
    ids = [f"01K5J8M2Q7V3X9R4T6N0B8C2{one:02X}" for one in range(0x10, 0x1B)]
    for one in ids:
        write_request(
            bench.spool_root, one, request_body({"chaperone": NEW_VERSION}, request_id=one)
        )

    _serve_manifests(bench, _all_manifests(bench))
    _stage_builds(bench)
    spool = bench.open_spool()
    try:
        drain(spool, bench.wiring)
    finally:
        spool.close()

    assert not list((bench.spool_root / REQUESTS_DIR).glob("*.json"))
    refused = [
        one
        for one in ids
        if json.loads((bench.spool_root / DONE_DIR / f"{one}.json").read_text(encoding="utf-8"))[
            "refused_check"
        ]
        == "rate"
    ]
    assert refused


# -- sets, rollbacks and releasing itself ---------------------------------


def test_a_set_of_two_components_is_no_longer_refused(bench: Bench) -> None:
    """A set of two is released like one component, and §2.1
    says why there is no separate path: a single component still changes
    the resolved set, still needs the contract check and still needs one
    approval. `test_release_r7d_sets.py` proves the set END TO END; this
    one pins that no "set of more than one" refusal stands in its way."""
    stamp_tree(bench.components, "attendance", "1.4.6")
    write_live_state(
        bench.tmp_path,
        live_state_body(
            {"chaperone": LIVE_VERSION, "attendance": "1.4.6"},
            {"chaperone": NEW_VERSION, "attendance": "1.4.7"},
        ),
    )
    body = request_body({"chaperone": NEW_VERSION, "attendance": "1.4.7"})
    _run_one(bench, body)

    assert "set of more than one" not in str(bench.ledger()["reason"])


def test_a_rollback_refuses_at_intake(bench: Bench) -> None:
    body = request_body({"chaperone": NEW_VERSION}, kind="rollback") | {"rollback_of": OTHER_ID}
    _run_one(bench, body)

    assert "rollback" in str(bench.ledger()["reason"])


def test_a_staged_tree_is_stamped_before_it_is_switched(bench: Bench) -> None:
    """The stamp is how the NEXT release learns what is live, and it is
    written while the tree is still `.new` — never onto a live tree."""
    _run_one(bench)

    assert (bench.components / "chaperone" / STAMP_FILE).read_text(encoding="utf-8").strip() == (
        NEW_VERSION
    )

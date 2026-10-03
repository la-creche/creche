"""Steps 8 to 10 for one component (`stage7-releases.md` §2.4, contract 06).

Every test drives the real `Installer` against a host whose paths all sit
under `tmp_path` and whose children are recorded, never started. What is
proved here is what a reading of the code cannot settle: which identity a
hook runs as, what a failed verify leaves behind, and that a manifest cannot
name a path outside the roots the executor was configured with.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_release.catalog import RestoreMode, VerifyUser
from agent_release.errors import Refusal, RefusalCode
from agent_release.executor.host import As
from agent_release.executor.install import (
    Installer,
    StepFailed,
    paths_of,
    restorable,
)
from agent_release.executor.live_state import installed_version
from agent_release.manifest import ComponentManifest, parse_manifest
from release_executor_fixtures import FakeRun, fake_host, git_host_answers, stamp_tree
from release_fixtures import manifest_text


def _manifest(tmp_path: Path, name: str = "pep", **edits: str) -> ComponentManifest:
    """One valid manifest whose install paths sit under `tmp_path`."""
    text = manifest_text(name)
    root = tmp_path / "components"
    text = text.replace(f"/opt/components/{name}", str(root / name))
    for old, new in edits.items():
        text = text.replace(old, new)

    return parse_manifest(text, f"{name}/component.yaml")


def test_a_root_verify_hook_runs_as_root(tmp_path: Path) -> None:
    """Contract 06 §4: the hook runs as its own `user`. `pep` is a root
    component, and a hook run as the operator would read the operator's systemd and sbx
    state instead of root's. A comparison with `is` against a string never
    matches a `VerifyUser` member, so it would run EVERY hook as the operator."""
    manifest = _manifest(tmp_path, "pep", **{"user: operator": "user: root"})
    assert manifest.verify.user is VerifyUser.ROOT

    run = FakeRun(answers=git_host_answers())
    Installer(fake_host(tmp_path, run)).verify(manifest)

    assert run.seen[-1].identity is As.ROOT


def test_an_operator_verify_hook_runs_as_the_operator(tmp_path: Path) -> None:
    run = FakeRun(answers=git_host_answers())
    Installer(fake_host(tmp_path, run)).verify(_manifest(tmp_path))

    assert run.seen[-1].identity is As.OPERATOR


def test_a_failing_hook_reports_the_exit_code_and_the_stderr_tail(tmp_path: Path) -> None:
    """Contract 06 §4 rules 3 and 4."""
    manifest = _manifest(tmp_path)
    run = FakeRun(fails={"pep-verify": 3}, fail_stdout={"pep-verify": "not much"})
    outcome = Installer(fake_host(tmp_path, run)).verify(manifest)

    assert not outcome.ok
    assert "exit 3" in outcome.detail
    assert "pep-verify failed" in outcome.detail


def test_a_failing_hook_still_carries_its_stdout_report(tmp_path: Path) -> None:
    """`view-verify --json` writes its whole report to stdout and nothing
    to stderr, even on exit 1. A `run_hook` that kept stdout only on
    success would leave the ledger's failure line at `verify ui: exit 1:`
    — the exit code alone, with the one line that explains the fault (a
    missing access key, say) nowhere in `done/<id>.json`."""
    manifest = _manifest(tmp_path)
    report = '{"ok": false, "checks": [{"name": "config", "ok": false}]}'
    run = FakeRun(fails={"pep-verify": 1}, fail_stdout={"pep-verify": report})
    outcome = Installer(fake_host(tmp_path, run)).verify(manifest)

    assert not outcome.ok
    assert report in outcome.detail


def test_a_restart_failure_carries_systemctls_stderr(tmp_path: Path) -> None:
    """Every `_must` failure carries its stderr, not the verify hook alone:
    `cannot restart agent-view.service (exit 1)` is followed by what
    `systemctl` says, `Failed to restart … see status`."""
    manifest = _manifest(tmp_path, "pep", **{"unit: null": "unit: agent-pep.service"})
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / "agent-pep.service").write_text("[Unit]\n", encoding="utf-8")

    run = FakeRun(fails={"restart": 1})
    with pytest.raises(StepFailed) as raised:
        Installer(fake_host(tmp_path, run)).restart(manifest)

    assert "restart failed" in raised.value.detail


#: What `agent-sessiond.service` lets an in-flight turn take to settle
#: (`TimeoutStopSec=60`), and systemd's default `TimeoutStartSec`.
SESSIOND_STOP_S = 60
DEFAULT_START_S = 90


def test_a_restart_waits_out_the_units_own_stop(tmp_path: Path) -> None:
    """`systemctl restart` answers only once the unit has stopped and
    started again. A wait of 30 s cut sessiond's 60 s stop on 2026-10-01,
    and the release restored while the unit was still restarting."""
    manifest = _manifest(tmp_path, "pep", **{"unit: null": "unit: agent-pep.service"})
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / "agent-pep.service").write_text("[Unit]\n", encoding="utf-8")

    run = FakeRun(answers={"is-active": "active"})
    Installer(fake_host(tmp_path, run)).restart(manifest)

    restart = next(one for one in run.seen if "restart" in one.argv)
    assert restart.timeout_s > SESSIOND_STOP_S + DEFAULT_START_S


def test_an_install_path_outside_every_root_is_refused(tmp_path: Path) -> None:
    """The containment check. A manifest reaches root from a merged pull
    request, and one that could name `/etc` would make a release a way to
    write anywhere on the host."""
    manifest = _manifest(tmp_path, "pep", **{str(tmp_path / "components" / "pep"): "/etc/pep"})
    with pytest.raises(Refusal) as raised:
        paths_of(manifest, (tmp_path / "components",))

    assert raised.value.code is RefusalCode.MANIFEST


def test_an_install_path_is_accepted_under_a_configured_root(tmp_path: Path) -> None:
    paths = paths_of(_manifest(tmp_path), (tmp_path / "components",))

    assert paths.new.name == "pep.new"


def test_the_install_root_itself_is_refused(tmp_path: Path) -> None:
    """`swap_in` renames `to` away. A `to` that IS the root would rename
    every installed tree to `<root>.prev` and put one component in its
    place."""
    root = tmp_path / "components"
    manifest = _manifest(tmp_path, "pep", **{str(root / "pep"): str(root)})
    with pytest.raises(Refusal) as raised:
        paths_of(manifest, (root,))

    assert raised.value.code is RefusalCode.MANIFEST


def test_a_nested_install_path_is_refused(tmp_path: Path) -> None:
    """Direct children only: `<root>/<other>/pep` would live inside another
    component's tree."""
    root = tmp_path / "components"
    manifest = _manifest(tmp_path, "pep", **{str(root / "pep"): str(root / "x" / "pep")})
    with pytest.raises(Refusal) as raised:
        paths_of(manifest, (root,))

    assert raised.value.code is RefusalCode.MANIFEST


def test_a_prev_that_names_another_tree_is_refused(tmp_path: Path) -> None:
    """`swap_in` removes `prev` first. A free `prev` under the root removes
    whichever tree it names, so it may only be `<to>.prev`."""
    root = tmp_path / "components"
    manifest = _manifest(tmp_path, "pep", **{str(root / "pep.prev"): str(root / "releasectl")})
    with pytest.raises(Refusal) as raised:
        paths_of(manifest, (root,))

    assert raised.value.code is RefusalCode.MANIFEST


def test_a_swap_puts_the_old_tree_at_prev_and_back_again(tmp_path: Path) -> None:
    """Step 9's two renames, then step 10's. The tree that failed goes to
    `.new`, and the run that staged it removes it before it ends
    (`steps.remove_staged`, and `test_release_executor_steps.py`)."""
    root = tmp_path / "components"
    manifest = _manifest(tmp_path)
    paths = paths_of(manifest, (root,))
    stamp_tree(root, "pep", "2.0.3")
    stamp_tree(root, "pep.new", "2.1.0")

    installer = Installer(fake_host(tmp_path, FakeRun()))
    installer.swap_in(paths)
    assert installed_version(paths.to) == "2.1.0"
    assert installed_version(paths.prev) == "2.0.3"

    installer.swap_back(paths)
    assert installed_version(paths.to) == "2.0.3"
    assert installed_version(paths.new) == "2.1.0"


def test_a_restore_with_no_previous_artifact_stops(tmp_path: Path) -> None:
    """A first install has nothing to go back to. Stopping is the honest
    end: contract 06 §5.2's third row pushes the phone, and nothing further
    is automatic."""
    root = tmp_path / "components"
    paths = paths_of(_manifest(tmp_path), (root,))
    stamp_tree(root, "pep", "2.1.0")

    with pytest.raises(StepFailed):
        Installer(fake_host(tmp_path, FakeRun())).swap_back(paths)


def test_an_uninstalled_unit_is_listed_as_manual_and_never_installed(tmp_path: Path) -> None:
    """Contract 06 §1 rule 7 and §2.4's note on step 9. `ui`'s unit is not
    installed anywhere yet, and installing one stays the installer's job."""
    manifest = _manifest(tmp_path, "pep", **{"unit: null": "unit: agent-pep.service"})
    run = FakeRun()
    manual = Installer(fake_host(tmp_path, run)).refresh_unit(manifest, tmp_path / "source")

    assert manual is not None
    assert "not installed" in manual
    assert not run.ran("install")


def test_an_installed_unit_is_refreshed_in_place(tmp_path: Path) -> None:
    """Rule 4: refreshed where one already exists, with `install`, never by
    running a bootstrap script."""
    manifest = _manifest(tmp_path, "pep", **{"unit: null": "unit: agent-pep.service"})
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / "agent-pep.service").write_text("[Unit]\nold\n", encoding="utf-8")
    source = tmp_path / "source" / "systemd"
    source.mkdir(parents=True)
    (source / "agent-pep.service").write_text("[Unit]\nnew\n", encoding="utf-8")

    run = FakeRun()
    manual = Installer(fake_host(tmp_path, run)).refresh_unit(manifest, tmp_path / "source")

    assert manual is None
    assert run.ran("/usr/bin/install")
    assert run.seen[-1].identity is As.ROOT


def test_a_unit_that_does_not_come_back_active_fails_the_switch(tmp_path: Path) -> None:
    """§2.4 row 9: `is-active` is polled, and a unit that is not active is a
    failed switch, which step 10 then restores."""
    manifest = _manifest(tmp_path, "pep", **{"unit: null": "unit: agent-pep.service"})
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / "agent-pep.service").write_text("[Unit]\n", encoding="utf-8")

    run = FakeRun(answers={"is-active": "failed", "NRestarts": "0"})
    with pytest.raises(StepFailed):
        Installer(fake_host(tmp_path, run)).restart(manifest)


def test_a_unit_that_restarts_again_inside_the_window_fails(tmp_path: Path) -> None:
    """A crash loop climbs `NRestarts` inside the 20 second window, which is
    the one thing `is-active` alone cannot see."""
    manifest = _manifest(tmp_path, "pep", **{"unit: null": "unit: agent-pep.service"})
    (tmp_path / "system-units").mkdir(exist_ok=True)
    (tmp_path / "system-units" / "agent-pep.service").write_text("[Unit]\n", encoding="utf-8")

    climbing = iter(["0", "0", "1", "2", "3", "4", "5"])
    run = FakeRun(answers={"is-active": "active"})
    run.answers["NRestarts"] = "0"

    def climb(_: object) -> None:
        run.answers["NRestarts"] = next(climbing, "9")

    run.hooks["NRestarts"] = climb
    with pytest.raises(StepFailed):
        Installer(fake_host(tmp_path, run)).restart(manifest)


def test_a_manual_restore_component_is_not_restorable(tmp_path: Path) -> None:
    """Contract 06 §5.2: a forward-only migration is not reversed by
    reinstalling the previous artifact."""
    manual = _manifest(tmp_path, "pep", **{"mode: automatic": "mode: manual"})

    assert manual.restore.mode is RestoreMode.MANUAL
    assert not restorable(manual)
    assert restorable(_manifest(tmp_path))

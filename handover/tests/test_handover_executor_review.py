"""The executor, read as a hostile requester: one regression per fault.

Each test names the fault it pins and fails on code that has it, so a later
refactor that brings a fault back fails here rather than on the host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME
from handover.discovery import read_one
from handover.errors import Refusal, RefusalCode
from handover.executor.drain import (
    TOKEN_ENV,
    Counter,
    Secrets,
    build_wiring,
    drain,
    handle,
    keep_only_the_secrets,
)
from handover.executor.host import (
    INSTALL_ROOT,
    TIMEOUT_EXIT_CODE,
    As,
    Command,
    Host,
    Result,
    make_runner,
)
from handover.executor.install import paths_of
from handover.executor.spool import DONE_DIR, MAX_REQUESTS_PER_PASS, Spool
from handover.executor.steps import Wiring
from handover.manifest import parse_manifest
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
from handover_fixtures import manifest_text, write_manifest

STATE_FILE = "/srv/agents/state/rework/releases/live-state.json"
SOME_UID = 1000

LIVE_VERSION = "2.0.3"
NEW_VERSION = "2.1.0"

#: A `build` entry, so the manifest stages something. Contract 06 §8's shape.
BUILD_LINE = 'build:\n  - ["/usr/local/bin/uv", "sync", "--frozen"]\ninstall:'


def _no_child(_: object) -> Result:
    """A host nothing runs on: these tests read its configuration only."""
    return Result(0)


# -- the production wiring passed no install root -------------------------


def test_the_production_wiring_carries_an_install_root() -> None:
    """`build_wiring` is what `main` uses. With no install root, two
    defences go quiet at once: `_require_contained` returns for every path,
    and `_stamp_of` finds no version, which makes `check_monotonic` skip a
    downgrade."""
    wiring = build_wiring(SOME_UID, Secrets("", "", ""), STATE_FILE)

    # The second root is the operator's. Contract 06 §1 puts three of the eight
    # releasable components under the operator's home, and one root alone refused
    # every one of them.
    assert Path(INSTALL_ROOT) in wiring.host.install_roots


def test_a_host_built_with_no_roots_still_contains_install_paths() -> None:
    """A `Host` built the way `build_wiring` builds it refuses `/etc`. The
    default is the containment check, not an empty tuple that turns it off."""
    text = manifest_text("chaperone").replace("/opt/components/chaperone", "/etc/chaperone")
    manifest = parse_manifest(text, "chaperone/component.yaml")

    with pytest.raises(Refusal) as raised:
        paths_of(manifest, Host(run=_no_child).install_roots)

    assert raised.value.code is RefusalCode.MANIFEST


# -- one clone per component, one repository per clone --------------------


def _repo_tree(root: Path, component: str, components: Path) -> None:
    """What a real `git checkout` leaves in one clone: EVERY manifest that
    component's repository holds, not only its own.

    One manifest per clone is the shape no real checkout has, and that
    shape hides a step 2 that refuses every real release.
    """
    repo = CATALOG_BY_NAME[component].repo
    for row in CATALOG:
        if row.repo is not repo:
            continue

        text = manifest_text(row.name).replace("/opt/components", str(components))
        write_manifest(root, row.name, text.replace("install:", BUILD_LINE, 1))


def test_read_one_takes_the_component_out_of_a_whole_repository(tmp_path: Path) -> None:
    """§2.4 row 2 reads ONE `component.yaml` per component, at the path the
    catalog fixes. A walk of the whole tree finds seven."""
    clone = tmp_path / "chaperone-clone"
    _repo_tree(clone, "chaperone", tmp_path / "components")

    assert read_one(clone, "chaperone").manifest.name == "chaperone"
    assert read_one(clone, "attendance").manifest.name == "attendance"


def test_a_release_lands_when_every_clone_is_a_whole_repository(tmp_path: Path) -> None:
    """`bin/rework-release-gate.sh` section 1, against the trees a real
    fetch makes. A step 2 that clones `agent-control` once per component
    and then walks all seven raises `infra is declared twice` at the
    second clone, and EVERY release refuses before the tap."""
    spool_root = make_spool_dirs(tmp_path)
    components = tmp_path / "components"
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=grant_transport(),
        readers=fake_readers(latest={"chaperone": NEW_VERSION}),
        api=green_api("agent-control", f"chaperone-v{NEW_VERSION}", SHA_OF["chaperone"]),
    )
    stamp_tree(components, "chaperone", LIVE_VERSION)
    run.dynamic = GitFake(lambda into, name: _repo_tree(into, name, components))
    run.hooks["sync"] = lambda _: stamp_tree(components, "chaperone.new", NEW_VERSION)
    write_request(spool_root, REQUEST_ID, request_body({"chaperone": NEW_VERSION}))

    spool = Spool(str(spool_root), this_uid())
    try:
        handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
    finally:
        spool.close()

    entry: object = json.loads((spool_root / DONE_DIR / f"{REQUEST_ID}.json").read_text("utf-8"))
    assert isinstance(entry, dict)

    assert entry["status"] == "succeeded"  # pyright: ignore[reportUnknownMemberType]


# -- the flood cap keyed on a field the requester writes ------------------


#: Distinct 26-character Crockford ULIDs, one per flooded request.
_FLOOD = 40


def _flood_requests(spool_root: Path) -> None:
    """One request per made-up requester, all valid, all cheap to refuse.

    §3.2 rule 8 caps requests PER REQUESTER, and `requested_by` is a field
    the requester writes. Forty names defeat the cap forty times over.
    """
    for index in range(_FLOOD):
        request_id = f"{REQUEST_ID[:-2]}{index:02d}".upper().replace("I", "J")
        write_request(
            spool_root,
            request_id,
            request_body(
                {"chaperone": NEW_VERSION},
                request_id=request_id,
                requested_by=f"flood-{index:02d}",
            ),
        )


def test_a_flood_cannot_grow_the_ledger_or_hold_root_in_the_drain_loop(tmp_path: Path) -> None:
    """A `drain` that re-lists `requests/` until it is empty and writes one
    ledger entry and one log per file lets an operator-side writer fill `done/`
    and keep root busy to `TimeoutStartSec`. §6 row 13 says that is
    stopped."""
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(
        host=fake_host(tmp_path, run),
        transport=grant_transport(),
        readers=fake_readers(),
        api=None,
    )
    _flood_requests(spool_root)

    spool = Spool(str(spool_root), this_uid())
    try:
        handled = drain(spool, wiring)
        left = spool.pending()
    finally:
        spool.close()

    ledgered = list((spool_root / DONE_DIR).glob("*.json"))

    assert handled <= MAX_REQUESTS_PER_PASS
    assert len(ledgered) <= MAX_REQUESTS_PER_PASS
    # The path unit is a PathExistsGlob: one *.json left behind re-fires it.
    assert left == []


# -- a child that outran its timeout left the process ---------------------


def test_a_child_that_outruns_its_timeout_answers_instead_of_raising() -> None:
    """Contract 06 §4 rule 3 makes a verify timeout a failure. No layer
    above `make_runner` catches `subprocess.TimeoutExpired`:
    `Installer._run` takes `OSError`, `Release._step` takes `Refusal` and
    `StepFailed`, `drain.main` takes `SpoolError` and `OSError`. A runner
    that let it out would kill the run with no ledger entry, after the
    switch has already swapped the tree."""
    run = make_runner(os.getuid())
    command = Command(argv=("/bin/sleep", "5"), identity=As.ROOT, timeout_s=0.2)

    result = run(command)

    assert result.code == TIMEOUT_EXIT_CODE


# -- the executor held every secret of the compose stack ------------------


def test_the_executor_keeps_one_secret_and_drops_the_rest() -> None:
    """The wrapper runs the executor under `sops exec-env`, so root's
    environment carries EVERY name in `infra/secrets.enc.env` for the whole
    run. The executor needs one. `host.child_env` already builds each
    child's environment from scratch, so this bounds what a future child
    started outside `Host`, or anything that prints `os.environ`, could
    reach."""
    environ = {
        TOKEN_ENV: "the-token",
        "REDIS_PASSWORD": "a compose secret",
        "LITELLM_MASTER_KEY": "another one",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }

    found = keep_only_the_secrets(environ)

    assert found.github_token == "the-token"
    assert sorted(environ) == ["HOME", "PATH"]

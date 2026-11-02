"""One `host` release, end to end (contract 06 §8.3).

The whole executor runs, as in `test_handover_executor_steps.py`: a real
spool, the real resolver and the real ten steps. Only the child, the GitHub
API, the phone and the clock are fakes.

What a `host` release does today: step 8 copies the fetched tree
(`install.py` rule 10) and reads its unit files (`executor/units.py`).
Step 9 swaps the tree in and runs `host-verify`. No unit file is linked
yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from handover.catalog import CATALOG
from handover.executor.drain import Counter, handle
from handover.executor.live_state import STAMP_FILE
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import Wiring
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

HOST = "host"
LIVE = "0.1.0"
NEW = "0.1.1"
TIMER = "units/user/creche-follow.timer"
DROP_IN = "units/system/creche-chaperone.service.d/creche.conf"
HOOK = f"bin/{HOST}-verify"


def _release(tmp_path: Path, files: tuple[str, ...]) -> tuple[dict[str, object], Path]:
    """File one request for `host` at `NEW`, run it, and return the ledger
    entry and the components root. The fetched tree holds the manifest,
    the verify hook and `files`."""
    spool_root = make_spool_dirs(tmp_path)
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    components = tmp_path / "components"
    stamp_tree(components, HOST, LIVE)
    manifests = {
        row.name: manifest_text(row.name).replace("/opt/components", str(components))
        for row in CATALOG
    }

    def write_tree(destination: Path, component: str) -> None:
        text = manifests.get(component)
        if text is None:
            return

        destination.mkdir(parents=True, exist_ok=True)
        (destination / "component.yaml").write_text(text, encoding="utf-8")
        (destination / ".git").mkdir()
        for name in (HOOK, *files):
            (destination / name).parent.mkdir(parents=True, exist_ok=True)
            (destination / name).write_text(f"# {name}\n", encoding="utf-8")

        (destination / HOOK).chmod(0o755)

    run.dynamic = GitFake(write_tree)
    wiring = Wiring(
        host=host,
        transport=grant_transport(),
        readers=fake_readers(latest={HOST: NEW}),
        api=green_api("creche-release", f"{HOST}-v{NEW}", SHA_OF[HOST]),
    )
    write_request(spool_root, REQUEST_ID, request_body({HOST: NEW}))
    spool = Spool(str(spool_root), this_uid())
    try:
        handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
    finally:
        spool.close()

    path = spool_root / DONE_DIR / f"{REQUEST_ID}.json"
    loaded = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))

    return loaded, components


def test_a_host_release_installs_the_fetched_tree(tmp_path: Path) -> None:
    entry, components = _release(tmp_path, (TIMER, DROP_IN))

    assert entry["status"] == "succeeded", entry
    live = components / HOST
    assert (live / STAMP_FILE).read_text(encoding="utf-8").strip() == NEW
    assert (live / TIMER).is_file()
    assert (live / DROP_IN).is_file()
    assert not (live / ".git").exists()


@pytest.mark.parametrize(
    ("name", "detail"),
    [
        ("units/user/notes.txt", "is not units/user/<unit>"),
        ("units/user/creche-handover.service", "a handover unit"),
    ],
)
def test_a_tree_with_a_wrong_unit_file_is_refused_and_the_host_is_unchanged(
    tmp_path: Path, name: str, detail: str
) -> None:
    entry, components = _release(tmp_path, (TIMER, name))

    assert entry["status"] == "refused", entry
    assert entry["refused_check"] == "unit"
    assert detail in str(entry["reason"])
    assert (components / HOST / STAMP_FILE).read_text(encoding="utf-8").strip() == LIVE
    assert not (components / f"{HOST}.new").exists()

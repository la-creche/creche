"""`caregiver` publishes what is live, from root's ledger.

Contract 06 §3.4: root never writes to a git repository, so the resolved
lock lives in `done/<ULID>.json` and `caregiver` — which runs as the operator and
already owns the registry checkout — copies values out of it. This is the
`bin/rework-release-gate.sh` §2 case "`caregiver` publishes `live-manifest.json`
afterwards", proved with ledger entries and no host.
"""

from __future__ import annotations

import json
from pathlib import Path

from caregiver.live_manifest import (
    LIVE_MANIFEST_FILE,
    LiveManifest,
    newest_live,
    publish,
)

OLDER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DA"
NEWER_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DE"


def _entry(
    ledger: Path,
    request_id: str,
    *,
    status: str = "succeeded",
    caregiver: str = "1.2.0",
    row: str = "caregiver",
    resolved_at: float = 1758153600.0,
    contract: str = "1.0",
) -> None:
    ledger.mkdir(parents=True, exist_ok=True)
    body = {
        "id": request_id,
        "status": status,
        "manifest": {
            "id": request_id,
            "resolved_at": resolved_at,
            "components": [
                {"name": row, "action": "deploy", "to_version": caregiver},
                {"name": "chaperone", "action": "unchanged", "to_version": "2.0.3"},
            ],
            "contracts": [
                {"contract": "family-file", "provider": "caregiver", "version": contract},
                {"contract": "pep-grant", "provider": "chaperone", "version": "2.0"},
            ],
        },
    }
    (ledger / f"{request_id}.json").write_text(json.dumps(body), encoding="utf-8")


def test_the_newest_succeeded_entry_is_what_is_live(tmp_path: Path) -> None:
    ledger = tmp_path / "done"
    _entry(ledger, OLDER_ID, caregiver="1.1.0")
    _entry(ledger, NEWER_ID, caregiver="1.2.0")

    found = newest_live(ledger)

    assert found == LiveManifest("1.2.0", "1.0", 1758153600.0, NEWER_ID)


def test_a_restored_release_is_not_what_is_live(tmp_path: Path) -> None:
    """A release that deployed, failed verify and went back did not change
    what is live. Publishing from it would name a version that never ran."""
    ledger = tmp_path / "done"
    _entry(ledger, OLDER_ID, caregiver="1.1.0")
    _entry(ledger, NEWER_ID, caregiver="1.2.0", status="restored")

    assert newest_live(ledger) == LiveManifest("1.1.0", "1.0", 1758153600.0, OLDER_ID)


def test_a_ledger_with_nothing_succeeded_publishes_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "done"
    _entry(ledger, NEWER_ID, status="refused")

    assert newest_live(ledger) is None
    assert publish(tmp_path / "checkout", ledger) is None


def test_publish_writes_contract_06_s_four_fields(tmp_path: Path) -> None:
    """`validate.yml` reads the `managerd` key out of this file and checks
    out that version. The pin survives, and its human writer does not."""
    ledger = tmp_path / "done"
    checkout = tmp_path / "checkout"
    _entry(ledger, NEWER_ID)

    publish(checkout, ledger)

    written: object = json.loads((checkout / LIVE_MANIFEST_FILE).read_text(encoding="utf-8"))
    assert written == {
        "managerd": "1.2.0",
        "family_file_contract": "1.0",
        "released_at": 1758153600.0,
        "release_id": NEWER_ID,
    }


def test_a_half_written_entry_does_not_raise(tmp_path: Path) -> None:
    """The reconcile loop calls this. A ledger entry root was still writing
    must cost one skipped entry, never the loop."""
    ledger = tmp_path / "done"
    _entry(ledger, OLDER_ID, caregiver="1.1.0")
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / f"{NEWER_ID}.json").write_text('{"id": "half', encoding="utf-8")

    assert newest_live(ledger) == LiveManifest("1.1.0", "1.0", 1758153600.0, OLDER_ID)


def test_a_missing_ledger_directory_publishes_nothing(tmp_path: Path) -> None:
    """A host where no release has ever run has no `done/` yet."""
    assert newest_live(tmp_path / "nothing") is None


def test_an_entry_whose_set_left_caregiver_alone_is_skipped(tmp_path: Path) -> None:
    """`live-manifest.json` names the `caregiver` version, so an entry with
    no `caregiver` row has nothing to copy. It is skipped, not invented."""
    ledger = tmp_path / "done"
    ledger.mkdir(parents=True)
    body = {"id": NEWER_ID, "status": "succeeded", "manifest": {"components": []}}
    (ledger / f"{NEWER_ID}.json").write_text(json.dumps(body), encoding="utf-8")
    _entry(ledger, OLDER_ID, caregiver="1.1.0")

    assert newest_live(ledger) == LiveManifest("1.1.0", "1.0", 1758153600.0, OLDER_ID)


def test_an_entry_from_before_the_rename_still_counts(tmp_path: Path) -> None:
    """Entries written before the rename name the `managerd` row."""
    ledger = tmp_path / "done"
    _entry(ledger, OLDER_ID, caregiver="1.1.0", row="managerd")

    assert newest_live(ledger) == LiveManifest("1.1.0", "1.0", 1758153600.0, OLDER_ID)

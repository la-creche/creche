"""The requester's preview of the `restarts` row of a `host` release.

Root names each unit whose drop-in the release changes, at step 5, from the
tree it fetched (contract 06 §8.3 item 6). The requester reads no such
tree, so its row says that root names them.
"""

from __future__ import annotations

from handover.catalog import CATALOG
from handover.executor.live_state import Chosen
from handover.manifest import Markup, parse_manifest
from handover.requester import preview_of
from handover.requester.preview import UNITS_RESTARTS
from handover.resolve import resolve
from handover.state import ReleaseState, SourceFacts
from handover_fixtures import manifest_text

HOST = "host"


def test_the_preview_says_root_names_the_restarts_of_a_host_release() -> None:
    manifests = {
        row.name: parse_manifest(manifest_text(row.name), f"{row.name}/c.yaml", Markup.YAML)
        for row in CATALOG
    }
    state = ReleaseState(
        live={HOST: "0.1.0"},
        provided={},
        latest={HOST: "0.1.1"},
        facts={
            HOST: SourceFacts(sha="a" * 40, input_digest="sha256:" + "b" * 64, artifact_digest=None)
        },
    )
    resolution = resolve(manifests, state, {HOST: "0.1.1"})

    preview = preview_of(resolution, Chosen(manifests), "operator")

    assert preview.summary.restarts == f"{HOST}: {UNITS_RESTARTS}"

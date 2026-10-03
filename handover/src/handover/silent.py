"""The manifest of a component that is not installed.

Contract 06 §10.2 says what root may believe about a component it is not
releasing, row by row, and this module is the third row: **a component
with no install tree declares nothing.**

It is not a stub and it is not a default. It is a claim root can prove:
nothing is installed, so nothing is running, so no interface this component
might provide or require can be broken by the release under consideration.
Rules C1 to C5 (contract 06 §3.2) then pass over it for the only honest
reason — there is nobody on the other end.

The alternative was to read the manifest at a SHA the operator-written
live-state document supplies, which is the hole §10.2 closes: a
requester that drops a `requires` entry from a consumer clears rule C4 and
ships a breaking major bump without the consumers.

The document is built as YAML and parsed by `manifest.parse_manifest`, so
it passes every check a real file passes. A synthesized object that skipped
the parser would be the one manifest in the system with no validation
behind it.
"""

from __future__ import annotations

from typing import Final

from .catalog import CATALOG_BY_NAME, MANIFEST_CONTRACT_MAJOR, MANIFEST_CONTRACT_MINOR, Kind
from .manifest import ComponentManifest, parse_manifest

#: Where a component that is not installed would install. It sits under an
#: install root so the containment check passes, and nothing ever writes
#: there: a silent manifest is only ever produced for a component this
#: release does NOT deploy.
SILENT_ROOT: Final = "/opt/components"

#: What a silent manifest's `verify` names. It never runs — the hook of a
#: component with no action is not called — and an argv list is required,
#: so it names the same path the artifact would carry.
_TIMEOUT_S: Final = 60


def silent_manifest(name: str) -> ComponentManifest:
    """A valid `component.yaml` for `name` that declares no interface."""
    row = CATALOG_BY_NAME[name]
    kind = Kind.DATA if row.kind is Kind.DATA else Kind.VENV

    text = _text(name, str(row.repo), row.path, str(kind))

    return parse_manifest(text, f"{name} (not installed)")


def _text(name: str, repo: str, path: str, kind: str) -> str:
    version = f"{MANIFEST_CONTRACT_MAJOR}.{MANIFEST_CONTRACT_MINOR}"

    return f"""
manifest_version: "{version}"
name: {name}
repo: {repo}
path: {path}
kind: {kind}
unit: null
runs_as: none
install:
  to: {SILENT_ROOT}/{name}
  prev: {SILENT_ROOT}/{name}.prev
provides: []
requires: []
depends_on: []
verify:
  command: ["{SILENT_ROOT}/{name}/bin/{name}-verify", "--json"]
  user: root
  timeout_s: {_TIMEOUT_S}
restore:
  mode: automatic
  keep: 1
secrets: []
release: no
"""

"""Manifest text and fixture trees the release tests share.

A test that needs a hostile manifest writes the text itself. This module
builds only VALID manifests, so a test can change one field and prove that
one refusal.

`manifest_text` gives the YAML form of a manifest and `manifest_toml` the
TOML form. The two builders take the same arguments, and the two forms of
one set of arguments read to one `ComponentManifest`.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from handover.catalog import CATALOG_BY_NAME
from handover.manifest import Markup

#: The contract version the fixtures declare. Kept equal to the catalog's
#: ceiling so a fixture never fails on `manifest_version` by accident.
FIXTURE_MANIFEST_VERSION = "0.4"

#: Where a fixture component installs. No test touches these paths: they only
#: have to satisfy contract 06 §8's absolute-path rule.
_INSTALL_ROOT = "/opt/components"

#: The TOML value of each `release` word that `manifest_text` writes.
_RELEASE_WORDS = {"yes": "true", "no": "false"}


def manifest_text(
    name: str,
    *,
    provides: str = "",
    requires: str = "",
    depends_on: str = "",
    path: str | None = None,
    release: str | None = None,
) -> str:
    """One valid `component.yaml`, with three lists a caller may fill in."""
    row = CATALOG_BY_NAME[name]
    kind = row.kind
    declared = release if release is not None else str(row.releases)

    return f"""
manifest_version: "{FIXTURE_MANIFEST_VERSION}"
name: {name}
repo: {row.repo}
path: "{path if path is not None else row.path}"
kind: {kind}
unit: null
runs_as: operator
install:
  to: {_INSTALL_ROOT}/{name}
  prev: {_INSTALL_ROOT}/{name}.prev
provides: [{provides}]
requires: [{requires}]
depends_on: [{depends_on}]
verify:
  command: ["{_INSTALL_ROOT}/{name}/bin/{name}-verify", "--json"]
  user: operator
  timeout_s: 60
restore:
  mode: automatic
  keep: 3
secrets: []
release: "{declared}"
"""


def manifest_toml(
    name: str,
    *,
    provides: str = "",
    requires: str = "",
    depends_on: str = "",
    path: str | None = None,
    release: str | None = None,
) -> str:
    """One valid `component.toml`: the TOML form of `manifest_text` for the
    same arguments.

    Each of the three lists is the text of a YAML flow list without its
    brackets, as for `manifest_text`. TOML has no null, so the text has no
    `unit` key. `release` is a boolean."""
    row = CATALOG_BY_NAME[name]
    declared = release if release is not None else str(row.releases)
    word = _RELEASE_WORDS.get(declared, json.dumps(declared))

    return f"""
manifest_version = "{FIXTURE_MANIFEST_VERSION}"
name = "{name}"
repo = "{row.repo}"
path = "{path if path is not None else row.path}"
kind = "{row.kind}"
runs_as = "operator"
install.to = "{_INSTALL_ROOT}/{name}"
install.prev = "{_INSTALL_ROOT}/{name}.prev"
provides = {_toml_array(provides)}
requires = {_toml_array(requires)}
depends_on = {_toml_array(depends_on)}
verify.command = ["{_INSTALL_ROOT}/{name}/bin/{name}-verify", "--json"]
verify.user = "operator"
verify.timeout_s = 60
restore.mode = "automatic"
restore.keep = 3
secrets = []
release = {word}
"""


def _toml_array(items: str) -> str:
    """The TOML array of the items of a YAML flow list without its brackets."""
    loaded: list[object] = yaml.safe_load(f"[{items}]")

    return "[" + ", ".join(_toml_value(item) for item in loaded) + "]"


def _toml_value(value: object) -> str:
    """A text, a number or a mapping of them, in TOML. The JSON form of a
    text or a number is also its TOML form."""
    if not isinstance(value, dict):
        return json.dumps(value)

    pairs = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())

    return f"{{ {pairs} }}"


def provides_entry(contract: str, major: int, minor: int) -> str:
    return f"{{ contract: {contract}, major: {major}, minor: {minor} }}"


def requires_entry(contract: str, major: int, min_minor: int) -> str:
    return f"{{ contract: {contract}, major: {major}, min_minor: {min_minor} }}"


def write_manifest(root: Path, name: str, text: str, markup: Markup = Markup.YAML) -> Path:
    """Write one manifest at the directory the catalog gives that component,
    under the file name of its markup."""
    row = CATALOG_BY_NAME[name]
    directory = root if row.path == "." else root / row.path
    directory.mkdir(parents=True, exist_ok=True)
    written = directory / markup.file_name
    written.write_text(text, encoding="utf-8")

    return written

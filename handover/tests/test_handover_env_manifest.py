"""The `env` table of a manifest (`manifest.py`, contract 06 §8).

The table names the env file that a unit reads and the keys that a release
adds to it (`executor/envfill.py`). The reader takes it in the YAML form and
in the TOML form, with the same checks, and refuses each fault with the code
`manifest`.
"""

from __future__ import annotations

import pytest
from handover.catalog import MANIFEST_CONTRACT_MINOR
from handover.errors import Refusal, RefusalCode
from handover.manifest import (
    ENV_BAD_KEY,
    ENV_NO_KEYS,
    ENV_NOT_CRECHE,
    ENV_OTHER_FILE,
    ENV_REPEATED_KEY,
    EnvDecl,
    Markup,
    parse_manifest,
)
from handover_fixtures import manifest_text, manifest_toml

COMPONENT = "caregiver"
ENV_FILE = "/srv/agents/state/creche/vmm.env"
KEY = "VMM_GIT_SPOOL_ROOT"
OTHER_KEY = "VMM_MIRROR_ROOT"

#: The operator home of the example site file of the suite (`conftest.py`).
OPERATOR_HOME = "/home/operator"


def _yaml(env: str, name: str = COMPONENT) -> str:
    return manifest_text(name) + env


def _yaml_env(file: str = ENV_FILE, keys: str = f"[{KEY}]") -> str:
    return f"env:\n  file: {file}\n  keys: {keys}\n"


def _parse(text: str, markup: Markup = Markup.YAML) -> EnvDecl | None:
    return parse_manifest(text, f"{COMPONENT}/component.yaml", markup).env


def _refusal(text: str, markup: Markup = Markup.YAML) -> Refusal:
    with pytest.raises(Refusal) as caught:
        parse_manifest(text, f"{COMPONENT}/component.yaml", markup)

    return caught.value


# -- accepted -------------------------------------------------------------


def test_a_manifest_with_no_env_table_has_none() -> None:
    assert _parse(manifest_text(COMPONENT)) is None


def test_the_yaml_form_gives_the_file_and_the_keys() -> None:
    assert _parse(_yaml(_yaml_env(keys=f"[{KEY}, {OTHER_KEY}]"))) == EnvDecl(
        file=ENV_FILE, keys=(KEY, OTHER_KEY)
    )


def test_the_toml_form_gives_the_same_table() -> None:
    text = manifest_toml(COMPONENT) + f'env.file = "{ENV_FILE}"\nenv.keys = ["{KEY}"]\n'

    assert _parse(text, Markup.TOML) == EnvDecl(file=ENV_FILE, keys=(KEY,))


def test_a_file_below_the_home_of_the_operator_takes_the_home() -> None:
    decl = _parse(_yaml(_yaml_env(file="~/.local/state/creche/vmm.env")))

    assert decl == EnvDecl(file=f"{OPERATOR_HOME}/.local/state/creche/vmm.env", keys=(KEY,))


def test_a_verify_env_file_equal_to_the_env_file_is_taken() -> None:
    text = _yaml(_yaml_env()).replace('"--json"]', f'"--json", "--env-file", "{ENV_FILE}"]')

    assert _parse(text) == EnvDecl(file=ENV_FILE, keys=(KEY,))


def test_the_reader_takes_the_new_minor_and_no_later_one() -> None:
    """The `env` table came with minor 27 of the manifest contract."""
    assert MANIFEST_CONTRACT_MINOR == 27
    newest = manifest_text(COMPONENT).replace('manifest_version: "0.4"', 'manifest_version: "0.27"')
    later = newest.replace('"0.27"', '"0.28"')

    assert _parse(newest + _yaml_env()) == EnvDecl(file=ENV_FILE, keys=(KEY,))
    assert "outside" in _refusal(later).detail


# -- refused --------------------------------------------------------------


def test_a_component_of_another_repository_has_no_env_table() -> None:
    refusal = _refusal(_yaml(_yaml_env(), name="mcp-servers"))

    assert refusal.code is RefusalCode.MANIFEST
    assert refusal.detail == ENV_NOT_CRECHE


@pytest.mark.parametrize(
    ("env", "detail"),
    [
        ("env: [x]\n", "field 'env' must be a mapping"),
        (f"env:\n  keys: [{KEY}]\n", "field 'file' must be a string"),
        (_yaml_env(file="vmm.env"), "field 'env.file' must be an absolute path"),
        (_yaml_env(file="/srv/../etc/vmm.env"), "field 'env.file' must not hold '..'"),
        (f"env:\n  file: {ENV_FILE}\n", ENV_NO_KEYS),
        (_yaml_env(keys="[]"), ENV_NO_KEYS),
        (_yaml_env(keys=KEY), ENV_NO_KEYS),
        (_yaml_env(keys="[vmm_git_spool_root]"), ENV_BAD_KEY),
        (_yaml_env(keys="[1]"), ENV_BAD_KEY),
        (_yaml_env(keys=f"[{KEY}, {KEY}]"), ENV_REPEATED_KEY),
        (_yaml_env(keys="[" + ", ".join(f"K{n}" for n in range(65)) + "]"), "more than 64"),
        (f"env:\n  file: {ENV_FILE}\n  keys: [{KEY}]\n  mode: 600\n", "unknown field: mode"),
    ],
)
def test_each_fault_of_the_table_is_refused(env: str, detail: str) -> None:
    refusal = _refusal(_yaml(env))

    assert refusal.code is RefusalCode.MANIFEST
    assert detail in refusal.detail


def test_a_verify_env_file_that_is_not_the_env_file_is_refused() -> None:
    other = "/srv/agents/state/creche/other.env"
    text = _yaml(_yaml_env()).replace('"--json"]', f'"--json", "--env-file", "{other}"]')

    assert _refusal(text).detail == ENV_OTHER_FILE


def test_the_toml_form_has_the_same_refusals() -> None:
    text = manifest_toml(COMPONENT) + f'env.file = "{ENV_FILE}"\nenv.keys = ["{KEY}", "{KEY}"]\n'

    assert _refusal(text, Markup.TOML).detail == ENV_REPEATED_KEY

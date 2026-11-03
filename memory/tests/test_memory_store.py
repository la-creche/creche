"""The store settings reach Cognee under the env names it reads."""

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
from creche_memory import store
from creche_memory.store import StoreSettings, cognee_env, configure

KEY = "sk-test-not-a-real-key"

#: Imports Cognee after `configure` and prints what its own settings hold.
#: It runs in a process of its own, because Cognee reads the env only once.
PROBE = """
import json, sys
from pathlib import Path
from creche_memory.store import StoreSettings, configure

root = Path(sys.argv[1])
configure(StoreSettings(
    store_root=root / "store",
    llm_base_url="http://llm.test:4000",
    llm_key=sys.argv[2],
    embedding_base_url="http://embed.test:8085",
    tokenizer_cache=root / "hf",
))

from cognee.base_config import get_base_config
from cognee.infrastructure.databases.vector.embeddings.config import get_embedding_config
from cognee.infrastructure.llm.config import get_llm_config

base, llm, embed = get_base_config(), get_llm_config(), get_embedding_config()
print(json.dumps({
    "system": base.system_root_directory,
    "data": base.data_root_directory,
    "logs": base.logs_root_directory,
    "llm_model": llm.llm_model,
    "llm_endpoint": llm.llm_endpoint,
    "llm_key": llm.llm_api_key == sys.argv[2],
    "llm_args": llm.llm_args,
    "rate": [llm.llm_rate_limit_enabled, llm.llm_rate_limit_requests],
    "embedding": [embed.embedding_model, embed.embedding_endpoint, embed.embedding_dimensions],
}))
"""


def _settings(root: Path, **changes: object) -> StoreSettings:
    settings = StoreSettings(
        store_root=root / "store",
        llm_base_url="http://llm.test:4000",
        llm_key=KEY,
        embedding_base_url="http://embed.test:8085",
        tokenizer_cache=root / "hf",
    )

    return dataclasses.replace(settings, **changes)


def test_cognee_reads_each_setting(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(tmp_path), KEY],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr[-2000:]

    seen = json.loads(done.stdout.strip().splitlines()[-1])

    assert seen["system"] == str(tmp_path / "store" / "system")
    assert seen["data"] == str(tmp_path / "store" / "data")
    assert seen["logs"] == str(tmp_path / "store" / "logs")
    assert seen["llm_model"] == store.LLM_MODEL
    assert seen["llm_endpoint"] == "http://llm.test:4000/v1"
    assert seen["llm_key"] is True
    assert seen["llm_args"] == store.LLM_ARGS
    assert seen["rate"] == [True, store.LLM_REQUESTS_PER_MINUTE]
    assert seen["embedding"] == [
        store.EMBEDDING_MODEL,
        "http://embed.test:8085/v1",
        store.EMBEDDING_DIMENSIONS,
    ]


def test_the_env_turns_telemetry_off_and_reads_the_tokenizer_offline(tmp_path: Path) -> None:
    env = cognee_env(_settings(tmp_path))

    assert env["TELEMETRY_DISABLED"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HF_HUB_CACHE"] == str(tmp_path / "hf")


def test_the_key_is_not_in_the_repr(tmp_path: Path) -> None:
    assert KEY not in repr(_settings(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"llm_base_url": "http://llm.test:4000/v1"},
        {"embedding_base_url": "embed.test:8085"},
        {"llm_key": "  "},
        {"store_root": Path("relative/store")},
    ],
    ids=["a URL with /v1", "a URL with no scheme", "an empty key", "a relative store root"],
)
def test_settings_refuse_a_bad_value(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _settings(tmp_path, **changes)


def test_configure_refuses_after_cognee_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "cognee", object())

    with pytest.raises(RuntimeError):
        configure(_settings(tmp_path))

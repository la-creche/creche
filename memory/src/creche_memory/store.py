"""The settings of the memory store, and the env that Cognee reads them from.

Cognee reads its settings from the process env when it is first imported.
So `configure` sets the env, and it refuses to run once Cognee is imported.
The values come from the Cognee spike on the host: reasoning off, and small
batches at 40 requests per minute, because the model provider rate-limits
every family on one account."""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: The LiteLLM alias that the `memory` key allows (decision MM10).
LLM_MODEL = "openai/fast"
LLM_MAX_COMPLETION_TOKENS = 8192
LLM_REQUESTS_PER_MINUTE = 40
LLM_RATE_INTERVAL_S = 60

#: Reasoning tokens cost about four times the answer and add nothing to an
#: extraction.
LLM_ARGS = {"reasoning_effort": "none"}

#: The model that the embedding server serves, its size and its input limit.
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIMENSIONS = 768
EMBEDDING_MAX_TOKENS = 512

#: The embedding server takes no key. Cognee's client needs a value.
EMBEDDING_KEY = "unused"

URL_SCHEMES = ("http://", "https://")
API_SUFFIX = "/v1"


@dataclass(frozen=True)
class StoreSettings:
    """Where the store lives and which servers it calls."""

    #: The folder of the store. Cognee keeps its databases, files, cache and
    #: logs here. It is outside the vault.
    store_root: Path
    #: The base URL of LiteLLM, with no `/v1`.
    llm_base_url: str
    #: The `memory` key of LiteLLM. It is never printed.
    llm_key: str = field(repr=False)
    #: The base URL of the embedding server, with no `/v1`.
    embedding_base_url: str
    #: A Hugging Face hub cache that holds the tokenizer of `EMBEDDING_MODEL`.
    #: Cognee reads it offline, so a sync downloads nothing.
    tokenizer_cache: Path

    def __post_init__(self) -> None:
        if not self.store_root.is_absolute() or not self.tokenizer_cache.is_absolute():
            raise ValueError("the store root and the tokenizer cache must be absolute paths")

        for url in (self.llm_base_url, self.embedding_base_url):
            if not url.startswith(URL_SCHEMES) or url.rstrip("/").endswith(API_SUFFIX):
                raise ValueError(f"a base URL is http or https, with no {API_SUFFIX}: {url}")

        if not self.llm_key.strip():
            raise ValueError("the LiteLLM key is empty")


def _api(url: str) -> str:
    return url.rstrip("/") + API_SUFFIX


def cognee_env(settings: StoreSettings) -> dict[str, str]:
    """The env names that Cognee 1.6.6 reads, with the values of `settings`."""
    root = settings.store_root

    return {
        "TELEMETRY_DISABLED": "1",
        "COGNEE_LOGS_DIR": str(root / "logs"),
        "DATA_ROOT_DIRECTORY": str(root / "data"),
        "SYSTEM_ROOT_DIRECTORY": str(root / "system"),
        "CACHE_ROOT_DIRECTORY": str(root / "cache"),
        "ENABLE_BACKEND_ACCESS_CONTROL": "false",
        "CACHING": "false",
        "LLM_PROVIDER": "custom",
        "LLM_MODEL": LLM_MODEL,
        "LLM_ENDPOINT": _api(settings.llm_base_url),
        "LLM_API_KEY": settings.llm_key,
        "LLM_MAX_COMPLETION_TOKENS": str(LLM_MAX_COMPLETION_TOKENS),
        "LLM_ARGS": json.dumps(LLM_ARGS),
        "LLM_RATE_LIMIT_ENABLED": "true",
        "LLM_RATE_LIMIT_REQUESTS": str(LLM_REQUESTS_PER_MINUTE),
        "LLM_RATE_LIMIT_INTERVAL": str(LLM_RATE_INTERVAL_S),
        "EMBEDDING_PROVIDER": "openai_compatible",
        "EMBEDDING_MODEL": EMBEDDING_MODEL,
        "EMBEDDING_ENDPOINT": _api(settings.embedding_base_url),
        "EMBEDDING_API_KEY": EMBEDDING_KEY,
        "EMBEDDING_DIMENSIONS": str(EMBEDDING_DIMENSIONS),
        "EMBEDDING_MAX_COMPLETION_TOKENS": str(EMBEDDING_MAX_TOKENS),
        "HUGGINGFACE_TOKENIZER": EMBEDDING_MODEL,
        "HF_HUB_CACHE": str(settings.tokenizer_cache),
        "HF_HUB_OFFLINE": "1",
    }


def configure(settings: StoreSettings) -> None:
    """Set the env of `settings` for Cognee. Call it before the first import
    of Cognee: a later call would change nothing that Cognee reads."""
    if "cognee" in sys.modules:
        raise RuntimeError("cognee is imported already, so its settings are fixed")

    os.environ.update(cognee_env(settings))

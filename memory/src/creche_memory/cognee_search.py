# pyright: basic
"""The calls into Cognee for a search.

Cognee's search results and its SQLAlchemy models carry no full types, so
this module is checked in pyright's basic mode. Each function turns what
Cognee returns into plain typed values before it returns. The rest of the
package stays in strict mode."""

from typing import Any
from uuid import UUID

from .search import Hit
from .sync import DATASET

#: The prefix of a document's external id that the Obsidian connector sets.
NOTE_ID_PREFIX = "obsidian:"


async def cognee_backend(query: str, count: int) -> tuple[str, list[Hit]]:
    """The two searches of Cognee. Neither calls the LLM."""
    import cognee
    from cognee.modules.search.types import SearchType

    graph: list[Any] = await cognee.search(
        query_text=query,
        query_type=SearchType.GRAPH_COMPLETION,
        only_context=True,
        datasets=[DATASET],
        top_k=count,
    )
    chunks: list[Any] = await cognee.search(
        query_text=query,
        query_type=SearchType.CHUNKS,
        include_references=True,
        datasets=[DATASET],
        top_k=count,
    )
    facts = "\n".join(str(item) for item in graph)
    hits = [
        Hit(document_id=str(item["document_id"]), text=str(item["text"]))
        for item in chunks
        if isinstance(item, dict) and "document_id" in item and "text" in item
    ]

    return facts, hits


async def cognee_paths() -> dict[str, str]:
    """The vault path of each document of the dataset, from the external id
    that the Obsidian connector gives each note."""
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    if not datasets:
        return {}

    paths: dict[str, str] = {}
    # The model types the id as a column. At run time it is a UUID.
    for data in await get_dataset_data(UUID(str(datasets[0].id))):
        meta = data.system_metadata
        if not isinstance(meta, dict):
            continue

        external = meta.get("external_id")
        if isinstance(external, str) and external.startswith(NOTE_ID_PREFIX):
            paths[str(data.id)] = external.removeprefix(NOTE_ID_PREFIX)

    return paths

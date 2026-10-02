"""agent-indexer: per-scope hybrid index builder."""

from .indexer import IndexReport, chunk_text, index_scope

__version__ = "0.1.0"

__all__ = ["IndexReport", "__version__", "chunk_text", "index_scope"]

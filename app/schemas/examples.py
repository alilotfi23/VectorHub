"""OpenAPI request examples generated from the live capability matrix.

The request shapes that vary by backend — hybrid fields (``query_text`` vs
``sparse_vector``), sparse vectors on upsert, metadata filtering — get their
Swagger examples built from each backend's ``CapabilityEntry`` rather than
hand-written. Pydantic v2 ``json_schema_extra`` callables (see
``examples_extra``) run while FastAPI builds OpenAPI, so the docs reflect
the registry *at that moment*: flip a backend's capability (or register a
fifth backend) and the examples follow without touching docs code.

Each list builder emits one example per distinct capability flavor across
the live registry (deduplicated), so Swagger shows every valid variant the
matrix currently permits — e.g. HybridQueryRequest shows the text+vector
body (Weaviate) and the sparse+vector body (Qdrant/Milvus), and nothing for
a backend with no hybrid (Chroma). The per-entry helpers exist so tests can
prove the mapping tracks a capability flip.
"""

from collections.abc import Callable
from typing import Any

from app.adapters.base import CapabilityEntry
from app.adapters.registry import registry

_DENSE_VECTOR = [0.1, 0.2, 0.3, 0.4]
_SPARSE_VECTOR = {"indices": [0, 3, 7], "values": [0.35, 0.5, 0.15]}


def _capabilities() -> list[CapabilityEntry]:
    """The live registry's CapabilityEntry rows, in registry order."""
    entries: list[CapabilityEntry] = []
    for name in registry.list():
        entry = registry.capabilities(name)
        if entry is not None:
            entries.append(entry)
    return entries


# --- per-entry builders (pure: testable against a synthetic entry) ----------


def hybrid_example_for(entry: CapabilityEntry) -> dict[str, Any] | None:
    """The HybridQueryRequest body this backend's hybrid mode permits; None
    for backends without hybrid support (Chroma)."""
    mode = entry.hybrid_mode
    if mode is None:
        return None
    body: dict[str, Any] = {"vector": _DENSE_VECTOR, "alpha": 0.75, "top_k": 10}
    if mode == "text+vector":
        body["query_text"] = "keyword phrase for the BM25 side"
    else:  # sparse+vector
        body["sparse_vector"] = _SPARSE_VECTOR
    return body


def vector_record_example(entry: CapabilityEntry) -> dict[str, Any]:
    """The VectorRecordIn body this backend accepts: plain, or with the
    sparse_vector field when the backend stores sparse vectors."""
    body: dict[str, Any] = {"id": "doc-1", "vector": _DENSE_VECTOR, "metadata": {"tag": "demo"}}
    if entry.sparse_vectors:
        body["sparse_vector"] = _SPARSE_VECTOR
    return body


def query_request_example(entry: CapabilityEntry) -> dict[str, Any]:
    """The QueryRequest body this backend accepts: with a metadata filter
    when the backend filters metadata (Weaviate's capability is
    filtering=False — its metadata is JSON text, not a queryable object)."""
    body: dict[str, Any] = {"vector": _DENSE_VECTOR, "top_k": 10}
    if entry.filtering:
        body["filters"] = {"status": "active", "price": {"$gte": 10}}
    return body


# --- registry-driven lists (one example per distinct flavor) ----------------


def hybrid_request_examples() -> list[dict[str, Any]]:
    """One body per distinct hybrid mode in the matrix, registry order."""
    seen: set[str] = set()
    examples: list[dict[str, Any]] = []
    for entry in _capabilities():
        mode = entry.hybrid_mode
        if mode is None or mode in seen:
            continue
        seen.add(mode)
        body = hybrid_example_for(entry)
        if body is not None:
            examples.append(body)
    return examples


def vector_record_examples() -> list[dict[str, Any]]:
    """The plain record always; the sparse variant once, if any backend
    stores sparse vectors (Qdrant/Milvus)."""
    examples: list[dict[str, Any]] = [
        {"id": "doc-1", "vector": _DENSE_VECTOR, "metadata": {"tag": "demo"}}
    ]
    sparse_entries = [entry for entry in _capabilities() if entry.sparse_vectors]
    if sparse_entries:
        examples.append(vector_record_example(sparse_entries[0]))
    return examples


def query_request_examples() -> list[dict[str, Any]]:
    """The plain query always; the filters variant once, if any backend
    filters metadata."""
    examples: list[dict[str, Any]] = [{"vector": _DENSE_VECTOR, "top_k": 10}]
    filtering_entries = [entry for entry in _capabilities() if entry.filtering]
    if filtering_entries:
        examples.append(query_request_example(filtering_entries[0]))
    return examples


def examples_extra(
    builder: Callable[[], list[dict[str, Any]]],
) -> Callable[[dict[str, Any]], None]:
    """A Pydantic v2 ``json_schema_extra`` callable attaching the
    capability-driven examples. Runs while the model's JSON schema is built —
    which is when FastAPI builds OpenAPI — so the docs match the registry at
    that moment. No examples are attached when the registry has no matching
    flavor (e.g. schemas imported before any backend registers)."""

    def _extra(schema: dict[str, Any]) -> None:
        examples = builder()
        if examples:
            schema["examples"] = examples

    return _extra

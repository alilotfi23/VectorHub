"""Capability-matrix-driven OpenAPI examples (docs-track-the-feature-set).

Two layers of proof:

1. Per-entry builders are pure: given a synthetic ``CapabilityEntry``, the
   example body follows the capability — flip the capability and the example
   flips (the "docs always match the current backend feature set" contract
   at unit level).

2. The live OpenAPI reflects the registry at generation time: each distinct
   hybrid mode / sparse / filtering flavor in the matrix appears as a
   request-body example on the corresponding schema, and a backend leaving
   the registry removes its example.
"""

from dataclasses import replace
from typing import Any

from fastapi.openapi.utils import get_openapi

import app.adapters  # noqa: F401  (registers the four built-ins)
from app.adapters.base import CapabilityEntry
from app.adapters.registry import registry
from app.main import app as platform_app
from app.schemas.examples import (
    hybrid_example_for,
    query_request_example,
    vector_record_example,
)
from tests.support import registry_preserved


def _entry(**overrides: Any) -> CapabilityEntry:
    base = CapabilityEntry(
        backend="stub",
        tenancy_model="native-tenant",
        hybrid_mode=None,
        sparse_required=False,
        filtering=True,
        batch_async=True,
        quantization=False,
        multi_vector=False,
        sparse_vectors=False,
    )
    return replace(base, **overrides)


# --- per-entry builders track a capability flip -----------------------------


def test_hybrid_example_follows_hybrid_mode() -> None:
    """text+vector -> query_text body; sparse+vector -> sparse_vector body;
    no hybrid -> no example. The mapping is the capability, not the backend."""
    text = hybrid_example_for(_entry(hybrid_mode="text+vector"))
    assert text is not None
    assert text["query_text"] == "keyword phrase for the BM25 side"
    assert "sparse_vector" not in text

    sparse = hybrid_example_for(
        _entry(hybrid_mode="sparse+vector", sparse_required=True, sparse_vectors=True)
    )
    assert sparse is not None
    assert sparse["sparse_vector"] == {"indices": [0, 3, 7], "values": [0.35, 0.5, 0.15]}
    assert "query_text" not in sparse

    assert hybrid_example_for(_entry(hybrid_mode=None)) is None


def test_vector_record_example_follows_sparse_capability() -> None:
    """A backend that stores sparse vectors documents the sparse_vector field
    on the record; one that doesn't omits it."""
    plain = vector_record_example(_entry(sparse_vectors=False))
    assert "sparse_vector" not in plain
    sparse = vector_record_example(_entry(sparse_vectors=True))
    assert "sparse_vector" in sparse
    assert sparse["id"] == "doc-1"


def test_query_request_example_follows_filtering_capability() -> None:
    """filtering=True backends document the metadata-filter variant (all
    built-ins except Weaviate — whose metadata is JSON text, not queryable)."""
    plain = query_request_example(_entry(filtering=False))
    assert "filters" not in plain
    filtered = query_request_example(_entry(filtering=True))
    assert filtered["filters"] == {"status": "active", "price": {"$gte": 10}}


# --- the live OpenAPI is generated from the registry ------------------------


def _openapi() -> dict[str, Any]:
    return get_openapi(title="test", version="0", routes=platform_app.routes)


def _examples(schema_name: str) -> list[dict[str, Any]]:
    docs = _openapi()
    schema = docs["components"]["schemas"][schema_name]
    return list(schema.get("examples", [])) if isinstance(schema, dict) else []


def _live_entries() -> list[CapabilityEntry]:
    entries: list[CapabilityEntry] = []
    for name in registry.list():
        entry = registry.capabilities(name)
        if entry is not None:
            entries.append(entry)
    return entries


def test_openapi_hybrid_examples_cover_every_supported_mode() -> None:
    """One HybridQueryRequest example per distinct hybrid mode in the live
    matrix — Weaviate's text+vector body and Qdrant/Milvus' sparse+vector
    body — and nothing for the hybrid-less backend (Chroma)."""
    modes = {e.hybrid_mode for e in _live_entries() if e.hybrid_mode}
    assert modes, "the four built-ins must be registered"
    examples = _examples("HybridQueryRequest")

    if "text+vector" in modes:
        assert any("query_text" in ex and "sparse_vector" not in ex for ex in examples), (
            "text+vector mode must be documented"
        )
    if "sparse+vector" in modes:
        assert any("sparse_vector" in ex and "query_text" not in ex for ex in examples), (
            "sparse+vector mode must be documented"
        )
    # One example per mode, no duplicates, none beyond the supported modes.
    assert len(examples) == len(modes)


def test_openapi_query_and_record_examples_follow_filtering_and_sparse_flavors() -> None:
    """The filter variant appears iff a registered backend filters metadata;
    the sparse record variant appears iff a registered backend stores sparse
    vectors (both true for the four built-ins)."""
    entries = _live_entries()

    query_examples = _examples("QueryRequest")
    if any(e.filtering for e in entries):
        assert any("filters" in ex for ex in query_examples)
    else:
        assert all("filters" not in ex for ex in query_examples)

    record_examples = _examples("VectorRecordIn")
    if any(e.sparse_vectors for e in entries):
        assert any("sparse_vector" in ex for ex in record_examples)
    else:
        assert all("sparse_vector" not in ex for ex in record_examples)


def test_openapi_examples_track_a_backend_leaving_the_registry() -> None:
    """Unregister the only text+vector backend and its hybrid example
    disappears from OpenAPI; re-register and it returns — the docs are
    generated from the live feature set, not hand-edited per backend."""
    weaviate_modes = {
        e.hybrid_mode for e in _live_entries() if e.backend == "weaviate" and e.hybrid_mode
    }
    assert weaviate_modes == {"text+vector"}, "weaviate is the text+vector backend"

    with registry_preserved("weaviate"):
        registry.unregister("weaviate")
        examples = _examples("HybridQueryRequest")
        assert not any("query_text" in ex for ex in examples)

    # The displaced instance is restored — the registry is back to the live
    # feature set regardless of what the test did.
    examples = _examples("HybridQueryRequest")
    assert any("query_text" in ex for ex in examples)

"""CapabilityMatrix endpoint (Phase 6) — ``GET /api/v1/capabilities``.

Lets clients introspect which features each backend supports before calling
(hybrid mode + sparse requirement, filtering, batch async, quantization,
multi-vector, sparse vectors, the mutable PATCH /config subset, and the
worker's default batch chunk size). Keeps the abstraction honest: the matrix
is generated from the live registry, so a registered fifth backend appears
here without touching the endpoint.

The ``hybrid`` field is reshaped into the canonical introspection shape from
CLAUDE.md: ``{"mode": "sparse+vector" | "text+vector" | false,
"sparse_required": bool}`` — the field clients check before calling
``POST /collections/{name}/hybrid-query``.
"""

from typing import Any

from fastapi import APIRouter

from app.adapters.registry import registry

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


def _shape(entry: Any) -> dict[str, Any]:
    """CapabilityEntry -> the wire shape (hybrid flattened into the
    documented {mode, sparse_required} object)."""
    hybrid_mode = getattr(entry, "hybrid_mode", None)
    return {
        "backend": getattr(entry, "backend", ""),
        "tenancy_model": getattr(entry, "tenancy_model", ""),
        "hybrid": {
            "mode": hybrid_mode if hybrid_mode is not None else False,
            "sparse_required": bool(getattr(entry, "sparse_required", False)),
        },
        "filtering": bool(getattr(entry, "filtering", False)),
        "batch_async": bool(getattr(entry, "batch_async", False)),
        "quantization": bool(getattr(entry, "quantization", False)),
        "multi_vector": bool(getattr(entry, "multi_vector", False)),
        "sparse_vectors": bool(getattr(entry, "sparse_vectors", False)),
        "mutable_config": sorted(getattr(entry, "mutable_config", frozenset())),
        "default_batch_chunk_size": int(getattr(entry, "default_batch_chunk_size", 1000)),
        "notes": list(getattr(entry, "notes", ())),
    }


@router.get("")
async def get_capabilities() -> dict[str, Any]:
    """Per-backend feature matrix, keyed by backend name. Any adapter
    registered with the registry appears here — the pluggable contract means
    a fifth backend is visible without changing this route."""
    result: dict[str, Any] = {}
    for name in registry.list():
        adapter = registry.get(name)
        if adapter is not None:
            result[name] = _shape(adapter.capability())
    return result

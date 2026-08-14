"""Pluggable adapter registry (Phase 3: class-based, singleton-per-backend).

The full contract from CLAUDE.md:

- ``register(backend_name, adapter_cls)`` — the registry constructs exactly
  **one** instance per backend name and holds it for the app's lifetime
  (never per-request, never per-tenant). Registration also accepts an already
  built instance (duck-typed to ``HealthCheckable``); that path exists for
  test fixtures and the /health probe's trivial adapters — production code
  registers classes.
- ``unregister(backend_name)``, ``get(backend_name) -> VectorDBAdapter``,
  ``list() -> list[str]`` (sorted), and
  ``capabilities(backend_name) -> CapabilityEntry | None`` (None when the
  backend isn't registered — GET /capabilities, Phase 6, reports only
  registered backends).
- The built-in adapters register themselves at startup through this same
  API (``app/adapters/__init__.py``), so a fifth backend later (e.g.
  Pinecone) is "write an adapter + register it", never a change to the
  routing layer. As of Phase 3 only Chroma is implemented; Qdrant, Weaviate
  and Milvus register in their phases.

**Singleton-per-backend assumption (documented per CLAUDE.md):** each backend
name maps to exactly one configured instance — all tenants whose collections
live on the platform's single self-hosted Qdrant share that one client and
connection pool; isolation is enforced by the vector backend's native tenancy
mechanism, never by separate physical connections. If cloud-managed mode ever
needs per-tenant vector-DB endpoints (tenant A on their own Qdrant Cloud
cluster, tenant B on another), that is a distinct future capability — a
``backend_instance_id`` resolved per-collection alongside ``backend`` — and
is explicitly out of scope for v1. Do not silently violate this by creating
clients per request or per tenant.
"""

from typing import Any, Protocol

from app.adapters.base import CapabilityEntry, VectorDBAdapter


class HealthCheckable(Protocol):
    """Minimal surface the /health probe needs — satisfied by full
    VectorDBAdapter implementations and by the health tests' trivial
    adapters (which are not full backends)."""

    async def health_check(self) -> None: ...


type AdapterLike = type[VectorDBAdapter] | HealthCheckable


class AdapterRegistry:
    """Maps backend names to adapter instances. Pluggable by design."""

    def __init__(self) -> None:
        self._adapters: dict[str, VectorDBAdapter] = {}
        self._raw: dict[str, AdapterLike] = {}

    def register(
        self,
        backend_name: str,
        adapter: AdapterLike,
        **init_kwargs: Any,
    ) -> VectorDBAdapter:
        """Register a backend. Pass a class to have the registry construct
        the singleton (optionally with ``init_kwargs``, e.g. a test
        container URL); pass an instance to register it as-is. Re-registering
        a name replaces the previous instance.

        Construction must be side-effect-free (no network): adapters build
        their SDK client lazily on first use, so registering at import time
        is safe even when the backend isn't running.
        """
        if init_kwargs and not isinstance(adapter, type):
            raise ValueError("init_kwargs are only valid when registering a class")
        instance = adapter(**init_kwargs) if isinstance(adapter, type) else adapter
        self._adapters[backend_name] = instance  # type: ignore[assignment]
        self._raw[backend_name] = adapter
        return instance  # type: ignore[return-value]

    def unregister(self, backend_name: str) -> None:
        self._adapters.pop(backend_name, None)
        self._raw.pop(backend_name, None)

    def get(self, backend_name: str) -> VectorDBAdapter | None:
        return self._adapters.get(backend_name)

    def list(self) -> list[str]:
        return sorted(self._adapters)

    def capabilities(self, backend_name: str) -> CapabilityEntry | None:
        """The backend's CapabilityMatrix row, or None when the backend isn't
        registered (test doubles without a capability are None too)."""
        adapter = self._adapters.get(backend_name)
        if adapter is None:
            return None
        capability = getattr(adapter, "capability", None)
        return capability() if callable(capability) else None


# Process singleton. Each registered adapter owns exactly one SDK client for
# the app's lifetime (see CLAUDE.md's adapter-client-lifecycle note); all
# tenants sharing a backend share that one client/connection pool.
registry = AdapterRegistry()

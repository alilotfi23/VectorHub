"""Pluggable adapter registry (v0.5 scaffold, pulled forward for GET /health).

Phase 3 evolves this into the full contract from CLAUDE.md: class-based
`register(backend_name, adapter_cls)` backed by a `VectorDBAdapter` ABC,
with each registered adapter owning exactly one SDK client for the app's
lifetime (never per-request, never per-tenant) and the four built-ins
registering themselves at startup through this same API — so a fifth
backend is "write an adapter + register it", never a change to the
routing layer.

For now the registry holds *instances* exposing `health_check()`, which is
the only surface GET /health needs: it iterates `list()` and probes each
backend. Nothing is registered pre-Phase-3, so the probe reports an empty
`adapters` map until the built-ins land.
"""

from typing import Protocol


class HealthCheckable(Protocol):
    """Minimal surface the health probe needs from an adapter (pre-ABC)."""

    async def health_check(self) -> None: ...


class AdapterRegistry:
    """Maps backend names to adapter instances. Pluggable by design."""

    def __init__(self) -> None:
        self._adapters: dict[str, HealthCheckable] = {}

    def register(self, backend_name: str, adapter: HealthCheckable) -> None:
        self._adapters[backend_name] = adapter

    def unregister(self, backend_name: str) -> None:
        self._adapters.pop(backend_name, None)

    def get(self, backend_name: str) -> HealthCheckable | None:
        return self._adapters.get(backend_name)

    def list(self) -> list[str]:
        return sorted(self._adapters)


# Process singleton. Each registered adapter owns exactly one SDK client for
# the app's lifetime (see CLAUDE.md's adapter-client-lifecycle note); all
# tenants sharing a backend share that one client/connection pool.
registry = AdapterRegistry()

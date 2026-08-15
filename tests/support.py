"""Shared test helpers (no pytest fixtures — plain utilities)."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.adapters.registry import registry


@contextmanager
def registry_preserved(*backend_names: str) -> Iterator[None]:
    """Temporarily replace (or unregister) the named backends; whatever was
    registered before is restored on exit.

    Restoring the *displaced instance* matters, not re-registering the
    adapter class: a class re-registration rebuilds the adapter from settings
    defaults, silently discarding the testcontainer URL the integration
    layer's session-scoped fixtures had installed. That poisoning (the
    health-fixture bug — see tests/integration/test_health_api.py) left
    every later backend-dependent test pointing at dead localhost ports:
    ``COLLECTION_BACKEND_UNAVAILABLE`` on collection create. Snapshoting the
    exact displaced instance keeps the health tests deterministic (dead URLs
    for the duration) without corrupting registry state for the rest of the
    session.
    """
    saved = {name: registry.get(name) for name in backend_names}
    try:
        yield
    finally:
        for name in backend_names:
            instance = saved[name]
            if instance is None:
                registry.unregister(name)
            else:
                registry.register(name, instance)

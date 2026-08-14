"""Vector DB adapter layer — the four built-ins register themselves here.

Importing this package registers every implemented backend with the process
singleton ``registry`` via the same public API a fifth backend would use
(``registry.register(name, adapter_cls)``) — the pluggable contract from
CLAUDE.md. As of Phase 4: Chroma, Qdrant and Weaviate are implemented;
Milvus joins in Phase 5. Registration constructs adapters (no network —
clients are lazy), so importing this package is side-effect-free beyond the
registry map.
"""

from app.adapters.base import CapabilityEntry, VectorDBAdapter, VectorRecord
from app.adapters.chroma_adapter import ChromaAdapter
from app.adapters.qdrant_adapter import QdrantAdapter
from app.adapters.registry import AdapterRegistry, registry
from app.adapters.weaviate_adapter import WeaviateAdapter

# Each backend registers exactly one singleton for the app's lifetime
# (singleton-per-backend; see the registry docstring).
registry.register("chroma", ChromaAdapter)
registry.register("qdrant", QdrantAdapter)
registry.register("weaviate", WeaviateAdapter)

__all__ = [
    "AdapterRegistry",
    "CapabilityEntry",
    "ChromaAdapter",
    "QdrantAdapter",
    "VectorDBAdapter",
    "VectorRecord",
    "WeaviateAdapter",
    "registry",
]

"""Vector DB adapter layer — the four built-ins register themselves here.

Importing this package registers every implemented backend with the process
singleton ``registry`` via the same public API a fifth backend would use
(``registry.register(name, adapter_cls)``) — the pluggable contract from
CLAUDE.md. As of Phase 5: Chroma, Qdrant, Weaviate and Milvus are all
implemented. Registration constructs adapters (no network — clients are
lazy), so importing this package is side-effect-free beyond the registry map.
"""

from app.adapters.base import CapabilityEntry, VectorDBAdapter, VectorRecord
from app.adapters.chroma_adapter import ChromaAdapter
from app.adapters.milvus_adapter import MilvusAdapter
from app.adapters.qdrant_adapter import QdrantAdapter
from app.adapters.registry import AdapterRegistry, registry
from app.adapters.weaviate_adapter import WeaviateAdapter

# Each backend registers exactly one singleton for the app's lifetime
# (singleton-per-backend; see the registry docstring).
registry.register("chroma", ChromaAdapter)
registry.register("qdrant", QdrantAdapter)
registry.register("weaviate", WeaviateAdapter)
registry.register("milvus", MilvusAdapter)

__all__ = [
    "AdapterRegistry",
    "CapabilityEntry",
    "ChromaAdapter",
    "MilvusAdapter",
    "QdrantAdapter",
    "VectorDBAdapter",
    "VectorRecord",
    "WeaviateAdapter",
    "registry",
]

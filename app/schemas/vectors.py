"""Vector and query schemas (Phase 3).

All request envelopes are strict (``extra=\"forbid\"``) — there is no
``tenant_id`` or owner field on any envelope, per the isolation-suite R3/E3
contract; tenant identity is derived exclusively from the authenticated
principal. The platform limits (vector dimension, sync batch size, top_k) are
enforced here as Pydantic constraints (documented in OpenAPI) and mapped to
their taxonomy error codes by the RequestValidationError handler in
app/main.py.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.schemas.auth import StrictRequest

_VECTOR_MAX_DIMENSION = get_settings().vector_max_dimension

RESERVED_METADATA_PREFIX = "_vhk_"


class VectorRecordIn(StrictRequest):
    """One client-supplied vector record. No tenant_id, no timestamps —
    both are derived server-side."""

    id: str = Field(
        min_length=1,
        max_length=512,
        description="Client-supplied id; upserts are idempotent on this id",
        examples=["doc-1"],
    )
    vector: list[float] = Field(
        min_length=1,
        max_length=_VECTOR_MAX_DIMENSION,
        description=f"Pre-computed embedding, 1–{_VECTOR_MAX_DIMENSION} floats",
        examples=[[0.1, 0.2, 0.3]],
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary user payload; backend-native filtering applies to primitive-valued "
            f"fields. Keys with the reserved '{RESERVED_METADATA_PREFIX}' prefix are rejected."
        ),
    )

    @field_validator("metadata")
    @classmethod
    def _reject_reserved_keys(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        for key in metadata:
            if key.startswith(RESERVED_METADATA_PREFIX):
                raise ValueError(
                    f"metadata keys with the reserved '{RESERVED_METADATA_PREFIX}' prefix "
                    "are not allowed"
                )
        return metadata


class VectorUpsertRequest(StrictRequest):
    vectors: list[VectorRecordIn] = Field(
        min_length=1,
        max_length=100,
        description=(
            "1–100 records per sync request (VECTOR_MAX_DIMENSION caps each vector). "
            "Larger loads must use the async batch endpoint "
            "POST /api/v1/collections/{name}/vectors/batch."
        ),
    )


class VectorResponse(BaseModel):
    id: str
    vector: list[float]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class UpsertResponse(BaseModel):
    upserted: int


# --- Metadata filters (the normalized v1 subset, chroma-shaped) ---
#
# The platform's filter DSL for Phase 3: a JSON object where field keys map
# to either a scalar (equality shorthand) or an operator dict over
# $eq/$ne/$gt/$gte/$lt/$lte/$in/$nin/$contains/$not_contains, and $and/$or
# nest lists of filters ($not wraps a single filter). This maps 1:1 onto
# Chroma's ``where``; Phase 4's adapters translate it to their native filter
# forms (Qdrant payload filters, etc.). Shape is validated here; semantic
# rejection by a backend maps to VALIDATION_INVALID_FILTER.

_FILTER_OPS = frozenset(
    {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$contains", "$not_contains"}
)
_FILTER_LOGIC = frozenset({"$and", "$or", "$not"})

_JSON_PRIMITIVES = (str, int, float, bool)


def validate_filter(node: Any, path: str = "filters") -> None:
    """Validate the normalized filter tree; raises ValueError on malformed
    shapes (mapped to a 422 VALIDATION_GENERIC by the app's handler)."""
    if not isinstance(node, dict):
        raise ValueError(f"{path}: metadata filter must be a JSON object")
    for key, value in node.items():
        if key in _FILTER_LOGIC:
            if key == "$not":
                validate_filter(value, f"{path}.$not")
                continue
            if not isinstance(value, list) or not value:
                raise ValueError(f"{path}.{key}: must be a non-empty list of filters")
            for i, sub in enumerate(value):
                validate_filter(sub, f"{path}.{key}[{i}]")
        elif key.startswith("$"):
            raise ValueError(f"{path}: unknown operator '{key}'")
        elif isinstance(value, dict):
            for op, operand in value.items():
                if op not in _FILTER_OPS:
                    raise ValueError(f"{path}.{key}: unknown operator '{op}'")
                if op in ("$in", "$nin"):
                    if not isinstance(operand, list) or not operand:
                        raise ValueError(f"{path}.{key}.{op}: must be a non-empty list")
                    for item in operand:
                        if not isinstance(item, _JSON_PRIMITIVES):
                            raise ValueError(
                                f"{path}.{key}.{op}: list values must be JSON primitives"
                            )
                elif not isinstance(operand, _JSON_PRIMITIVES) and operand is not None:
                    raise ValueError(f"{path}.{key}.{op}: value must be a JSON primitive")
        elif not isinstance(value, _JSON_PRIMITIVES) and value is not None:
            raise ValueError(f"{path}.{key}: value must be a JSON primitive")


class QueryRequest(StrictRequest):
    vector: list[float] = Field(
        min_length=1,
        max_length=_VECTOR_MAX_DIMENSION,
        description=f"Pre-computed query embedding, 1–{_VECTOR_MAX_DIMENSION} floats",
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Number of results, 1–1000 (platform-wide ceiling regardless of backend)",
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Normalized metadata filter: equality shorthand or operator dicts "
            "($eq/$ne/$gt/$gte/$lt/$lte/$in/$nin/$contains/$not_contains), composed with "
            "$and/$or/$not. See the OpenAPI description of the platform filter subset."
        ),
    )

    @field_validator("filters")
    @classmethod
    def _validate_filters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            validate_filter(value)
        return value


class QueryResultOut(BaseModel):
    id: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class QueryResponse(BaseModel):
    results: list[QueryResultOut]

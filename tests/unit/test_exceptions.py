from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.exceptions import AppError, ErrorCode, error_response_handler


def test_app_error_handler_shape() -> None:
    app = FastAPI()
    app.add_exception_handler(AppError, error_response_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(
            ErrorCode.COLLECTION_NOT_FOUND,
            "Collection not found",
            {"hint": "check the name"},
            status_code=404,
        )

    resp = TestClient(app).get("/boom")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "COLLECTION_NOT_FOUND"
    assert body["message"] == "Collection not found"
    assert body["details"] == {"hint": "check the name"}


def test_taxonomy_covers_required_codes() -> None:
    required = {
        "AUTH_INVALID_CREDENTIALS",
        "AUTH_TOKEN_EXPIRED",
        "AUTH_TOKEN_REVOKED",
        "AUTH_INSUFFICIENT_SCOPE",
        "TENANT_NOT_FOUND",
        "TENANT_QUOTA_EXCEEDED",
        "COLLECTION_NOT_FOUND",
        "COLLECTION_ALREADY_EXISTS",
        "COLLECTION_BACKEND_UNAVAILABLE",
        "REQUIRES_REINDEX",
        "REINDEX_NOT_IMPLEMENTED",
        "VECTOR_NOT_FOUND",
        "VECTOR_DIMENSION_MISMATCH",
        "VECTOR_DIMENSION_EXCEEDED",
        "BATCH_SIZE_EXCEEDED",
        "TOP_K_EXCEEDED",
        "VECTOR_SPARSE_REQUIRED",
        "JOB_NOT_FOUND",
        "JOB_FAILED",
        "JOB_PAYLOAD_INVALID",
        "RATE_LIMIT_TENANT_QPS",
        "RATE_LIMIT_API_KEY_QPS",
        "RATE_LIMIT_ROUTE_QPS",
        "VALIDATION_UNSUPPORTED_OPERATION",
    }
    assert {c.value for c in ErrorCode} >= required

from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorCode(StrEnum):
    """Error taxonomy from CLAUDE.md — namespaced, SCREAMING_SNAKE_CASE.
    Extend within a namespace as needed; never introduce a new namespace
    without updating the taxonomy in CLAUDE.md."""

    # AUTH_*
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_TOKEN_REVOKED = "AUTH_TOKEN_REVOKED"
    AUTH_INSUFFICIENT_SCOPE = "AUTH_INSUFFICIENT_SCOPE"
    AUTH_EMAIL_TAKEN = "AUTH_EMAIL_TAKEN"
    # API_KEY_*
    API_KEY_NOT_FOUND = "API_KEY_NOT_FOUND"
    # TENANT_*
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    TENANT_ALREADY_EXISTS = "TENANT_ALREADY_EXISTS"
    TENANT_MEMBER_NOT_FOUND = "TENANT_MEMBER_NOT_FOUND"
    TENANT_LAST_OWNER = "TENANT_LAST_OWNER"
    TENANT_QUOTA_EXCEEDED = "TENANT_QUOTA_EXCEEDED"
    # COLLECTION_*
    COLLECTION_NOT_FOUND = "COLLECTION_NOT_FOUND"
    COLLECTION_ALREADY_EXISTS = "COLLECTION_ALREADY_EXISTS"
    COLLECTION_BACKEND_UNAVAILABLE = "COLLECTION_BACKEND_UNAVAILABLE"
    REQUIRES_REINDEX = "REQUIRES_REINDEX"
    REINDEX_NOT_IMPLEMENTED = "REINDEX_NOT_IMPLEMENTED"
    # VECTOR_*
    VECTOR_NOT_FOUND = "VECTOR_NOT_FOUND"
    VECTOR_DIMENSION_MISMATCH = "VECTOR_DIMENSION_MISMATCH"
    VECTOR_DIMENSION_EXCEEDED = "VECTOR_DIMENSION_EXCEEDED"
    BATCH_SIZE_EXCEEDED = "BATCH_SIZE_EXCEEDED"
    TOP_K_EXCEEDED = "TOP_K_EXCEEDED"
    VECTOR_SPARSE_REQUIRED = "VECTOR_SPARSE_REQUIRED"
    # JOB_*
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_FAILED = "JOB_FAILED"
    JOB_PAYLOAD_INVALID = "JOB_PAYLOAD_INVALID"
    # RATE_LIMIT_*
    RATE_LIMIT_TENANT_QPS = "RATE_LIMIT_TENANT_QPS"
    RATE_LIMIT_API_KEY_QPS = "RATE_LIMIT_API_KEY_QPS"
    RATE_LIMIT_ROUTE_QPS = "RATE_LIMIT_ROUTE_QPS"
    # VALIDATION_*
    VALIDATION_UNSUPPORTED_OPERATION = "VALIDATION_UNSUPPORTED_OPERATION"


class ErrorResponse(BaseModel):
    error_code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class AppError(Exception):
    """Base for all platform errors. Services/routes raise AppError (or a
    subclass) with an ErrorCode — never raw strings."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int = 400,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code
        super().__init__(message)


async def error_response_handler(request: Request, exc: Exception) -> JSONResponse:
    # Registered only for AppError; anything else re-raises to Starlette's
    # default 500 handler (Starlette requires the Exception-typed signature).
    if not isinstance(exc, AppError):
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error_code=exc.code, message=exc.message, details=exc.details
        ).model_dump(mode="json"),
    )

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal
from app.core.security import Principal
from app.db.session import get_session
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import AuthService
from app.services.tenant_service import TenantService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    body: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> AuthResponse:
    user, pair = await AuthService(session).register(
        email=body.email, password=body.password, tenant_name=body.tenant_name
    )
    return AuthResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    user, pair = await AuthService(session).login(email=body.email, password=body.password)
    return AuthResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    pair = await AuthService(session).refresh(body.refresh_token)
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


@router.post("/logout", status_code=204)
async def logout(
    body: LogoutRequest,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke both credentials: the refresh token in the body and the
    presented access token's jti (deny-listed, so it dies immediately).
    Requires a valid bearer access token — a client with an expired one
    refreshes first, then logs out (the expired token is already dead)."""
    await AuthService(session).logout(
        body.refresh_token,
        access_jti=request.state.token_jti,
        actor=principal,
    )
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse)
async def me(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    user = await AuthService(session).me(principal)
    tenant = await TenantService(session).get_tenant(principal, tenant_id=user.tenant_id)
    return MeResponse(
        id=user.id,
        email=user.email,
        tenant_id=user.tenant_id,
        tenant_name=tenant.name,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
    )

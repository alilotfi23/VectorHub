from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  (register models on Base.metadata)
from app.db.base import Base
from app.db.models import AuditLog, Collection, Tenant, User


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_tenant_and_collection_roundtrip(session: AsyncSession) -> None:
    tenant = Tenant(name="acme")
    session.add(tenant)
    await session.flush()
    collection = Collection(
        tenant_id=tenant.id,
        name="products",
        backend="chroma",
        dimension=1536,
        distance_metric="cosine",
        physical_name="col_00000000-0000-0000-0000-000000000001",
    )
    session.add(collection)
    await session.commit()

    result = await session.execute(select(Collection).where(Collection.name == "products"))
    fetched = result.scalar_one()
    assert fetched.tenant_id == tenant.id
    assert fetched.physical_name.startswith("col_")


async def test_collection_name_unique_per_tenant(session: AsyncSession) -> None:
    t_a = Tenant(name="a")
    t_b = Tenant(name="b")
    session.add_all([t_a, t_b])
    await session.flush()
    session.add_all(
        [
            Collection(
                tenant_id=t_a.id,
                name="products",
                backend="chroma",
                dimension=1536,
                distance_metric="cosine",
                physical_name="col_a",
            ),
            Collection(
                tenant_id=t_b.id,
                name="products",
                backend="chroma",
                dimension=1536,
                distance_metric="cosine",
                physical_name="col_b",
            ),
        ]
    )
    await session.commit()  # same name, different tenants: OK

    session.add(
        Collection(
            tenant_id=t_a.id,
            name="products",
            backend="chroma",
            dimension=1536,
            distance_metric="cosine",
            physical_name="col_c",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_physical_name_globally_unique(session: AsyncSession) -> None:
    t_a = Tenant(name="a")
    t_b = Tenant(name="b")
    session.add_all([t_a, t_b])
    await session.flush()
    session.add_all(
        [
            Collection(
                tenant_id=t_a.id,
                name="x",
                backend="chroma",
                dimension=16,
                distance_metric="cosine",
                physical_name="col_dup",
            ),
            Collection(
                tenant_id=t_b.id,
                name="y",
                backend="chroma",
                dimension=16,
                distance_metric="cosine",
                physical_name="col_dup",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_audit_log_append(session: AsyncSession) -> None:
    tenant = Tenant(name="acme")
    session.add(tenant)
    await session.flush()
    session.add(
        AuditLog(
            tenant_id=tenant.id,
            actor_id=None,
            action="collection.create",
            resource_type="collection",
            resource_id="col_x",
            details={"backend": "chroma"},
            result="success",
        )
    )
    await session.commit()

    rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "collection.create"
    assert rows[0].details == {"backend": "chroma"}


async def test_user_belongs_to_tenant(session: AsyncSession) -> None:
    tenant = Tenant(name="acme")
    session.add(tenant)
    await session.flush()
    session.add(User(tenant_id=tenant.id, email="a@b.c", password_hash="x", role="viewer"))
    await session.commit()

    rows = (await session.execute(select(User))).scalars().all()
    assert rows[0].tenant_id == tenant.id
    assert rows[0].role == "viewer"

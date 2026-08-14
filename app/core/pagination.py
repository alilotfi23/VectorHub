"""Generic keyset (cursor) pagination for SQLAlchemy async selects.

List endpoints must not materialize a full table just to slice a page. This
module provides cursor pagination over an arbitrary stable sort key: the
cursor is an opaque base64(JSON) encoding of the sort-key values of the last
returned item, and the continuation predicate is the standard keyset form —
for keys (k1 DESC, k2 ASC): (k1 < c1) OR (k1 = c1 AND k2 > c2) — honoring
each key's direction. Pages resume exactly regardless of page size and are
stable under concurrent writes (no OFFSET drift).

Cursors encode JSON scalars only (int/str). Typed columns compare against
them via Postgres's coercion of the parameter to the column type. Callers
supply `row_key_values` to read the sort-key values back off a result row —
expressions and derived values (e.g. a rank CASE) have no ORM attribute, so
they cannot be read generically.
"""

import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode

Direction = Literal["asc", "desc"]
# A SQLAlchemy column or expression plus its sort direction.
SortKey = tuple[Any, Direction]


@dataclass(frozen=True)
class Page[T]:
    """One page of a cursor-paginated result: `items`, an opaque
    `next_cursor` (None on the last page), and the total count."""

    items: list[T]
    next_cursor: str | None
    total: int


def encode_cursor(values: Sequence[int | str]) -> str:
    """Opaque cursor encoding the sort-key values of the last returned item."""
    return base64.urlsafe_b64encode(json.dumps(list(values)).encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str, key_count: int) -> list[int | str]:
    """Decode a cursor; malformed cursors are a client error (422)."""
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        if (
            not isinstance(values, list)
            or len(values) != key_count
            or not all(isinstance(v, (int, str)) and not isinstance(v, bool) for v in values)
        ):
            raise ValueError("malformed cursor payload")
    except (ValueError, UnicodeDecodeError):
        raise AppError(
            ErrorCode.VALIDATION_INVALID_CURSOR, "Invalid cursor", status_code=422
        ) from None
    return values


def continuation_predicate(sort_keys: Sequence[SortKey], cursor_values: Sequence[int | str]) -> Any:
    """Keyset continuation predicate: for each key i, all earlier keys equal
    their cursor values and key i continues in its direction (`>` for asc,
    `<` for desc)."""
    clauses: list[Any] = []
    for i, ((key, direction), value) in enumerate(zip(sort_keys, cursor_values, strict=True)):
        prefix = [
            earlier_key == earlier_value
            for (earlier_key, _), earlier_value in zip(
                sort_keys[:i], cursor_values[:i], strict=True
            )
        ]
        comparison = key > value if direction == "asc" else key < value
        clauses.append(and_(*prefix, comparison))
    return or_(*clauses)


async def paginate[T](
    session: AsyncSession,
    *,
    base: Select[tuple[T]],
    count: Select[tuple[int]],
    sort_keys: Sequence[SortKey],
    limit: int,
    cursor: str | None,
    row_key_values: Callable[[T], list[int | str]],
) -> Page[T]:
    """Run one page of `base` ordered by `sort_keys`.

    `base` is the filtered select (no ORDER BY/LIMIT); `count` is a matching
    COUNT query for the total. `limit + 1` rows are fetched to detect another
    page; `next_cursor` encodes the last returned item's sort-key values.
    """
    stmt = base.order_by(
        *(key.asc() if direction == "asc" else key.desc() for key, direction in sort_keys)
    ).limit(limit + 1)
    if cursor is not None:
        stmt = stmt.where(continuation_predicate(sort_keys, decode_cursor(cursor, len(sort_keys))))
    rows = list(await session.scalars(stmt))
    total = await session.scalar(count)
    assert total is not None
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(row_key_values(items[-1])) if has_more else None
    return Page(items=items, next_cursor=next_cursor, total=total)

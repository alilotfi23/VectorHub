"""Unit tests for the generic keyset-pagination machinery
(app.core.pagination): the opaque cursor codec and the direction-aware
continuation predicate. The end-to-end behavior is exercised through the
grant-list integration suites.
"""

from typing import Any

import pytest
from sqlalchemy import column

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import continuation_predicate, decode_cursor, encode_cursor


def test_cursor_round_trip() -> None:
    values: list[int | str] = [3, "user-abc"]
    cursor = encode_cursor(values)
    assert decode_cursor(cursor, key_count=2) == values
    assert cursor != str(values)  # opaque, not the raw values


def test_cursor_requires_matching_key_count() -> None:
    cursor = encode_cursor([1, "u"])
    with pytest.raises(AppError) as exc:
        decode_cursor(cursor, key_count=1)
    assert exc.value.code == ErrorCode.VALIDATION_INVALID_CURSOR
    assert exc.value.status_code == 422


@pytest.mark.parametrize("garbage", ["not-a-cursor", "", "!!!", "W10="])
def test_cursor_rejects_garbage(garbage: str) -> None:
    # "W10=" decodes to an empty JSON list — wrong key count for any page.
    with pytest.raises(AppError) as exc:
        decode_cursor(garbage, key_count=1)
    assert exc.value.code == ErrorCode.VALIDATION_INVALID_CURSOR


def test_continuation_two_keys_desc_then_asc() -> None:
    rank: Any = column("rank")
    user_id: Any = column("user_id")
    predicate = continuation_predicate([(rank, "desc"), (user_id, "asc")], [3, "u-1"])
    sql = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    # rank DESC continues below 3; ties on rank break upward on user_id.
    assert "rank < 3" in sql
    assert "user_id > 'u-1'" in sql


def test_continuation_single_key_asc() -> None:
    name: Any = column("name")
    predicate = continuation_predicate([(name, "asc")], ["b"])
    sql = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    assert "name > 'b'" in sql

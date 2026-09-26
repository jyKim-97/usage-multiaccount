from __future__ import annotations

import pytest

from usage_common.token_format import compact_tokens


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1.0K"),
        (11_150_000, "11.2M"),
        (999_949, "999.9K"),
        (999_950, "1.0M"),
        (1_234_567_890, "1.23B"),
    ],
)
def test_compact_tokens(count: int, expected: str) -> None:
    assert compact_tokens(count) == expected

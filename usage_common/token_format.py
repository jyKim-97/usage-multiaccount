# SPDX-License-Identifier: AGPL-3.0-only
"""Compact token counts ("11.2M") for glanceable totals."""

from __future__ import annotations


def compact_tokens(count: int) -> str:
    # Thresholds round up at the boundary: 999_950 would print "1000.0K".
    if count >= 999_950_000:
        return f"{count / 1_000_000_000:.2f}B"
    if count >= 999_950:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)

from __future__ import annotations

import os
import time
from uuid import UUID


def _uuid7() -> UUID:
    """RFC 9562 UUID version 7: 48-bit ms timestamp + 74 bits of randomness.

    Layout (128 bits, big-endian):
      48 bits  unix_ts_ms
       4 bits  version (= 7)
      12 bits  rand_a
       2 bits  variant (= 0b10)
      62 bits  rand_b
    """
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = rand >> 68 & 0x0FFF                  # 12 bits
    rand_b = rand & ((1 << 62) - 1)               # 62 bits
    value = (
        (ts_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | rand_a << 64
        | 0b10 << 62
        | rand_b
    )
    return UUID(int=value)


def new_id() -> str:
    """Generate a time-ordered UUID7 string (canonical 36-char form)."""
    return str(_uuid7())


def storage_prefix(uuid_str: str) -> tuple[str, str]:
    """Return (top, sub) two-byte hex prefixes used to shard storage keys."""
    clean = uuid_str.replace("-", "")
    return clean[0:2], clean[2:4]

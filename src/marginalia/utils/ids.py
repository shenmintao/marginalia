from __future__ import annotations

import uuid


def new_id() -> str:
    """Generate a random UUID4 string (canonical 36-char form).

    UUID4's fully random layout avoids the prefix-collision problem of
    UUID7, whose time-based first 48 bits cause many IDs created in the
    same millisecond to share the same 8-char prefix — breaking the
    resolve_entry_id_prefix short-prefix lookup.
    """
    return str(uuid.uuid4())


def storage_prefix(uuid_str: str) -> tuple[str, str]:
    """Return (top, sub) two-byte hex prefixes used to shard storage keys."""
    clean = uuid_str.replace("-", "")
    return clean[0:2], clean[2:4]

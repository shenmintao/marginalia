from __future__ import annotations

from typing import AsyncIterator, Protocol


class StorageBackend(Protocol):
    """Pluggable object storage. Implementations: local filesystem, S3/MinIO."""

    async def put(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        size: int | None = None,
        content_type: str | None = None,
    ) -> None: ...

    async def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        """Return bytes [start, end] inclusive (HTTP Range semantics)."""
        ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

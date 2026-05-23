from __future__ import annotations

import hashlib
from typing import AsyncIterator


class StreamHasher:
    """Wraps an async byte stream, computing sha256 as bytes pass through."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream
        self._hasher = hashlib.sha256()
        self._size = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._hasher.update(chunk)
            self._size += len(chunk)
            yield chunk

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    @property
    def size(self) -> int:
        return self._size

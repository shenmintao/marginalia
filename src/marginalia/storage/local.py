from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator

import aiofiles
import aiofiles.os

from marginalia.storage.base import StorageBackend

_CHUNK = 1024 * 256


class LocalStorage(StorageBackend):
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Defence-in-depth: refuse keys that escape the object root. A
        # storage_key is normally a UUID we minted, but WebDAV metadata
        # import builds keys from a remote-supplied `file_id`, so a hostile
        # snapshot could otherwise smuggle '../' segments and turn hydrate
        # into an arbitrary local file write. Mirror MirrorStorage._abs.
        candidate = (self.root / key).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(
                f"storage_key {key!r} escapes object root"
            ) from exc
        return candidate

    async def put(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        size: int | None = None,
        content_type: str | None = None,
        display_name: str | None = None,
        folder_path: str | None = None,
    ) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        async with aiofiles.open(tmp, "wb") as f:
            async for chunk in stream:
                await f.write(chunk)
        os.replace(tmp, target)
        return key

    async def rename(self, old_key: str, new_key: str) -> str:
        # UUID-flat: rename is a no-op. Storage key never changes for
        # local-mode files.
        return old_key

    async def get(self, key: str) -> AsyncIterator[bytes]:
        async with aiofiles.open(self._path(key), "rb") as f:
            while True:
                chunk = await f.read(_CHUNK)
                if not chunk:
                    return
                yield chunk

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        length = max(0, end - start + 1)
        async with aiofiles.open(self._path(key), "rb") as f:
            await f.seek(start)
            return await f.read(length)

    async def delete(self, key: str) -> None:
        try:
            await aiofiles.os.remove(self._path(key))
        except FileNotFoundError:
            pass

    async def exists(self, key: str) -> bool:
        return await aiofiles.os.path.isfile(self._path(key))

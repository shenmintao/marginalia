from __future__ import annotations

from functools import lru_cache

from marginalia.config import get_settings
from marginalia.storage.base import StorageBackend
from marginalia.storage.local import LocalStorage
from marginalia.storage.s3 import S3Storage


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(settings.local_storage_root)
    return S3Storage(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
    )


__all__ = ["StorageBackend", "LocalStorage", "S3Storage", "get_storage"]

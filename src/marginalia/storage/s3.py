from __future__ import annotations

from typing import AsyncIterator

import aioboto3
from botocore.exceptions import ClientError

from marginalia.storage.base import StorageBackend

_CHUNK = 1024 * 256


class S3Storage(StorageBackend):
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._endpoint_url = endpoint_url

    def _client(self):  # type: ignore[no-untyped-def]
        return self._session.client("s3", endpoint_url=self._endpoint_url)

    async def put(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        size: int | None = None,
        content_type: str | None = None,
    ) -> None:
        buf = bytearray()
        async for chunk in stream:
            buf.extend(chunk)
        async with self._client() as s3:
            kwargs: dict[str, object] = {"Bucket": self.bucket, "Key": key, "Body": bytes(buf)}
            if content_type:
                kwargs["ContentType"] = content_type
            await s3.put_object(**kwargs)

    async def get(self, key: str) -> AsyncIterator[bytes]:
        async with self._client() as s3:
            obj = await s3.get_object(Bucket=self.bucket, Key=key)
            async with obj["Body"] as body:
                while True:
                    chunk = await body.read(_CHUNK)
                    if not chunk:
                        return
                    yield chunk

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        async with self._client() as s3:
            obj = await s3.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}"
            )
            async with obj["Body"] as body:
                return await body.read()

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return False
                raise

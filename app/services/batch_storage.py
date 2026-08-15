"""Batch payload staging on object storage (MinIO in compose / S3 in cloud).

The async-batch data path from CLAUDE.md: the NDJSON payload **never
transits Redis/arq** — the enqueue route streams the body straight to object
storage at ``{tenant_id}/{job_id}.jsonl``, and the worker receives only
``{job_id, payload_key}``, streams the file back, validates per line, and
upserts in chunks. Per-vector outcomes stream to
``{tenant_id}/{job_id}.results.jsonl``. Retry is safe because upserts are
idempotent.

Streaming discipline (the wire format dominates the throughput model — JSON
text is ~10 B/float, so a 100k x 1536-dim job is ~1.6 GB JSONL): upload never
buffers the request body whole, and the worker reads the file in bounded
chunks (``iter_lines`` carries a partial line across chunk boundaries), so
parse/validate overlaps ingest instead of serializing after a full download.
All boto3 calls are sync — each runs via ``asyncio.to_thread`` so the event
loop is never blocked.

Credentials/endpoint come from ``BATCH_STORAGE_*`` settings; the same image
runs against MinIO (dev) or AWS S3 (cloud) unchanged.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from typing import Any, Protocol, cast, runtime_checkable

import boto3  # type: ignore[import-untyped]
from botocore.response import StreamingBody  # type: ignore[import-untyped]

from app.core.config import get_settings

CHUNK_SIZE = 1024 * 1024  # 1 MiB read/upload chunks


@runtime_checkable
class BatchStorageProtocol(Protocol):
    """The object-storage surface the service/worker layers depend on — a
    structural seam so container-free tests (Layer 2 R4) can stand in a
    recording storage without talking to MinIO/S3. BatchStorage implements
    it; the worker additionally uses the streaming readers below."""

    @property
    def bucket(self) -> str: ...

    async def ensure_bucket(self) -> None: ...

    async def upload_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int: ...

    async def delete(self, key: str) -> None: ...


def payload_key(tenant_id: str, job_id: str) -> str:
    """Object key for a job's staged payload: tenant-scoped by construction
    (the Layer 2/E5 isolation contract — a job object can never be addressed
    under another tenant's prefix)."""
    return f"{tenant_id}/{job_id}.jsonl"


def results_key(tenant_id: str, job_id: str) -> str:
    return f"{tenant_id}/{job_id}.results.jsonl"


class _AsyncBodyReader(io.RawIOBase):
    """File-like adapter so boto3's uploader can pull an async request stream:
    boto3 reads on its own thread, this pulls the next chunk from the asyncio
    stream via ``run_coroutine_threadsafe`` on the running loop. Bounded by
    construction — one chunk in flight, never the whole body. Counts bytes
    read for the upload return value."""

    def __init__(self, chunks: AsyncIterator[bytes], loop: asyncio.AbstractEventLoop) -> None:
        self._chunks = chunks
        self._loop = loop
        self._buffer = b""
        self._done = False
        self.bytes_read = 0

    def readable(self) -> bool:  # noqa: D102
        return True

    def readinto(self, b: bytearray) -> int:  # type: ignore[override]  # noqa: D102
        target = len(b)
        while len(self._buffer) < target and not self._done:
            try:
                chunk: bytes = asyncio.run_coroutine_threadsafe(
                    cast("Any", anext(self._chunks)), self._loop
                ).result()
            except StopAsyncIteration:
                self._done = True
                break
            self._buffer += chunk
        if not self._buffer:
            return 0
        n = min(target, len(self._buffer))
        b[:n] = self._buffer[:n]
        self._buffer = self._buffer[n:]
        self.bytes_read += n
        return n


class BatchStorage:
    """S3-compatible object storage for batch payloads/results. One instance
    per process (lazy boto3 client from ``BATCH_STORAGE_*`` settings);
    boto3's client is thread-safe, so ``to_thread`` calls share it safely."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
    ) -> None:
        settings = get_settings()
        self._endpoint = endpoint or settings.batch_storage_endpoint
        self._access_key = access_key or settings.batch_storage_access_key
        self._secret_key = secret_key or settings.batch_storage_secret_key
        self._bucket = bucket or settings.batch_storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name="us-east-1",
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        """Create the bucket if absent (idempotent; dev/MinIO friendly). A
        failed create surfaces on the next upload with a clear error — this
        never silently swallows credential problems."""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
            return
        except Exception:
            pass  # 404/403 = missing bucket; try to create
        try:
            await asyncio.to_thread(self._client.create_bucket, Bucket=self._bucket)
        except Exception:
            pass  # already-exists race between the head and the create

    async def upload_stream(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        """Stream an async body to ``key`` (never buffered whole). Returns the
        byte count uploaded."""
        reader = _AsyncBodyReader(chunks, asyncio.get_running_loop())
        await asyncio.to_thread(
            self._client.upload_fileobj,
            reader,
            self._bucket,
            key,
            Config=boto3.s3.transfer.TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
            ),
        )
        return reader.bytes_read

    async def iter_chunks(self, key: str, size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Stream ``key`` in bounded chunks (read-ahead friendly)."""
        body = await self._get_body(key)
        while True:
            data = await asyncio.to_thread(body.read, size)
            if not data:
                break
            yield data

    async def iter_lines(self, key: str) -> AsyncIterator[bytes]:
        """Stream ``key`` as complete newline-delimited lines (a partial line
        at a chunk boundary is carried across). Parsing stays streaming — the
        worker's read->parse->upsert pipeline never materializes the file."""
        carry = b""
        async for chunk in self.iter_chunks(key):
            parts = (carry + chunk).splitlines(keepends=True)
            carry = parts.pop() if parts else b""
            for part in parts:
                yield part.rstrip(b"\r\n")
        if carry:
            yield carry.rstrip(b"\r\n")

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except Exception:
            pass  # idempotent cleanup

    async def head(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    async def _get_body(self, key: str) -> StreamingBody:
        result = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=key)
        return cast(StreamingBody, result["Body"])

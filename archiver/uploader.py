import random
import time
from pathlib import Path
from typing import Iterator, Protocol
from botocore.exceptions import ClientError

from archiver.logger import logger
from archiver.telemetry import Telemetry

TRANSIENT_S3_ERROR_CODES = {
    "InternalError",
    "SlowDown",
    "ServiceUnavailable",
    "RequestTimeout",
    "RequestTimeTooSkewed",
    "Throttling",
    "ThrottlingException",
}


class Uploader(Protocol):
    def exists(self, bucket: str, key: str) -> bool:
        pass

    def upload(
        self,
        bucket: str,
        key: str,
        local_path: Path,
        *,
        storage_class: str | None = None,
    ) -> None: ...

    def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        storage_class: str | None = None,
        content_type: str | None = None,
    ) -> None: ...

    def list_keys(self, bucket: str, prefix: str) -> Iterator[str]: ...  # paginated
    def get_bytes(self, bucket: str, key: str) -> bytes: ...
    def delete_objects(self, bucket: str, keys: list[str]) -> None: ...


class S3Uploader:
    """Concrete Uploader backed by a boto3 S3 client."""

    def __init__(self, client) -> None:
        # client = boto3.client("s3", region_name=...); construction happens in loader
        self.client = client

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            # HeadObject returns "404" for missing keys; some other ops use "NoSuchKey".
            # Catch both to be safe.
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise  # permissions error, transient failure, etc. — surface it

    def upload(
        self,
        bucket: str,
        key: str,
        local_path: Path,
        *,
        storage_class: str | None = None,
    ) -> None:
        extra_args = {"StorageClass": storage_class} if storage_class else {}
        # upload_file handles multipart automatically; needs str not Path
        self.client.upload_file(str(local_path), bucket, key, ExtraArgs=extra_args)

    def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        storage_class: str | None = None,
        content_type: str | None = None,
    ) -> None:
        # put_object takes params directly (not ExtraArgs) and sends the body
        # in a single request — no automatic multipart.
        kwargs = {"Bucket": bucket, "Key": key, "Body": data}
        if storage_class:
            kwargs["StorageClass"] = storage_class
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)

    def list_keys(self, bucket: str, prefix: str) -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def get_bytes(self, bucket: str, key: str) -> bytes:
        obj = self.client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    def delete_objects(
        self, bucket: str, keys: list[str], max_retries: int = 5
    ) -> None:
        errors = []
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            errors.extend(self._delete_chunk(bucket, chunk, max_retries))
        if errors:
            for e in errors:
                logger.error("delete_objects failed: %s %s", e["Key"], e["Code"])
            raise RuntimeError(
                f"delete_objects: {len(errors)} key(s) failed in {bucket}"
            )

    def _delete_chunk(
        self, bucket: str, chunk: list[str], max_retries: int
    ) -> list[dict]:
        permanent_errors: list[dict] = []
        to_delete = chunk

        for attempt in range(max_retries + 1):
            try:
                response = self.client.delete_objects(
                    Bucket=bucket,
                    Delete={
                        "Objects": [{"Key": k} for k in to_delete],
                        "Quiet": True,
                    },
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in TRANSIENT_S3_ERROR_CODES or attempt == max_retries:
                    raise
                self._sleep_backoff(attempt)
                continue

            batch_errors = response.get("Errors", [])
            transient = [
                e for e in batch_errors if e["Code"] in TRANSIENT_S3_ERROR_CODES
            ]
            permanent_errors.extend(
                e for e in batch_errors if e["Code"] not in TRANSIENT_S3_ERROR_CODES
            )

            if not transient:
                return permanent_errors
            if attempt == max_retries:
                permanent_errors.extend(transient)
                return permanent_errors

            to_delete = [e["Key"] for e in transient]
            self._sleep_backoff(attempt)

        return permanent_errors

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        delay = min(2**attempt, 30) * (0.5 + random.random())
        time.sleep(delay)


class InstrumentedUploader:
    def __init__(self, inner: Uploader, telemetry: Telemetry) -> None:
        self._inner = inner
        self._tel = telemetry

    def put_bytes(self, bucket, key, data, **kw):
        self._tel.incr("s3.request", tags={"op": "put", "bucket": bucket})
        return self._inner.put_bytes(bucket, key, data, **kw)

    def upload(self, bucket, key, local_path, **kw):
        self._tel.incr("s3.request", tags={"op": "put", "bucket": bucket})
        return self._inner.upload(bucket, key, local_path, **kw)

    def get_bytes(self, bucket, key, **kw):
        self._tel.incr("s3.request", tags={"op": "get", "bucket": bucket})
        return self._inner.get_bytes(bucket, key, **kw)

    def exists(self, bucket, key, **kw):
        self._tel.incr("s3.request", tags={"op": "head", "bucket": bucket})
        return self._inner.exists(bucket, key, **kw)

    def list_keys(self, bucket, prefix, **kw):
        self._tel.incr("s3.request", tags={"op": "list", "bucket": bucket})
        return self._inner.list_keys(bucket, prefix, **kw)

    def delete_objects(self, bucket, keys, **kw):
        self._tel.incr("s3.request", tags={"op": "delete", "bucket": bucket})
        return self._inner.delete_objects(bucket, keys, **kw)

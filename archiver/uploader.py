from pathlib import Path
from typing import Protocol
from botocore.exceptions import ClientError


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
    ) -> None:
        pass


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

"""S3-compatible blob store (requires the [s3] extra). Works against AWS S3,
Cloudflare R2, or MinIO — set OBS_S3_ENDPOINT for non-AWS providers.
Credentials come from the standard AWS chain (env vars, config files, IAM)."""

from .base import BlobStore


class S3BlobStore(BlobStore):
    def __init__(self, bucket: str, endpoint: str | None = None) -> None:
        import boto3

        kwargs = {}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        self.client = boto3.client("s3", **kwargs)
        self.bucket = bucket

    def put_text(self, key: str, text: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    def get_text(self, key: str) -> str:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read().decode("utf-8")

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return sorted(keys)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def copy(self, src_key: str, dst_key: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            Key=dst_key,
            CopySource={"Bucket": self.bucket, "Key": src_key},
        )

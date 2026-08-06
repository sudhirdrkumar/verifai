from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings


class StorageConfigError(Exception):
    pass


class StorageOperationError(Exception):
    pass


def _s3_client():
    if not settings.s3_bucket:
        raise StorageConfigError("S3 bucket is not configured")

    kwargs = {"region_name": settings.s3_region}
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url

    if settings.s3_access_key and settings.s3_secret_key:
        kwargs["aws_access_key_id"] = settings.s3_access_key
        kwargs["aws_secret_access_key"] = settings.s3_secret_key

    return boto3.client("s3", **kwargs)


def _public_object_url(bucket: str, key: str) -> str:
    escaped_key = quote(key, safe="/-_.~")
    if settings.s3_endpoint_url:
        endpoint = settings.s3_endpoint_url.rstrip("/")
        return f"{endpoint}/{bucket}/{escaped_key}"
    return f"https://{bucket}.s3.{settings.s3_region}.amazonaws.com/{escaped_key}"


def upload_bytes(object_key: str, payload: bytes, content_type: str) -> dict:
    client = _s3_client()
    try:
        response = client.put_object(
            Bucket=settings.s3_bucket,
            Key=object_key,
            Body=payload,
            ContentType=content_type,
        )
    except (ClientError, BotoCoreError) as exc:
        raise StorageOperationError(f"S3 upload failed: {exc}") from exc

    return {
        "bucket": settings.s3_bucket,
        "region": settings.s3_region,
        "key": object_key,
        "url": _public_object_url(settings.s3_bucket, object_key),
        "etag": response.get("ETag", "").strip('"'),
    }


def generate_download_url(object_key: str, expires_in: int = 900) -> str:
    client = _s3_client()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": object_key},
            ExpiresIn=expires_in,
        )
    except (ClientError, BotoCoreError) as exc:
        raise StorageOperationError(f"S3 presign failed: {exc}") from exc


def download_bytes(object_key: str) -> bytes:
    client = _s3_client()
    try:
        response = client.get_object(Bucket=settings.s3_bucket, Key=object_key)
        body = response["Body"].read()
        return body
    except (ClientError, BotoCoreError) as exc:
        raise StorageOperationError(f"S3 download failed: {exc}") from exc


def delete_object(object_key: str) -> None:
    client = _s3_client()
    try:
        client.delete_object(Bucket=settings.s3_bucket, Key=object_key)
    except (ClientError, BotoCoreError) as exc:
        raise StorageOperationError(f"S3 delete failed: {exc}") from exc

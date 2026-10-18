"""Object storage (MinIO / any S3) for admin-uploaded images. boto3 is blocking, so calls run in a thread.

Objects under public/ are readable anonymously (bucket policy), which is what lets Meta fetch them by URL.
"""
import asyncio

import boto3
from botocore.config import Config

from app.core.config import settings

_client = None


def configured() -> bool:
    return bool(settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key and settings.s3_public_base_url)


def _s3():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(s3={"addressing_style": "path"}),  # MinIO: bucket in the path, not the hostname
        )
    return _client


async def upload(key: str, data: bytes, content_type: str) -> str:
    """Store the bytes under `key`; returns the public URL."""
    await asyncio.to_thread(
        _s3().put_object, Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type
    )
    return f"{settings.s3_public_base_url.rstrip('/')}/{key}"


async def delete(key: str) -> None:
    await asyncio.to_thread(_s3().delete_object, Bucket=settings.s3_bucket, Key=key)

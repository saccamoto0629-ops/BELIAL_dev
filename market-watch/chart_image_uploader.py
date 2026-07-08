"""Upload private chart images to S3 and create short-lived presigned URLs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_REGION = "ap-northeast-1"
DEFAULT_BUCKET = "belial-chart-snapshots"
DEFAULT_PREFIX = "charts"


def load_s3_config(env_values: dict[str, str]) -> dict[str, str]:
    return {
        "region": (
            env_values.get("AWS_DEFAULT_REGION")
            or env_values.get("AWS_REGION")
            or DEFAULT_REGION
        ).strip(),
        "bucket": (
            env_values.get("S3_BUCKET_NAME")
            or env_values.get("CHART_S3_BUCKET")
            or DEFAULT_BUCKET
        ).strip(),
        "prefix": (
            env_values.get("S3_CHART_PREFIX")
            or env_values.get("CHART_S3_PREFIX")
            or DEFAULT_PREFIX
        ).strip().strip("/"),
        "aws_access_key_id": env_values.get("AWS_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": env_values.get("AWS_SECRET_ACCESS_KEY", "").strip(),
        "aws_session_token": env_values.get("AWS_SESSION_TOKEN", "").strip(),
    }


def build_chart_s3_key(
    symbol: str,
    timeframe: str,
    snapshot_at: datetime,
    suffix: str = "png",
    *,
    prefix: str = DEFAULT_PREFIX,
    object_id: str | None = None,
) -> str:
    captured = snapshot_at.astimezone(UTC)
    clean_suffix = suffix.lstrip(".")
    name = object_id or str(uuid.uuid4())
    return (
        f"{prefix.strip('/')}/{symbol.upper()}/{timeframe}/"
        f"{captured:%Y/%m/%d}/{name}.{clean_suffix}"
    )


def create_s3_client(config: dict[str, str] | None = None):
    import boto3

    values = config or load_s3_config({})
    kwargs = {"region_name": values["region"]}
    if values.get("aws_access_key_id") and values.get("aws_secret_access_key"):
        kwargs["aws_access_key_id"] = values["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = values["aws_secret_access_key"]
    if values.get("aws_session_token"):
        kwargs["aws_session_token"] = values["aws_session_token"]
    return boto3.client("s3", **kwargs)


def upload_chart_image(
    local_path: Path,
    symbol: str,
    timeframe: str,
    snapshot_at: datetime,
    *,
    config: dict[str, str] | None = None,
    s3_client=None,
    s3_key: str | None = None,
) -> str:
    values = config or load_s3_config({})
    key = s3_key or build_chart_s3_key(
        symbol,
        timeframe,
        snapshot_at,
        prefix=values["prefix"],
    )
    client = s3_client if s3_client is not None else create_s3_client(values)
    client.upload_file(
        str(local_path),
        values["bucket"],
        key,
        ExtraArgs={"ContentType": "image/png"},
    )
    return key


def generate_presigned_chart_url(
    s3_key: str,
    expires_in: int = 3600,
    *,
    config: dict[str, str] | None = None,
    s3_client=None,
) -> str:
    values = config or load_s3_config({})
    client = s3_client if s3_client is not None else create_s3_client(values)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": values["bucket"], "Key": s3_key},
        ExpiresIn=expires_in,
    )

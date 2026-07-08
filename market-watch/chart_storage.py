"""Store and retrieve BELIAL chart snapshot images in S3."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


DEFAULT_REGION = "ap-northeast-1"
DEFAULT_BUCKET = "belial-chart-snapshots"
DEFAULT_PREFIX = "charts"


def load_s3_config(env_values: dict[str, str]) -> dict[str, str]:
    return {
        "region": env_values.get("AWS_REGION", DEFAULT_REGION).strip() or DEFAULT_REGION,
        "bucket": env_values.get("CHART_S3_BUCKET", DEFAULT_BUCKET).strip() or DEFAULT_BUCKET,
        "prefix": env_values.get("CHART_S3_PREFIX", DEFAULT_PREFIX).strip().strip("/") or DEFAULT_PREFIX,
        "aws_access_key_id": env_values.get("AWS_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": env_values.get("AWS_SECRET_ACCESS_KEY", "").strip(),
        "aws_session_token": env_values.get("AWS_SESSION_TOKEN", "").strip(),
    }


def build_s3_key(
    *,
    symbol: str,
    timeframe: str,
    snapshot_id: str,
    captured_at: datetime,
    prefix: str = DEFAULT_PREFIX,
) -> str:
    captured = captured_at.astimezone(UTC)
    return (
        f"{prefix.strip('/')}/{symbol.upper()}/{timeframe}/"
        f"{captured:%Y/%m/%d}/{snapshot_id}.png"
    )


def create_s3_client(config: dict[str, str]):
    import boto3

    kwargs = {"region_name": config["region"]}
    if config.get("aws_access_key_id") and config.get("aws_secret_access_key"):
        kwargs["aws_access_key_id"] = config["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = config["aws_secret_access_key"]
    if config.get("aws_session_token"):
        kwargs["aws_session_token"] = config["aws_session_token"]
    return boto3.client("s3", **kwargs)


def upload_chart_image(
    local_path: Path,
    *,
    bucket: str,
    s3_key: str,
    s3_client=None,
) -> str:
    client = s3_client if s3_client is not None else create_s3_client(load_s3_config({}))
    client.upload_file(str(local_path), bucket, s3_key, ExtraArgs={"ContentType": "image/png"})
    return s3_key


def download_chart_image(
    *,
    bucket: str,
    s3_key: str,
    s3_client=None,
) -> bytes:
    client = s3_client if s3_client is not None else create_s3_client(load_s3_config({}))
    response = client.get_object(Bucket=bucket, Key=s3_key)
    return response["Body"].read()

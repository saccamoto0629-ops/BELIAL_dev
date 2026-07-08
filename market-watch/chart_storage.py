"""Backward-compatible aliases for chart image S3 helpers."""

from chart_image_uploader import (  # noqa: F401
    DEFAULT_BUCKET,
    DEFAULT_PREFIX,
    DEFAULT_REGION,
    build_chart_s3_key,
    create_s3_client,
    generate_presigned_chart_url,
    load_s3_config,
    upload_chart_image,
)


def build_s3_key(*, symbol, timeframe, snapshot_id, captured_at, prefix=DEFAULT_PREFIX):
    return build_chart_s3_key(
        symbol,
        timeframe,
        captured_at,
        prefix=prefix,
        object_id=snapshot_id,
    )

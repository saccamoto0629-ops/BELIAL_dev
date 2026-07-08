"""Persist alert chart context and retrieve private S3 images for Discord."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from chart_collector import (
    chart_db_path,
    insert_alert,
    insert_chart_snapshot,
    load_env_file,
)
from chart_renderer import render_chart_snapshot
from chart_storage import (
    build_s3_key,
    create_s3_client,
    download_chart_image,
    load_s3_config,
    upload_chart_image,
)


def _json_default(value):
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    return str(value)


def prepare_alert_image(
    *,
    alert_type: str,
    symbol: str,
    timeframe: str,
    candle_time: str,
    candles: pd.DataFrame | pd.Series,
    metadata: dict,
    dry_run: bool = False,
) -> tuple[str | None, bytes | None, str | None]:
    """Create an explicit snapshot for an alert and return image bytes.

    The alert is linked by snapshot_id. No timestamp JOIN is used as the
    primary relationship.
    """
    if dry_run:
        return None, None, None

    env_values = load_env_file()
    db_path = chart_db_path(env_values)
    captured_at = datetime.now(UTC)
    frame = candles.to_frame(name="close") if isinstance(candles, pd.Series) else candles.copy()
    config = load_s3_config(env_values)
    client = create_s3_client(config)
    snapshot_id = insert_chart_snapshot(
        db_path,
        symbol=symbol,
        timeframe=timeframe,
        captured_at=captured_at.isoformat(),
        candle_time=candle_time,
        ohlcv_json=frame.tail(120).reset_index().to_json(orient="records", date_format="iso"),
        indicators_json=json.dumps(metadata, ensure_ascii=False, default=_json_default),
    )

    s3_key = build_s3_key(
        symbol=symbol,
        timeframe=timeframe,
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        prefix=config["prefix"],
    )
    with tempfile.TemporaryDirectory(prefix="belial_alert_chart_") as temp_dir:
        local_path = Path(temp_dir) / f"{snapshot_id}.png"
        render_chart_snapshot(frame, local_path, title=f"{symbol} {timeframe} {alert_type}")
        upload_chart_image(local_path, bucket=config["bucket"], s3_key=s3_key, s3_client=client)
        image_bytes = download_chart_image(bucket=config["bucket"], s3_key=s3_key, s3_client=client)

    insert_alert(
        db_path,
        alert_type=alert_type,
        symbol=symbol,
        timeframe=timeframe,
        occurred_at=metadata.get("detected_at", captured_at.isoformat()),
        candle_time=candle_time,
        snapshot_id=snapshot_id,
        metadata_json=json.dumps(metadata, ensure_ascii=False, default=_json_default),
    )
    return snapshot_id, image_bytes, f"{snapshot_id}.png"

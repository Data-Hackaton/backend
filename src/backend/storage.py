"""S3 (SeaweedFS): видео, кадры-доказательства, результаты обработки.

Раскладка ключей:
  videos/<video_id>/source.<ext>   оригинал, на анализ
  videos/<video_id>/play.mp4       перепакованный для браузера (H.264 без перекодирования)
  jobs/<job_id>/evidence/<n>.jpg   кадры-доказательства
  jobs/<job_id>/events.json, boxes.json, result.json — результат cv-worker
"""

from collections.abc import Iterator

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from backend.config import S3_ACCESS_KEY, S3_BUCKET, S3_ENDPOINT, S3_SECRET_KEY

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3", endpoint_url=S3_ENDPOINT, aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
            region_name="us-east-1", config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )
    return _client


def ensure_bucket() -> None:
    try:
        client().head_bucket(Bucket=S3_BUCKET)
    except ClientError:
        client().create_bucket(Bucket=S3_BUCKET)


def put_file(key: str, path: str, content_type: str) -> None:
    client().upload_file(path, S3_BUCKET, key, ExtraArgs={"ContentType": content_type})


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    client().put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)


def get_bytes(key: str) -> bytes:
    return client().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def presign(key: str, expires: int = 3600) -> str:
    """Ссылка на объект для сервисов внутри compose (ffmpeg бэкенда, cv-worker); браузеру — /stream."""
    return client().generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires)


def stream(key: str, range_header: str | None) -> tuple[Iterator[bytes], dict, int]:
    """Отдать объект кусками с поддержкой Range — плеер перематывает видео без полной загрузки."""
    args = {"Bucket": S3_BUCKET, "Key": key}
    if range_header:
        args["Range"] = range_header
    obj = client().get_object(**args)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(obj["ContentLength"])}
    if "ContentRange" in obj:
        headers["Content-Range"] = obj["ContentRange"]
    return obj["Body"].iter_chunks(1 << 20), headers, 206 if range_header else 200

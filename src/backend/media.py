"""ffprobe / ffmpeg: метаданные, перепаковка для браузера, кадр по времени."""

import hashlib
import json
import math
import subprocess
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image

from backend import deadlines


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            deadlines.remaining()
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=deadlines.remaining(15),
    )
    info = json.loads(out.stdout)
    streams = info.get("streams") or []
    if not streams or not streams[0].get("width") or not streams[0].get("height"):
        raise ValueError("file has no video stream")
    stream = streams[0]
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid video duration")
    num, _, den = stream.get("r_frame_rate", "0/1").partition("/")
    return {
        "duration": duration,
        "fps": round(int(num) / int(den or 1), 3) if num else None,
        "width": stream.get("width"),
        "height": stream.get("height"),
    }


def validate_decodable(path: Path) -> None:
    """Require an actual decoded frame; container metadata alone is insufficient."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-i", str(path),
         "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "gray",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=deadlines.remaining(15),
    )
    # ffmpeg can exit successfully without producing a frame.
    if len(out.stdout) != 1:
        raise ValueError("video contains no decodable frames")


def remux_mp4(src: Path, dst: Path) -> None:
    """Тот же H.264 в mp4 с moov в начале — браузер начинает играть сразу и перематывает по Range."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", "-an", "-movflags", "+faststart",
                    str(dst)], capture_output=True, check=True, timeout=deadlines.remaining(10))


def frame_jpeg(url: str, t: float) -> bytes:
    """JPEG кадра на момент t; пусто, если кадра нет (t за концом видео — ffmpeg завершается с ошибкой)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", url, "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"],
        capture_output=True, timeout=deadlines.remaining(15),
    )
    return out.stdout if out.returncode == 0 else b""


def validate_jpeg(data: bytes) -> None:
    """Decode the entire image; an FF D8 prefix alone is not proof of a JPEG."""
    if not data or len(data) > 10 * 1024 * 1024:
        raise ValueError("invalid evidence frame size")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.format != "JPEG":
                raise ValueError("evidence must be JPEG")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()

"""ffprobe / ffmpeg: метаданные, перепаковка для браузера, кадр по времени."""

import hashlib
import json
import subprocess
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(out.stdout)
    stream = (info.get("streams") or [{}])[0]
    num, _, den = stream.get("r_frame_rate", "0/1").partition("/")
    return {
        "duration": float(info.get("format", {}).get("duration", 0) or 0),
        "fps": round(int(num) / int(den or 1), 3) if num else None,
        "width": stream.get("width"),
        "height": stream.get("height"),
    }


def remux_mp4(src: Path, dst: Path) -> None:
    """Тот же H.264 в mp4 с moov в начале — браузер начинает играть сразу и перематывает по Range."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", "-an", "-movflags", "+faststart",
                    str(dst)], check=True)


def frame_jpeg(url: str, t: float) -> bytes:
    """JPEG кадра на момент t; пусто, если кадра нет (t за концом видео — ffmpeg завершается с ошибкой)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", url, "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"],
        capture_output=True,
    )
    return out.stdout if out.returncode == 0 else b""

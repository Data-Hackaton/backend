"""Validate model evidence before publication and when reading older results."""

import math

from backend import deadlines, media, storage


class InvalidResult(ValueError):
    pass


def timestamp(value, duration: float | None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidResult("event/frame timestamp must be a finite number")
    if value < 0 or (duration is not None and value > duration):
        raise InvalidResult("event/frame timestamp is outside the video")
    return float(value)


def jpeg_bytes(frame: dict) -> bytes:
    deadlines.remaining()
    try:
        key = frame["key"]
        if not isinstance(key, str) or not key:
            raise InvalidResult("evidence object key is missing")
        data = storage.get_bytes(key)
        media.validate_jpeg(data)
        deadlines.remaining()
        return data
    except (InvalidResult, TimeoutError):
        raise
    except Exception as exc:
        raise InvalidResult("evidence frame is missing or is not a valid JPEG") from exc


def validate_violation(event: dict, duration: float | None, *, read_frames: bool = True) -> None:
    timestamp(event["t_start"], duration)
    end = timestamp(event["t_end"], duration)
    if end < event["t_start"]:
        raise InvalidResult("event ends before it starts")
    for name, value in (("type", event.get("title") or event.get("event")), ("description", event.get("message"))):
        if not isinstance(value, str) or not value.strip():
            raise InvalidResult(f"violation {name} is missing")
    frames = event.get("evidence")
    if not isinstance(frames, list) or not frames:
        raise InvalidResult("violation has no evidence frames")
    for frame in frames[:3]:
        timestamp(frame["t"], duration)
        if read_frames:
            jpeg_bytes(frame)


def validate_result(result: dict, events: list, duration: float | None) -> None:
    if not isinstance(events, list):
        raise InvalidResult("events must be an array")
    violations = [event for event in events if event["status"] == "violation"]
    expected = "violation" if violations else "no_violation"
    if result.get("verdict") != expected:
        raise InvalidResult("verdict does not match detected events")
    for event in violations:
        validate_violation(event, duration)

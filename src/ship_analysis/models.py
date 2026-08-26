from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _number(value: Any, cast: type[float] | type[int] = float) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return 1
        if lowered in {"false", "0", "no"}:
            return 0
        return None
    return int(bool(value))


def _timestamp(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class NormalizedObservation:
    observation_key: str
    provider: str
    track_id: str | None
    position_time_utc: str | None
    reception_time_utc: str | None
    fetched_at_utc: str
    lat: float | None
    lon: float | None
    speed_ground: float | None
    course_ground: float | None
    is_moving: int | None
    vessel_name: str | None
    call_sign: str | None
    mmsi: int | None
    eni: str | None
    imo: int | None
    length_m: float | None
    beam_m: float | None
    ais_ship_type: int | None
    eri_ship_type: int | None
    isrs_code: str | None
    isrs_name: str | None
    direction: int | None
    privacy_class: int | None
    raw_json: str


def normalize_observation(
    record: dict[str, Any], provider: str, fetched_at_utc: str
) -> NormalizedObservation:
    raw_json = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    track_id = _text(_first(record, "trackID", "trackId"))
    position_time = _timestamp(_first(record, "posTS", "positionMeasuredAt"))
    lat = _number(record.get("lat"))
    lon = _number(record.get("lon"))

    identity = {
        "provider": provider,
        "track_id": track_id,
        "position_time": position_time,
        "lat": lat,
        "lon": lon,
    }
    if track_id is None and position_time is None:
        identity["record"] = raw_json
    observation_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # The complete provider response already lives in the compressed source
    # snapshot for at least 24 hours. Keeping another full JSON object in every
    # SQLite row creates substantial write amplification. Retain it only when
    # it is required to identify an otherwise anonymous observation.
    stored_raw_json = raw_json if track_id is None and position_time is None else ""

    return NormalizedObservation(
        observation_key=observation_key,
        provider=provider,
        track_id=track_id,
        position_time_utc=position_time,
        reception_time_utc=_timestamp(
            _first(record, "recTS", "firstReceptionOfBaseStationAt")
        ),
        fetched_at_utc=fetched_at_utc,
        lat=lat,
        lon=lon,
        speed_ground=_number(_first(record, "sog", "speedGround")),
        course_ground=_number(_first(record, "cog", "courseGround")),
        is_moving=_boolean(_first(record, "moving", "isMoving")),
        vessel_name=_text(_first(record, "name")),
        call_sign=_text(_first(record, "callSign")),
        mmsi=_number(_first(record, "mmsi"), int),
        eni=_text(_first(record, "eni")),
        imo=_number(_first(record, "imo"), int),
        length_m=_number(_first(record, "inlen", "length")),
        beam_m=_number(_first(record, "inbm", "beam")),
        ais_ship_type=_number(_first(record, "aismst", "aisShipType"), int),
        eri_ship_type=_number(_first(record, "erist", "eriShipType"), int),
        isrs_code=_text(_first(record, "positionISRS", "isrsPosition")),
        isrs_name=_text(_first(record, "positionISRSName", "isrsPositionName")),
        direction=_number(_first(record, "direction"), int),
        privacy_class=_number(_first(record, "privacyClass"), int),
        raw_json=stored_raw_json,
    )

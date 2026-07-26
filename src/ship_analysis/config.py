from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


@dataclass(frozen=True)
class BBox:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def __post_init__(self) -> None:
        if not (-180 <= self.min_lon < self.max_lon <= 180):
            raise ValueError(f"Invalid longitude range: {self}")
        if not (-90 <= self.min_lat < self.max_lat <= 90):
            raise ValueError(f"Invalid latitude range: {self}")

    @classmethod
    def from_list(cls, values: list[float]) -> "BBox":
        if len(values) != 4:
            raise ValueError("bbox must contain [min_lon, min_lat, max_lon, max_lat]")
        return cls(*(float(value) for value in values))

    def query_parameters(self) -> dict[str, str]:
        return {
            "minLon": f"{self.min_lon:.7f}",
            "minLat": f"{self.min_lat:.7f}",
            "maxLon": f"{self.max_lon:.7f}",
            "maxLat": f"{self.max_lat:.7f}",
        }

    def compact(self) -> str:
        return (
            f"{self.min_lon:.5f},{self.min_lat:.5f},"
            f"{self.max_lon:.5f},{self.max_lat:.5f}"
        )


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    token_env: str
    timeout_seconds: float
    max_retries: int
    max_pages: int
    request_gap_seconds: float
    user_agent: str

    @property
    def token(self) -> str | None:
        value = os.environ.get(self.token_env, "").strip()
        return value or None


@dataclass(frozen=True)
class CompactionConfig:
    enabled: bool
    timezone: str
    day_boundary_hour: int
    process_delay_minutes: int
    stationary_radius_m: float
    stationary_min_duration_minutes: int
    stationary_min_samples: int
    stationary_max_gap_minutes: int


@dataclass(frozen=True)
class RetentionConfig:
    enabled: bool
    cleanup_interval_hours: int
    require_completed_compaction: bool


@dataclass(frozen=True)
class AreaConfig:
    id: str
    label: str
    bbox: BBox
    interval_seconds: int
    grid_columns: int
    grid_rows: int
    start_delay_seconds: int
    stagger_seconds: int
    priority: int
    enabled: bool


@dataclass(frozen=True)
class CollectionTarget:
    id: str
    area_id: str
    area_label: str
    tile_id: str
    bbox: BBox
    interval_seconds: int
    initial_delay_seconds: int
    priority: int


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    project_root: Path
    data_dir: Path
    database: Path
    raw_dir: Path
    provider: ProviderConfig
    compaction: CompactionConfig
    retention: RetentionConfig
    idle_sleep_seconds: float
    areas: tuple[AreaConfig, ...]

    def targets(self, enabled_only: bool = True) -> tuple[CollectionTarget, ...]:
        targets: list[CollectionTarget] = []
        for area in self.areas:
            if enabled_only and not area.enabled:
                continue
            targets.extend(expand_area(area))
        return tuple(targets)


def _resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (project_root / path).resolve()


def load_config(path: str | Path = "config/regions.toml") -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("rb") as file:
        document = tomllib.load(file)

    project_root = config_path.parent.parent.resolve()
    project = document["project"]
    provider = document["provider"]
    runner = document.get("runner", {})
    compaction = document.get("compaction", {})
    retention = document.get("retention", {})

    areas: list[AreaConfig] = []
    seen_ids: set[str] = set()
    for raw_area in document.get("areas", []):
        area_id = str(raw_area["id"]).strip()
        if not area_id or area_id in seen_ids:
            raise ValueError(f"Area id is empty or duplicated: {area_id!r}")
        seen_ids.add(area_id)

        interval_seconds = int(raw_area["interval_seconds"])
        columns = int(raw_area.get("grid_columns", 1))
        rows = int(raw_area.get("grid_rows", 1))
        if interval_seconds < 10:
            raise ValueError(f"{area_id}: interval_seconds must be at least 10")
        if columns < 1 or rows < 1:
            raise ValueError(f"{area_id}: grid dimensions must be positive")

        areas.append(
            AreaConfig(
                id=area_id,
                label=str(raw_area.get("label", area_id)),
                bbox=BBox.from_list(raw_area["bbox"]),
                interval_seconds=interval_seconds,
                grid_columns=columns,
                grid_rows=rows,
                start_delay_seconds=int(raw_area.get("start_delay_seconds", 0)),
                stagger_seconds=int(raw_area.get("stagger_seconds", 0)),
                priority=int(raw_area.get("priority", 0)),
                enabled=bool(raw_area.get("enabled", True)),
            )
        )

    day_boundary_hour = int(compaction.get("day_boundary_hour", 4))
    if not 0 <= day_boundary_hour <= 23:
        raise ValueError("compaction.day_boundary_hour must be between 0 and 23")
    process_delay_minutes = int(compaction.get("process_delay_minutes", 15))
    stationary_radius_m = float(compaction.get("stationary_radius_m", 30))
    stationary_min_duration_minutes = int(
        compaction.get("stationary_min_duration_minutes", 10)
    )
    stationary_min_samples = int(compaction.get("stationary_min_samples", 5))
    stationary_max_gap_minutes = int(
        compaction.get("stationary_max_gap_minutes", 5)
    )
    if process_delay_minutes < 0:
        raise ValueError("compaction.process_delay_minutes cannot be negative")
    if stationary_radius_m <= 0:
        raise ValueError("compaction.stationary_radius_m must be positive")
    if stationary_min_duration_minutes <= 0:
        raise ValueError(
            "compaction.stationary_min_duration_minutes must be positive"
        )
    if stationary_min_samples < 2:
        raise ValueError("compaction.stationary_min_samples must be at least 2")
    if stationary_max_gap_minutes <= 0:
        raise ValueError("compaction.stationary_max_gap_minutes must be positive")
    cleanup_interval_hours = int(retention.get("cleanup_interval_hours", 24))
    if cleanup_interval_hours < 1:
        raise ValueError("retention.cleanup_interval_hours must be at least 1")

    return AppConfig(
        config_path=config_path,
        project_root=project_root,
        data_dir=_resolve_project_path(project_root, str(project["data_dir"])),
        database=_resolve_project_path(project_root, str(project["database"])),
        raw_dir=_resolve_project_path(project_root, str(project["raw_dir"])),
        provider=ProviderConfig(
            name=str(provider["name"]),
            base_url=str(provider["base_url"]),
            token_env=str(provider.get("token_env", "EURIS_API_TOKEN")),
            timeout_seconds=float(provider.get("timeout_seconds", 60)),
            max_retries=int(provider.get("max_retries", 4)),
            max_pages=int(provider.get("max_pages", 200)),
            request_gap_seconds=float(provider.get("request_gap_seconds", 0.25)),
            user_agent=str(provider.get("user_agent", "ship-analysis/0.1")),
        ),
        compaction=CompactionConfig(
            enabled=bool(compaction.get("enabled", True)),
            timezone=str(compaction.get("timezone", "Europe/Amsterdam")),
            day_boundary_hour=day_boundary_hour,
            process_delay_minutes=process_delay_minutes,
            stationary_radius_m=stationary_radius_m,
            stationary_min_duration_minutes=stationary_min_duration_minutes,
            stationary_min_samples=stationary_min_samples,
            stationary_max_gap_minutes=stationary_max_gap_minutes,
        ),
        retention=RetentionConfig(
            enabled=bool(retention.get("enabled", True)),
            cleanup_interval_hours=cleanup_interval_hours,
            require_completed_compaction=bool(
                retention.get("require_completed_compaction", True)
            ),
        ),
        idle_sleep_seconds=float(runner.get("idle_sleep_seconds", 1.0)),
        areas=tuple(areas),
    )


def expand_area(area: AreaConfig) -> list[CollectionTarget]:
    lon_step = (area.bbox.max_lon - area.bbox.min_lon) / area.grid_columns
    lat_step = (area.bbox.max_lat - area.bbox.min_lat) / area.grid_rows
    targets: list[CollectionTarget] = []

    for row in range(area.grid_rows):
        for column in range(area.grid_columns):
            index = row * area.grid_columns + column
            tile_id = (
                "all"
                if area.grid_columns == 1 and area.grid_rows == 1
                else f"r{row + 1:02d}c{column + 1:02d}"
            )
            bbox = BBox(
                min_lon=area.bbox.min_lon + column * lon_step,
                min_lat=area.bbox.min_lat + row * lat_step,
                max_lon=area.bbox.min_lon + (column + 1) * lon_step,
                max_lat=area.bbox.min_lat + (row + 1) * lat_step,
            )
            targets.append(
                CollectionTarget(
                    id=f"{area.id}:{tile_id}",
                    area_id=area.id,
                    area_label=area.label,
                    tile_id=tile_id,
                    bbox=bbox,
                    interval_seconds=area.interval_seconds,
                    initial_delay_seconds=(
                        area.start_delay_seconds + index * area.stagger_seconds
                    ),
                    priority=area.priority,
                )
            )

    return targets

# AIS analysis roadmap

## Phase 1 — collection foundation

- EuRIS bbox adapter with complete pagination, retry and backoff.
- Netherlands-wide 8x6 grid sampled every 60 seconds without overlapping
  priority areas.
- Immutable compressed snapshots, SQLite normalization and collection audit.
- Daily 04:00 operational-day stationary consolidation with auditable,
  rebuildable derived output.
- Per-operational-day staging cleanup guarded by successful daily compaction.
- Configuration-only changes for bbox and sampling frequency.

## Phase 2 — data quality and enrichment

- Coverage dashboard: successful runs, gaps, stale positions and field
  completeness.
- Fairway/port/lock reference data from EuRIS and Rijkswaterstaat FIS.
- Coordinate validity, impossible-speed and duplicate-position checks.
- Explicit unit validation for speed/course fields.
- Parquet partition export and long-term backup policy.

## Phase 3 — reusable analytical marts

- Trajectories and trips with stable identity-confidence labels.
- Gate/section traffic counts and direction.
- Speed profiles, congestion, waiting and anchoring/dwell events built from the
  daily position/stationary layer.
- Origin/destination zones and port-call detection.
- Vessel dimension/type segmentation where source fields support it.
- Route matching and ETA features.

## Phase 4 — production scale

- Add a licensed/raw coastal AIS provider adapter if North Sea coverage is
  required; EuRIS is primarily inland-waterway track data.
- Move normalized observations to PostgreSQL/PostGIS or TimescaleDB.
- Write partitioned Parquet for DuckDB/Python research workloads.
- Service supervision, metrics, alerts, backups and controlled retention.

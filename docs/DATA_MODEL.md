# Data model

## Storage layers

The collector intentionally writes two layers:

1. `data/raw/.../*.json.gz` is an immutable source snapshot. It contains run
   metadata, source URL, bbox, page counts, attribution and every returned
   provider record.
2. `data/ship_analysis.db` is a normalized SQLite research store. It is useful
   for quick exploration and acts as the contract for a later
    PostgreSQL/TimescaleDB or Parquet implementation.
3. `data/compacted/.../*.json.gz` is a rebuildable daily analytical layer. It
   keeps individual `position` records but collapses qualified unchanged
   sequences into `stationary` records.

The raw and normalized layers are staging data for one 04:00-to-04:00
operational day. Daily compaction never rewrites them while processing. After
the matching operational day completes successfully, a separate retention
worker removes raw `.json.gz`, provenance links, and normalized observations
that are no longer referenced by another operational day. Daily position and
stationary records remain available.

## Main tables

### `collection_runs`

One record per logical bbox collection. It records success/failure, the
requested area/tile, pagination counts, item counts, timings and snapshot path.
Use this table to audit gaps and collector health.

### `observations`

One record per unique provider position observation. The uniqueness key is
derived from provider, track id, provider position timestamp and coordinates.
Overlapping national/priority boxes therefore do not duplicate the analytical
row. `within_run_duplicate_count` measures exact observations repeated during
one live pagination run, while `duplicate_count` measures observations already
present from an earlier run. These are kept separate because they represent
different quality questions.

`reported_count_delta = item_count - reported_count` measures a second live
pagination effect: the provider's reported total can change while the pages are
being fetched. A non-zero value does not by itself mean that a `nextPageLink`
was skipped; it means the response was not a transactionally frozen snapshot.

`first_fetched_at_utc`, `last_fetched_at_utc` and `seen_count` distinguish the
provider position time from the times at which this collector saw the record.

### `collection_observations`

Many-to-many provenance between collection runs and observations. An
observation can be returned by both a national tile and a priority area without
losing either provenance. `inside_requested_bbox` is calculated from the actual
returned coordinate because EuRIS can return a small number of positions just
outside the requested bbox. The raw response is retained, while strict spatial
    analysis can filter this flag.

### `daily_compaction_runs`

One audit row per operational day. An operational day is defined in
`Europe/Amsterdam` local time from 04:00 to 04:00 the following day. The row
stores the exact UTC bounds, compaction settings, counts, output path and
success/failure state. Completed days are skipped unless a forced rebuild is
requested.

### Raw retention audit

`collection_runs.raw_deleted_at_utc` and `raw_retention_reason` record removal
of a raw snapshot. `details_deleted_at_utc` and `details_retention_reason`
record cleanup of normalized provenance. The original `snapshot_path` is
retained for audit. Cleanup only accepts `.json.gz` paths resolved below the
configured raw directory and requires a completed `daily_compaction_runs` row
for the matching operational day. An observation shared with an uncleaned day
is retained until its final provenance link is removed.

### `daily_track_records`

The derived daily trajectory layer has two record types:

- `position`: one source sample that was not eligible for stationary
  consolidation.
- `stationary`: a continuous group for one non-empty `trackID`, lasting at
  least 10 minutes and five samples, with no gap over five minutes, no explicit
  `isMoving=true`, and all positions inside a 30 metre radius.

A stationary record keeps its start/end time, centroid, start/end coordinates,
duration, source sample count, distinct observation count, maximum radius and
latest available vessel metadata. It does not contain `raw_json`; source
records remain available through the immutable raw snapshot and normalized
tables.

### `collection_period_stats`

Durable minute, hour and 04:00-to-04:00 operational-day collection metrics.
`received_item_count` is the sum returned by the provider,
`unique_item_count` is the sum after deduplication inside each request, and
`new_observation_count` is the number newly inserted into the normalized
store. These are deliberately separate metrics.

`distinct_observation_count` and `distinct_track_count` are exact across runs
only while provenance details are available. They are materialized before
daily staging cleanup; `details_complete=0` marks a period that can no longer
be exactly recomputed from cleaned provenance.

### `daily_collection_summaries`

One deterministic health summary per operational day. It stores the exact UTC
bounds, health status, human-readable findings, the complete JSON payload and
the output-file path. The JSON includes per-provider/channel totals, coverage,
peak minute/hour, collection quality signals and daily compaction status.

## Semantic cautions

- `position_time_utc` is the provider position timestamp. It is not the local
  collection time.
- `speed_ground` and `course_ground` preserve the EuRIS values. The current
  schema deliberately does not put a unit in the speed column name until the
  provider contract and observed values are validated for the intended
  analysis.
- EuRIS `trackID` is a provider/session target identifier, not automatically a
  permanent vessel identity.
- `mmsi`, `eni`, vessel type and other identity fields may be absent, masked,
  zero or `-1` because of privacy and source availability. Do not infer vessel
  type from those sentinel values.
- Every normalized row retains the complete provider object in `raw_json`.

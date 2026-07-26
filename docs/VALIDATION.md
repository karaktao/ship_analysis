# Live validation — 2026-07-26

This note records the bootstrap checks used to choose the default collection
strategy. Counts are time-sensitive and must not be treated as permanent
properties of the EuRIS service.

## Source contract

- Provider: EuRIS Tracks v2 `GetTracksByBBoxV2`.
- Netherlands research bbox: `3.20, 50.70, 7.30, 53.70` (WGS 84).
- Pagination followed `nextPageLink` until it was empty.
- Exact observation identity used provider, `trackID`, position timestamp and
  coordinates.

## One large bbox

The direct whole-bbox check completed in 90.24 seconds:

| Metric | Result |
|---|---:|
| Initial provider `count` | 9,571 |
| Records obtained from 96 pages | 9,538 |
| `records - count` | -33 |
| Unique position observations | 6,967 |
| Unique observations inside the requested bbox | 6,967 |
| Distinct track IDs inside the bbox | 6,960 |

The gap between 9,538 records and 6,967 unique observations demonstrates that
the page stream contains many repeated current positions. The `count` also
changed while pagination was in progress.

## Rolling 8 × 6 grid

The 48-tile baseline completed successfully in approximately 39 seconds:

| Metric | Result |
|---|---:|
| Successful targets | 48 / 48 |
| Source records summed across tiles | 7,794 |
| Per-tile unique observations summed | 7,329 |
| Exact duplicates within tile pagination | 465 |
| Unique observations across all tiles | 6,835 |
| Unique observations inside the national bbox | 6,822 |
| Distinct track IDs across tiles | 6,800 |
| Total provider pages | 110 |

The grid and large-bbox runs were not simultaneous, so their 2.1% unique-count
difference is not a formal completeness comparison. It is sufficient evidence
that smaller rolling tiles materially reduce pagination duplication and
collection latency while preserving similar coverage.

The configured production cadence is now 60 seconds for every tile. The
observed 39-second serial cycle leaves limited headroom; monitor cycle latency,
retry counts and HTTP 429 responses continuously. A slow or retried cycle can
miss the one-minute target even though the scheduler remains alive.

EuRIS also returned observations just outside individual requested tiles. The
collector therefore retains every source record but writes
`collection_observations.inside_requested_bbox` for strict spatial filtering.
For the full national boundary, apply the national bbox or a future Netherlands
waterway polygon rather than summing the per-tile `outside_bbox_count`.

## Consequences for analysis

- Never use provider `count`, raw page rows or a sum of tile rows as a vessel
  count.
- Use normalized position identity across all relevant collection runs.
- Use `position_time_utc`, not fetch time, to order a trajectory.
- Treat `trackID` as a provider/session target id until a stable identity
  linkage is validated.
- For exact gate/section traffic, prefer small spatial gates or the EuRIS
  fairway-section endpoint and perform crossing detection on trajectories.

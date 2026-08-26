from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from .collector import Collector
from .compaction import DailyCompactor, latest_ready_operational_date
from .config import CollectionTarget, load_config
from .dashboard import export_dashboard_snapshot, serve_dashboard_api
from .maintenance import MaintenanceWorker
from .reporting import CollectionReporter
from .retention import StagingRetention
from .storage import Storage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ship-analysis",
        description="Collect Netherlands EuRIS AIS/track snapshots",
    )
    parser.add_argument(
        "--config",
        default="config/regions.toml",
        help="TOML configuration path (default: config/regions.toml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create or upgrade the local SQLite schema")
    subparsers.add_parser("plan", help="Print expanded targets and schedules")

    collect = subparsers.add_parser("collect", help="Run one collection")
    collect.add_argument(
        "--area",
        required=True,
        help="Configured area id, for example nl_coverage",
    )
    collect.add_argument(
        "--tile",
        help="Optional expanded tile id, for example r01c01",
    )

    subparsers.add_parser("run", help="Run the continuous national scheduler")

    maintenance = subparsers.add_parser(
        "maintenance",
        help="Run reporting, daily compaction and retention outside the collector",
    )
    maintenance.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="Seconds between maintenance passes (default: 60)",
    )
    maintenance.add_argument(
        "--with-retention",
        action="store_true",
        help=(
            "Also run staging deletion; disabled by default because large "
            "SQLite deletes can delay collection writes"
        ),
    )

    recover = subparsers.add_parser(
        "recover-stale-runs",
        help="Mark abandoned running requests as failed after a grace period",
    )
    recover.add_argument(
        "--max-age-minutes",
        type=int,
        default=15,
        help="Minimum age of a running row to recover (default: 15)",
    )

    compact = subparsers.add_parser(
        "compact-day",
        help="Build the stationary-compacted layer for one operational day",
    )
    compact.add_argument(
        "--date",
        help="Operational date YYYY-MM-DD; defaults to the latest ready day",
    )
    compact.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing completed compaction for the day",
    )

    prune = subparsers.add_parser(
        "prune-staging",
        aliases=["prune-raw"],
        help="Preview or clean compacted operational-day staging data",
    )
    prune.add_argument(
        "--apply",
        action="store_true",
        help="Delete eligible raw snapshots; without this flag only preview",
    )
    prune.add_argument(
        "--max-runs",
        type=int,
        help="Maximum oldest collection runs to inspect in this pass",
    )
    prune.add_argument(
        "--batch-pause-seconds",
        type=float,
        default=0.0,
        help="Pause between committed SQLite delete batches (default: 0)",
    )

    status = subparsers.add_parser("status", help="Show local database status")
    status.add_argument("--limit", type=int, default=20)

    collection_log = subparsers.add_parser(
        "collection-log",
        help="Show materialized minute, hour or operational-day collection totals",
    )
    collection_log.add_argument(
        "--period",
        choices=("minute", "hour", "day"),
        default="minute",
        help="Aggregation period (default: minute)",
    )
    collection_log.add_argument("--limit", type=int, default=20)

    daily_summary = subparsers.add_parser(
        "daily-summary",
        help="Generate or show the deterministic daily collection summary",
    )
    daily_summary.add_argument(
        "--date",
        help="Operational date YYYY-MM-DD; defaults to the latest ready day",
    )
    daily_summary.add_argument(
        "--force",
        action="store_true",
        help="Recalculate and replace an existing daily summary",
    )
    daily_summary.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Allow a partial summary before the operational day closes",
    )

    dashboard_api = subparsers.add_parser(
        "dashboard-api",
        help="Serve the local read-only dashboard JSON API",
    )
    dashboard_api.add_argument("--host", default="127.0.0.1")
    dashboard_api.add_argument("--port", type=int, default=8765)

    dashboard_snapshot = subparsers.add_parser(
        "dashboard-snapshot",
        help="Export an aggregate dashboard snapshot without vessel details",
    )
    dashboard_snapshot.add_argument(
        "--output",
        default="dashboard/public/dashboard-snapshot.json",
    )
    return parser


def _matching_targets(
    targets: tuple[CollectionTarget, ...], area_id: str, tile_id: str | None
) -> list[CollectionTarget]:
    matches = [
        target
        for target in targets
        if target.area_id == area_id
        and (tile_id is None or target.tile_id == tile_id)
    ]
    if not matches:
        choices = ", ".join(sorted({target.area_id for target in targets}))
        raise ValueError(f"No matching target. Available areas: {choices}")
    return matches


def _print_plan(config_path: Path) -> None:
    config = load_config(config_path)
    print(
        f"{'TARGET':35} {'INTERVAL':>10} {'DELAY':>8} "
        f"{'PRIORITY':>8}  BBOX"
    )
    for target in config.targets():
        print(
            f"{target.id:35} {target.interval_seconds:>9}s "
            f"{target.initial_delay_seconds:>7}s {target.priority:>8}  "
            f"{target.bbox.compact()}"
        )


def _print_status(storage: Storage, limit: int) -> None:
    counts = storage.counts()
    print(
        f"runs={counts['runs']} observations={counts['observations']} "
        f"distinct_tracks={counts['tracks']} "
        f"compacted_days={counts['compacted_days']} "
        f"compacted_records={counts['compacted_records']} "
        f"raw_deleted={counts['raw_deleted']} "
        f"details_deleted_runs={counts['details_deleted_runs']}"
    )
    rows = storage.recent_runs(max(1, limit))
    if not rows:
        print("No collection runs yet.")
        return
    print(
        f"{'STARTED UTC':27} {'AREA/TILE':36} {'STATUS':10} "
        f"{'ITEMS':>7} {'UNIQUE':>7} {'NEW':>7} {'OLD':>7} "
        f"{'PGDUP':>7} {'DELTA':>7} {'OUT':>5} {'PAGES':>6}"
    )
    for row in rows:
        area = f"{row['area_id']}:{row['tile_id']}"
        print(
            f"{row['started_at_utc'][:26]:27} {area[:36]:36} "
            f"{row['status']:10} {str(row['item_count'] or ''):>7} "
            f"{str(row['unique_item_count'] or ''):>7} "
            f"{str(row['inserted_count'] or ''):>7} "
            f"{str(row['duplicate_count'] or ''):>7} "
            f"{str(row['within_run_duplicate_count'] or ''):>7} "
            f"{str(row['reported_count_delta'] if row['reported_count_delta'] is not None else ''):>7} "
            f"{str(row['outside_bbox_count'] if row['outside_bbox_count'] is not None else ''):>5} "
            f"{str(row['pages'] or ''):>6}"
        )
        if row["error"]:
            print(f"  error: {row['error']}")
    compactions = storage.recent_compactions(3)
    if compactions:
        print("\nRECENT DAILY COMPACTIONS")
        for row in compactions:
            print(
                f"{row['operational_date']} {row['status']} "
                f"samples={row['source_sample_count'] or 0} "
                f"records={row['output_record_count'] or 0} "
                f"stationary={row['stationary_record_count'] or 0} "
                f"collapsed={row['stationary_source_sample_count'] or 0}"
            )
            if row["error"]:
                print(f"  error: {row['error']}")


def _configure_file_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "collector.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=14,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.getLogger().addHandler(handler)


def _print_collection_log(
    reporter: CollectionReporter, period: str, limit: int
) -> None:
    rows = reporter.list_periods(period, max(1, limit))
    if not rows:
        print("No collection statistics yet.")
        return
    print(
        f"{'PERIOD LOCAL':27} {'FINAL':>5} {'RUNS':>12} {'FAIL':>5} "
        f"{'RECEIVED':>10} {'UNIQUE':>10} {'NEW':>10} {'OLD':>10} "
        f"{'P95 SEC':>8}"
    )
    for row in rows:
        runs = f"{row.completed_run_count}/{row.expected_run_count}"
        p95 = (
            f"{row.p95_elapsed_seconds:.2f}"
            if row.p95_elapsed_seconds is not None
            else ""
        )
        print(
            f"{row.period_label_local[:27]:27} "
            f"{('yes' if row.is_final else 'no'):>5} "
            f"{runs:>12} {row.failed_run_count:>5} "
            f"{row.received_item_count:>10} {row.unique_item_count:>10} "
            f"{row.new_observation_count:>10} "
            f"{row.existing_observation_count:>10} {p95:>8}"
        )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_config(arguments.config)
        storage = Storage(config.database, config.raw_dir)
        if arguments.command in {"collect", "run", "maintenance"}:
            _configure_file_logging(config.data_dir)

        if arguments.command == "init-db":
            storage.initialize()
            print(f"Initialized {config.database}")
            return 0
        if arguments.command == "plan":
            _print_plan(config.config_path)
            return 0
        if arguments.command == "status":
            _print_status(storage, arguments.limit)
            return 0
        if arguments.command == "recover-stale-runs":
            storage.initialize()
            recovered = storage.recover_stale_runs(
                max_age_seconds=arguments.max_age_minutes * 60
            )
            print(
                f"recovered-stale-runs={recovered} "
                f"max_age_minutes={arguments.max_age_minutes}"
            )
            return 0
        if arguments.command == "collection-log":
            reporter = CollectionReporter(config, storage)
            _print_collection_log(
                reporter, arguments.period, arguments.limit
            )
            return 0
        if arguments.command == "daily-summary":
            reporter = CollectionReporter(config, storage)
            operational_date = (
                date.fromisoformat(arguments.date)
                if arguments.date
                else latest_ready_operational_date(
                    datetime.now(timezone.utc), config.compaction
                )
            )
            summary = reporter.generate_daily_summary(
                operational_date,
                force=arguments.force,
                allow_incomplete=arguments.allow_incomplete,
            )
            print(summary.summary_text)
            print(f"output={summary.output_path}")
            return 0
        if arguments.command == "dashboard-api":
            if not 1 <= arguments.port <= 65535:
                raise ValueError("dashboard API port must be between 1 and 65535")
            serve_dashboard_api(
                config,
                storage,
                host=arguments.host,
                port=arguments.port,
            )
            return 0
        if arguments.command == "dashboard-snapshot":
            output = Path(arguments.output)
            if not output.is_absolute():
                output = (config.project_root / output).resolve()
            path = export_dashboard_snapshot(config, storage, output)
            print(f"dashboard-snapshot={path}")
            return 0
        if arguments.command == "maintenance":
            worker = MaintenanceWorker(
                config, run_retention=arguments.with_retention
            )
            try:
                worker.run_forever(arguments.interval_seconds)
            except KeyboardInterrupt:
                print("\nMaintenance stopped.")
            return 0
        if arguments.command == "compact-day":
            storage.initialize()
            compactor = DailyCompactor(config, storage)
            operational_date = (
                date.fromisoformat(arguments.date)
                if arguments.date
                else latest_ready_operational_date(
                    datetime.now(timezone.utc), config.compaction
                )
            )
            outcome = compactor.compact_day(
                operational_date, force=arguments.force
            )
            if outcome is None:
                print("No compaction was produced.")
                return 0
            print(
                f"compacted day={outcome.operational_date} "
                f"samples={outcome.source_samples} tracks={outcome.tracks} "
                f"records={outcome.output_records} "
                f"positions={outcome.position_records} "
                f"stationary={outcome.stationary_records} "
                f"collapsed_samples={outcome.stationary_source_samples}"
            )
            print(f"output={outcome.output_path}")
            return 0
        if arguments.command in {"prune-staging", "prune-raw"}:
            outcome = StagingRetention(config, storage).prune(
                dry_run=not arguments.apply,
                max_runs=arguments.max_runs,
                batch_pause_seconds=arguments.batch_pause_seconds,
            )
            mode = "applied" if arguments.apply else "preview"
            print(
                f"staging-retention mode={mode} "
                f"through={outcome.eligible_through_operational_date} "
                f"runs={outcome.candidate_runs} raw={outcome.raw_deleted} "
                f"missing={outcome.raw_missing} "
                f"raw_too_new={outcome.skipped_raw_too_new} "
                f"links={outcome.provenance_links_deleted} "
                f"observations={outcome.observations_deleted} "
                f"run_history={outcome.run_history_deleted} "
                f"uncompacted={outcome.skipped_uncompacted} "
                f"unsafe={outcome.skipped_unsafe_path} "
                f"bytes={outcome.bytes_deleted} "
                f"checkpoint_busy={outcome.wal_checkpoint_busy}"
            )
            if not arguments.apply:
                print("Preview only. Add --apply to delete eligible files.")
            return 0

        collector = Collector(config)
        if arguments.command == "collect":
            targets = _matching_targets(
                config.targets(), arguments.area, arguments.tile
            )
            outcomes = []
            failures = 0
            for target in targets:
                try:
                    outcome = collector.collect(target)
                except Exception as error:
                    failures += 1
                    print(
                        f"failed target={target.id} "
                        f"error={type(error).__name__}: {error}",
                        file=sys.stderr,
                    )
                    continue
                outcomes.append(outcome)
                print(
                    f"ok target={outcome.target_id} items={outcome.items} "
                    f"unique={outcome.unique_items} inserted={outcome.inserted} "
                    f"existing={outcome.existing} "
                    f"page_duplicates={outcome.within_run_duplicates} "
                    f"count_delta={outcome.reported_count_delta} "
                    f"pages={outcome.pages} elapsed={outcome.elapsed_seconds:.2f}s"
                )
                print(f"snapshot={outcome.snapshot_path}")
            if len(outcomes) > 1:
                area = next(
                    area for area in config.areas if area.id == arguments.area
                )
                aggregate = storage.aggregate_runs(
                    [outcome.run_id for outcome in outcomes], area.bbox
                )
                print(
                    f"area-complete targets={len(outcomes)} "
                    f"items={sum(item.items for item in outcomes)} "
                    f"per_target_unique_sum="
                    f"{sum(item.unique_items for item in outcomes)} "
                    f"distinct_across_targets="
                    f"{aggregate['distinct_observations']} "
                    f"inside_area_bbox={aggregate['inside_area_bbox']} "
                    f"inserted={sum(item.inserted for item in outcomes)} "
                    f"failures={failures}"
                )
            return 1 if failures else 0
        if arguments.command == "run":
            try:
                collector.run_forever()
            except KeyboardInterrupt:
                print("\nCollector stopped.")
            return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0

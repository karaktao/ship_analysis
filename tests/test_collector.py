from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
import threading
import unittest

from ship_analysis.collector import CollectionOutcome, Collector
from ship_analysis.config import BBox, CollectionTarget


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        self.futures: list[Future[CollectionOutcome]] = []

    def submit(self, function: object, *args: object, **kwargs: object) -> Future[CollectionOutcome]:
        future: Future[CollectionOutcome] = Future()
        self.calls.append((function, args, kwargs))
        self.futures.append(future)
        return future


def target(name: str) -> CollectionTarget:
    return CollectionTarget(
        id=name,
        area_id="test",
        area_label="Test",
        tile_id=name,
        bbox=BBox(3.0, 50.0, 4.0, 51.0),
        interval_seconds=60,
        initial_delay_seconds=0,
        priority=1,
    )


class CollectorSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = object.__new__(Collector)
        self.collector.config = SimpleNamespace(collection_workers=2)
        self.collector.collect = lambda *_args, **_kwargs: None

    def test_dispatch_is_bounded_and_does_not_overlap_a_tile(self) -> None:
        executor = FakeExecutor()
        in_flight: dict[str, Future[CollectionOutcome]] = {}

        self.assertEqual(
            "submitted",
            self.collector._dispatch_target(
                executor, in_flight, target("tile-1"), scheduler_lag_seconds=0
            ),
        )
        self.assertEqual(
            "target-still-running",
            self.collector._dispatch_target(
                executor, in_flight, target("tile-1"), scheduler_lag_seconds=60
            ),
        )
        self.assertEqual(
            "submitted",
            self.collector._dispatch_target(
                executor, in_flight, target("tile-2"), scheduler_lag_seconds=0
            ),
        )
        self.assertEqual(
            "worker-pool-full",
            self.collector._dispatch_target(
                executor, in_flight, target("tile-3"), scheduler_lag_seconds=0
            ),
        )
        self.assertEqual(2, len(executor.calls))

    def test_reap_frees_slots_after_success_and_failure(self) -> None:
        success: Future[CollectionOutcome] = Future()
        failure: Future[CollectionOutcome] = Future()
        pending: Future[CollectionOutcome] = Future()
        success.set_result(None)  # type: ignore[arg-type]
        failure.set_exception(RuntimeError("recorded by collect"))
        in_flight = {
            "success": success,
            "failure": failure,
            "pending": pending,
        }

        self.collector._reap_completed(in_flight)

        self.assertEqual({"pending": pending}, in_flight)

    def test_one_blocked_tile_does_not_block_another_worker(self) -> None:
        release = threading.Event()
        both_started = threading.Event()
        started: set[str] = set()
        started_lock = threading.Lock()

        def blocked_collect(
            collection_target: CollectionTarget,
            *,
            scheduler_lag_seconds: float,
        ) -> None:
            del scheduler_lag_seconds
            with started_lock:
                started.add(collection_target.id)
                if len(started) == 2:
                    both_started.set()
            release.wait(timeout=2)

        self.collector.collect = blocked_collect
        in_flight: dict[str, Future[CollectionOutcome]] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            for name in ("slow-tile", "other-tile"):
                self.assertEqual(
                    "submitted",
                    self.collector._dispatch_target(
                        executor,
                        in_flight,
                        target(name),
                        scheduler_lag_seconds=0,
                    ),
                )
            self.assertTrue(both_started.wait(timeout=1))
            release.set()

        self.collector._reap_completed(in_flight)
        self.assertEqual({}, in_flight)


if __name__ == "__main__":
    unittest.main()

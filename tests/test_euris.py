import json
from urllib.error import HTTPError
import unittest
from unittest.mock import patch

from ship_analysis.config import BBox, ProviderConfig
from ship_analysis.providers import EurisClient, FetchError, ProviderUnavailable


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def provider_config(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "name": "test",
        "base_url": "https://example.test/tracks",
        "token_env": "TEST_EURIS_TOKEN",
        "timeout_seconds": 25,
        "max_retries": 2,
        "max_pages": 10,
        "request_gap_seconds": 0,
        "user_agent": "test",
        "request_budget_seconds": 45,
        "circuit_failure_threshold": 8,
        "circuit_cooldown_seconds": 60,
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


def server_error() -> HTTPError:
    return HTTPError(
        "https://example.test/tracks",
        500,
        "Internal Server Error",
        {},
        None,
    )


def auth_error() -> HTTPError:
    return HTTPError(
        "https://example.test/tracks",
        401,
        "Unauthorized",
        {},
        None,
    )


class EurisClientTests(unittest.TestCase):
    bbox = BBox(4.0, 51.0, 5.0, 52.0)

    def test_retry_count_is_reported_after_transient_recovery(self) -> None:
        clock = FakeClock()
        client = EurisClient(provider_config())
        response = FakeResponse({"count": 1, "items": [{"trackID": "1"}]})
        with (
            patch(
                "ship_analysis.providers.euris.urlopen",
                side_effect=[server_error(), response],
            ) as request,
            patch(
                "ship_analysis.providers.euris.time.monotonic",
                side_effect=clock.monotonic,
            ),
            patch(
                "ship_analysis.providers.euris.time.sleep",
                side_effect=clock.sleep,
            ),
            patch("ship_analysis.providers.euris.random.random", return_value=0),
        ):
            result = client.fetch_bbox(self.bbox)

        self.assertEqual(2, request.call_count)
        self.assertEqual(1, result.retry_count)
        self.assertEqual(1, len(result.items))

    def test_bbox_budget_bounds_a_timeout_before_all_retries(self) -> None:
        clock = FakeClock()
        client = EurisClient(
            provider_config(
                timeout_seconds=25,
                max_retries=4,
                request_budget_seconds=3,
            )
        )
        observed_timeouts: list[float] = []

        def timeout_request(_request: object, *, timeout: float) -> None:
            observed_timeouts.append(timeout)
            clock.now += timeout
            raise TimeoutError("timed out")

        with (
            patch(
                "ship_analysis.providers.euris.urlopen",
                side_effect=timeout_request,
            ) as request,
            patch(
                "ship_analysis.providers.euris.time.monotonic",
                side_effect=clock.monotonic,
            ),
        ):
            with self.assertRaisesRegex(FetchError, "after 1 attempt"):
                client.fetch_bbox(self.bbox)

        self.assertEqual(1, request.call_count)
        self.assertEqual([3], observed_timeouts)

    def test_circuit_opens_and_suppresses_network_requests(self) -> None:
        client = EurisClient(
            provider_config(
                max_retries=0,
                circuit_failure_threshold=2,
            )
        )
        with patch(
            "ship_analysis.providers.euris.urlopen",
            side_effect=server_error(),
        ) as request:
            with self.assertRaises(FetchError):
                client.fetch_bbox(self.bbox)
            with self.assertRaises(FetchError):
                client.fetch_bbox(self.bbox)
            with self.assertRaises(ProviderUnavailable):
                client.fetch_bbox(self.bbox)

        self.assertEqual(2, request.call_count)

    def test_auth_failure_is_fail_fast_and_does_not_open_circuit(self) -> None:
        client = EurisClient(
            provider_config(
                max_retries=2,
                circuit_failure_threshold=1,
            )
        )
        with patch(
            "ship_analysis.providers.euris.urlopen",
            side_effect=[auth_error(), auth_error()],
        ) as request:
            with self.assertRaisesRegex(FetchError, "EuRIS HTTP 401"):
                client.fetch_bbox(self.bbox)
            with self.assertRaisesRegex(FetchError, "EuRIS HTTP 401"):
                client.fetch_bbox(self.bbox)

        self.assertEqual(2, request.call_count)

    def test_budget_is_shared_across_all_pagination_pages(self) -> None:
        clock = FakeClock()
        client = EurisClient(
            provider_config(
                request_budget_seconds=45,
                request_gap_seconds=0.35,
            )
        )
        first_page = FakeResponse(
            {
                "count": 2,
                "items": [{"trackID": "1"}],
                "nextPageLink": "/tracks?page=2",
            }
        )

        def slow_first_page(_request: object, *, timeout: float) -> FakeResponse:
            self.assertEqual(25, timeout)
            clock.now += 44.8
            return first_page

        with (
            patch(
                "ship_analysis.providers.euris.urlopen",
                side_effect=slow_first_page,
            ) as request,
            patch(
                "ship_analysis.providers.euris.time.monotonic",
                side_effect=clock.monotonic,
            ),
        ):
            with self.assertRaisesRegex(FetchError, "during pagination"):
                client.fetch_bbox(self.bbox)

        self.assertEqual(1, request.call_count)

    def test_successful_probe_closes_circuit(self) -> None:
        clock = FakeClock()
        client = EurisClient(
            provider_config(
                max_retries=0,
                circuit_failure_threshold=1,
                circuit_cooldown_seconds=5,
            )
        )
        response = FakeResponse({"count": 0, "items": []})
        with (
            patch(
                "ship_analysis.providers.euris.urlopen",
                side_effect=[server_error(), response, response],
            ) as request,
            patch(
                "ship_analysis.providers.euris.time.monotonic",
                side_effect=clock.monotonic,
            ),
        ):
            with self.assertRaises(FetchError):
                client.fetch_bbox(self.bbox)
            with self.assertRaises(ProviderUnavailable):
                client.fetch_bbox(self.bbox)
            clock.now = 6
            client.fetch_bbox(self.bbox)
            client.fetch_bbox(self.bbox)

        self.assertEqual(3, request.call_count)


if __name__ == "__main__":
    unittest.main()

from threading import Event
from unittest.mock import patch
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secs_inference.provider.api import (
    HelloRejected,
    HelloUnavailable,
    ProviderApi,
)
from secs_inference.provider.config import HelloPolicy
from secs_inference.provider.hello import HelloAccepted, prepare_hello
from secs_inference.provider.http import (
    HttpResponse,
    HttpsEndpoint,
    RequestDelivery,
    RequestUnavailable,
)
from secs_inference.provider.process import publish_hello_until_stopped


class StopAfterWaits(Event):
    def __init__(self, maximum_waits: int):
        super().__init__()
        self.maximum_waits = maximum_waits
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.maximum_waits:
            self.set()
        return self.is_set()


class ProviderProcessTests(unittest.TestCase):
    def setUp(self):
        self.api = ProviderApi(
            HttpsEndpoint("https://api.example.test", "web", 1, 1),
            "provider:secs",
            "credential:provider:secs",
            Ed25519PrivateKey.generate(),
        )
        self.prepared = prepare_hello(
            display_name="Provider",
            description="Description",
            analysis_offerings=(),
        )
        self.policy = HelloPolicy("Provider", "Description", 3600, 5)

    def test_success_publishes_immediately_then_waits_for_refresh(self):
        stop = StopAfterWaits(1)

        with patch.object(
            ProviderApi,
            "publish_hello",
            return_value=HelloAccepted(
                "provider:secs",
                "2026-08-31T12:34:56Z",
            ),
        ) as publish:
            publish_hello_until_stopped(
                api=self.api,
                prepared=self.prepared,
                policy=self.policy,
                stop=stop,
            )

        publish.assert_called_once_with(self.prepared)
        self.assertEqual(stop.waits, [3600])

    def test_unavailability_backs_off_and_success_restores_refresh_cadence(self):
        stop = StopAfterWaits(3)
        unavailable = HelloUnavailable(
            RequestUnavailable(RequestDelivery.NOT_SENT)
        )

        with (
            patch.object(
                ProviderApi,
                "publish_hello",
                side_effect=(
                    unavailable,
                    unavailable,
                    HelloAccepted(
                        "provider:secs",
                        "2026-08-31T12:34:56Z",
                    ),
                ),
            ) as publish,
            self.assertLogs(
                "secs_inference.provider.process",
                level="INFO",
            ) as logs,
        ):
            publish_hello_until_stopped(
                api=self.api,
                prepared=self.prepared,
                policy=self.policy,
                stop=stop,
            )

        self.assertEqual(publish.call_count, 3)
        self.assertEqual(stop.waits, [5, 10, 3600])
        self.assertEqual(
            [line.split(":", 1)[0] for line in logs.output],
            ["WARNING", "INFO"],
        )
        self.assertIn("unavailable", logs.output[0])
        self.assertIn("recovered", logs.output[1])

    def test_fixed_request_rejection_stops_without_retry(self):
        stop = StopAfterWaits(1)
        rejected = HelloRejected(HttpResponse(400, "request-test", b"{}"))

        with (
            patch.object(
                ProviderApi,
                "publish_hello",
                return_value=rejected,
            ) as publish,
            self.assertRaisesRegex(RuntimeError, "HTTP 400.*request-test"),
        ):
            publish_hello_until_stopped(
                api=self.api,
                prepared=self.prepared,
                policy=self.policy,
                stop=stop,
            )

        publish.assert_called_once_with(self.prepared)
        self.assertEqual(stop.waits, [])


if __name__ == "__main__":
    unittest.main()

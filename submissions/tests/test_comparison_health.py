from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from submissions.controllers.pdf_comparison import formatting_comparison_services
from submissions.services import comparison_health as health
from submissions.services.comparison_client import TransportUnavailable
from submissions.services.comparison_protocol import contract_id
from submissions.services.pdf_comparison_errors import PDFComparisonError
from submissions.version import APP_VERSION


@override_settings(CFM_COMPARISON_TRANSPORT="service",
    CFM_COMPARISON_LOCAL_URL="http://local:8091", CFM_COMPARISON_LOCAL_TOKEN="l" * 32,
    CFM_COMPARISON_REMOTE_URL="http://remote:8091", CFM_COMPARISON_REMOTE_TOKEN="r" * 32,
    CFM_COMPARISON_REMOTE_HEADERS="")
class ComparisonHealthTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.contract = contract_id()
        self.calls = []
        self.offline = False
        def call(endpoint, path, **kwargs):
            self.calls.append((endpoint.name, path, kwargs))
            if self.offline and endpoint.name == "remote":
                raise TransportUnavailable("Worker request timed out.")
            return 200, {"protocol": 1, "contract": self.contract, "state": "ready", "backend": "cuda" if endpoint.name == "remote" else "cpu",
                         "worker_version": APP_VERSION, "settings": {"threads": 4, "token": "NEVER-EXPOSE"}}
        self.transport = self.enterContext(patch("submissions.services.comparison_client.Endpoint.call", autospec=True, side_effect=call))

    def test_health_endpoint_is_get_only_bounded_and_uses_no_database_or_job_requests(self):
        response = formatting_comparison_services(RequestFactory().get("/reviews/formatting/comparison-services/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        result = health.comparison_service_status()
        self.assertEqual(result["remote"]["backend"], "cuda")
        self.assertEqual(result["local"]["backend"], "cpu")
        self.assertEqual(result["app_version"], APP_VERSION)
        self.assertEqual(result["remote"]["worker_version"], APP_VERSION)
        self.assertEqual(result["local"]["settings"], {"threads": 4})
        self.assertNotIn("NEVER-EXPOSE", str(result))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(path == "/v1/health" and kwargs == {"timeout": 2} for _, path, kwargs in self.calls))
        self.assertEqual(formatting_comparison_services(RequestFactory().post("/" )).status_code, 405)

    def test_failures_keep_last_seen_backend_without_claiming_it_is_current(self):
        health.comparison_service_status()
        self.offline = True
        with patch.object(health, "CACHE_SECONDS", 0):
            # Expire the observation, not the independently retained backend.
            key = "cfm-comparison-health:" + health.configuration_identity() + self.contract
            cache.delete(key)
            result = health.comparison_service_status()
        self.assertEqual(result["remote"]["state"], "unreachable")
        self.assertEqual(result["remote"]["backend"], "")
        self.assertEqual(result["remote"]["last_backend"], "cuda")
        self.assertEqual(result["remote"]["last_metadata"]["worker_version"], APP_VERSION)
        self.assertEqual(result["remote"]["last_metadata"]["settings"], {"threads": 4})
        self.assertEqual(result["remote"]["settings"], {})
        self.assertEqual(result["local"]["state"], "ready")

    def test_gateway_error_is_displayed_and_other_worker_still_checked(self):
        def call(endpoint, path, **kwargs):
            if endpoint.name == "remote":
                raise PDFComparisonError("HTTP 403: Cloudflare error 1010.")
            return 200, {"protocol": 1, "contract": self.contract, "state": "ready", "backend": "cpu"}
        self.transport.side_effect = call
        result = health.comparison_service_status()
        self.assertIn("1010", result["remote"]["message"])
        self.assertEqual(result["local"]["state"], "ready")

    def test_changed_credentials_do_not_reuse_old_health_or_backend(self):
        health.comparison_service_status()
        self.offline = True
        with override_settings(CFM_COMPARISON_REMOTE_HEADERS='{"X-Key":"rotated-secret"}'):
            result = health.comparison_service_status()
        self.assertEqual(result["remote"]["last_backend"], "")
        self.assertEqual(result["remote"]["last_metadata"], {})
        self.assertNotIn("rotated-secret", str(result))

    def test_embedded_mode_does_not_probe_or_pretend_remote_is_used(self):
        with override_settings(CFM_COMPARISON_TRANSPORT="embedded"):
            result = health.comparison_service_status()
        self.assertEqual(self.calls, [])
        self.assertEqual(result["remote"]["state"], "unused")
        self.assertEqual(result["local"]["state"], "embedded")
        self.assertTrue(result["local"]["backend_configured"])
        self.assertEqual(result["local"]["worker_version"], APP_VERSION)

    def test_old_worker_metadata_is_unavailable_not_replaced_by_last_seen(self):
        health.comparison_service_status()
        cache.delete("cfm-comparison-health:" + health.configuration_identity() + self.contract)
        self.transport.side_effect = lambda *args, **kwargs: (200, {
            "protocol": 1, "contract": self.contract, "state": "ready", "backend": "cpu"})
        result = health.comparison_service_status()
        self.assertEqual(result["remote"]["worker_version"], "")
        self.assertEqual(result["remote"]["settings"], {})
        self.assertNotIn("last_metadata", result["remote"])

    def test_concurrent_observation_never_starts_second_probe(self):
        with health._lock:
            self.assertTrue(health.comparison_service_status()["checking"])
        self.assertEqual(self.calls, [])

    def test_untrusted_backend_and_health_categories_are_not_rendered_as_text(self):
        with patch("submissions.services.comparison_client.Endpoint.call", return_value=(200, {
            "protocol": 1, "contract": self.contract, "state": {"bad": "state"},
            "backend": {"bad": "backend"}, "error": "private-remote-prose",
        })):
            result = health.comparison_service_status()
        self.assertEqual(result["remote"]["state"], "not_ready")
        self.assertNotIn("private-remote-prose", str(result))

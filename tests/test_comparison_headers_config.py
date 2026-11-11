"""The deployment header parser has no Django/native dependency or secret output."""
import json
import unittest

from conference_final_manager.comparison_config import parse_remote_headers


class RemoteHeaderConfigTests(unittest.TestCase):
    def parse(self, raw, url="https://worker.example"):
        return parse_remote_headers(raw, url=url)

    def test_blank_and_empty_object_preserve_existing_deployments(self):
        for raw in ("", " \t\n", "{}", " { } "):
            self.assertEqual(self.parse(raw, url=""), {})

    def test_valid_gateway_headers_preserve_case_and_values(self):
        values = {"CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "a$#'\\\" b", "X-Empty": ""}
        self.assertEqual(self.parse(json.dumps(values)), values)

    def test_invalid_json_types_duplicates_and_depth_fail_secret_safe(self):
        cases = [None, {}, '[]', 'null', 'true', '1', '"private-secret"',
                 '{"X-Key":"private-secret",}', '{"X-Key":null}', '{"X-Key":1}',
                 '{"X-Key":true}', '{"X-Key":[]}', '{"X-Key":{}}',
                 '{"X-Key":"private-secret","x-key":"other"}',
                 '{"X-Key":"private-secret","X-Key":"other"}', '[' * 1200]
        for raw in cases:
            with self.subTest(raw_type=type(raw).__name__), self.assertRaises(ValueError) as raised:
                self.parse(raw)
            self.assertNotIn("private-secret", str(raised.exception))

    def test_reserved_headers_cannot_override_auth_transport_or_evidence(self):
        for name in ("Authorization", "HOST", "Content-Length", "content-type", "Accept",
                     "Transfer-Encoding", "Connection", "Proxy-Authorization", "TE", "Trailer",
                     "Upgrade", "Expect", "Content-Encoding", "Keep-Alive", "Proxy-Connection",
                     "Proxy-Authenticate", "x-CfM-Contract", "X-CFM-Before-SHA256", "X-CFM-Retry-Failed"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.parse(json.dumps({name: "private-secret"}))

    def test_invalid_header_name_and_control_or_non_ascii_values_rejected(self):
        for name in ("", "X Key", " X-Key", "X-Key:", "X-Key\r\nInjected", "X-\u00e9", "x" * 129):
            with self.subTest(name_length=len(name)), self.assertRaises(ValueError):
                self.parse(json.dumps({name: "private-secret"}))
        for value in ("a\rb", "a\nb", "a\tb", "a\0b", "a\x7fb", "\u00e9"):
            with self.subTest(value_length=len(value)), self.assertRaises(ValueError):
                self.parse(json.dumps({"X-Key": value}))

    def test_size_count_and_missing_url_bounds(self):
        for raw in (json.dumps({f"X-{i}": "v" for i in range(33)}),
                    json.dumps({"X-Key": "v" * 4097}), " " * 16385):
            with self.assertRaises(ValueError):
                self.parse(raw)
        self.assertEqual(len(self.parse(json.dumps({"X-Key": "v" * 4096}))["X-Key"]), 4096)
        with self.assertRaisesRegex(ValueError, "requires"):
            self.parse('{"X-Key":"private-secret"}', url=" ")

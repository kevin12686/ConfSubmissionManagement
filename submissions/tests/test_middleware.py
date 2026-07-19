import gzip

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from submissions.middleware import GZIP_CONTENT_TYPES, SelectiveGZipMiddleware


class SelectiveGZipMiddlewareTests(SimpleTestCase):
    content = b"compressible response content " * 100

    def setUp(self):
        self.request = RequestFactory().get("/", HTTP_ACCEPT_ENCODING="gzip")

    def response_for(self, content_type):
        middleware = SelectiveGZipMiddleware(
            lambda request: HttpResponse(
                self.content,
                content_type=content_type,
            )
        )
        return middleware(self.request)

    def test_compresses_only_allowlisted_content_types(self):
        for content_type in GZIP_CONTENT_TYPES:
            with self.subTest(content_type=content_type):
                response = self.response_for(f"{content_type}; charset=utf-8")

                self.assertEqual(response["Content-Encoding"], "gzip")
                self.assertEqual(gzip.decompress(response.content), self.content)

    def test_does_not_compress_binary_or_unknown_content_types(self):
        content_types = (
            "application/zip",
            "application/pdf",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "image/jpeg",
            "image/png",
            "image/svg+xml",
        )

        for content_type in content_types:
            with self.subTest(content_type=content_type):
                response = self.response_for(content_type)

                self.assertNotIn("Content-Encoding", response)
                self.assertEqual(response.content, self.content)

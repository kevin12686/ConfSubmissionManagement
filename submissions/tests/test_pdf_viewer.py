import hashlib
import zipfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.core.files.base import ContentFile
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from submissions.forms import FinalSubmissionForm
from submissions.models import FinalSubmission
from submissions.services.checks import publication_readiness_rows
from submissions.services.crosscheck import prepare_crosscheck_upload
from submissions.services.formatting_history import capture_existing_formatting
from submissions.services.pdf_comparison import create_pdf_comparison
from submissions.services.pdf_viewer import PDF_ROUTES, pdf_viewer_source, pdf_viewer_url
from submissions.services.publication_read import publication_database_signature
from submissions.services.reports import export_publication_package
from submissions.tests.test_acceptance import EditorialAcceptanceTestCase
from submissions.tests.test_pdf_comparison import make_pdf


class Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.links = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


class PDFViewerURLTests(SimpleTestCase):
    def test_reader_layout_isolates_pdf_pages_from_the_site_page_layout(self):
        css = (Path(__file__).resolve().parents[1] / "static/submissions/pdf_viewer.css").read_text()
        page_rule = css.split(".cfm-pdf-reader .pdfViewer .page {", 1)[1].split("}", 1)[0]
        self.assertIn("box-sizing: content-box;", page_rule)
        self.assertIn("display: block;", page_rule)
        self.assertIn("min-height: 0;", page_rule)
        self.assertNotIn("width:", page_rule)
        self.assertNotIn("height:", page_rule.replace("min-height: 0;", ""))

    def test_proxy_module_mapping_retains_standard_static_mime_types(self):
        config = (Path(__file__).resolve().parents[2] / "docker/nginx/default.conf.template").read_text()
        static = config.split("location ^~ /static/ {", 1)[1].split("location ^~ /media/ {", 1)[0]
        self.assertIn("include /etc/nginx/mime.types;", static)
        self.assertIn("application/javascript mjs;", static)

    def test_supported_routes_preserve_the_exact_endpoint_not_a_paper_lookup(self):
        for name in PDF_ROUTES:
            kwargs = {"pk": 17}
            if name == "formatting_revision_pdf":
                kwargs["revision_id"] = 29
            elif name == "editor_upload_preview_pdf":
                kwargs = {"token": "a" * 32}
            elif name == "formatting_comparison_pdf":
                kwargs.update(token="a" * 32, side="before")
            elif name in {"crosscheck_service_report", "crosscheck_service_input_pdf"}:
                kwargs = {"pk": "11111111-1111-4111-8111-111111111111", "item_id": 1}
            raw = reverse(f"submissions:{name}", kwargs=kwargs)
            with self.subTest(name=name):
                wrapped = pdf_viewer_url(raw)
                self.assertEqual(parse_qs(urlsplit(wrapped).query)["file"], [raw])
                self.assertEqual(pdf_viewer_source(raw)["url"], raw)

    @override_settings(MEDIA_URL="/media/")
    def test_media_pdf_names_are_preserved_including_spaces_unicode_and_case(self):
        raw = "/media/formatted_pdfs/An%20%26%20%E6%B8%AC%E8%A9%A6.PDF"
        self.assertEqual(pdf_viewer_source(raw)["detail"], "An & 測試.PDF")
        self.assertEqual(parse_qs(urlsplit(pdf_viewer_url(raw)).query)["file"], [raw])
        self.assertEqual(pdf_viewer_url(""), "")

    @override_settings(MEDIA_URL="/conference-files/")
    def test_configured_media_prefix_is_used(self):
        self.assertEqual(pdf_viewer_source("/conference-files/original.pdf")["url"], "/conference-files/original.pdf")
        with self.assertRaises(ValueError):
            pdf_viewer_source("/media/original.pdf")

    def test_external_recursive_non_pdf_and_unsafe_paths_are_rejected(self):
        invalid = [
            "", None, "https://example.org/paper.pdf", "//example.org/paper.pdf",
            "http://testserver/submissions/1/publication-pdf/", "javascript:alert(1)",
            "data:application/pdf;base64,AAA", "file:///tmp/paper.pdf",
            "/pdf-viewer/?file=/media/paper.pdf", "/settings/", "/reports/", "/media/source.zip",
            "/media/../paper.pdf", "/media/%2e%2e/paper.pdf", "/media/%5c..%5cpaper.pdf",
            "/media/paper.pdf?redirect=https://example.org", "/media/paper.pdf#page=2",
            "/media/paper%00.pdf", "/media/paper.pdf\n", "x" * 8193,
            "/reviews/formatting/1/compare/token/wrong/pdf/",
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pdf_viewer_source(raw)

    def test_templates_escape_sources_and_do_not_require_javascript_link_rewriting(self):
        raw = reverse("submissions:publication_pdf", args=[17])
        html = Template('{% load pdf_viewer %}<a href="{{ url|pdf_viewer_url }}">PDF</a>').render(Context({"url": raw}))
        self.assertIn(pdf_viewer_url(raw), html)
        tagged = Template("{% load pdf_viewer %}{% pdf_viewer 'submissions:publication_pdf' 17 %}").render(Context())
        self.assertEqual(unescape(tagged), pdf_viewer_url(raw))


class PDFViewerTests(EditorialAcceptanceTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(BASE_DIR=self.root.resolve()))
        self.make_master_paper()
        self.submission = self.make_final_submission()
        self.original = make_pdf(["Original body"])
        self.corrected = make_pdf(["Corrected body", "Second page"])
        self.submission.pdf_file.save("original.pdf", ContentFile(self.original))
        self.submission.formatted_pdf_file.save("corrected.pdf", ContentFile(self.corrected))
        self.pdf_url = reverse("submissions:publication_pdf", args=[self.submission.pk])

    def get_reader(self, raw):
        return self.client.get(pdf_viewer_url(raw))

    def read_pdf(self, raw):
        response = self.client.get(raw)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        content = b"".join(response.streaming_content)
        response.close()
        return content

    def test_reader_only_renders_a_source_link_and_cannot_change_any_workflow_state(self):
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        signature = publication_database_signature()
        readiness = publication_readiness_rows(strict_hash=True)
        response = self.get_reader(self.pdf_url)
        self.assertContains(response, "PDF controls")
        self.assertContains(response, "Find in PDF")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.context["pdf_source"]["url"], self.pdf_url)
        links = Links(response.content.decode()).links
        raw_links = [link for link in links if link.get("href") == self.pdf_url]
        self.assertEqual(len(raw_links), 2)
        self.assertTrue(any("download" in link for link in raw_links))
        self.assertEqual(self.read_pdf(self.pdf_url), self.corrected)
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)
        self.assertEqual(publication_database_signature(), signature)
        self.assertEqual(publication_readiness_rows(strict_hash=True), readiness)

    def test_reader_get_does_not_fetch_hash_render_or_proxy_the_pdf(self):
        from unittest.mock import patch
        with patch("submissions.services.file_manager.publication_pdf_info", side_effect=AssertionError("Viewer must not select files")):
            self.assertEqual(self.get_reader(self.pdf_url).status_code, 200)
        with self.assertNumQueries(0):
            self.assertEqual(pdf_viewer_source(self.pdf_url)["url"], self.pdf_url)
            self.assertTrue(pdf_viewer_url(self.pdf_url))

    def test_full_window_shell_is_shared_by_every_pdf_source_kind(self):
        for name in PDF_ROUTES:
            kwargs = {"pk": self.submission.pk}
            if name == "formatting_revision_pdf":
                kwargs["revision_id"] = 29
            elif name == "editor_upload_preview_pdf":
                kwargs = {"token": "a" * 32}
            elif name == "formatting_comparison_pdf":
                kwargs.update(token="a" * 32, side="before")
            elif name in {"crosscheck_service_report", "crosscheck_service_input_pdf"}:
                kwargs = {"pk": "11111111-1111-4111-8111-111111111111", "item_id": 1}
            raw = reverse(f"submissions:{name}", kwargs=kwargs)
            with self.subTest(source=name):
                response = self.get_reader(raw)
                self.assertContains(response, '<body class="cfm-pdf-reader-page">')
                self.assertContains(response, '<main class="cfm-pdf-reader-main">')
                self.assertContains(response, 'aria-label="PDF controls"', count=1)
                self.assertContains(response, 'aria-label="PDF pages"', count=1)
                self.assertNotContains(response, 'aria-label="Primary navigation"')
                self.assertNotContains(response, "<footer")
                self.assertNotContains(response, 'id="global-workflow-alerts"')
                self.assertEqual(response.context["pdf_source"]["url"], raw)
                raw_links = [link for link in Links(response.content.decode()).links if link.get("href") == raw]
                self.assertEqual(len(raw_links), 2)

    def test_work_pages_keep_the_normal_site_chrome(self):
        for name in ("initial_paper_list", "organized_list", "formatting", "export_reports"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"submissions:{name}"))
                self.assertContains(response, '<main class="container-fluid py-4">')
                self.assertContains(response, 'aria-label="Primary navigation"')
                self.assertContains(response, "<footer")
                self.assertContains(response, 'id="global-workflow-alerts"')
                self.assertNotContains(response, '<body class="cfm-pdf-reader-page">')

    def test_reader_defaults_to_fit_page_and_native_single_page_layout(self):
        response = self.get_reader(self.pdf_url)
        self.assertContains(response, '<option value="page-fit" selected>Fit page</option>')
        self.assertContains(response, 'aria-label="Page layout"', count=1)
        self.assertContains(response, 'data-pdf-layout="single" aria-label="Single page"')
        self.assertContains(response, 'data-pdf-layout="continuous" aria-label="Continuous scrolling"')
        script = (Path(__file__).resolve().parents[1] / "static/submissions/pdf_viewer.js").read_text()
        self.assertIn("viewer.scrollMode = ScrollMode.PAGE;", script)
        self.assertIn('viewer.currentScaleValue = "page-fit";', script)
        self.assertIn("single: ScrollMode.PAGE, continuous: ScrollMode.VERTICAL", script)
        self.assertIn('eventBus.on("scrollmodechanged", syncLayout)', script)
        self.assertIn("number !== viewer.currentPageNumber", script)
        styles = (Path(__file__).resolve().parents[1] / "static/submissions/pdf_viewer.css").read_text()
        self.assertIn("mask-mode: alpha;", styles)
        self.assertIn(".cfm-pdf-icon-single-page { mask-image:", styles)
        self.assertIn(".cfm-pdf-icon-continuous { mask-image:", styles)

    def test_keyboard_and_wheel_share_a_reader_scoped_module_without_a_different_source(self):
        response = self.get_reader(self.pdf_url)
        self.assertContains(response, 'pdf_viewer.js?v=')
        self.assertContains(response, 'type="module"')
        script = (Path(__file__).resolve().parents[1] / "static/submissions/pdf_viewer.js").read_text()
        self.assertIn('new URL("./pdf_navigation.mjs", import.meta.url)', script)
        self.assertIn("await import(navigationModule.href)", script)
        self.assertIn("installPDFNavigation({container, viewer, singlePageMode: ScrollMode.PAGE})", script)
        self.assertIn("navigation.destroy()", script)
        self.assertIn("if (document.activeElement === document.body) container.focus({preventScroll: true});", script)
        self.assertEqual(response.context["pdf_source"]["url"], self.pdf_url)

    def test_invalid_source_is_400_and_post_is_not_supported(self):
        url = reverse("submissions:pdf_viewer")
        for raw in ("https://example.org/paper.pdf", "/settings/", "/media/source.zip", ""):
            self.assertEqual(self.client.get(url, {"file": raw}).status_code, 400)
        self.assertEqual(self.client.post(url, {"file": self.pdf_url}).status_code, 405)

    def test_thumbnail_sidebar_uses_the_same_loaded_document_not_another_file_endpoint(self):
        response = self.get_reader(self.pdf_url)
        self.assertContains(response, 'data-pdf-sidebar-toggle')
        self.assertContains(response, 'aria-controls="pdf-page-sidebar"')
        self.assertContains(response, 'data-pdf-thumbnails aria-label="Choose PDF page"')
        script = (Path(__file__).resolve().parents[1] / "static/submissions/pdf_viewer.js").read_text()
        self.assertIn("thumbnails.setDocument(pdfDocument)", script)
        self.assertIn("thumbnails.setCurrentPage(viewer.currentPageNumber)", script)
        self.assertIn('eventBus.on("rotationchanging", event => thumbnails.setRotation(event.pagesRotation))', script)
        self.assertIn("thumbnails.destroy()", script)
        module = (Path(__file__).resolve().parents[1] / "static/submissions/pdf_thumbnails.mjs").read_text()
        self.assertNotIn("getDocument(", module)
        self.assertNotIn("fetch(", module)
        self.assertNotIn(".cleanup(", module)
        self.assertEqual(response.context["pdf_source"]["url"], self.pdf_url)

    def test_missing_selected_corrected_does_not_fall_back_to_original(self):
        Path(self.submission.formatted_pdf_file.path).unlink()
        self.assertEqual(self.get_reader(self.pdf_url).status_code, 200)
        self.assertEqual(self.client.get(self.pdf_url).status_code, 404)
        self.assertTrue(Path(self.submission.pdf_file.path).exists())

    def test_inactive_row_and_immutable_revision_keep_their_own_files(self):
        revision = capture_existing_formatting(self.submission.pk)
        revision_url = reverse("submissions:formatting_revision_pdf", args=[self.submission.pk, revision.pk])
        newer = self.make_final_submission(final_submission_id="newer", active_version=True)
        self.submission.active_version = False
        self.submission.discarded = True
        self.submission.save()
        raw = reverse("submissions:final_submission_display_pdf", args=[self.submission.pk])
        self.assertEqual(self.get_reader(raw).context["pdf_source"]["url"], raw)
        self.assertEqual(self.read_pdf(raw), self.corrected)
        self.assertEqual(self.read_pdf(revision_url), self.corrected)
        Path(revision.pdf_file.path).unlink()
        self.assertEqual(self.client.get(revision_url).status_code, 404)
        self.assertNotEqual(newer.pk, self.submission.pk)

    def test_comparison_snapshots_keep_the_exact_side_and_reject_changed_current_files(self):
        payload = create_pdf_comparison(self.submission.pk)
        for side, expected in (("before", self.original), ("after", self.corrected)):
            raw = reverse("submissions:formatting_comparison_pdf", args=[self.submission.pk, payload["token"], side])
            self.assertEqual(self.get_reader(raw).context["pdf_source"]["url"], raw)
            self.assertEqual(self.read_pdf(raw), expected)
        Path(self.submission.formatted_pdf_file.path).write_bytes(self.original)
        self.assertEqual(self.client.get(raw).status_code, 409)

    def test_review_list_modes_and_lazy_history_use_viewer_links(self):
        raw_display = reverse("submissions:final_submission_display_pdf", args=[self.submission.pk])
        cases = [
            ("submissions:organized_list", {}, self.pdf_url),
            ("submissions:active_versions", {}, self.pdf_url),
            ("submissions:final_submission_list", {}, raw_display),
            ("submissions:verify_paper_ids", {"filter": "all"}, self.pdf_url),
            ("submissions:title_author_extraction", {"filter": "all"}, self.pdf_url),
            ("submissions:formatting", {"filter": "all"}, self.pdf_url),
            ("submissions:formatting", {"mode": "single", "filter": "all", "current": self.submission.pk}, self.pdf_url),
            ("submissions:formatting", {"mode": "focus", "submission": self.submission.pk}, self.pdf_url),
        ]
        for route, params, raw in cases:
            with self.subTest(route=route, params=params):
                response = self.client.get(reverse(route), params, follow=True)
                self.assertContains(response, pdf_viewer_url(raw))
        revision = capture_existing_formatting(self.submission.pk)
        history = self.client.get(reverse("submissions:formatting_history", args=[self.submission.pk]), HTTP_HX_REQUEST="true")
        raw = reverse("submissions:formatting_revision_pdf", args=[self.submission.pk, revision.pk])
        self.assertContains(history, pdf_viewer_url(raw))
        self.assertContains(history, reverse("submissions:formatting_revision_source", args=[self.submission.pk, revision.pk]))

    def test_original_file_widget_preserves_upload_clear_controls_and_uses_reader(self):
        html = str(FinalSubmissionForm(instance=self.submission)["pdf_file"])
        self.assertIn(pdf_viewer_url(self.submission.pdf_file.url), html)
        self.assertIn('type="file"', html)
        self.assertIn('name="pdf_file-clear"', html)

    def test_viewing_leaves_publication_and_crosscheck_zip_bytes_identical(self):
        self.mark_submission_publication_ready(self.submission)
        before = FinalSubmission.objects.values().get(pk=self.submission.pk)
        self.get_reader(self.pdf_url)
        self.assertEqual(self.read_pdf(self.pdf_url), self.corrected)
        with zipfile.ZipFile(export_publication_package()) as archive:
            pdf = next(name for name in archive.namelist() if name.startswith("PDF/"))
            self.assertEqual(archive.read(pdf), self.corrected)
        with zipfile.ZipFile(prepare_crosscheck_upload("VIEWER")["zip_path"]) as archive:
            self.assertEqual(hashlib.sha256(archive.read("P001_VIEWER.pdf")).digest(), hashlib.sha256(self.corrected).digest())
        self.assertEqual(FinalSubmission.objects.values().get(pk=self.submission.pk), before)

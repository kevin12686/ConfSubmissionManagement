"""No network, native OCR libraries, Django or real models are used here."""

from contextlib import redirect_stdout
import hashlib
import http.client
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from scripts import deployment_provision as provision


class Response(io.BytesIO):
    def __init__(self, body, *, length=None, status=200, url="https://models.example/model"):
        super().__init__(body)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.status = status
        self.url = url
        self.request_sizes = []

    def geturl(self):
        return self.url

    def read1(self, size):
        self.request_sizes.append(size)
        return super().read1(size)


class ModelProvisionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.directory = self.root / "models"
        self.payloads = {name: ("test model " + name).encode() for name in provision.MODEL_URLS}
        self.hashes = {name: hashlib.sha256(body).hexdigest() for name, body in self.payloads.items()}
        self.names = list(self.payloads)
        self.enterContext(patch.object(provision, "_model_hashes", return_value=self.hashes))
        self.opener = self.enterContext(patch.object(provision.urllib.request, "build_opener")).return_value
        self.opener.open.side_effect = self.response

    def response(self, url, *, timeout):
        self.assertEqual(timeout, provision.SOCKET_TIMEOUT)
        self.assertIn(url, provision.MODEL_URLS.values())
        name = next(name for name, source in provision.MODEL_URLS.items() if source == url)
        return Response(self.payloads[name], length=len(self.payloads[name]))

    def existing(self, name=None, body=None):
        name = name or self.names[0]
        self.directory.mkdir(exist_ok=True)
        path = self.directory / name
        path.write_bytes(self.payloads[name] if body is None else body)
        return path

    def assert_clean(self, expected=()):
        self.assertEqual(set(p.name for p in self.directory.iterdir()), set(expected))

    def test_missing_models_are_verified_and_published(self):
        result = provision.provision_models(self.directory)
        self.assertEqual(result, {name: self.directory / name for name in self.names})
        for name, path in result.items():
            self.assertEqual(path.read_bytes(), self.payloads[name])
        self.assertEqual(self.opener.open.call_count, 2)
        self.assert_clean(self.names)

    def test_existing_valid_models_work_offline_without_lock_or_network(self):
        for name in self.names:
            self.existing(name)
        with patch.object(provision, "_lock", side_effect=AssertionError("must not lock")):
            result = provision.provision_models(self.directory, offline=True)
        self.assertEqual(set(result), set(self.names))
        self.opener.open.assert_not_called()
        self.assert_clean(self.names)

    def test_only_missing_model_downloaded_existing_inode_preserved(self):
        existing = self.existing()
        before = existing.stat()
        provision.provision_models(self.directory)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual((existing.stat().st_ino, existing.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_dry_run_has_no_network_or_filesystem_writes(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = provision.provision_models(self.directory, dry_run=True)
        self.assertFalse(self.directory.exists())
        self.assertEqual(set(result), set(self.names))
        self.assertIn("Would download and verify", output.getvalue())
        self.opener.open.assert_not_called()

    def test_offline_missing_reports_sources_and_hashes_without_writes(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(provision.ProvisionError, "Offline models missing") as raised:
                provision.provision_models(self.directory, offline=True, dry_run=dry_run)
            self.assertIn(self.hashes[self.names[0]], str(raised.exception))
            self.assertIn(provision.MODEL_URLS[self.names[0]], str(raised.exception))
        self.assertFalse(self.directory.exists())
        self.opener.open.assert_not_called()

    def test_corrupt_existing_file_preserved_in_all_modes_before_network(self):
        path = self.existing(body=b"do not overwrite")
        for options in ({}, {"offline": True}, {"dry_run": True}):
            with self.subTest(options=options), self.assertRaisesRegex(provision.ProvisionError, "Checksum mismatch"):
                provision.provision_models(self.directory, **options)
            self.assertEqual(path.read_bytes(), b"do not overwrite")
        self.opener.open.assert_not_called()
        self.assert_clean([path.name])

    def test_empty_oversized_and_nonregular_existing_models_rejected(self):
        path = self.existing(body=b"")
        with self.assertRaisesRegex(provision.ProvisionError, "Invalid existing model"):
            provision.provision_models(self.directory)
        path.write_bytes(b"large")
        with patch.object(provision, "MAX_BYTES", 4), self.assertRaisesRegex(provision.ProvisionError, "Invalid existing model"):
            provision.provision_models(self.directory)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(provision.ProvisionError, "Invalid existing model"):
            provision.provision_models(self.directory)
        self.opener.open.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Symlink fixtures require POSIX permissions")
    def test_model_symlink_and_dangling_symlink_preserved(self):
        self.directory.mkdir()
        target = self.root / "source"
        target.write_bytes(self.payloads[self.names[0]])
        model = self.directory / self.names[0]
        for destination in (target, self.root / "absent"):
            model.symlink_to(destination)
            with self.assertRaisesRegex(provision.ProvisionError, "symlink"):
                provision.provision_models(self.directory)
            self.assertTrue(model.is_symlink())
            model.unlink()
        self.assertEqual(target.read_bytes(), self.payloads[self.names[0]])
        self.opener.open.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Symlink fixtures require POSIX permissions")
    def test_directory_and_parent_symlinks_rejected_without_writes(self):
        target = self.root / "outside"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        for path in (self.directory, self.directory / "nested"):
            with self.subTest(path=path), self.assertRaisesRegex(provision.ProvisionError, "symlink"):
                provision.provision_models(path)
        self.assertEqual(list(target.iterdir()), [])
        self.opener.open.assert_not_called()

    def test_parent_file_and_dotdot_rejected(self):
        self.directory.write_text("preserve")
        with self.assertRaisesRegex(provision.ProvisionError, "not a directory"):
            provision.provision_models(self.directory / "nested")
        with self.assertRaisesRegex(provision.ProvisionError, "must not contain"):
            provision.provision_models(self.root / "unused" / ".." / "models")
        self.assertEqual(self.directory.read_text(), "preserve")
        self.opener.open.assert_not_called()

    def test_wrong_checksum_cleans_stage_and_installs_nothing(self):
        self.opener.open.return_value = Response(b"untrusted")
        self.opener.open.side_effect = None
        with self.assertRaisesRegex(provision.ProvisionError, "pinned SHA256"):
            provision.provision_models(self.directory)
        self.assert_clean()

    def test_truncated_body_rejected_and_cleaned(self):
        self.opener.open.side_effect = lambda *a, **k: Response(self.payloads[self.names[0]], length=1000)
        with self.assertRaisesRegex(provision.ProvisionError, "Truncated"):
            provision.provision_models(self.directory)
        self.assert_clean()

    def test_http_exception_timeout_and_second_download_failure_leave_no_models(self):
        for failure in (http.client.IncompleteRead(b"partial", 100), TimeoutError("timeout"), OSError("offline")):
            with self.subTest(failure=type(failure).__name__):
                self.opener.open.side_effect = [Response(self.payloads[self.names[0]]), failure]
                with self.assertRaisesRegex(provision.ProvisionError, "Retry online or transfer"):
                    provision.provision_models(self.directory)
                self.assert_clean()

    def test_failure_preserves_existing_valid_model_and_retry_recovers(self):
        self.existing()
        self.opener.open.side_effect = TimeoutError("timeout")
        with self.assertRaises(provision.ProvisionError):
            provision.provision_models(self.directory)
        self.assert_clean([self.names[0]])
        self.opener.open.side_effect = self.response
        provision.provision_models(self.directory)
        self.assert_clean(self.names)

    def test_stream_exception_cleans_partial_download(self):
        response = Response(b"unused")
        response.read1 = unittest.mock.Mock(side_effect=[b"partial", http.client.IncompleteRead(b"x", 20)])
        self.opener.open.side_effect = lambda *a, **k: response
        with self.assertRaisesRegex(provision.ProvisionError, "Cannot provision"):
            provision.provision_models(self.directory)
        self.assert_clean()

    def test_no_model_published_until_both_downloads_validate(self):
        original_response = self.response
        def inspect_stage(url, *, timeout):
            self.assertFalse(any((self.directory / name).exists() for name in self.names))
            return original_response(url, timeout=timeout)
        self.opener.open.side_effect = inspect_stage
        provision.provision_models(self.directory)
        self.assert_clean(self.names)

    def test_invalid_content_lengths_rejected_before_body_read(self):
        for length in ("-1", "invalid", "0", str(provision.MAX_BYTES + 1)):
            response = Response(b"untrusted", length=length)
            self.opener.open.side_effect = lambda *a, **k: response
            with self.subTest(length=length), self.assertRaisesRegex(provision.ProvisionError, "Content-Length"):
                provision.provision_models(self.directory)
            self.assertEqual(response.request_sizes, [])
            self.assert_clean()

    def test_stream_size_limit_without_content_length(self):
        response = Response(b"x" * 32)
        self.opener.open.side_effect = lambda *a, **k: response
        with patch.object(provision, "MAX_BYTES", 16), patch.object(provision, "CHUNK_BYTES", 8):
            with self.assertRaisesRegex(provision.ProvisionError, "exceeds 100 MiB"):
                provision.provision_models(self.directory)
        self.assertEqual(response.request_sizes, [8, 8, 1])
        self.assert_clean()

    def test_download_wall_deadline_stops_streaming(self):
        self.opener.open.side_effect = lambda *a, **k: Response(self.payloads[self.names[0]])
        with patch.object(provision.time, "monotonic", side_effect=[0, 1, 121]):
            with self.assertRaisesRegex(provision.ProvisionError, "deadline"):
                provision.provision_models(self.directory)
        self.assert_clean()

    def test_http_status_and_non_https_response_rejected(self):
        for response in (Response(b"", status=206), Response(b"", url="http://unsafe/model")):
            self.opener.open.side_effect = lambda *a, **k: response
            with self.assertRaisesRegex(provision.ProvisionError, "successful HTTPS"):
                provision.provision_models(self.directory)
            self.assert_clean()

    def test_downgrade_redirect_rejected_before_request(self):
        handler = provision._HTTPSRedirects()
        request = urllib.request.Request(provision.MODEL_URLS[self.names[0]])
        with self.assertRaisesRegex(provision.ProvisionError, "non-HTTPS redirect"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://unsafe/model")

    def test_preexisting_lock_never_stolen_or_changed(self):
        self.directory.mkdir()
        lock = self.directory / provision.LOCK_NAME
        lock.write_text("previous owner")
        os.utime(lock, (0, 0))
        with self.assertRaisesRegex(provision.ProvisionError, "lock exists"):
            provision.provision_models(self.directory)
        self.assertEqual(lock.read_text(), "previous owner")
        self.opener.open.assert_not_called()

    def test_concurrent_provisioner_fails_fast_without_network(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def blocking_response(url, *, timeout):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            return self.response(url, timeout=timeout)

        def first():
            try:
                provision.provision_models(self.directory)
            except Exception as exc:
                errors.append(exc)

        self.opener.open.side_effect = blocking_response
        worker = threading.Thread(target=first)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(provision.ProvisionError, "lock exists"):
                provision.provision_models(self.directory)
            self.assertEqual(self.opener.open.call_count, 1)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assert_clean(self.names)

    def test_file_appearing_at_publication_is_never_overwritten(self):
        original_link = os.link
        def competing_link(source, destination):
            destination.write_bytes(b"concurrent owner")
            original_link(source, destination)
        with patch.object(provision.os, "link", side_effect=competing_link):
            with self.assertRaisesRegex(provision.ProvisionError, "Existing models are not replaced"):
                provision.provision_models(self.directory)
        self.assertEqual((self.directory / self.names[0]).read_bytes(), b"concurrent owner")
        self.assert_clean([self.names[0]])

    def test_publication_failure_cleans_private_stage(self):
        with patch.object(provision.os, "link", side_effect=OSError("unsupported filesystem")):
            with self.assertRaisesRegex(provision.ProvisionError, "hard-link support"):
                provision.provision_models(self.directory)
        self.assert_clean()

    def test_partial_publication_keeps_verified_file_and_retry_only_fills_missing(self):
        original_link = os.link
        def fail_second(source, destination):
            if destination.name == self.names[1]:
                raise OSError("disk full")
            original_link(source, destination)
        with patch.object(provision.os, "link", side_effect=fail_second):
            with self.assertRaises(provision.ProvisionError):
                provision.provision_models(self.directory)
        self.assert_clean([self.names[0]])
        self.assertEqual((self.directory / self.names[0]).read_bytes(), self.payloads[self.names[0]])
        self.opener.open.reset_mock()
        provision.provision_models(self.directory)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assert_clean(self.names)


class PolicyTests(unittest.TestCase):
    def test_real_policy_read_without_native_imports(self):
        result = subprocess.run([sys.executable, "-S", "-c",
            "from scripts.deployment_provision import _model_hashes; import sys; "
            "assert len(_model_hashes()) == 2; "
            "assert not any(x in sys.modules for x in ('fitz', 'django', 'onnxruntime', 'rapidocr'))"],
            cwd=provision.RUNTIME_SOURCE.parents[3], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ast_policy_is_literal_and_matches_fixed_upstream_filenames(self):
        for source in ("MODEL_HASHES = make_policy()", "MODEL_HASHES = {}", "x = 1",
                       "MODEL_HASHES = {}; MODEL_HASHES = {}", "not python !"):
            with patch.object(Path, "read_text", return_value=source):
                with self.subTest(source=source), self.assertRaisesRegex(provision.ProvisionError, "MODEL_HASHES"):
                    provision._model_hashes()


if __name__ == "__main__":
    unittest.main()

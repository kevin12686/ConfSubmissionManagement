import hashlib
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.utils import timezone

from submissions.services.preview_storage import (
    purge_expired_preview_directories,
    save_preview_upload,
)


class PreviewStorageTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_upload_hash_is_calculated_while_streaming_to_disk(self):
        token_root = self.root / ("a" * 32)
        token_root.mkdir()
        content = b"publication preview bytes" * 100

        info = save_preview_upload(
            SimpleUploadedFile("paper.pdf", content),
            token_root,
            "preview",
        )

        self.assertEqual(info["size"], len(content))
        self.assertEqual(info["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(Path(info["path"]).read_bytes(), content)

    def test_expired_preview_directories_are_purged_but_live_tokens_remain(self):
        expired = self.root / ("a" * 32)
        live = self.root / ("b" * 32)
        incomplete = self.root / ("c" * 32)
        ignored = self.root / "operator-folder"
        for path in (expired, live, incomplete, ignored):
            path.mkdir()
        (expired / "payload.json").write_text(
            json.dumps(
                {"created_at": (timezone.now() - timedelta(hours=3)).isoformat()}
            ),
            encoding="utf-8",
        )
        (live / "payload.json").write_text(
            json.dumps({"created_at": timezone.now().isoformat()}),
            encoding="utf-8",
        )
        old_timestamp = (timezone.now() - timedelta(days=2)).timestamp()
        os.utime(incomplete, (old_timestamp, old_timestamp))

        purge_expired_preview_directories(
            self.root,
            timedelta(hours=2),
        )

        self.assertFalse(expired.exists())
        self.assertTrue(live.exists())
        self.assertTrue(incomplete.exists())
        self.assertTrue(ignored.exists())

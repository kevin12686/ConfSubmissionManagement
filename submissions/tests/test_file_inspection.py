import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from submissions.services import file_inspection
from submissions.services.file_inspection import (
    FileChangedDuringInspection,
    FileInspectionContext,
    clear_file_hash_cache,
)


class FileInspectionContextTests(SimpleTestCase):
    def setUp(self):
        clear_file_hash_cache()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "paper.pdf"
        self.path.write_bytes(b"first")

    def test_status_is_read_once_per_context(self):
        context = FileInspectionContext()

        with mock.patch.object(
            file_inspection,
            "_read_status",
            wraps=file_inspection._read_status,
        ) as read_status:
            self.assertTrue(context.exists(self.path))
            self.assertTrue(context.exists(self.path))

        self.assertEqual(read_status.call_count, 1)

    def test_hash_is_reused_across_contexts_for_same_stat_signature(self):
        with mock.patch.object(
            file_inspection,
            "_read_sha256",
            wraps=file_inspection._read_sha256,
        ) as read_sha256:
            first = FileInspectionContext().sha256(self.path)
            second = FileInspectionContext().sha256(self.path)

        self.assertEqual(first, second)
        self.assertEqual(read_sha256.call_count, 1)

    def test_changed_file_gets_a_new_hash(self):
        first = FileInspectionContext().sha256(self.path)
        self.path.write_bytes(b"second-content")

        second = FileInspectionContext().sha256(self.path)

        self.assertNotEqual(first, second)

    def test_fresh_hash_bypasses_cross_request_cache(self):
        with mock.patch.object(
            file_inspection,
            "_read_sha256",
            wraps=file_inspection._read_sha256,
        ) as read_sha256:
            FileInspectionContext().sha256(self.path, fresh=True)
            FileInspectionContext().sha256(self.path, fresh=True)

        self.assertEqual(read_sha256.call_count, 2)

    def test_fresh_hash_detects_change_inside_the_same_context(self):
        context = FileInspectionContext()
        first = context.sha256(self.path)
        self.path.write_bytes(b"changed within context")

        second = context.sha256(self.path, fresh=True)

        self.assertNotEqual(first, second)

    def test_snapshot_bytes_match_the_inspected_file(self):
        context = FileInspectionContext()
        context.status(self.path)

        self.assertEqual(context.read_snapshot_bytes(self.path), b"first")

    def test_snapshot_bytes_reject_a_file_changed_after_inspection(self):
        context = FileInspectionContext()
        context.status(self.path)
        self.path.write_bytes(b"changed after inspection")

        with self.assertRaises(FileChangedDuringInspection):
            context.read_snapshot_bytes(self.path)

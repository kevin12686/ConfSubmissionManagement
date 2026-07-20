import json
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from submissions.models import AppSetting, InitialPaper
from submissions.services.system_state import (
    CONFIRMATION_TEXT,
    SystemStateError,
    _model_payload_sha256,
    apply_system_state_restore,
    export_system_state,
    preview_system_state_restore,
)


class SystemStateRestoreAtomicityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.media_root = self.root / "data" / "media"
        self.backup_root = self.root / "data" / "system_state_backups"
        self.preview_root = self.root / "data" / "system_state_restore_previews"
        self.audit_root = self.root / "data" / "logs"
        for path in (
            self.media_root,
            self.backup_root,
            self.preview_root,
            self.audit_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

        settings_override = override_settings(
            BASE_DIR=self.root,
            MEDIA_ROOT=self.media_root,
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        patchers = [
            patch(
                "submissions.services.system_state.system_state_reports_root",
                lambda: self.backup_root,
            ),
            patch(
                "submissions.services.system_state.restore_preview_root",
                lambda: self.preview_root,
            ),
            patch(
                "submissions.services.system_state.audit_log_root",
                lambda: self.audit_root,
            ),
            patch(
                "submissions.services.audit.audit_log_root",
                lambda: self.audit_root,
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        settings_obj = AppSetting.load()
        settings_obj.incoming_folder = "data/incoming"
        settings_obj.active_final_folder = "data/active_final"
        settings_obj.old_versions_folder = "data/old_versions"
        settings_obj.publication_pdf_debug_folder = "data/publication_pdf_debug"
        settings_obj.reports_folder = "data/reports"
        settings_obj.extraction_results_folder = "data/extraction_results"
        settings_obj.plagiarism_reports_folder = "data/plagiarism_reports"
        settings_obj.save()

    def _prepare_restore(self):
        InitialPaper.objects.create(
            paper_id="ARCHIVED",
            title="Archived state",
            authors="Archive Author",
        )
        managed_file = self.media_root / "final_submissions" / "paper.pdf"
        managed_file.parent.mkdir(parents=True, exist_ok=True)
        managed_file.write_bytes(b"archived publication bytes")
        snapshot = export_system_state()

        InitialPaper.objects.all().delete()
        InitialPaper.objects.create(
            paper_id="CURRENT",
            title="Current state",
            authors="Current Author",
        )
        managed_file.write_bytes(b"current publication bytes")
        current_only = self.media_root / "current-only.txt"
        current_only.write_bytes(b"must survive a failed restore")

        upload = SimpleUploadedFile(
            "snapshot.zip",
            snapshot["path"].read_bytes(),
            content_type="application/zip",
        )
        preview = preview_system_state_restore(upload)
        return preview["token"], managed_file, current_only

    def _assert_current_state_preserved(self, managed_file, current_only):
        self.assertEqual(
            list(InitialPaper.objects.values_list("paper_id", flat=True)),
            ["CURRENT"],
        )
        self.assertEqual(managed_file.read_bytes(), b"current publication bytes")
        self.assertEqual(
            current_only.read_bytes(),
            b"must survive a failed restore",
        )
        self.assertEqual(list(self.root.rglob(".cfm-restore-staging-*")), [])
        self.assertEqual(list(self.root.rglob(".cfm-restore-backup-*")), [])

    def _snapshot_upload(self, path):
        return SimpleUploadedFile(
            "snapshot.zip",
            Path(path).read_bytes(),
            content_type="application/zip",
        )

    def _rewrite_snapshot(self, path, mutate):
        with zipfile.ZipFile(path) as archive:
            files = {
                name: archive.read(name)
                for name in archive.namelist()
            }
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
        state = json.loads(files["state.json"].decode("utf-8"))
        mutate(manifest, state)
        target = self.root / f"rewritten-{len(list(self.root.glob('rewritten-*')))}.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, contents in files.items():
                if name == "manifest.json":
                    contents = json.dumps(manifest).encode("utf-8")
                elif name == "state.json":
                    contents = json.dumps(state).encode("utf-8")
                archive.writestr(name, contents)
        return target

    def test_success_promotes_staged_files_and_database_together(self):
        token, managed_file, current_only = self._prepare_restore()

        result = apply_system_state_restore(token, CONFIRMATION_TEXT)

        self.assertEqual(
            list(InitialPaper.objects.values_list("paper_id", flat=True)),
            ["ARCHIVED"],
        )
        self.assertEqual(managed_file.read_bytes(), b"archived publication bytes")
        self.assertFalse(current_only.exists())
        self.assertTrue(Path(result["pre_restore_backup"]).exists())
        self.assertEqual(result["retained_recovery_paths"], [])
        self.assertFalse((self.preview_root / token).exists())
        self.assertEqual(list(self.root.rglob(".cfm-restore-staging-*")), [])
        self.assertEqual(list(self.root.rglob(".cfm-restore-backup-*")), [])

    def test_model_restore_failure_leaves_live_files_and_database_untouched(self):
        token, managed_file, current_only = self._prepare_restore()

        with patch(
            "submissions.services.system_state._restore_models",
            side_effect=RuntimeError("injected model restore failure"),
        ):
            with self.assertRaisesMessage(RuntimeError, "injected model restore failure"):
                apply_system_state_restore(token, CONFIRMATION_TEXT)

        self._assert_current_state_preserved(managed_file, current_only)
        self.assertTrue((self.preview_root / token / "snapshot.zip").exists())

    def test_partial_file_promotion_failure_restores_quarantined_live_files(self):
        token, managed_file, current_only = self._prepare_restore()
        from submissions.services import system_state

        original_rename = system_state._rename_restore_path
        injected = False

        def fail_first_promotion(source, target):
            nonlocal injected
            if ".cfm-restore-staging-" in source.parent.name and not injected:
                injected = True
                raise OSError("injected promotion failure")
            return original_rename(source, target)

        with patch(
            "submissions.services.system_state._rename_restore_path",
            side_effect=fail_first_promotion,
        ):
            with self.assertRaisesMessage(OSError, "injected promotion failure"):
                apply_system_state_restore(token, CONFIRMATION_TEXT)

        self.assertTrue(injected)
        self._assert_current_state_preserved(managed_file, current_only)

    def test_transaction_failure_after_file_promotion_rolls_back_both_sides(self):
        token, managed_file, current_only = self._prepare_restore()

        @contextmanager
        def fail_when_transaction_body_completes():
            with transaction.atomic():
                yield
                raise RuntimeError("injected transaction completion failure")

        with patch(
            "submissions.services.system_state._restore_atomic",
            side_effect=fail_when_transaction_body_completes,
        ):
            with self.assertRaisesMessage(
                RuntimeError,
                "injected transaction completion failure",
            ):
                apply_system_state_restore(token, CONFIRMATION_TEXT)

        self._assert_current_state_preserved(managed_file, current_only)

    def test_restore_never_clears_current_external_configured_folder(self):
        external_temp = tempfile.TemporaryDirectory()
        self.addCleanup(external_temp.cleanup)
        external_root = Path(external_temp.name).resolve()
        external_file = external_root / "report.zip"
        external_file.write_bytes(b"archived external report")
        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(external_root)
        settings_obj.save(update_fields=["reports_folder"])
        InitialPaper.objects.create(
            paper_id="EXTERNAL",
            title="External folder archive",
        )
        snapshot = export_system_state()

        external_file.write_bytes(b"current shared report")
        current_only = external_root / "current-only.txt"
        current_only.write_bytes(b"must remain shared")
        preview = preview_system_state_restore(
            self._snapshot_upload(snapshot["path"])
        )

        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)

        self.assertEqual(external_file.read_bytes(), b"current shared report")
        self.assertEqual(current_only.read_bytes(), b"must remain shared")
        restored = (
            self.root
            / "data"
            / "restored_external"
            / "reports_folder"
            / "report.zip"
        )
        self.assertEqual(restored.read_bytes(), b"archived external report")
        self.assertEqual(
            AppSetting.load().reports_folder,
            "data/restored_external/reports_folder",
        )

    def test_preview_rejects_project_root_folder_setting(self):
        snapshot = export_system_state()

        def use_project_root(_manifest, state):
            state["models"]["settings"][0]["reports_folder"] = "."
            payload_hash = _model_payload_sha256(state["models"])
            state["model_payload_sha256"] = payload_hash
            _manifest["model_payload_sha256"] = payload_hash

        rewritten = self._rewrite_snapshot(
            snapshot["path"],
            use_project_root,
        )

        with self.assertRaisesMessage(
            SystemStateError,
            "application-owned data folders",
        ):
            preview_system_state_restore(self._snapshot_upload(rewritten))

    def test_preview_rejects_missing_model_payload_section(self):
        snapshot = export_system_state()

        def remove_models(_manifest, state):
            state["models"].pop("final_submissions")
            payload_hash = _model_payload_sha256(state["models"])
            state["model_payload_sha256"] = payload_hash
            _manifest["model_payload_sha256"] = payload_hash

        rewritten = self._rewrite_snapshot(
            snapshot["path"],
            remove_models,
        )

        with self.assertRaisesMessage(
            SystemStateError,
            "model payload is incomplete",
        ):
            preview_system_state_restore(self._snapshot_upload(rewritten))

    def test_preview_rejects_non_boolean_model_value(self):
        snapshot = export_system_state()

        def corrupt_boolean(_manifest, state):
            state["models"]["settings"][0]["grobid_enabled"] = "false"
            payload_hash = _model_payload_sha256(state["models"])
            state["model_payload_sha256"] = payload_hash
            _manifest["model_payload_sha256"] = payload_hash

        rewritten = self._rewrite_snapshot(
            snapshot["path"],
            corrupt_boolean,
        )

        with self.assertRaisesMessage(
            SystemStateError,
            "must be boolean",
        ):
            preview_system_state_restore(self._snapshot_upload(rewritten))

    def test_preview_rejects_fractional_integer_model_value(self):
        snapshot = export_system_state()

        def corrupt_integer(_manifest, state):
            state["models"]["settings"][0]["page_limit"] = 12.5
            payload_hash = _model_payload_sha256(state["models"])
            state["model_payload_sha256"] = payload_hash
            _manifest["model_payload_sha256"] = payload_hash

        rewritten = self._rewrite_snapshot(
            snapshot["path"],
            corrupt_integer,
        )

        with self.assertRaisesMessage(
            SystemStateError,
            "must be an integer",
        ):
            preview_system_state_restore(self._snapshot_upload(rewritten))

    def test_preview_rejects_invalid_negative_setting(self):
        snapshot = export_system_state()

        def corrupt_setting(_manifest, state):
            state["models"]["settings"][0]["page_limit"] = -1
            payload_hash = _model_payload_sha256(state["models"])
            state["model_payload_sha256"] = payload_hash
            _manifest["model_payload_sha256"] = payload_hash

        rewritten = self._rewrite_snapshot(
            snapshot["path"],
            corrupt_setting,
        )

        with self.assertRaisesMessage(
            SystemStateError,
            "failed validation in: page_limit",
        ):
            preview_system_state_restore(self._snapshot_upload(rewritten))

    def test_legacy_project_child_folder_is_restored_into_safe_data_folder(self):
        legacy_root = self.root / "legacy_reports"
        legacy_root.mkdir()
        archived_file = legacy_root / "report.txt"
        archived_file.write_bytes(b"archived report")
        settings_obj = AppSetting.load()
        settings_obj.reports_folder = str(legacy_root)
        settings_obj.save(update_fields=["reports_folder"])
        snapshot = export_system_state()

        old_prefix = "data/restored_external/reports_folder"

        def emulate_legacy_project_path(manifest, state):
            state["models"]["settings"][0]["reports_folder"] = "legacy_reports"
            payload_hash = _model_payload_sha256(state["models"])
            state["model_payload_sha256"] = payload_hash
            manifest["model_payload_sha256"] = payload_hash
            for entry in manifest["root_maps"]:
                if entry.get("label") == "reports_folder":
                    entry["restore_rel"] = "legacy_reports"
            for entry in manifest["files"]:
                restore_rel = entry.get("restore_rel", "")
                if restore_rel == old_prefix:
                    entry["restore_rel"] = "legacy_reports"
                elif restore_rel.startswith(old_prefix + "/"):
                    entry["restore_rel"] = (
                        "legacy_reports" + restore_rel[len(old_prefix) :]
                    )

        rewritten = self._rewrite_snapshot(
            snapshot["path"],
            emulate_legacy_project_path,
        )
        archived_file.write_bytes(b"current report")
        preview = preview_system_state_restore(
            self._snapshot_upload(rewritten)
        )

        apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)

        self.assertEqual(archived_file.read_bytes(), b"current report")
        restored = (
            self.root
            / "data"
            / "restored_external"
            / "reports_folder"
            / "report.txt"
        )
        self.assertEqual(restored.read_bytes(), b"archived report")
        self.assertEqual(
            AppSetting.load().reports_folder,
            "data/restored_external/reports_folder",
        )

    def test_settings_change_after_preview_invalidates_restore(self):
        snapshot = export_system_state()
        preview = preview_system_state_restore(
            self._snapshot_upload(snapshot["path"])
        )
        settings_obj = AppSetting.load()
        settings_obj.page_limit += 1
        settings_obj.save(update_fields=["page_limit"])

        with self.assertRaisesMessage(
            SystemStateError,
            "Database changed after preview",
        ):
            apply_system_state_restore(preview["token"], CONFIRMATION_TEXT)

    def test_restore_audit_failure_rolls_back_database_and_files(self):
        token, managed_file, current_only = self._prepare_restore()
        from submissions.services import system_state

        original_audit_success = system_state.audit_success

        def fail_restore_success(action, *args, **kwargs):
            if action == "system_state_restore_apply":
                raise OSError("injected audit write failure")
            return original_audit_success(action, *args, **kwargs)

        with patch(
            "submissions.services.system_state.audit_success",
            side_effect=fail_restore_success,
        ):
            with self.assertRaisesMessage(
                OSError,
                "injected audit write failure",
            ):
                apply_system_state_restore(token, CONFIRMATION_TEXT)

        self._assert_current_state_preserved(managed_file, current_only)

    def test_back_to_back_exports_never_overwrite_each_other(self):
        first = export_system_state()
        second = export_system_state()

        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(first["path"].exists())
        self.assertTrue(second["path"].exists())

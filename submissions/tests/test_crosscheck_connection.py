import base64
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from submissions.forms import AppSettingForm
from submissions.models import AppSetting, CrossCheckServiceJob
from submissions.services.crosscheck_client import (
    CREDENTIAL_FIELDS, CrossCheckClient, CrossCheckServiceError,
    read_service_secrets, save_service_secrets, secrets_path,
)
from submissions.services.crosscheck_service import preview_service_job
from submissions.services.workflow_evidence import app_setting_evidence, make_evidence_token


class CrossCheckConnectionTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.enterContext(override_settings(BASE_DIR=self.root, MEDIA_ROOT=self.root / "data/media"))
        self.enterContext(patch.dict(os.environ, {"SMS_CROSSCHECK_" + key.upper(): "" for key in CREDENTIAL_FIELDS}))
        self.config = AppSetting.load()
        self.url = "http://private-crosscheck:18765"

    def evidence(self):
        return make_evidence_token("app-settings-edit", app_setting_evidence(AppSetting.read()))

    def settings_data(self, **changes):
        data = {name: getattr(AppSetting.read(), name) for name in AppSettingForm.Meta.fields}
        data.update(changes)
        return data

    def test_unconfigured_address_is_blank_and_disabled_settings_can_be_saved(self):
        self.assertEqual(AppSetting().crosscheck_service_url, "")
        self.assertEqual(AppSetting.read().crosscheck_service_url, "")
        form = AppSettingForm(data=self.settings_data(), instance=AppSetting.read())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["crosscheck_service_url"], "")
        page = self.client.get(reverse("submissions:settings"))
        self.assertContains(page, 'id="cc-web-password"')
        self.assertNotContains(page, 'value="http://127.0.0.1:18765"')
        self.assertNotContains(page, "Local credential file needs attention")
        self.assertEqual(AppSettingForm(instance=AppSetting.read())["crosscheck_service_url"].value(), "")

    def test_enabled_integration_requires_an_explicit_address(self):
        form = AppSettingForm(data=self.settings_data(
            crosscheck_service_enabled=True, crosscheck_confirm_folder=True,
            crosscheck_service_folder="Conference folder"), instance=AppSetting.read())
        self.assertFalse(form.is_valid())
        self.assertIn("crosscheck_service_url", form.errors)
        self.config.crosscheck_service_enabled = True
        self.config.save()
        with self.assertRaisesRegex(ValueError, "Enter the CrossCheck service URL"):
            preview_service_job("all")
        self.assertFalse(CrossCheckServiceJob.objects.exists())

    def test_explicit_clear_is_not_replaced_by_previously_saved_address(self):
        self.config.crosscheck_service_url = self.url
        self.config.save()
        form = AppSettingForm(data=self.settings_data(crosscheck_service_url=""), instance=AppSetting.read())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["crosscheck_service_url"], "")
        with patch("submissions.services.crosscheck_client.request.build_opener") as transport:
            response = self.client.post(reverse("submissions:crosscheck_service_connection"), {"url": ""})
        self.assertEqual(response.status_code, 400)
        transport.assert_not_called()

    def test_connection_uses_entered_service_login_without_saving_or_enabling(self):
        before = AppSetting.objects.values().get()
        password = "test-only password with spaces "
        with patch("submissions.services.crosscheck_client.request.build_opener") as transport:
            transport.return_value.open.return_value = io.BytesIO(b'{"folders":["Conference folder"],"username":"Service account"}')
            response = self.client.post(reverse("submissions:crosscheck_service_connection"), {
                "url": self.url, "web_username": " editor ", "web_password": password,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["folders"], ["Conference folder"])
        request = transport.return_value.open.call_args.args[0]
        expected = base64.b64encode(("editor:" + password).encode()).decode()
        self.assertEqual(request.get_header("Authorization"), "Basic " + expected)
        self.assertEqual(request.full_url, self.url + "/api/folders")
        self.assertEqual(AppSetting.objects.values().get(), before)
        self.assertFalse(secrets_path().exists())
        self.assertNotIn(password, response.content.decode())
        self.assertNotIn(password, (self.root / "data/logs/audit.log").read_text())

    def test_service_login_failure_is_clear_and_never_echoes_secrets(self):
        password = "private-test-password"
        error = HTTPError(self.url + "/api/folders", 401, "Unauthorized", {}, io.BytesIO(password.encode()))
        with patch("submissions.services.crosscheck_client.request.build_opener") as transport:
            transport.return_value.open.side_effect = error
            response = self.client.post(reverse("submissions:crosscheck_service_connection"), {
                "url": self.url, "web_username": "editor", "web_password": password,
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Service login required or rejected", response.json()["message"])
        self.assertNotIn(password, response.content.decode())
        self.assertNotIn(password, (self.root / "data/logs/audit.log").read_text())

    def test_incomplete_login_is_rejected_before_network_access(self):
        with patch("submissions.services.crosscheck_client.request.build_opener") as transport:
            response = self.client.post(reverse("submissions:crosscheck_service_connection"), {
                "url": self.url, "web_username": "editor",
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("both username and password", response.json()["message"])
        transport.assert_not_called()

    def test_saved_credentials_are_never_reused_for_another_address(self):
        save_service_secrets(self.url, {"web_username": "saved-editor", "web_password": "saved-password"})
        before = secrets_path().read_bytes()
        with patch("submissions.services.crosscheck_client.request.build_opener") as transport:
            transport.return_value.open.return_value = io.BytesIO(b'{"folders":[]}')
            response = self.client.post(reverse("submissions:crosscheck_service_connection"), {"url": "http://another-host:18765"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(transport.return_value.open.call_args.args[0].get_header("Authorization"))
        self.assertEqual(secrets_path().read_bytes(), before)

    def test_local_credentials_can_be_saved_before_other_settings_without_navigation(self):
        before = AppSetting.objects.values().get()
        response = self.client.post(reverse("submissions:crosscheck_service_credentials"), {
            "url": self.url, "web_username": "editor", "web_password": "saved-password",
            "evidence_token": self.evidence(),
        }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["login_label"], "Service login configured")
        self.assertNotIn("saved-password", response.content.decode())
        self.assertEqual(read_service_secrets(self.url)["web_password"], "saved-password")
        self.assertEqual(AppSetting.objects.values().get(), before)
        self.assertEqual(secrets_path().stat().st_mode & 0o777, 0o600)

    def test_stale_settings_evidence_cannot_save_credentials_for_a_draft_address(self):
        evidence = self.evidence()
        self.config.conference_name = "Changed in another window"
        self.config.save()
        response = self.client.post(reverse("submissions:crosscheck_service_credentials"), {
            "url": self.url, "web_username": "editor", "web_password": "saved-password",
            "evidence_token": evidence,
        }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(secrets_path().exists())

    def test_first_credential_save_does_not_create_or_enable_settings(self):
        AppSetting.objects.all().delete()
        page = self.client.get(reverse("submissions:settings"))
        evidence = page.context["settings_evidence_token"]
        response = self.client.post(reverse("submissions:crosscheck_service_credentials"), {
            "url": self.url, "web_username": "editor", "web_password": "saved-password",
            "evidence_token": evidence,
        }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AppSetting.objects.exists())
        self.assertEqual(read_service_secrets(self.url)["web_password"], "saved-password")

    def test_first_settings_apply_accepts_reviewed_defaults_but_rejects_invalid_evidence(self):
        from submissions.services.settings_workflow import apply_app_settings_form, reset_app_setting_folders

        AppSetting.objects.all().delete()
        evidence = self.evidence()
        form = AppSettingForm(data=self.settings_data(
            crosscheck_service_enabled=True, crosscheck_service_url=self.url,
            crosscheck_service_folder="Conference folder", crosscheck_confirm_folder=True), instance=AppSetting.read())
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises(ValueError):
            apply_app_settings_form(form, expected_evidence_token="invalid")
        self.assertFalse(AppSetting.objects.exists())
        current, _ = apply_app_settings_form(form, expected_evidence_token=evidence)
        self.assertEqual(current.crosscheck_service_url, self.url)
        self.assertTrue(current.crosscheck_service_enabled)
        self.assertEqual(current.crosscheck_service_conference, current.conference_name)
        AppSetting.objects.all().delete()
        current, _ = reset_app_setting_folders({"reports_folder": "data/reports"}, expected_evidence_token=self.evidence())
        self.assertTrue(AppSetting.objects.filter(pk=current.pk).exists())

    def test_background_initialization_does_not_invalidate_default_settings_evidence(self):
        AppSetting.objects.all().delete()
        page = self.client.get(reverse("submissions:settings"))
        evidence = page.context["settings_evidence_token"]
        AppSetting.load()
        before = AppSetting.objects.values().get()
        response = self.client.post(reverse("submissions:crosscheck_service_credentials"), {
            "url": self.url, "web_username": "editor", "web_password": "saved-password",
            "evidence_token": evidence,
        }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AppSetting.objects.values().get(), before)
        self.assertEqual(read_service_secrets(self.url)["web_password"], "saved-password")
        AppSetting.objects.update(plagiarism_percent_threshold=36)
        response = self.client.post(reverse("submissions:crosscheck_service_credentials"), {
            "url": self.url, "remove": "1", "evidence_token": evidence,
        }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(read_service_secrets(self.url)["web_password"], "saved-password")

    def test_saved_login_and_environment_precedence_remain_unchanged(self):
        save_service_secrets(self.url, {"web_username": "saved-editor", "web_password": "saved-password"})
        entered = {"web_username": "entered-editor", "web_password": "entered-password"}
        self.assertEqual(CrossCheckClient(self.url).credentials["web_password"], "saved-password")
        self.assertEqual(CrossCheckClient(self.url, credentials=entered).credentials["web_password"], "entered-password")
        with patch.dict(os.environ, {"SMS_CROSSCHECK_WEB_USERNAME": "environment-editor", "SMS_CROSSCHECK_WEB_PASSWORD": "environment-password"}):
            client = CrossCheckClient(self.url, credentials=entered)
            self.assertEqual(client.credentials["web_username"], "environment-editor")
            self.assertEqual(client.credentials["web_password"], "environment-password")
        self.assertEqual(read_service_secrets(self.url)["web_password"], "saved-password")

    def test_ieee_override_is_separate_from_service_login_and_not_persisted_by_test(self):
        credentials = {"web_username": "web-editor", "web_password": "web-password",
                       "ieee_username": "ieee-editor", "ieee_password": "ieee-password"}
        client = CrossCheckClient(self.url, credentials=credentials)
        with patch.object(client, "json", return_value={"folders": []}) as request:
            client.folders()
        request.assert_called_once_with("/api/folders", method="POST", payload={"username": "ieee-editor", "password": "ieee-password"})
        self.assertFalse(secrets_path().exists())
        with self.assertRaises(CrossCheckServiceError):
            CrossCheckClient(self.url, credentials={"ieee_username": "incomplete"})

    def test_removing_credentials_preserves_settings_and_keeps_environment_overrides(self):
        save_service_secrets(self.url, {"web_username": "editor", "web_password": "saved-password"})
        before = AppSetting.objects.values().get()
        response = self.client.post(reverse("submissions:crosscheck_service_credentials"), {
            "url": self.url, "remove": "1", "evidence_token": self.evidence(),
        }, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(secrets_path().exists())
        self.assertEqual(response.json()["login_label"], "Service login not configured")
        self.assertEqual(AppSetting.objects.values().get(), before)


class CrossCheckAddressMigrationTests(TransactionTestCase):
    def test_blank_default_migration_preserves_every_existing_setting_and_submission(self):
        previous = [("submissions", "0035_appsetting_crosscheck_service_conference_and_more")]
        target = [("submissions", "0036_alter_appsetting_crosscheck_service_url")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(previous)
        old_apps = executor.loader.project_state(previous).apps
        old_settings = old_apps.get_model("submissions", "AppSetting")
        old_final = old_apps.get_model("submissions", "FinalSubmission")
        old_settings.objects.create(pk=1, crosscheck_service_url="http://saved-service:18765",
            crosscheck_service_enabled=True, crosscheck_service_folder="Saved folder",
            conference_name="Saved conference", crosscheck_service_conference="Saved conference",
            active_version_rule="upload_date", plagiarism_percent_threshold="37.50", single_similarity_threshold="8.25")
        old_final.objects.create(final_submission_id="SAVED-FINAL", paper_id_filled="P001", active_version=True,
                                 paper_id_verified=True, pdf_file="final_submissions/saved.pdf", pdf_hash="saved-pdf-hash")
        settings_before = old_settings.objects.values().get(pk=1)
        final_before = old_final.objects.values().get()
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        restored_apps = executor.loader.project_state(target).apps
        migrated_settings = restored_apps.get_model("submissions", "AppSetting")
        migrated_final = restored_apps.get_model("submissions", "FinalSubmission")
        self.assertEqual(migrated_settings.objects.values().get(pk=1), settings_before)
        self.assertEqual(migrated_final.objects.values().get(), final_before)
        self.assertEqual(migrated_settings().crosscheck_service_url, "")

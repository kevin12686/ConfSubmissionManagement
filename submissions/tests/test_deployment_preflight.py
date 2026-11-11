"""Startup/cutover checks must fail before conference writes or replacement."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from django.test import SimpleTestCase

from scripts import check_comparison_runtime as preflight
from submissions.services import comparison_client
from submissions.services.comparison_protocol import contract_id


class RuntimePreflightTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "CFM_COMPARISON_TRANSPORT": "service",
            "CFM_COMPARISON_LOCAL_URL": "http://local:8091",
            "CFM_COMPARISON_LOCAL_TOKEN": "local-test-token-" * 3,
            "CFM_COMPARISON_REMOTE_URL": "",
            "CFM_COMPARISON_REMOTE_TOKEN": "",
        }))
        self.health = {"protocol": 1, "contract": contract_id(), "state": "ready"}

    def test_service_only_reads_health_without_conference_database(self):
        with patch.object(comparison_client.Endpoint, "call", return_value=(200, self.health)) as call:
            messages = preflight.check_service(0)
        call.assert_called_once_with("/v1/health")
        self.assertIn("authenticated", messages[0])

    def test_absent_token_fails_before_network_and_does_not_leak_secret(self):
        with patch.dict(os.environ, {"CFM_COMPARISON_LOCAL_TOKEN": "short-secret"}):
            with patch.object(comparison_client.Endpoint, "call") as call:
                with self.assertRaises(preflight.PreflightError) as raised:
                    preflight.check_service(0)
                call.assert_not_called()
            self.assertNotIn("short-secret", str(raised.exception))

    def test_no_endpoint_and_token_without_url_are_rejected(self):
        for token in ("", "not-used-secret" * 3):
            with patch.dict(os.environ, {"CFM_COMPARISON_LOCAL_URL": "", "CFM_COMPARISON_LOCAL_TOKEN": token}):
                with self.assertRaises(preflight.PreflightError):
                    preflight.check_service(0)

    def test_missing_or_incompatible_local_fallback_blocks_even_ready_remote(self):
        with patch.dict(os.environ, {"CFM_COMPARISON_REMOTE_URL": "http://remote:8091", "CFM_COMPARISON_REMOTE_TOKEN": "r" * 32}):
            for state in ("not_ready", "wrong_contract", "offline"):
                def health(endpoint, path):
                    if endpoint.name == "remote":
                        return 200, self.health
                    if state == "offline":
                        raise comparison_client.TransportUnavailable
                    return 200, {**self.health, **({"contract": "bad"} if state == "wrong_contract" else {"state": state})}
                with self.subTest(state=state), patch.object(comparison_client.Endpoint, "call", autospec=True, side_effect=health):
                    with self.assertRaisesRegex(preflight.PreflightError, "Required local worker"):
                        preflight.check_service(0)

    def test_unreachable_remote_is_warning_when_local_validated(self):
        with patch.dict(os.environ, {"CFM_COMPARISON_REMOTE_URL": "http://remote:8091", "CFM_COMPARISON_REMOTE_TOKEN": "r" * 32}):
            def health(endpoint, path):
                if endpoint.name == "remote":
                    raise comparison_client.TransportUnavailable
                return 200, self.health
            with patch.object(comparison_client.Endpoint, "call", autospec=True, side_effect=health):
                self.assertIn("validated local fallback", preflight.check_service(0)[1])

    def test_unauthorized_endpoint_is_error_not_silent_fallback(self):
        from submissions.services.pdf_comparison_errors import PDFComparisonError
        with patch.object(comparison_client.Endpoint, "call", side_effect=PDFComparisonError("Unauthorized")):
            with self.assertRaisesRegex(preflight.PreflightError, "token"):
                preflight.check_service(0)

    def test_service_waits_for_startup_with_finite_budget(self):
        with patch.object(comparison_client.Endpoint, "call", side_effect=[
            (200, {**self.health, "state": "not_ready"}), (200, self.health),
        ]) as call, patch.object(preflight.time, "sleep"):
            preflight.check_service(1)
        self.assertEqual(call.call_count, 2)

    def test_embedded_loads_models_using_actual_configured_interpreter(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"ready": True, "backend": "cpu"}).encode())
        with patch.dict(os.environ, {"CFM_RAPIDOCR_PYTHON": sys.executable}), patch.object(preflight.subprocess, "run", return_value=result) as run:
            self.assertIn("sessions loaded", preflight.check_embedded()[0])
        self.assertEqual(run.call_args.args[0][0], sys.executable)
        self.assertNotIn("DJANGO_SETTINGS_MODULE", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["timeout"], 65)

    def test_embedded_missing_model_crash_and_timeout_fail_closed(self):
        responses = [subprocess.CompletedProcess([], 1, b'{"error":"model missing"}'),
                     subprocess.CompletedProcess([], -11, b''),
                     subprocess.TimeoutExpired([], 65)]
        for result in responses:
            options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=result), patch.object(preflight.subprocess, "run", **options):
                with self.assertRaises(preflight.PreflightError):
                    preflight.check_embedded()

    def test_invalid_transport_fails(self):
        with patch.dict(os.environ, {"CFM_COMPARISON_TRANSPORT": "guess"}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(preflight.main([]), 1)

    def test_docker_entrypoint_checks_before_database_or_static_writes(self):
        script = (preflight.ROOT / "docker" / "entrypoint.sh").read_text()
        check = script.index("python scripts/check_comparison_runtime.py")
        for action in ("mkdir", "manage.py migrate", "manage.py collectstatic", "exec gunicorn"):
            self.assertLess(check, script.index(action))


class CutoverPreflightTests(SimpleTestCase):
    def setUp(self):
        sys.path.insert(0, str(preflight.ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(preflight.ROOT / "scripts"))

    def test_restart_preflight_only_for_running_images_with_startup_guard(self):
        from scripts import docker_instance_tools as tools
        with patch.object(tools, "run") as runner:
            tools.preflight_instance_restart({"running": True, "id": "legacy"}, runner=runner)
            tools.preflight_instance_restart({"running": False, "startup_preflight": "1", "id": "stopped"}, runner=runner)
            runner.assert_not_called()
            tools.preflight_instance_restart({"running": True, "startup_preflight": "1", "id": "web"}, runner=runner)
            runner.assert_called_once_with(["docker", "exec", "web", "python", "scripts/check_comparison_runtime.py"], capture=False)

    def test_backup_failure_does_not_stop_or_copy_conference(self):
        import backup_docker_instances as backup
        with patch.object(backup, "preflight_instance_restart", side_effect=RuntimeError("not ready")), \
             patch.object(backup, "stop_instance") as stop, patch.object(backup, "transfer_data") as transfer:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                backup.backup_instance({"sms_data_dir": "/tmp/test-mirror", "volume_name": "qa", "name": "qa-web", "project": "qa", "running": True}, preflight.ROOT, dry_run=False, stop_timeout=30)
        stop.assert_not_called()
        transfer.assert_not_called()

    def test_migration_failure_does_not_stop_or_transfer_data(self):
        import migrate_docker_data_volumes as migration
        instance = {"project": "qa", "name": "qa-web", "sms_data_dir": "/tmp/qa", "sms_bind_host": "127.0.0.1", "sms_port": "19084", "env": {}, "running": True}
        with patch.object(migration, "planned_volume_name", return_value="qa_data"), \
             patch.object(migration, "require_comparison_network"), patch.object(migration, "run"), \
             patch.object(migration, "GatewayOperationStatus"), \
             patch.object(migration, "preflight_compose_runtime", side_effect=RuntimeError("not ready")), \
             patch.object(migration, "stop_instance") as stop, patch.object(migration, "transfer_data") as transfer:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                migration.migrate_instance(instance, preflight.ROOT, dry_run=False, stop_timeout=30)
        stop.assert_not_called()
        transfer.assert_not_called()

    def test_compose_probe_bypasses_entrypoint_and_never_starts_dependencies(self):
        from scripts import docker_instance_tools as tools
        with patch.object(tools, "run") as runner:
            tools.preflight_compose_runtime(Path("/repo"), Path("/env"), "test-conf", runner=runner)
        self.assertEqual(runner.call_count, 2)
        cmd = runner.call_args_list[0].args[0]
        self.assertIn("--rm", cmd)
        self.assertIn("--no-deps", cmd)
        self.assertIn("--entrypoint", cmd)
        self.assertEqual(cmd[-2:], ["web", "scripts/check_comparison_runtime.py"])
        self.assertNotIn("up", cmd)
        self.assertEqual(runner.call_args_list[1].args[0][-3:], ["web", "manage.py", "check"])
        self.assertIn("SMS_DATABASE_PATH=:memory:", runner.call_args_list[1].args[0])

    def test_rebuild_failure_does_not_replace_existing_container(self):
        # Import through the same helper namespace as the supported CLI.
        import rebuild_docker_instances as rebuild
        instance = {"project": "qa", "name": "qa-web", "public_service": "proxy", "mount_type": "volume", "gateway_version": "1", "env": {}}
        with patch.object(rebuild, "require_comparison_network"), patch.object(rebuild, "run") as runner, \
             patch.object(rebuild, "GatewayOperationStatus") as status, \
             patch.object(rebuild, "preflight_compose_runtime", side_effect=RuntimeError("not ready")):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                rebuild.rebuild_instance(instance, preflight.ROOT, dry_run=False, env_values={"CFM_COMPARISON_TRANSPORT": "service"})
        self.assertFalse(any("up" in call.args[0] or "stop" in call.args[0] for call in runner.call_args_list))
        status.return_value.close.assert_called_with(clear=True)

    def test_create_failure_never_starts_conference(self):
        import update_docker_instances as update
        plan = {"env_path": Path("/env"), "project": "qa", "desired": {"env_values": {}}}
        with patch.object(update, "require_comparison_network"), patch.object(update, "run") as runner, \
             patch.object(update, "preflight_compose_runtime", side_effect=RuntimeError("not ready")):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                update.create_instance(plan, preflight.ROOT)
        self.assertFalse(any("up" in call.args[0] for call in runner.call_args_list))

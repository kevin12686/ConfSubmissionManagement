"""Unified deployment tests use disposable files and mock every Docker/model operation."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from scripts import deploy


class WorkerOwnershipTests(unittest.TestCase):
    def test_foreign_worker_is_rejected_before_health_or_restart(self):
        env = Mock()
        env.get.return_value = "shared-worker"
        responses = [subprocess.CompletedProcess([], 0, "worker-id\n"),
                     subprocess.CompletedProcess([], 0, json.dumps([{"Config": {"Labels": {
                         "com.docker.compose.project.working_dir": "/different/checkout"}}}]))]
        with patch.object(deploy.docker, "run", side_effect=responses), patch.object(deploy.request, "build_opener") as opener:
            with self.assertRaisesRegex(deploy.DeployError, "another checkout"):
                deploy.worker_status(env, {"existing": "a" * 40})
        opener.assert_not_called()

    def test_multiple_workers_are_not_arbitrarily_selected(self):
        with patch.object(deploy.docker, "run", return_value=subprocess.CompletedProcess([], 0, "one\ntwo\n")):
            with self.assertRaisesRegex(deploy.DeployError, "Multiple workers"):
                deploy.worker_status(Mock(), {"existing": "a" * 40})


class UnifiedDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.enterContext(patch.object(deploy, "ROOT", self.root))
        self.provision = self.enterContext(patch.object(deploy.deployment_provision, "provision_models"))
        self.model_options = self.enterContext(patch.object(deploy, "worker_model_options", return_value={
            "det_model": "small", "rec_model": "small"}))
        self.status = self.enterContext(patch.object(deploy, "worker_status", return_value="absent"))
        self.launch = self.enterContext(patch.object(deploy.slave, "deploy"))
        self.fingerprint = self.enterContext(patch.object(deploy, "worker_fingerprint", side_effect=lambda env: {
            "version": 1, "project": env.get("CFM_SLAVE_PROJECT_NAME"),
            "backend": env.get("CFM_DEPLOY_WORKER_BACKEND"), "fingerprint": "a" * 64}))
        self.inspect = self.enterContext(patch.object(deploy.updater, "inspect_compose_containers", return_value=[]))
        self.enterContext(patch.object(deploy.updater, "planned_instance_from_env", side_effect=lambda root, path, project, **kwargs: {
            "env_values": deploy.EnvFile(Path(path).resolve()).values,
        }))
        self.plan = self.enterContext(patch.object(deploy, "conference_plan", side_effect=lambda path, name: {
            "project": name, "env_path": path, "action": "create"}))
        self.apply = self.enterContext(patch.object(deploy.updater, "apply_update_plans"))
        self.real_print_plans = deploy.updater.print_update_plans
        self.print_plans = self.enterContext(patch.object(deploy.updater, "print_update_plans"))
        self.network = self.enterContext(patch.object(deploy, "ensure_network"))
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(redirect_stderr(self.errors))

    def write(self, relative, text, mode=0o600):
        path = self.root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def clients(self):
        return json.loads((self.root / "runtime/comparison-slave/clients.json").read_text())

    def worker(self, *args):
        return deploy.main(["worker", *args])

    def conference(self, name="conf-a", *args):
        return deploy.main(["conference", "--name", name, *args])

    def existing_plan(self, name="conf-a", port="9001"):
        path = self.root / (".env." + name)
        if not path.exists():
            self.write(path, f"COMPOSE_PROJECT_NAME={name}\nSMS_PORT={port}\nSMS_DATA_DIR=./runtime/{name}\n")
        existing = {"project": name, "mount_type": "volume", "sms_data_dir": str(self.root / "runtime" / name),
                    "sms_bind_host": "127.0.0.1", "sms_port": port, "env": {}}
        return {"action": "update", "project": name, "existing": existing, "env_path": path,
                "desired": {**existing, "env_values": {}}, "changes": {}, "force_proxy_recreate": False}

    def prepare_update_mocks(self, plans):
        planner = self.enterContext(patch.object(deploy.updater, "build_update_plans", return_value=plans))
        def compose(root, path, name, *, bind_override):
            values = deploy.EnvFile(Path(path).resolve()).values
            return {"env_values": values, "env": {k: v for k, v in values.items() if k in deploy.updater.ENV_KEYS},
                    "sms_data_dir": str(root / values["SMS_DATA_DIR"]),
                    "sms_port": values["SMS_PORT"], "sms_bind_host": values["SMS_BIND_HOST"]}
        self.enterContext(patch.object(deploy.updater, "planned_instance_from_env", side_effect=compose))
        return planner

    def test_dry_runs_write_nothing_and_never_contact_worker(self):
        self.assertEqual(self.worker("--client", "conf-a", "--client", "conf-b", "--dry-run"), 0)
        self.assertEqual(self.conference("conf-c", "--dry-run"), 0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(deploy.main(["update", "--dry-run"]), 0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.status.assert_not_called()
        self.launch.assert_not_called()
        self.apply.assert_not_called()
        self.assertTrue(all(call.kwargs["dry_run"] for call in self.provision.call_args_list))

    def test_remote_headers_preserved_and_never_printed(self):
        raw = json.dumps({"X-API-Key": "private$ #value'with\"quote"})
        path = self.write(".env.conf-a", "COMPOSE_PROJECT_NAME=conf-a\n"
            "CFM_COMPARISON_REMOTE_URL=https://worker.example\n"
            "CFM_COMPARISON_REMOTE_TOKEN=" + "r" * 40 + "\n" + deploy.docker.format_env_line("CFM_COMPARISON_REMOTE_HEADERS", raw))
        original = path.read_text()
        self.assertEqual(self.conference(), 0)
        self.assertIn(original, path.read_text())
        self.assertEqual(json.loads(deploy.EnvFile(path).get("CFM_COMPARISON_REMOTE_HEADERS")), json.loads(raw))
        self.assertNotIn("private$", self.output.getvalue() + self.errors.getvalue())
        self.assertNotIn("private$", (self.root / "runtime/deployment.log").read_text())

    def test_invalid_headers_stop_before_worker_or_conference_changes(self):
        for raw in ('{"Authorization":"private-secret"}', '{"X-Key":"private-secret"}', '[]'):
            path = self.write(".env.conf-a", "COMPOSE_PROJECT_NAME=conf-a\n" +
                deploy.docker.format_env_line("CFM_COMPARISON_REMOTE_HEADERS", raw))
            original = path.read_bytes()
            self.assertEqual(self.conference(), 1)
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn("private-secret", self.errors.getvalue())
        self.launch.assert_not_called()
        self.provision.assert_not_called()
        self.apply.assert_not_called()

    def test_worker_generates_distinct_private_enrollments_and_preserves_tokens(self):
        args = ("--gpu", "--bind-host", "192.0.2.10", "--client", "conf-a", "--client", "conf-b")
        self.assertEqual(self.worker(*args), 0)
        original = self.clients()
        self.assertNotEqual(original["conf-a"], original["conf-b"])
        for name in original:
            path = self.root / f"runtime/comparison-slave/enrollments/{name}.json"
            value = json.loads(path.read_text())
            self.assertEqual(value, {"version": 1, "namespace": name, "token": original[name], "endpoint": "http://192.0.2.10:8091"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(original[name], self.output.getvalue() + self.errors.getvalue())
        self.assertEqual(self.worker(*args), 0)
        self.assertEqual(self.clients(), original)
        self.assertTrue(self.launch.call_args.kwargs["gpu"])

    def test_bootstrap_is_internal_and_not_exported_or_importable(self):
        self.assertEqual(self.worker(), 0)
        self.assertEqual(set(self.clients()), {deploy.BOOTSTRAP})
        self.assertFalse((self.root / "runtime/comparison-slave/enrollments").exists())
        self.assertEqual(self.conference(deploy.BOOTSTRAP), 1)

    def test_worker_update_preserves_explicit_ocr_settings_and_credentials(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        path = self.root / "runtime/comparison-slave/worker.env"
        values = {"CFM_RAPIDOCR_PAGES": "1", "CFM_RAPIDOCR_THREADS": "2"}
        env = deploy.EnvFile(path)
        path.write_text("".join(deploy.docker.format_env_line(key, value)
            for key, value in {**env.values, **values}.items()))
        original, clients = path.read_bytes(), self.clients()
        self.status.return_value = "ready"
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.clients(), clients)
        for key, value in values.items():
            self.assertEqual(deploy.EnvFile(path).get(key), value)

    def test_gpu_update_retains_explicit_disabled_residency_and_credentials(self):
        self.assertEqual(self.worker("--gpu", "--client", "conf-a"), 0)
        path = self.root / "runtime/comparison-slave/worker.env"
        path.write_bytes(path.read_bytes() + b"CFM_GPU_MODEL_IDLE_SECONDS=0\n")
        original, clients = path.read_bytes(), self.clients()
        self.status.return_value = "ready"
        self.assertEqual(self.worker("--gpu", "--client", "conf-a"), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.clients(), clients)

    def test_wildcard_bind_requires_explicit_advertised_endpoint(self):
        self.assertEqual(self.worker("--bind-host", "0.0.0.0", "--client", "conf-a"), 1)
        self.assertFalse((self.root / "runtime/comparison-slave/worker.env").exists())
        self.assertEqual(self.worker("--bind-host", "0.0.0.0", "--client", "conf-a", "--endpoint", "https://worker.example"), 0)
        exported = json.loads((self.root / "runtime/comparison-slave/enrollments/conf-a.json").read_text())
        self.assertEqual(exported["endpoint"], "https://worker.example")

    def test_registered_healthy_worker_is_reused_without_rewriting_clients(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        path = self.root / "runtime/comparison-slave/clients.json"
        self.write(path, json.dumps(self.clients(), separators=(",", ":")))
        before = path.read_bytes()
        self.status.return_value = "ready"
        self.launch.reset_mock()
        self.assertEqual(self.conference(), 0)
        self.launch.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        env = deploy.EnvFile(self.root / ".env.conf-a")
        self.assertEqual(env.get("CFM_COMPARISON_LOCAL_TOKEN"), self.clients()["conf-a"])

    def test_new_namespace_checks_global_busy_before_config_or_provision_changes(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        path = self.root / "runtime/comparison-slave/clients.json"
        before = path.read_bytes()
        self.status.return_value = "busy"
        self.launch.reset_mock()
        self.provision.reset_mock()
        self.assertEqual(self.conference("conf-b", "--port", "8001"), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / ".env.conf-b").exists())
        self.launch.assert_not_called()
        self.provision.assert_not_called()

    def test_idle_enrollment_adds_only_one_client_and_retains_existing_token(self):
        self.assertEqual(self.conference(), 0)
        old = self.clients()["conf-a"]
        self.status.return_value = "ready"
        self.assertEqual(self.conference("conf-b", "--port", "8001"), 0)
        self.assertEqual(self.clients()["conf-a"], old)
        self.assertNotEqual(self.clients()["conf-b"], old)
        self.assertEqual(self.launch.call_count, 2)

    def test_source_marker_change_requires_update_and_obeys_busy_guard(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        old = self.clients()
        self.fingerprint.side_effect = lambda env: {"version": 1, "project": env.get("CFM_SLAVE_PROJECT_NAME"),
                                                   "backend": "cpu", "fingerprint": "b" * 64}
        self.status.return_value = "busy"
        self.assertEqual(self.conference(), 1)
        self.status.return_value = "ready"
        self.assertEqual(self.conference(), 0)
        marker = json.loads((self.root / "runtime/comparison-slave/deployed.json").read_text())
        self.assertEqual(marker["fingerprint"], "b" * 64)
        self.assertEqual(self.clients(), old)

    def test_failed_worker_deployment_restores_existing_client_and_env_bytes(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        paths = [self.root / "runtime/comparison-slave" / name for name in ("clients.json", "worker.env", "deployed.json")]
        before = [path.read_bytes() for path in paths]
        self.status.return_value = "ready"
        self.launch.side_effect = RuntimeError("simulated deployment failure")
        self.assertEqual(self.worker("--client", "conf-b"), 1)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse((self.root / "runtime/comparison-slave/enrollments/conf-b.json").exists())

    def test_existing_named_env_is_discovered_without_creating_duplicate(self):
        path = self.write(".env.conference-a", "# keep this comment\nCOMPOSE_PROJECT_NAME=conf-a\nSMS_PORT=8123\nSMS_DATA_DIR=./existing-data\nSMS_SECRET_KEY=existing-secret\nCFM_COMPARISON_LOCAL_TOKEN= # preserve note\n")
        self.assertEqual(self.conference(), 0)
        text = path.read_text()
        self.assertTrue(text.startswith("# keep this comment\nCOMPOSE_PROJECT_NAME=conf-a\nSMS_PORT=8123\nSMS_DATA_DIR=./existing-data\nSMS_SECRET_KEY=existing-secret\n"))
        self.assertIn("# preserve note", text)
        self.assertFalse((self.root / ".env.conf-a").exists())

    def test_explicit_different_env_cannot_duplicate_project(self):
        self.write(".env.conference-a", "COMPOSE_PROJECT_NAME=conf-a\n")
        self.assertEqual(self.conference("conf-a", "--env-file", str(self.root / "elsewhere.env")), 1)
        self.assertFalse((self.root / "elsewhere.env").exists())
        self.launch.assert_not_called()

    def test_existing_container_without_managed_env_requires_explicit_existing_file(self):
        self.inspect.return_value = [{"Config": {"Labels": {"com.docker.compose.project": "conf-a",
                                                           "com.docker.compose.project.working_dir": str(self.root)}}}]
        self.assertEqual(self.conference(), 1)
        self.assertFalse((self.root / ".env.conf-a").exists())

    def test_port_or_data_validation_failure_does_not_enroll_namespace(self):
        self.plan.side_effect = ValueError("port/data conflict")
        self.assertEqual(self.conference(), 1)
        self.assertFalse((self.root / "runtime/comparison-slave/clients.json").exists())
        self.launch.assert_not_called()
        self.provision.assert_not_called()

    def test_existing_token_mismatch_is_not_rotated(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        old = self.clients()
        path = self.write(".env.conf-a", "COMPOSE_PROJECT_NAME=conf-a\nCFM_COMPARISON_LOCAL_TOKEN=" + "z" * 40 + "\n")
        before = path.read_bytes()
        self.assertEqual(self.conference(), 1)
        self.assertEqual(self.clients(), old)
        self.assertEqual(path.read_bytes(), before)

    def test_remote_enrollment_is_namespace_bound_private_and_explicit(self):
        remote = {"version": 1, "namespace": "conf-a", "endpoint": "https://remote.example", "token": "r" * 40}
        path = self.write("remote.json", json.dumps(remote))
        self.assertEqual(self.conference("conf-b", "--remote-enrollment", str(path)), 1)
        self.assertEqual(self.conference("conf-a", "--remote-enrollment", str(path), "--remote-url", "https://wrong.example"), 1)
        self.assertEqual(self.conference("conf-a", "--remote-enrollment", str(path)), 0)
        env = deploy.EnvFile(self.root / ".env.conf-a")
        self.assertEqual(env.get("CFM_COMPARISON_REMOTE_TOKEN"), remote["token"])
        self.assertNotEqual(env.get("CFM_COMPARISON_LOCAL_TOKEN"), remote["token"])
        self.assertNotIn(remote["token"], self.output.getvalue() + self.errors.getvalue())

    def test_existing_remote_token_conflict_is_not_overwritten(self):
        env = self.write(".env.conf-a", "COMPOSE_PROJECT_NAME=conf-a\nCFM_COMPARISON_REMOTE_TOKEN=" + "x" * 40 + "\n")
        path = self.write("remote.json", json.dumps({"version": 1, "namespace": "conf-a", "endpoint": "https://remote.example", "token": "r" * 40}))
        before = env.read_bytes()
        self.assertEqual(self.conference("conf-a", "--remote-enrollment", str(path)), 1)
        self.assertEqual(env.read_bytes(), before)
        self.launch.assert_not_called()

    def test_existing_uppercase_worker_namespaces_are_preserved(self):
        path = self.write("clients-legacy.json", json.dumps({"DSA2026": "a" * 40}))
        before = path.read_bytes()
        self.assertEqual(deploy.clients_file(path), {"DSA2026": "a" * 40})
        self.assertEqual(path.read_bytes(), before)

    def test_unquoted_windows_paths_are_rejected_without_modification(self):
        path = self.write(".env.conf-a", "COMPOSE_PROJECT_NAME=conf-a\nSMS_DATA_DIR=C:\\Conference\\Data\n")
        before = path.read_bytes()
        self.assertEqual(self.conference(), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_quoted_windows_path_is_preserved(self):
        path = self.write("quoted.env", "SMS_DATA_DIR='C:\\Conference\\Data'\n")
        env = deploy.EnvFile(path)
        self.assertEqual(env.get("SMS_DATA_DIR"), "C:\\Conference\\Data")
        self.assertEqual(env.bytes(), path.read_bytes())

    def test_symlink_or_public_enrollment_is_rejected(self):
        path = self.write("remote.json", "{}", mode=0o644)
        with self.assertRaises(deploy.DeployError):
            deploy.enrollment(path, "conf-a")
        path.chmod(0o600)
        link = self.root / "remote-link.json"
        link.symlink_to(path)
        with self.assertRaises(deploy.DeployError):
            deploy.enrollment(link, "conf-a")

    def test_provisioning_failure_does_not_write_credentials(self):
        self.provision.side_effect = deploy.deployment_provision.ProvisionError("offline models missing")
        self.assertEqual(self.conference("conf-a", "--offline"), 1)
        self.assertFalse((self.root / "runtime/comparison-slave/clients.json").exists())
        self.assertFalse((self.root / ".env.conf-a").exists())
        self.launch.assert_not_called()

    def test_update_reuses_existing_updater_and_registered_worker(self):
        self.assertEqual(self.conference(), 0)
        self.status.return_value = "ready"
        self.launch.reset_mock()
        self.prepare_update_mocks([self.existing_plan()])
        self.assertEqual(deploy.main(["update", "--project", "conf-a"]), 0)
        self.launch.assert_not_called()
        self.assertEqual(self.apply.call_count, 2)

    def test_both_update_entrypoints_share_the_same_dispatch(self):
        from scripts import update_docker_instances as cli
        with patch.object(cli.deploy, "main", return_value=0) as main:
            self.assertEqual(cli.main(["--project", "conf-a", "--dry-run", "--offline"]), 0)
        main.assert_called_once_with(["update", "--project", "conf-a", "--dry-run", "--offline"])

    def test_bulk_update_prepares_one_worker_with_distinct_client_tokens(self):
        plans = [self.existing_plan(name, str(9001 + i)) for i, name in enumerate(("conf-a", "conf-b", "conf-c"))]
        planner = self.prepare_update_mocks(plans)
        self.assertEqual(deploy.main(["update"]), 0)
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(self.provision.call_count, 1)
        self.assertEqual(self.apply.call_count, 3)
        self.assertEqual(len(set(self.clients().values())), 3)
        for plan in plans:
            env = deploy.EnvFile(plan["env_path"])
            self.assertEqual(env.get("CFM_COMPARISON_LOCAL_TOKEN"), self.clients()[plan["project"]])
            self.assertEqual(env.get("SMS_DATA_DIR"), "./runtime/" + plan["project"])
        planner.assert_called_once_with(self.root, selected_projects=set())

    def test_bulk_dry_run_has_actual_redacted_plans_without_persistent_changes(self):
        plans = [self.existing_plan(name, str(9001 + i)) for i, name in enumerate(("conf-a", "conf-b"))]
        original = {p["env_path"]: p["env_path"].read_bytes() for p in plans}
        self.prepare_update_mocks(plans)
        self.print_plans.side_effect = self.real_print_plans
        self.assertEqual(deploy.main(["update", "--dry-run"]), 0)
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(set(self.root.iterdir()), set(original))
        self.assertIn("Project: conf-a", self.output.getvalue())
        self.assertIn("Project: conf-b", self.output.getvalue())
        self.assertIn("create/update required", self.output.getvalue())
        self.assertIn("Live HTTP/runtime checks", self.output.getvalue())
        self.assertNotIn("DRY_RUN_CREDENTIAL_NOT_WRITTEN", self.output.getvalue())
        self.launch.assert_not_called()
        self.status.assert_not_called()
        self.apply.assert_not_called()

    def test_later_invalid_config_aborts_before_any_provision_or_cutover(self):
        plans = [self.existing_plan(name, str(9001 + i)) for i, name in enumerate(("conf-a", "conf-b"))]
        with plans[1]["env_path"].open("a") as stream:
            stream.write("CFM_COMPARISON_REMOTE_URL=https://worker.example\nCFM_COMPARISON_REMOTE_TOKEN=short-private-token\n")
        original = {p["env_path"]: p["env_path"].read_bytes() for p in plans}
        self.prepare_update_mocks(plans)
        self.assertEqual(deploy.main(["update"]), 1)
        self.launch.assert_not_called()
        self.provision.assert_not_called()
        self.apply.assert_not_called()
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertNotIn("short-private-token", self.errors.getvalue())

    def test_port_conflict_is_rechecked_after_defaults_before_provision(self):
        self.prepare_update_mocks([self.existing_plan("conf-a", "9001"), self.existing_plan("conf-b", "9001")])
        self.assertEqual(deploy.main(["update"]), 1)
        self.launch.assert_not_called()
        self.provision.assert_not_called()
        self.apply.assert_not_called()
        self.assertIn("Host endpoint conflict", self.errors.getvalue())

    def test_create_missing_still_requires_explicit_opt_in(self):
        plan = self.existing_plan()
        plan.update(action="create", existing=None)
        self.prepare_update_mocks([plan])
        original = plan["env_path"].read_bytes()
        self.assertEqual(deploy.main(["update"]), 0)
        self.apply.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual(plan["env_path"].read_bytes(), original)
        self.assertEqual(deploy.main(["update", "--create-missing"]), 0)
        self.assertEqual(self.apply.call_count, 1)
        self.assertTrue(self.apply.call_args.kwargs["create_missing"])

    def test_remote_only_update_preserves_headers_without_local_provisioning(self):
        plan = self.existing_plan()
        with plan["env_path"].open("a") as stream:
            stream.write("CFM_COMPARISON_LOCAL_URL=\nCFM_COMPARISON_LOCAL_TOKEN=\n"
                "CFM_COMPARISON_REMOTE_URL=https://worker.example\nCFM_COMPARISON_REMOTE_TOKEN=" + "r" * 40 + "\n"
                "CFM_COMPARISON_REMOTE_HEADERS='{" + '"X-API-Key":"private-gateway-key"' + "}'\n")
        self.prepare_update_mocks([plan])
        self.assertEqual(deploy.main(["update"]), 0)
        self.launch.assert_not_called()
        self.provision.assert_not_called()
        self.assertEqual(self.apply.call_count, 1)
        self.assertNotIn("private-gateway-key", self.output.getvalue() + self.errors.getvalue())

    def test_missing_signing_secret_and_worker_settings_are_retained_from_live_instance(self):
        plan = self.existing_plan()
        plan["existing"]["env"] = {
            "SMS_SECRET_KEY": "existing-signing-secret$${LITERAL} # ' quote\" slash\\",
            "CFM_COMPARISON_TRANSPORT": "service",
            "CFM_COMPARISON_LOCAL_URL": "http://external-local:8091",
            "CFM_COMPARISON_LOCAL_TOKEN": "l" * 40,
            "CFM_COMPARISON_REMOTE_URL": "https://worker.example",
            "CFM_COMPARISON_REMOTE_TOKEN": "r" * 40,
            "CFM_COMPARISON_REMOTE_HEADERS": '{"X-API-Key":"existing-gateway-secret"}',
        }
        self.prepare_update_mocks([plan])
        self.assertEqual(deploy.main(["update"]), 0)
        env = deploy.EnvFile(plan["env_path"])
        for key, value in plan["existing"]["env"].items():
            self.assertEqual(env.get(key), value)
        self.launch.assert_not_called()
        self.assertNotIn("existing-signing-secret", self.output.getvalue() + self.errors.getvalue())
        self.assertNotIn("existing-gateway-secret", self.output.getvalue() + self.errors.getvalue())

    def test_remote_endpoint_change_never_reuses_the_previous_endpoints_token(self):
        plan = self.existing_plan()
        plan["existing"]["env"] = {"CFM_COMPARISON_REMOTE_URL": "https://old.example", "CFM_COMPARISON_REMOTE_TOKEN": "r" * 40}
        with plan["env_path"].open("a") as stream:
            stream.write("CFM_COMPARISON_REMOTE_URL=https://new.example\n")
        self.prepare_update_mocks([plan])
        self.assertEqual(deploy.main(["update"]), 1)
        self.launch.assert_not_called()
        self.apply.assert_not_called()

    def test_legacy_live_environment_is_retained_without_creating_an_env(self):
        plan = self.existing_plan()
        plan["env_path"].unlink()
        plan["env_path"] = None
        plan["action"] = "update_live"
        plan["desired"]["env_values"] = {"CFM_COMPARISON_TRANSPORT": "service",
            "CFM_COMPARISON_LOCAL_URL": "http://comparison-slave:8091", "CFM_COMPARISON_LOCAL_TOKEN": "l" * 40}
        self.prepare_update_mocks([plan])
        self.assertEqual(deploy.main(["update"]), 0)
        self.assertFalse(list(self.root.glob(".env*")))
        self.assertEqual(self.clients(), {"conf-a": "l" * 40})

    def test_later_preflight_failure_does_not_restore_an_already_updated_env(self):
        plans = [self.existing_plan(name, str(9001 + i)) for i, name in enumerate(("conf-a", "conf-b"))]
        original = {p["env_path"]: p["env_path"].read_bytes() for p in plans}
        self.prepare_update_mocks(plans)
        self.apply.side_effect = [None, RuntimeError("preflight failed")]
        self.assertEqual(deploy.main(["update"]), 1)
        self.assertNotEqual(plans[0]["env_path"].read_bytes(), original[plans[0]["env_path"]])
        self.assertEqual(plans[1]["env_path"].read_bytes(), original[plans[1]["env_path"]])

    def test_cutover_attempt_keeps_its_config_for_recovery(self):
        plan = self.existing_plan()
        original = plan["env_path"].read_bytes()
        self.prepare_update_mocks([plan])
        self.apply.side_effect = deploy.docker.CutoverStartedError("cutover failed")
        self.assertEqual(deploy.main(["update"]), 1)
        self.assertNotEqual(plan["env_path"].read_bytes(), original)
        self.assertIn("retaining its configuration", self.errors.getvalue())

    def test_concurrent_env_edit_during_provision_is_not_overwritten(self):
        plan = self.existing_plan()
        self.prepare_update_mocks([plan])
        current = plan["env_path"].read_bytes() + b"# Concurrent operator edit\n"
        def provision(*args, **kwargs):
            plan["env_path"].write_bytes(current)
        self.provision.side_effect = provision
        self.assertEqual(deploy.main(["update"]), 1)
        self.apply.assert_not_called()
        self.assertEqual(plan["env_path"].read_bytes(), current)
        self.assertIn("changed during deployment planning", self.errors.getvalue())

    def test_single_conference_embedded_setup_remains_supported(self):
        self.write(".env.conf-a", "COMPOSE_PROJECT_NAME=conf-a\nCFM_COMPARISON_TRANSPORT=embedded\n")
        self.assertEqual(self.conference(), 0)
        self.launch.assert_not_called()
        self.provision.assert_called_once()

    def test_public_worker_credentials_rejected_instead_of_permissions_relaxed(self):
        self.write("runtime/comparison-slave/clients.json", json.dumps({"conf-a": "t" * 40}), mode=0o644)
        self.assertEqual(self.worker("--client", "conf-a"), 1)
        self.launch.assert_not_called()

    def test_audit_records_fixed_fields_without_tokens_or_arguments(self):
        self.assertEqual(self.worker("--client", "conf-a"), 0)
        path = self.root / "runtime/deployment.log"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["state"] for row in rows], ["requested", "success"])
        self.assertEqual(rows[0]["operation"], rows[1]["operation"])
        self.assertEqual(set(rows[0]), {"at", "command", "operation", "state"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.clients()["conf-a"], path.read_text())
        self.assertNotIn("conf-a", path.read_text())

    def test_failed_request_audit_aborts_before_deployment_actions(self):
        with patch.object(deploy, "audit", side_effect=OSError("audit unavailable")):
            self.assertEqual(self.worker("--client", "conf-a"), 1)
        self.provision.assert_not_called()
        self.status.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse((self.root / "runtime/comparison-slave/clients.json").exists())

    def test_success_audit_failure_is_explicit_and_does_not_claim_rollback(self):
        with patch.object(deploy, "audit", side_effect=[None, OSError("audit unavailable")]):
            self.assertEqual(self.worker("--client", "conf-a"), 1)
        self.assertIn("conf-a", self.clients())
        self.launch.assert_called_once()
        self.assertIn("side effects were not rolled back", self.errors.getvalue())

    def test_failure_audit_omits_exception_message(self):
        self.launch.side_effect = RuntimeError("secret-error-detail")
        self.assertEqual(self.worker("--client", "conf-a"), 1)
        path = self.root / "runtime/deployment.log"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["state"] for row in rows], ["requested", "failed"])
        self.assertEqual(rows[-1]["error_type"], "RuntimeError")
        self.assertNotIn("secret-error-detail", path.read_text())


class HostBoundaryTests(unittest.TestCase):
    def test_host_import_uses_no_site_packages_or_conference_models(self):
        code = "import sys; from scripts import deploy; assert 'django' not in sys.modules; assert not any(x.startswith('submissions') for x in sys.modules); print('stdlib-only')"
        result = subprocess.run([sys.executable, "-S", "-B", "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), b"stdlib-only")


if __name__ == "__main__":
    unittest.main()

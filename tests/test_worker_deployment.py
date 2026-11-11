"""Worker deployment orchestration without Docker, models, or conference data."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from comparison_slave import entrypoint
from scripts import start_comparison_slave as deployment


class WorkerDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env_file = self.root / "worker.env"
        self.env_file.write_text("CFM_SLAVE_PROJECT_NAME=isolated-worker\n")
        self.clients = self.root / "clients.json"
        self.clients.write_text(json.dumps({"conference-a": "a" * 40}))
        self.clients.chmod(0o600)
        self.models = self.root / "approved-models"
        self.models.mkdir()
        self.config = {
            "name": "isolated-worker",
            "services": {"comparison-slave": {
                "environment": {"CFM_OCR_BACKEND": "cpu"},
                "healthcheck": {"test": ["CMD", "python", "-m", "comparison_slave.entrypoint", "--healthcheck"]},
                "volumes": [{"type": "bind", "source": str(self.models), "target": "/opt/rapidocr-models", "read_only": True}],
            }},
            "secrets": {"clients": {"file": str(self.clients)}},
        }
        self.commands = []
        self.fail_stage = None
        self.owned_services = "comparison-slave\n"
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(redirect_stderr(self.errors))
        self.runner = self.enterContext(patch.object(deployment, "run", side_effect=self.fake_run))

    def fake_run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if "config" in command:
            return json.dumps(self.config)
        if command[:2] == ["docker", "ps"]:
            return self.owned_services
        if self.fail_stage and self.fail_stage in command:
            raise deployment.DeploymentError("simulated operation failure")
        return ""

    def test_cpu_build_probe_then_bounded_up(self):
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 0)
        build = next(i for i, (cmd, _) in enumerate(self.commands) if "build" in cmd)
        probe = next(i for i, (cmd, _) in enumerate(self.commands) if "--preflight" in cmd)
        start = next(i for i, (cmd, _) in enumerate(self.commands) if "up" in cmd)
        self.assertLess(build, probe)
        self.assertLess(probe, start)
        command = self.commands[probe][0]
        self.assertIn("--rm", command)
        self.assertIn("--no-deps", command)
        self.assertEqual(command[command.index("--entrypoint") + 1], "python")
        self.assertTrue(command[command.index("-e") + 1].startswith("CFM_SLAVE_ROOT=/tmp/"))
        self.assertEqual(command[-4:], ["comparison-slave", "-m", "comparison_slave.entrypoint", "--preflight"])
        start_command, options = self.commands[start]
        self.assertIn("--no-build", start_command)
        self.assertIn("--force-recreate", start_command)
        self.assertIn("--wait", start_command)
        self.assertEqual(start_command[start_command.index("--wait-timeout") + 1], "240")
        self.assertEqual(options["timeout"], 300)
        self.assertFalse(any("down" in cmd or "stop" in cmd for cmd, _ in self.commands))
        self.assertNotIn("a" * 40, self.output.getvalue() + self.errors.getvalue())

    def test_gpu_is_only_enabled_explicitly(self):
        self.config["services"]["comparison-slave"]["environment"]["CFM_OCR_BACKEND"] = "cuda"
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 1)
        self.assertFalse(any("build" in cmd for cmd, _ in self.commands))
        self.commands.clear()
        self.assertEqual(deployment.main(["--env-file", str(self.env_file), "--gpu"]), 0)
        compose_commands = [cmd for cmd, _ in self.commands if cmd[:2] == ["docker", "compose"]]
        self.assertTrue(all(str(deployment.ROOT / "compose.comparison-slave.gpu.yml") in cmd for cmd in compose_commands))

    def test_dry_run_is_read_only_and_does_not_probe(self):
        self.assertEqual(deployment.main(["--env-file", str(self.env_file), "--dry-run"]), 0)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("config", self.commands[0][0])
        self.assertEqual(self.commands[1][0][:2], ["docker", "ps"])
        self.assertIn("Dry run", self.output.getvalue())

    def test_failed_probe_never_recreates_or_stops_existing_service(self):
        self.fail_stage = "--preflight"
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 1)
        self.assertFalse(any("up" in cmd or "down" in cmd or "stop" in cmd for cmd, _ in self.commands))
        probe = next(cmd for cmd, _ in self.commands if "--preflight" in cmd)
        cleanup = next(cmd for cmd, _ in self.commands if cmd[:2] == ["docker", "rm"])
        self.assertEqual(cleanup[-1], probe[probe.index("--name") + 1])
        self.assertIn("existing worker was not stopped", self.errors.getvalue())

    def test_clients_only_rotation_recreates_worker_after_successful_probe(self):
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 0)
        self.commands.clear()
        self.clients.write_text(json.dumps({"conference-a": "b" * 40}))
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 0)
        probe = next(i for i, (cmd, _) in enumerate(self.commands) if "--preflight" in cmd)
        starts = [(i, cmd) for i, (cmd, _) in enumerate(self.commands) if "up" in cmd]
        self.assertEqual(len(starts), 1)
        self.assertLess(probe, starts[0][0])
        self.assertIn("--force-recreate", starts[0][1])
        self.assertEqual(starts[0][1][-1], "comparison-slave")
        self.assertFalse(any("down" in cmd or "--volumes" in cmd for cmd, _ in self.commands))
        self.assertNotIn("b" * 40, self.output.getvalue() + self.errors.getvalue())

    def test_failed_build_never_runs_probe_or_cutover(self):
        self.fail_stage = "build"
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 1)
        self.assertFalse(any("--preflight" in cmd or "up" in cmd for cmd, _ in self.commands))

    def test_project_collision_fails_before_build(self):
        self.owned_services = "web\nproxy\n"
        self.assertEqual(deployment.main(["--env-file", str(self.env_file)]), 1)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("non-worker containers", self.errors.getvalue())

    def test_missing_clients_or_models_fail_before_build(self):
        for missing in ("clients", "models"):
            with self.subTest(missing=missing):
                config = copy.deepcopy(self.config)
                if missing == "clients":
                    config["secrets"]["clients"]["file"] = str(self.root / "missing.json")
                else:
                    config["services"]["comparison-slave"]["volumes"][0]["source"] = str(self.root / "missing-models")
                with self.assertRaises(deployment.DeploymentError):
                    deployment.validate_config(config, gpu=False)
        self.assertFalse((self.root / "missing-models").exists())

    def test_public_clients_or_disabled_healthcheck_fail(self):
        self.clients.chmod(0o644)
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_config(self.config, gpu=False)
        self.clients.chmod(0o600)
        self.config["services"]["comparison-slave"]["healthcheck"]["disable"] = True
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_config(self.config, gpu=False)

    def test_shell_conference_settings_cannot_override_worker_env_file(self):
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "production-conference", "COMPOSE_FILE": "docker-compose.yml",
                                     "CFM_SLAVE_PROJECT_NAME": "wrong-worker", "CFM_COMPARISON_NETWORK": "wrong-network"}):
            self.assertEqual(deployment.main(["--env-file", str(self.env_file), "--dry-run"]), 0)
        environment = self.commands[0][1]["environment"]
        for key in ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "CFM_SLAVE_PROJECT_NAME", "CFM_COMPARISON_NETWORK"):
            self.assertNotIn(key, environment)

    def test_env_file_is_required_and_wait_is_bounded(self):
        for args in ([], ["--env-file", str(self.env_file), "--wait-seconds", "0"],
                     ["--env-file", str(self.env_file), "--wait-seconds", "601"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                deployment.main(args)
        self.runner.assert_not_called()


class WorkerEntrypointPreflightTests(unittest.TestCase):
    def test_runtime_probe_uses_only_disposable_storage_and_closes_service(self):
        from submissions.services.comparison_protocol import contract_id
        service = Mock(backend="cpu", clients={"test": "s" * 40})
        service.run_process.return_value = {"contract": contract_id(), "backend": "cpu", "runtime": "f" * 64}
        roots = []
        def initialize(root, source):
            roots.append(Path(root))
            self.assertTrue(Path(root).is_dir())
            return Path(root) / "effective.json"
        with patch.object(entrypoint, "initialize", side_effect=initialize), \
                patch("comparison_slave.runner.Service", return_value=service) as constructor, \
                patch.dict(os.environ, {"CFM_SLAVE_ROOT": "/never-touch-persistent-worker"}):
            entrypoint.preflight("/private-clients.json")
        self.assertNotEqual(str(roots[0]), "/never-touch-persistent-worker")
        self.assertFalse(roots[0].exists())
        constructor.assert_called_once_with(roots[0], roots[0] / "effective.json", start=False)
        service.run_process.assert_called_once_with("preflight", "test")
        service.close.assert_called_once()

    def test_bad_probe_closes_service_and_cannot_start_server(self):
        service = Mock(backend="cpu", clients={"test": "s" * 40})
        service.run_process.return_value = {"error": "configuration_failed"}
        with patch.object(entrypoint, "initialize", return_value=Path("/temporary/effective.json")), \
                patch("comparison_slave.runner.Service", return_value=service):
            with self.assertRaises(ValueError):
                entrypoint.preflight("/private-clients.json")
        service.close.assert_called_once()

    def test_failed_startup_preflight_precedes_persistent_initialization_and_exec(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"CFM_SLAVE_CLIENTS_FILE": "/clients.json", "CFM_SLAVE_ROOT": "/persistent"}), \
                patch.object(entrypoint, "preflight", side_effect=ValueError("private-token-value")), \
                patch.object(entrypoint, "initialize") as initialize, patch.object(entrypoint.os, "execvp") as execute, \
                redirect_stderr(output):
            self.assertEqual(entrypoint.main(["gunicorn", "comparison_slave.server:application"]), 1)
        initialize.assert_not_called()
        execute.assert_not_called()
        self.assertNotIn("private-token-value", output.getvalue())

    def test_preflight_target_never_executes_server_or_initializes_persistent_root(self):
        with patch.dict(os.environ, {"CFM_SLAVE_CLIENTS_FILE": "/clients.json"}), \
                patch.object(entrypoint, "preflight") as probe, patch.object(entrypoint, "initialize") as initialize, \
                patch.object(entrypoint.os, "execvp") as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(entrypoint.main(["--preflight"]), 0)
        probe.assert_called_once_with("/clients.json")
        initialize.assert_not_called()
        execute.assert_not_called()

    def test_successful_startup_validates_then_initializes_then_executes(self):
        calls = []
        with patch.dict(os.environ, {"CFM_SLAVE_CLIENTS_FILE": "/clients.json", "CFM_SLAVE_ROOT": "/persistent"}), \
                patch.object(entrypoint, "preflight", side_effect=lambda source: calls.append("probe")), \
                patch.object(entrypoint, "initialize", side_effect=lambda *args: calls.append("initialize") or Path("/persistent/effective.json")), \
                patch.object(entrypoint.os, "execvp", side_effect=lambda *args: calls.append("exec")):
            entrypoint.main(["gunicorn", "comparison_slave.server:application"])
        self.assertEqual(calls, ["probe", "initialize", "exec"])

    def test_healthcheck_never_loads_models_or_initializes_storage(self):
        with patch.object(entrypoint, "healthy", return_value=True), patch.object(entrypoint, "preflight") as probe, \
                patch.object(entrypoint, "initialize") as initialize:
            self.assertEqual(entrypoint.main(["--healthcheck"]), 0)
        probe.assert_not_called()
        initialize.assert_not_called()


class WorkerCommandBoundaryTests(unittest.TestCase):
    def test_command_failure_does_not_echo_secret_stderr(self):
        def failed(command, **kwargs):
            kwargs["stderr"].write(b"private-secret-from-compose")
            return subprocess.CompletedProcess(command, 1)
        with patch.object(deployment.subprocess, "run", side_effect=failed):
            with self.assertRaises(deployment.DeploymentError) as raised:
                deployment.run(["docker", "compose", "config"], environment={}, timeout=30, capture=True)
        self.assertNotIn("private-secret", str(raised.exception))

    def test_config_output_and_command_time_are_bounded(self):
        def oversized(command, **kwargs):
            kwargs["stdout"].write(b"x" * (deployment.MAX_CONFIG_BYTES + 1))
            return subprocess.CompletedProcess(command, 0)
        with patch.object(deployment.subprocess, "run", side_effect=oversized):
            with self.assertRaises(deployment.DeploymentError):
                deployment.run(["docker", "compose", "config"], environment={}, timeout=30, capture=True)
        with patch.object(deployment.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 30)):
            with self.assertRaisesRegex(deployment.DeploymentError, "TimeoutExpired"):
                deployment.run(["docker", "compose", "config"], environment={}, timeout=30, capture=True)


if __name__ == "__main__":
    unittest.main()

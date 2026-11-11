"""Source/configuration acceptance only; no GPU, conference data or containers."""
import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from submissions.services import pdf_comparison_worker as worker
from submissions.services.rapidocr_comparison import cache, runtime


ROOT = Path(__file__).resolve().parents[2]


class BackendTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CFM_OCR_BACKEND": "cpu", "CFM_RAPIDOCR_THREADS": "1"}))
        self.enterContext(patch.object(runtime, "_validated_cuda_identity", None))
        self.enterContext(patch.object(runtime, "validate_models", return_value=Path("/approved")))
        self.versions = {"rapidocr": "3.10.0", "onnxruntime": "1.31.1"}

        def version(name):
            if name not in self.versions:
                raise importlib.metadata.PackageNotFoundError(name)
            return self.versions[name]

        self.enterContext(patch.object(importlib.metadata, "version", side_effect=version))
        self.enterContext(patch.object(importlib.metadata, "distribution", side_effect=version))
        self.ort = Mock()
        self.ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.recognizer = Mock()
        self.sessions = []
        for load in (self.recognizer._load_det_model, self.recognizer._load_rec_model):
            session = Mock()
            session.get_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            session.get_provider_options.return_value = {"CUDAExecutionProvider": {"device_id": "0"}}
            load.return_value.session.session = session
            self.sessions.append(session)
        self.factory = Mock(return_value=self.recognizer)
        self.enterContext(patch.dict(sys.modules, {
            "onnxruntime": self.ort, "rapidocr": SimpleNamespace(RapidOCR=self.factory),
        }))

    def cuda(self):
        os.environ["CFM_OCR_BACKEND"] = "cuda"
        self.versions = {"rapidocr": "3.10.0", "onnxruntime-gpu": "1.31.1"}

    def test_cpu_defaults_and_parameters_unchanged(self):
        os.environ.pop("CFM_OCR_BACKEND")
        self.assertEqual(runtime.ocr_backend(), "cpu")
        self.assertIs(runtime.build_recognizer(), self.recognizer)
        self.assertEqual(self.factory.call_args.kwargs["params"], {
            "Global.model_root_dir": "/approved", "Global.text_score": 0.0,
            "Global.max_side_len": 4000, "Global.use_cls": False,
            "Det.model_path": "/approved/PP-OCRv6_det_small.onnx",
            "Rec.model_path": "/approved/PP-OCRv6_rec_small.onnx",
            "Det.limit_side_len": 2560, "Det.limit_type": "max",
            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        self.recognizer._load_det_model.assert_not_called()
        self.ort.preload_dlls.assert_not_called()

    def test_invalid_backend_and_wrong_or_mixed_packages_fail(self):
        os.environ["CFM_OCR_BACKEND"] = "auto"
        with self.assertRaisesRegex(ValueError, "must be cpu or cuda"):
            runtime.build_recognizer()
        self.cuda()
        self.versions["onnxruntime"] = "1.31.1"
        with self.assertRaisesRegex(ValueError, "separate environments"):
            runtime.build_recognizer()
        del self.versions["onnxruntime"]
        self.versions["onnxruntime-gpu"] = "1.30.0"
        with self.assertRaisesRegex(ValueError, "onnxruntime-gpu==1.31.1"):
            runtime.build_recognizer()
        self.factory.assert_not_called()

    def test_missing_gpu_wheel_does_not_use_cpu_wheel(self):
        self.cuda()
        del self.versions["onnxruntime-gpu"]
        with self.assertRaises(importlib.metadata.PackageNotFoundError):
            runtime.build_recognizer()
        self.factory.assert_not_called()

    def test_cuda_absent_fails_before_session_construction(self):
        self.cuda()
        self.ort.get_available_providers.return_value = ["CPUExecutionProvider"]
        with self.assertRaisesRegex(ValueError, "CPU fallback is not permitted"):
            runtime.build_recognizer()
        self.factory.assert_not_called()

    def test_each_lazy_session_must_actually_select_cuda(self):
        self.cuda()
        for failed in self.sessions:
            with self.subTest(session=failed):
                failed.get_providers.return_value = ["CPUExecutionProvider"]
                with self.assertRaisesRegex(ValueError, "fell back to CPU"):
                    runtime.build_recognizer()
                with self.assertRaisesRegex(ValueError, "requires validated"):
                    runtime.backend_identity()
                failed.get_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def test_cuda_configuration_and_real_identity_are_retained(self):
        self.cuda()
        device = {"uuid": "device-a", "name": "test device", "driver": 13000}
        with patch.object(runtime, "cuda_device_identity", return_value=device):
            runtime.build_recognizer()
        params = self.factory.call_args.kwargs["params"]
        self.assertIs(params["EngineConfig.onnxruntime.use_cuda"], True)
        self.assertEqual(params["EngineConfig.onnxruntime.cuda_ep_cfg.device_id"], 0)
        self.assertEqual(params["EngineConfig.onnxruntime.cuda_ep_cfg.use_tf32"], 0)
        self.ort.preload_dlls.assert_called_once_with(directory="")
        for session in self.sessions:
            session.disable_fallback.assert_called_once_with()
        identity = runtime.backend_identity()
        self.assertEqual(identity["device"], device)
        self.assertEqual(set(identity["sessions"]), {"det", "rec"})
        identity["device"]["uuid"] = "changed-copy"
        self.assertEqual(runtime.backend_identity()["device"]["uuid"], "device-a")

    def test_failed_cuda_library_load_is_not_hidden(self):
        self.cuda()
        self.ort.preload_dlls.side_effect = OSError("missing CUDA")
        with self.assertRaisesRegex(OSError, "missing CUDA"):
            runtime.build_recognizer()
        self.factory.assert_not_called()


class ResourceLimitTests(SimpleTestCase):
    def setUp(self):
        self.resource = Mock(RLIMIT_CPU=0, RLIMIT_AS=9)
        self.enterContext(patch.dict(sys.modules, {"resource": self.resource}))
        self.enterContext(patch.object(sys, "platform", "linux"))
        self.enterContext(patch.dict(os.environ, {"CFM_OCR_BACKEND": "cpu"}))

    def test_cpu_bounds_unchanged(self):
        worker.resource_limits()
        self.resource.setrlimit.assert_any_call(0, (1920, 1940))
        self.resource.setrlimit.assert_any_call(9, (6 * 1024 ** 3, 6 * 1024 ** 3))

    def test_gpu_requires_finite_v1_or_v2_cgroup_not_address_space(self):
        os.environ["CFM_OCR_BACKEND"] = "cuda"
        for values in ((str(8 * 1024 ** 3), OSError()), (OSError(), str(8 * 1024 ** 3))):
            with self.subTest(values=values), patch.object(Path, "read_text", side_effect=values):
                self.resource.reset_mock()
                worker.resource_limits()
                self.resource.setrlimit.assert_called_once_with(0, (1920, 1940))

    def test_gpu_unbounded_unreadable_or_invalid_memory_fails(self):
        os.environ["CFM_OCR_BACKEND"] = "cuda"
        for values in (("max", str(9223372036854771712)), (OSError(), OSError()), ("0", "-1")):
            with self.subTest(values=values), patch.object(Path, "read_text", side_effect=values):
                with self.assertRaisesRegex(ValueError, "finite cgroup container memory limit"):
                    worker.resource_limits()

    def test_non_linux_gpu_fails_without_changing_limits(self):
        os.environ["CFM_OCR_BACKEND"] = "cuda"
        with patch.object(sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "requires Linux"):
                worker.resource_limits()
        self.resource.setrlimit.assert_not_called()


class WorkerIdentityTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CFM_SLAVE_ROOT": str(self.root), "CFM_OCR_BACKEND": "cpu"}))

    def test_concurrent_creation_and_reopen_are_stable(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            identities = list(pool.map(lambda _: cache.worker_identity(), range(12)))
        self.assertEqual(len(set(identities)), 1)
        with patch.object(os, "link", side_effect=AssertionError("must not rewrite identity")):
            self.assertEqual(cache.worker_identity(), identities[0])
        self.assertEqual([p.name for p in self.root.iterdir()], ["worker-identity"])

    def test_corrupt_or_symlink_identity_is_not_replaced(self):
        identity = self.root / "worker-identity"
        identity.write_text("invalid")
        with self.assertRaises(ValueError):
            cache.worker_identity()
        self.assertEqual(identity.read_text(), "invalid")
        identity.unlink()
        target = self.root / "target"
        target.write_text("a" * 32)
        identity.symlink_to(target)
        with self.assertRaises(ValueError):
            cache.worker_identity()

    def test_fingerprint_survives_container_recreation_but_not_runtime_changes(self):
        with patch.object(cache.platform, "node", return_value="container-a"), patch.object(cache.uuid, "getnode", return_value=2):
            before = cache.runtime_fingerprint()
        with patch.object(cache.platform, "node", return_value="container-b"), patch.object(cache.uuid, "getnode", return_value=4):
            self.assertEqual(cache.runtime_fingerprint(), before)
        with patch.object(runtime, "backend_identity", return_value={"backend": "cuda", "device": "a"}):
            gpu_a = cache.runtime_fingerprint()
        with patch.object(runtime, "backend_identity", return_value={"backend": "cuda", "device": "b"}):
            gpu_b = cache.runtime_fingerprint()
        self.assertEqual(len({before, gpu_a, gpu_b}), 3)
        with patch.object(cache.platform, "machine", return_value="different-cpu"):
            self.assertNotEqual(cache.runtime_fingerprint(), before)
        with patch.object(runtime, "ocr_thread_count", return_value=8):
            self.assertNotEqual(cache.runtime_fingerprint(), before)
        (self.root / "worker-identity").unlink()
        self.assertNotEqual(cache.runtime_fingerprint(), before)


class WorkerEntrypointTests(SimpleTestCase):
    def test_healthcheck_primes_authenticated_loopback_without_changing_files(self):
        from comparison_slave import entrypoint
        with patch.object(entrypoint, "clients_from_file", return_value={"test": "a" * 32}), \
                patch.object(entrypoint.http.client, "HTTPConnection") as connect, \
                patch.dict(os.environ, {"CFM_SLAVE_ROOT": "/worker-data"}):
            response = connect.return_value.getresponse.return_value
            response.status = 200
            for state, expected in (("not_ready", False), ("ready", True), ("busy", True)):
                response.read.return_value = json.dumps({"state": state}).encode()
                self.assertEqual(entrypoint.healthy(), expected)
            connect.assert_called_with("127.0.0.1", 8091, timeout=4)
            connect.return_value.request.assert_called_with("GET", "/v1/health", headers={"Authorization": "Bearer " + "a" * 32})
            response.read.return_value = b"x" * 4097
            self.assertFalse(entrypoint.healthy())
            response.status = 401
            response.read.return_value = b'{"state":"ready"}'
            self.assertFalse(entrypoint.healthy())
        with patch.object(entrypoint, "clients_from_file", side_effect=ValueError("invalid secret")), \
                patch.dict(os.environ, {"CFM_SLAVE_ROOT": "/worker-data"}):
            self.assertFalse(entrypoint.healthy())

    def test_initializes_only_root_permissions_without_replacing_files(self):
        from comparison_slave.entrypoint import initialize
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        root.chmod(0o755)
        child = root / "retained"
        child.write_text("keep")
        child.chmod(0o640)
        source = root / "clients.source.json"
        source.write_text(json.dumps({"test": "a" * 32}))
        source.chmod(0o600)
        # Permission initialization is independent of provisioned OCR models;
        # actual startup/model loading has separate deployment acceptance.
        self.assertEqual(initialize(root, source), root / "clients.effective.json")
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(child.stat().st_mode & 0o777, 0o640)
        self.assertEqual(child.read_text(), "keep")
        self.assertEqual((root / "clients.effective.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((root / "clients.effective.json").read_bytes(), source.read_bytes())

    def test_symlink_root_fails_without_changing_target(self):
        from comparison_slave.entrypoint import initialize
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        target = root / "target"
        target.mkdir(mode=0o755)
        link = root / "worker"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            initialize(link, root / "clients")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_public_symlink_oversized_and_invalid_clients_fail_without_overwriting(self):
        from comparison_slave.entrypoint import initialize
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        source = root / "clients.source.json"
        effective = root / "clients.effective.json"
        effective.write_text("retained")
        for data, mode in ((json.dumps({"test": "a" * 32}), 0o644), ("x" * 65537, 0o600), ("{}", 0o600)):
            source.write_text(data)
            source.chmod(mode)
            with self.subTest(mode=mode, size=len(data)), self.assertRaises(ValueError):
                initialize(root, source)
            self.assertEqual(effective.read_text(), "retained")
            self.assertFalse(list(root.glob(".clients-*")))
        link = root / "link"
        link.symlink_to(source)
        with self.assertRaises(ValueError):
            initialize(root, link)


class DeploymentUpdaterTests(SimpleTestCase):
    def test_transport_network_and_tokens_survive_generated_environment(self):
        from scripts import update_docker_instances as updater
        import docker_instance_tools as helpers
        from submissions.tests.test_docker_data_tools import make_container
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        values = {
            "CFM_COMPARISON_TRANSPORT": "service", "CFM_COMPARISON_NETWORK": "shared-ocr",
            "CFM_COMPARISON_LOCAL_URL": "http://comparison-slave:8091",
            "CFM_COMPARISON_LOCAL_TOKEN": "local-test-" + "a" * 32,
            "CFM_COMPARISON_REMOTE_URL": "https://ocr.example.test",
            "CFM_COMPARISON_REMOTE_TOKEN": "remote-test-" + "b" * 32,
        }
        container = make_container(root, project="sms-test", mount={
            "Type": "bind", "Destination": "/app/data", "Source": str(root / "data"),
        })
        container["Config"]["Env"].extend(f"{key}={value}" for key, value in values.items())
        instance = helpers.instance_from_container(container, "sms-test", root)
        env = helpers.compose_env(instance)
        for key, value in values.items():
            self.assertEqual(env[key], value)
            self.assertIn(key, updater.COMPOSE_INPUT_KEYS)
        with helpers.temporary_env_file(env) as path:
            content = Path(path).read_text()
            for key, value in values.items():
                self.assertIn(helpers.format_env_line(key, value), content)
        output = io.StringIO()
        plan = {"project": "sms-test", "env_path": None, "action": "update",
                "desired": instance, "changes": {k: ("old-" + v, v) for k, v in values.items()},
                "force_proxy_recreate": False}
        with redirect_stdout(output):
            updater.print_update_plans([plan], create_missing=False)
            updater.rebuild_instance(instance, root, dry_run=True, env_values=env)
        for key in ("CFM_COMPARISON_LOCAL_TOKEN", "CFM_COMPARISON_REMOTE_TOKEN"):
            self.assertIn(key, helpers.SENSITIVE_ENV_KEYS)
            self.assertNotIn(values[key], output.getvalue())
            self.assertEqual(updater.display_env_value(key, values[key]), "***")

    def test_slave_environment_files_are_never_discovered_as_conferences(self):
        from scripts import update_docker_instances as updater
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in (".env.slave", ".env.slave.example", ".env.slave.gpu"):
            (root / name).write_text("CFM_SLAVE_PROJECT_NAME=worker-only\n")
        conference = root / ".env.conference-a"
        conference.write_text("COMPOSE_PROJECT_NAME=conference-a\n")
        self.assertEqual(updater.discover_env_specs(root), [{"path": conference, "project": "conference-a"}])


@skipUnless(shutil.which("docker"), "Docker CLI required for read-only Compose config validation")
class DeploymentConfigTests(SimpleTestCase):
    def config(self, *files, env_file=".env.slave.example"):
        command = ["docker", "compose", "--env-file", env_file, "-p", "comparison-config-test"]
        for file in files:
            command.extend(["-f", file])
        command.extend(["config", "--format", "json"])
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_standalone_cpu_has_no_conference_dependency_or_mount(self):
        config = self.config("compose.comparison-slave.yml")
        self.assertEqual(set(config["services"]), {"comparison-slave"})
        service = config["services"]["comparison-slave"]
        self.assertEqual(service["build"]["target"], "cpu")
        self.assertEqual(service["environment"]["CFM_OCR_BACKEND"], "cpu")
        self.assertEqual(service["environment"]["CFM_SLAVE_CLIENTS_FILE"], "/run/secrets/clients.json")
        self.assertGreater(int(service["mem_limit"]), 0)
        self.assertEqual(service["mem_limit"], service["memswap_limit"])
        self.assertEqual({v["target"] for v in service["volumes"]}, {"/worker-data", "/opt/rapidocr-models"})
        self.assertNotIn("depends_on", service)
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "python", "-m", "comparison_slave.entrypoint", "--healthcheck"])
        self.assertEqual(service["healthcheck"]["interval"], "30s")

    def test_gpu_override_keeps_volume_and_finite_memory(self):
        base = self.config("compose.comparison-slave.yml")["services"]["comparison-slave"]
        gpu = self.config("compose.comparison-slave.yml", "compose.comparison-slave.gpu.yml")["services"]["comparison-slave"]
        self.assertEqual(gpu["build"]["target"], "gpu")
        self.assertEqual(gpu["environment"]["CFM_OCR_BACKEND"], "cuda")
        self.assertEqual(gpu["volumes"], base["volumes"])
        self.assertEqual(gpu["mem_limit"], base["mem_limit"])
        self.assertEqual(gpu["deploy"]["resources"]["reservations"]["devices"][0]["driver"], "nvidia")

    def test_conference_service_urls_and_credentials_are_separate(self):
        config = self.config("docker-compose.yml", env_file=".env.example")
        self.assertNotIn("comparison-slave", config["services"])
        env = config["services"]["web"]["environment"]
        self.assertEqual(env["CFM_COMPARISON_TRANSPORT"], "service")
        self.assertEqual(env["CFM_COMPARISON_LOCAL_URL"], "http://comparison-slave:8091")
        self.assertEqual(set(config["services"]["web"]["networks"]), {"default", "comparison"})
        self.assertEqual(set(config["services"]["proxy"]["networks"]), {"default"})
        self.assertTrue(config["networks"]["comparison"]["external"])
        slave = self.config("compose.comparison-slave.yml")
        self.assertEqual(config["networks"]["comparison"]["name"], slave["networks"]["comparison"]["name"])
        self.assertIn("comparison-slave", slave["services"]["comparison-slave"]["networks"]["comparison"]["aliases"])
        for key in ("LOCAL_TOKEN", "REMOTE_URL", "REMOTE_TOKEN"):
            self.assertEqual(env["CFM_COMPARISON_" + key], "")

    def test_image_stages_do_not_install_both_ort_packages(self):
        source = (ROOT / "Dockerfile.comparison-slave").read_text()
        base, stages = source.split("FROM base AS gpu")
        gpu, cpu = stages.split("FROM base AS cpu")
        self.assertNotIn("onnxruntime==", base + gpu)
        self.assertNotIn("requirements-slave-gpu", cpu)
        self.assertIn("onnxruntime==1.31.1", cpu)
        self.assertIn("onnxruntime-gpu[cuda,cudnn]==1.31.1", (ROOT / "requirements-slave-gpu.txt").read_text())
        self.assertNotIn("COPY . ", source)
        self.assertNotIn("sms-entrypoint", source)
        self.assertIn('"comparison_slave.entrypoint"', source)
        self.assertIn('"comparison_slave.server:application"', source)

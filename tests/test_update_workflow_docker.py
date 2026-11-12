"""Opt-in real updater acceptance in a disposable checkout/projects/volumes."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

from scripts import docker_instance_tools as tools
from scripts import deploy


@unittest.skipUnless(os.environ.get("CFM_UPDATE_DOCKER_ACCEPTANCE") == "1", "Opt-in isolated Docker updater acceptance")
class UpdateDockerAcceptance(unittest.TestCase):
    def test_two_existing_conferences_update_through_both_entrypoints_without_state_changes(self):
        import yaml
        source = Path(__file__).resolve().parents[1]
        models = source / "runtime/rapidocr-models"
        self.assertTrue((models / "PP-OCRv6_rec_small.onnx").is_file())
        prefix = "cfm-uqa-" + uuid.uuid4().hex[:10]
        projects = [prefix + "-a", prefix + "-b"]
        worker_project, network = prefix + "-worker", prefix + "-net"
        def port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]
        ports = [port(), port(), port()]
        self.assertEqual(len(set(ports)), 3)
        with tempfile.TemporaryDirectory(prefix="cfm-update-acceptance-") as directory:
            root = Path(directory).resolve() / "checkout"
            shutil.copytree(source, root, ignore=shutil.ignore_patterns(
                ".git", ".venv", "data", "runtime", "output", ".env", ".env.*",
                "__pycache__", ".playwright-cli", "*.log"))
            for filename, service, image in (("docker-compose.yml", "web", prefix + "-app"),
                    ("compose.comparison-slave.yml", "comparison-slave", prefix + "-cpu")):
                path = root / filename
                document = yaml.safe_load(path.read_text())
                document["services"][service]["image"] = image + ":test"
                path.write_text(yaml.safe_dump(document, sort_keys=False))
            worker_env = root / "runtime/comparison-slave/worker.env"
            worker_env.parent.mkdir(parents=True, mode=0o700)
            worker_values = {"CFM_SLAVE_PROJECT_NAME": worker_project, "CFM_COMPARISON_NETWORK": network,
                "CFM_SLAVE_BIND_HOST": "127.0.0.1", "CFM_SLAVE_PORT": str(ports[0]),
                "CFM_SLAVE_MODEL_DIR": str(models), "CFM_SLAVE_MEMORY_LIMIT": "8g",
                "CFM_RAPIDOCR_THREADS": "2", "CFM_RAPIDOCR_PAGES": "1"}
            worker_template = worker_env.parent / "worker.template"
            worker_template.write_text("".join(tools.format_env_line(k, v) for k, v in worker_values.items()))
            worker_template.chmod(0o600)
            envs = []
            for index, project in enumerate(projects):
                env = root / f".env.test-{index}"
                values = {"COMPOSE_PROJECT_NAME": project, "SMS_PORT": str(ports[index + 1]),
                    "SMS_BIND_HOST": "127.0.0.1", "SMS_DATA_DIR": "./runtime/" + project,
                    "SMS_SECRET_KEY": uuid.uuid4().hex + "$${LITERAL} # ' quote\" slash\\", "SMS_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
                    "CFM_COMPARISON_TRANSPORT": "service", "CFM_COMPARISON_NETWORK": network}
                env.write_text("# Disposable updater acceptance\n" + "".join(tools.format_env_line(k, v) for k, v in values.items()))
                env.chmod(0o600)
                envs.append(env)
            environment = {k: v for k, v in os.environ.items() if not k.startswith(("SMS_", "CFM_", "COMPOSE_"))}
            def run(command, *, timeout=1200, check=True):
                result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, timeout=timeout)
                if check:
                    self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-10000:])
                return result.stdout
            def compose(index, *args):
                return ["docker", "compose", "-f", "docker-compose.yml", "--env-file", str(envs[index]),
                        "-p", projects[index], *args]
            snapshot_code = (
                "import sqlite3,json,hashlib; from pathlib import Path; "
                "c=sqlite3.connect('file:/app/data/db.sqlite3?mode=ro',uri=True); "
                "rows=list(c.iterdump()); "
                "files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path('/app/data/media').rglob('*')) if p.is_file()}; "
                "print(hashlib.sha256(json.dumps([rows,files],sort_keys=True).encode()).hexdigest())"
            )
            def snapshot(index):
                return run(compose(index, "exec", "-T", "web", "python", "-c", snapshot_code)).strip()
            try:
                initialized = run([sys.executable, "scripts/deploy.py", "worker", "--init", "--template", str(worker_template),
                                   "--client", projects[0], "--client", projects[1]])
                self.assertEqual(initialized.count("Worker deployment is healthy."), 1)
                output = run([sys.executable, "scripts/update_docker_instances.py", "--create-missing"])
                self.assertEqual(output.count("Worker deployment is healthy."), 0)
                self.assertEqual(output.count("verified: proxy config, static asset, and same-origin CSRF POST"), 2)
                clients = json.loads((worker_env.parent / "clients.json").read_text())
                self.assertEqual(set(clients), set(projects))
                self.assertEqual(len(set(clients.values())), 2)
                for index in range(2):
                    seed = ("from submissions.tests.factories import create_master_paper,create_final_submission; "
                            "from pathlib import Path; import fitz; "
                            "create_master_paper(paper_id='P001',title='Generated updater fixture',notes='Keep this review note'); "
                            "d=fitz.open(); d.new_page().insert_text((72,72),'Original generated fixture'); "
                            "s=create_final_submission(Path('/app/data'),pdf_bytes=d.tobytes()); "
                            "d[0].insert_text((72,110),'Corrected generated fixture'); "
                            "p=Path('/app/data/media/qa-corrected.pdf'); p.write_bytes(d.tobytes()); "
                            "s.formatted_pdf_file='qa-corrected.pdf'; s.save(update_fields=['formatted_pdf_file'])")
                    run(compose(index, "exec", "-T", "web", "python", "manage.py", "shell", "-c", seed))
                before = [snapshot(i) for i in range(2)]
                env_before = [p.read_bytes() for p in envs]
                worker_before = (worker_env.parent / "clients.json").read_bytes()
                worker_env_before = worker_env.read_bytes()
                # Missing state must fail read-only, even with both conferences
                # running and recoverable values in their live environments.
                for missing in (worker_env, worker_env.parent / "clients.json"):
                    saved = missing.read_bytes()
                    audit_before = (root / "runtime/deployment.log").read_bytes()
                    try:
                        missing.unlink()
                        for command in (["scripts/update_docker_instances.py"], ["scripts/deploy.py", "update"],
                                        ["scripts/rebuild_docker_instances.py"], ["scripts/start_comparison_slave.py"]):
                            rejected = subprocess.run([sys.executable, *command], cwd=root,
                                env=environment, text=True, capture_output=True, timeout=120)
                            self.assertNotEqual(rejected.returncode, 0)
                            self.assertFalse(missing.exists())
                            self.assertNotIn("Worker deployment is healthy.", rejected.stdout)
                            self.assertEqual([snapshot(i) for i in range(2)], before)
                            self.assertEqual((root / "runtime/deployment.log").read_bytes(), audit_before)
                    finally:
                        missing.write_bytes(saved)
                        missing.chmod(0o600)
                for entry in (["scripts/update_docker_instances.py"], ["scripts/deploy.py", "update"]):
                    preview = run([sys.executable, *entry, "--dry-run"])
                    for project in projects:
                        self.assertIn("Project: " + project, preview)
                    self.assertIn("Live HTTP/runtime checks", preview)
                    for token in clients.values():
                        self.assertNotIn(token, preview)
                    self.assertEqual([snapshot(i) for i in range(2)], before)
                run([sys.executable, "scripts/update_docker_instances.py"])
                self.assertEqual([snapshot(i) for i in range(2)], before)
                # The compatibility entry point must have the same selected-project scope.
                other_id = run(compose(1, "ps", "-q", "web")).strip()
                run([sys.executable, "scripts/deploy.py", "update", "--project", projects[0]])
                self.assertEqual(run(compose(1, "ps", "-q", "web")).strip(), other_id)
                self.assertEqual([snapshot(i) for i in range(2)], before)
                self.assertEqual([p.read_bytes() for p in envs], env_before)
                self.assertEqual((worker_env.parent / "clients.json").read_bytes(), worker_before)
                self.assertEqual(worker_env.read_bytes(), worker_env_before)
                # The historical worker CLI uses the same update identity/auth;
                # a stopped worker is still an update, not initialization.
                worker_compose = ["docker", "compose", "--env-file", str(worker_env), "-f", "compose.comparison-slave.yml",
                                  "-p", worker_project]
                secret_code = "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/worker-data/signing.secret').read_bytes()).hexdigest())"
                signing_before = run([*worker_compose, "exec", "-T", "comparison-slave", "python", "-c", secret_code])
                run([*worker_compose, "stop", "comparison-slave"])
                run([sys.executable, "scripts/start_comparison_slave.py", "--env-file", str(worker_env)])
                self.assertEqual(run([*worker_compose, "exec", "-T", "comparison-slave", "python", "-c", secret_code]), signing_before)
                self.assertEqual((worker_env.parent / "clients.json").read_bytes(), worker_before)
                self.assertEqual(worker_env.read_bytes(), worker_env_before)
                self.assertEqual([snapshot(i) for i in range(2)], before)
                # A minimal legacy env must not rotate the live signing key or
                # discard the worker token when the updater fills prerequisites.
                retained = deploy.EnvFile(envs[0]).values
                omitted = {"SMS_SECRET_KEY", "CFM_COMPARISON_LOCAL_URL", "CFM_COMPARISON_LOCAL_TOKEN"}
                envs[0].write_text("".join(line for line in envs[0].read_text().splitlines(keepends=True)
                    if line.split("=", 1)[0] not in omitted))
                run([sys.executable, "scripts/update_docker_instances.py", "--project", projects[0]])
                restored = deploy.EnvFile(envs[0]).values
                for key in omitted:
                    self.assertEqual(restored[key], retained[key])
                self.assertEqual([snapshot(i) for i in range(2)], before)
                self.assertEqual((worker_env.parent / "clients.json").read_bytes(), worker_before)
                self.assertEqual(worker_env.read_bytes(), worker_env_before)
                worker_settings = run(["docker", "compose", "--env-file", str(worker_env),
                    "-f", "compose.comparison-slave.yml", "-p", worker_project, "exec", "-T",
                    "comparison-slave", "python", "-c", "import os; print(os.environ['CFM_RAPIDOCR_PAGES'], os.environ['CFM_RAPIDOCR_THREADS'])"])
                self.assertEqual(worker_settings.strip(), "1 2")
                print("Isolated updater acceptance: 2 conferences, shared CPU worker, both entrypoints, selected scope, env/token, database/review state and Original/Corrected bytes preserved.")
            finally:
                for index in range(2):
                    run(compose(index, "down", "--remove-orphans"), timeout=120, check=False)
                run(["docker", "compose", "--env-file", str(worker_env), "-f", "compose.comparison-slave.yml",
                     "-p", worker_project, "down", "--remove-orphans"], timeout=120, check=False)
                for project in [*projects, worker_project]:
                    volumes = run(["docker", "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project], check=False).split()
                    if volumes:
                        run(["docker", "volume", "rm", *volumes], check=False)
                run(["docker", "image", "rm", prefix + "-app:test", prefix + "-cpu:test"], check=False)

"""Opt-in real Compose/container round-trip; no conference volumes or services.

Build the app image, then set CFM_HEADER_TEST_IMAGE to its disposable image tag.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from scripts import docker_instance_tools as tools
from scripts import update_docker_instances as updater


@unittest.skipUnless(os.environ.get("CFM_HEADER_TEST_IMAGE"), "Set CFM_HEADER_TEST_IMAGE for disposable Docker acceptance")
class ComposeHeaderRoundTripTests(unittest.TestCase):
    def test_original_and_update_env_deliver_identical_headers_to_linux_container(self):
        root = Path(__file__).resolve().parents[1]
        project = "cfm-header-qa-" + uuid.uuid4().hex[:12]
        headers = {"CF-Access-Client-Id": "fake-id", "X-API-Key": "${UNDEFINED_HEADER_TEST}$ $$ # ' quote\" slash\\"}
        raw = json.dumps(headers)
        expected = hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest()
        code = (
            "import hashlib,json,os; "
            "from conference_final_manager.comparison_config import parse_remote_headers; "
            "v=parse_remote_headers(os.environ['CFM_COMPARISON_REMOTE_HEADERS'],url='https://worker.example'); "
            f"assert hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest() == {expected!r}; "
            "print('Container header values verified')"
        )
        with tempfile.TemporaryDirectory(prefix="cfm-header-docker-") as directory:
            compose = Path(directory) / "compose.json"
            def run_env(env_file):
                result = subprocess.run(tools.compose_command(root, env_file, project, "config", "--format", "json"),
                    cwd=root, text=True, capture_output=True, check=True, timeout=30)
                environment = json.loads(result.stdout)["services"]["web"]["environment"]
                # Keep Compose's own escaped serialization for this disposable
                # service; no conference mounts, published ports or entrypoint.
                compose.write_text(json.dumps({"services": {"check": {
                    "image": os.environ["CFM_HEADER_TEST_IMAGE"], "environment": environment,
                    "entrypoint": ["python"], "command": ["-c", code],
                }}}))
                command = ["docker", "compose", "--env-file", str(env_file), "-f", str(compose), "-p", project]
                try:
                    result = subprocess.run([*command, "run", "--rm", "--no-deps", "-T", "check"],
                        text=True, capture_output=True, check=True, timeout=90)
                    self.assertIn("Container header values verified", result.stdout)
                finally:
                    subprocess.run([*command, "down", "--remove-orphans"], text=True, capture_output=True, check=True, timeout=30)

            with tools.temporary_env_file({
                "CFM_COMPARISON_REMOTE_URL": "https://worker.example",
                "CFM_COMPARISON_REMOTE_HEADERS": raw,
            }) as env_file:
                run_env(env_file)
                desired = updater.planned_instance_from_env(root, env_file, project, bind_override=False)
            with tools.temporary_env_file(desired["env_values"]) as updated:
                run_env(updated)

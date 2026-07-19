import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.conf import settings as django_settings


SCRIPT_DIR = Path(django_settings.BASE_DIR) / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backup_docker_instances
import docker_data_transfer
import docker_instance_tools
import migrate_docker_data_volumes


class DockerDataTransferTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()

    def test_sync_tree_creates_exact_verified_raw_data_mirror(self):
        database = sqlite3.connect(self.source / "db.sqlite3")
        database.execute("CREATE TABLE papers (paper_id TEXT PRIMARY KEY)")
        database.execute("INSERT INTO papers VALUES ('P001')")
        database.commit()
        database.close()
        (self.source / "media").mkdir()
        (self.source / "media" / "paper.pdf").write_bytes(b"publication pdf")
        (self.destination / "stale.txt").write_text("stale", encoding="utf-8")

        result = docker_data_transfer.sync_tree(
            self.source,
            self.destination,
            verify_content=True,
            baseline_manifest=None,
            tolerate_source_changes=False,
        )
        verification = docker_data_transfer.verify_data_directory(
            self.destination,
            "db.sqlite3",
        )

        self.assertFalse((self.destination / "stale.txt").exists())
        self.assertEqual(
            (self.destination / "media" / "paper.pdf").read_bytes(),
            b"publication pdf",
        )
        self.assertGreaterEqual(result["copied_files"], 2)
        self.assertEqual(verification["integrity_check"], "ok")
        self.assertEqual(verification["file_count"], 2)

    def test_verified_sync_detects_same_size_same_mtime_content_change(self):
        source_file = self.source / "paper.pdf"
        destination_file = self.destination / "paper.pdf"
        source_file.write_bytes(b"new bytes")
        destination_file.write_bytes(b"old bytes")
        timestamp = 1_700_000_000
        os.utime(source_file, (timestamp, timestamp))
        os.utime(destination_file, (timestamp, timestamp))

        docker_data_transfer.sync_tree(
            self.source,
            self.destination,
            verify_content=True,
            baseline_manifest=None,
            tolerate_source_changes=False,
        )

        self.assertEqual(destination_file.read_bytes(), b"new bytes")


class DockerInstanceDiscoveryTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_named_volume_instance_uses_host_backup_label(self):
        container = make_container(
            self.root,
            project="sms-conf-a",
            mount={
                "Type": "volume",
                "Name": "sms-conf-a_sms_data",
                "Source": "/var/lib/docker/volumes/sms-conf-a_sms_data/_data",
                "Destination": "/app/data",
            },
            backup_dir="./runtime/conference-a",
        )

        instances = docker_instance_tools.matching_instances(
            [container],
            self.root,
            set(),
        )

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["mount_type"], "volume")
        self.assertEqual(instances[0]["volume_name"], "sms-conf-a_sms_data")
        self.assertEqual(
            instances[0]["sms_data_dir"],
            str((self.root / "runtime" / "conference-a").resolve()),
        )

    def test_bind_instance_remains_discoverable_for_migration(self):
        source = self.root / "runtime" / "conference-a"
        container = make_container(
            self.root,
            project="sms-conf-a",
            mount={
                "Type": "bind",
                "Source": str(source),
                "Destination": "/app/data",
            },
        )

        instance = docker_instance_tools.matching_instances(
            [container],
            self.root,
            set(),
        )[0]

        self.assertEqual(instance["mount_type"], "bind")
        self.assertEqual(instance["sms_data_dir"], str(source.resolve()))

    def test_migration_refuses_named_volume_owned_by_another_project(self):
        inspection = subprocess.CompletedProcess(
            ["docker", "volume", "inspect"],
            0,
            stdout=(
                '[{"Labels":{"com.docker.compose.project":"other",'
                '"com.docker.compose.volume":"sms_data"}}]'
            ),
            stderr="",
        )

        with patch.object(docker_instance_tools, "run", return_value=inspection):
            with self.assertRaisesRegex(RuntimeError, "unexpected ownership"):
                docker_instance_tools.ensure_compose_volume(
                    "sms-conf-a",
                    "sms-conf-a_sms_data",
                )


class DockerBackupOrchestrationTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_backup_failure_restarts_previously_running_container(self):
        instance = {
            "id": "container-id",
            "project": "sms-conf-a",
            "name": "sms-conf-a-web-1",
            "image": "conference-final-manager:local",
            "running": True,
            "mount_type": "volume",
            "volume_name": "sms-conf-a_sms_data",
            "sms_data_dir": str(self.root / "conference-a"),
        }

        with (
            patch.object(
                backup_docker_instances,
                "transfer_data",
                side_effect=[
                    {"manifest": {}},
                    RuntimeError("final sync failed"),
                ],
            ),
            patch.object(backup_docker_instances, "stop_container") as stop,
            patch.object(backup_docker_instances, "start_container") as start,
            patch.object(backup_docker_instances, "wait_until_running") as wait,
            patch.object(backup_docker_instances, "append_history"),
        ):
            with self.assertRaisesRegex(RuntimeError, "final sync failed"):
                backup_docker_instances.backup_instance(
                    instance,
                    self.root,
                    dry_run=False,
                    stop_timeout=30,
                )

        stop.assert_called_once_with(instance, 30)
        start.assert_called_once_with(instance)
        wait.assert_called_once_with("container-id")

    def test_backup_scans_all_projects_and_processes_each_named_volume(self):
        volume_a = make_container(
            self.root,
            project="sms-conf-a",
            mount={
                "Type": "volume",
                "Name": "sms-conf-a_sms_data",
                "Source": "/volumes/a",
                "Destination": "/app/data",
            },
            backup_dir="./runtime/conference-a",
        )
        volume_b = make_container(
            self.root,
            project="sms-conf-b",
            mount={
                "Type": "volume",
                "Name": "sms-conf-b_sms_data",
                "Source": "/volumes/b",
                "Destination": "/app/data",
            },
            backup_dir="./runtime/conference-b",
        )
        bind_instance = make_container(
            self.root,
            project="sms-conf-legacy",
            mount={
                "Type": "bind",
                "Source": str(self.root / "runtime" / "legacy"),
                "Destination": "/app/data",
            },
        )

        with (
            patch.object(
                backup_docker_instances,
                "inspect_compose_web_containers",
                return_value=[volume_a, bind_instance, volume_b],
            ),
            patch.object(backup_docker_instances, "backup_instance") as backup,
        ):
            result = backup_docker_instances.run_backups(
                self.root,
                selected_projects=set(),
                dry_run=False,
                stop_timeout=30,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0]["project"] for call in backup.call_args_list],
            ["sms-conf-a", "sms-conf-b"],
        )

    def test_promote_staging_keeps_previous_complete_mirror(self):
        target = self.root / "conference-a"
        previous = self.root / "conference-a.backup-previous"
        target.mkdir()
        previous.mkdir()
        (target / "state.txt").write_text("current", encoding="utf-8")
        (previous / "state.txt").write_text("next", encoding="utf-8")

        backup_docker_instances.promote_staging(previous, target)

        self.assertEqual(
            (target / "state.txt").read_text(encoding="utf-8"),
            "next",
        )
        self.assertEqual(
            (previous / "state.txt").read_text(encoding="utf-8"),
            "current",
        )


class DockerRebuildCompatibilityTests(TestCase):
    def test_rebuild_script_preserves_named_volume_host_mirror_setting(self):
        script_path = SCRIPT_DIR / "rebuild_docker_instances.py"
        spec = importlib.util.spec_from_file_location("docker_rebuild_named", script_path)
        docker_rebuild = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(docker_rebuild)
        root = Path(django_settings.BASE_DIR)
        container = make_container(
            root,
            project="sms-conf-a",
            mount={
                "Type": "volume",
                "Name": "sms-conf-a_sms_data",
                "Source": "/var/lib/docker/volumes/sms-conf-a_sms_data/_data",
                "Destination": "/app/data",
            },
            backup_dir="./runtime/conference-a",
        )

        instance = docker_rebuild.matching_instances(
            [container],
            root,
            set(),
        )[0]

        self.assertEqual(instance["mount_type"], "volume")
        self.assertEqual(instance["volume_name"], "sms-conf-a_sms_data")
        self.assertEqual(instance["sms_data_dir"], "./runtime/conference-a")


class DockerMigrationOrchestrationTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_migration_scans_all_projects_and_only_migrates_bind_mounts(self):
        bind_a = make_container(
            self.root,
            project="sms-conf-a",
            mount={
                "Type": "bind",
                "Source": str(self.root / "runtime" / "conference-a"),
                "Destination": "/app/data",
            },
        )
        bind_b = make_container(
            self.root,
            project="sms-conf-b",
            mount={
                "Type": "bind",
                "Source": str(self.root / "runtime" / "conference-b"),
                "Destination": "/app/data",
            },
        )
        volume = make_container(
            self.root,
            project="sms-conf-volume",
            mount={
                "Type": "volume",
                "Name": "sms-conf-volume_sms_data",
                "Source": "/volumes/current",
                "Destination": "/app/data",
            },
            backup_dir="./runtime/conference-volume",
        )

        with (
            patch.object(
                migrate_docker_data_volumes,
                "inspect_compose_web_containers",
                return_value=[bind_b, volume, bind_a],
            ),
            patch.object(migrate_docker_data_volumes, "migrate_instance") as migrate,
        ):
            result = migrate_docker_data_volumes.run_migrations(
                self.root,
                selected_projects=set(),
                dry_run=False,
                stop_timeout=30,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0]["project"] for call in migrate.call_args_list],
            ["sms-conf-a", "sms-conf-b"],
        )


def make_container(
    root: Path,
    *,
    project: str,
    mount: dict,
    backup_dir: str = "",
) -> dict:
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.project.working_dir": str(root),
        "com.docker.compose.service": "web",
    }
    if backup_dir:
        labels[docker_instance_tools.BACKUP_DIR_LABEL] = backup_dir
    return {
        "Id": f"{project}-container-id",
        "Name": f"/{project}-web-1",
        "Config": {
            "Image": "conference-final-manager:local",
            "Labels": labels,
            "Env": [
                "SMS_SECRET_KEY=secret",
                "SMS_DEBUG=1",
                "SMS_ALLOWED_HOSTS=127.0.0.1,localhost",
            ],
        },
        "HostConfig": {
            "PortBindings": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000"}]
            }
        },
        "Mounts": [
            mount,
            {"Type": "bind", "Destination": "/app", "Source": str(root)},
        ],
        "State": {"Running": True},
    }

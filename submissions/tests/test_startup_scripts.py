"""Native launcher boundaries: disposable shell mocks and Windows static checks."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from unittest import skipUnless

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[2]
PYTHON_STUB = r'''#!/bin/sh
case "$*" in
    "-c "*)
        case "$2" in
            *"sys.prefix"*) stage=venv_version ;;
            *) stage=bootstrap_version ;;
        esac ;;
    "-m venv .venv") stage=create_venv ;;
    "-m pip --version") stage=pip_available ;;
    "-m pip install --upgrade pip") stage=pip_upgrade ;;
    "-m pip install -r requirements.txt") stage=dependencies ;;
    "-m pip check") stage=pip_check ;;
    "scripts/check_comparison_runtime.py") stage=preflight ;;
    "manage.py check") stage=system_check ;;
    "manage.py migrate") stage=migrate ;;
    "manage.py runserver "*) stage=runserver ;;
    *) echo "Unexpected interpreter command: $*" >&2; exit 98 ;;
esac
printf '%s\n' "$stage" >> "$STARTUP_TEST_LOG"
if [ "${STUB_FAIL:-}" = "$stage" ]; then
    echo "Simulated $stage failure" >&2
    exit 23
fi
if [ "$stage" = create_venv ]; then
    mkdir -p .venv/bin
    cp "$0" .venv/bin/python
fi
if [ "$stage" = preflight ]; then
    printf '%s\n' "${CFM_COMPARISON_TRANSPORT:-}" "${CFM_RAPIDOCR_MODEL_DIR:-}" > "$STARTUP_CONFIG_LOG"
fi
if [ "$stage" = runserver ]; then
    printf '%s\n' "$3" > "$STARTUP_SERVER_LOG"
fi
'''


@skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX launcher fixtures require Bash")
class LocalStartupScriptTests(SimpleTestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.temporary / "checkout with spaces"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/start_local.sh", self.root / "scripts/start_local.sh")
        shutil.copy2(ROOT / "start.command", self.root / "start.command")
        (self.root / "scripts/check_comparison_runtime.py").write_text("# Shared checker is mocked by the interpreter fixture.\n")
        (self.root / "requirements.txt").write_text("# No packages are installed by this fixture.\n")
        (self.root / "manage.py").write_text("# No real Django command runs in this fixture.\n")
        self.bin = self.temporary / "bin"
        self.bin.mkdir()
        self.bootstrap = self.bin / "bootstrap python"
        self.executable(self.bootstrap, PYTHON_STUB)
        self.executable(self.bin / "python", "#!/bin/sh\necho 'Global python must not be used' >&2\nexit 99\n")
        self.executable(self.bin / "uname", '#!/bin/sh\nprintf "%s\\n" "${STUB_OS:-Linux}"\n')
        self.executable(self.bin / "sleep", "#!/bin/sh\nexit 0\n")
        self.executable(self.bin / "open", '#!/bin/sh\nprintf "%s\\n" "$*" > "$STARTUP_BROWSER_LOG"\n')
        self.log = self.temporary / "calls"
        self.browser = self.temporary / "browser"
        self.server = self.temporary / "server"
        self.config = self.temporary / "config"
        self.environment = {k: v for k, v in os.environ.items()
                            if not k.startswith(("CFM_", "DJANGO_", "STUB_", "STARTUP_"))
                            and k not in {"PYTHON_BIN", "OPEN_BROWSER", "BASH_ENV", "ENV"}}
        self.environment.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "PYTHON_BIN": str(self.bootstrap), "STARTUP_TEST_LOG": str(self.log),
            "STARTUP_BROWSER_LOG": str(self.browser), "STARTUP_SERVER_LOG": str(self.server),
            "STARTUP_CONFIG_LOG": str(self.config),
        })

    def executable(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        path.chmod(0o755)

    def existing_venv(self):
        self.executable(self.root / ".venv/bin/python", PYTHON_STUB)

    def launch(self, *, wrapper=False, **environment):
        script = self.root / ("start.command" if wrapper else "scripts/start_local.sh")
        return subprocess.run(["bash", str(script)], cwd=self.temporary,
                              env={**self.environment, **environment}, capture_output=True,
                              text=True, timeout=10)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_no_application_start(self):
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "db.sqlite3").exists())
        self.assertFalse(self.browser.exists())
        self.assertFalse(self.server.exists())
        self.assertNotIn("migrate", self.calls())

    def test_fresh_setup_validates_before_application_writes(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["bootstrap_version", "create_venv", "venv_version",
                                       "pip_available", "pip_upgrade", "dependencies", "pip_check",
                                       "preflight", "system_check", "migrate", "runserver"])
        self.assertEqual({p.name for p in (self.root / "data").iterdir()},
                         {"reports", "extraction_results", "plagiarism_reports", "media"})
        self.assertEqual(self.server.read_text().strip(), "127.0.0.1:8000")
        self.assertFalse(self.browser.exists())

    def test_valid_existing_venv_does_not_require_global_python_or_activation(self):
        self.existing_venv()
        result = self.launch(PYTHON_BIN="/missing/bootstrap-python")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("bootstrap_version", self.calls())
        self.assertNotIn("create_venv", self.calls())
        self.assertFalse((self.root / ".venv/bin/activate").exists())

    def test_missing_bootstrap_python_has_actionable_failure(self):
        result = self.launch(PYTHON_BIN="/missing/bootstrap-python")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.12", result.stdout)
        self.assertIn("PYTHON_BIN", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assert_no_application_start()

    def test_old_bootstrap_python_cannot_create_venv(self):
        result = self.launch(STUB_FAIL="bootstrap_version")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.12", result.stdout)
        self.assertEqual(self.calls(), ["bootstrap_version"])
        self.assertFalse((self.root / ".venv").exists())
        self.assert_no_application_start()

    def test_failed_venv_creation_stops_with_recovery_instruction(self):
        result = self.launch(STUB_FAIL="create_venv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("venv support", result.stdout)
        self.assertNotIn("dependencies", self.calls())
        self.assert_no_application_start()

    def test_incomplete_existing_venv_is_not_recreated_or_deleted(self):
        (self.root / ".venv").mkdir()
        sentinel = self.root / ".venv/keep"
        sentinel.write_text("existing environment")
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Rename only .venv", result.stdout)
        self.assertEqual(sentinel.read_text(), "existing environment")
        self.assertEqual(self.calls(), [])
        self.assert_no_application_start()

    def test_broken_venv_symlink_is_not_overwritten(self):
        (self.root / ".venv").symlink_to(self.root / "missing-environment", target_is_directory=True)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / ".venv").is_symlink())
        self.assertEqual(self.calls(), [])
        self.assert_no_application_start()

    def test_old_venv_and_missing_pip_stop_before_installation(self):
        self.existing_venv()
        for stage in ("venv_version", "pip_available"):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                result = self.launch(STUB_FAIL=stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Rename only .venv", result.stdout)
                self.assertNotIn("pip_upgrade", self.calls())
                self.assert_no_application_start()

    def test_failed_packages_or_preflight_never_create_data_migrate_or_open_browser(self):
        self.existing_venv()
        for stage in ("pip_upgrade", "dependencies", "pip_check", "preflight", "system_check"):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                result = self.launch(STUB_FAIL=stage, STUB_OS="Darwin")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls()[-1], stage)
                self.assert_no_application_start()

    def test_missing_shared_checker_fails_closed(self):
        self.existing_venv()
        (self.root / "scripts/check_comparison_runtime.py").unlink()
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete project checkout", result.stdout)
        self.assertEqual(self.calls()[-1], "pip_check")
        self.assert_no_application_start()

    def test_migration_failure_never_starts_server_or_browser(self):
        self.existing_venv()
        result = self.launch(STUB_FAIL="migrate", STUB_OS="Darwin")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls()[-1], "migrate")
        self.assertFalse(self.browser.exists())
        self.assertFalse(self.server.exists())

    def test_mac_wrapper_opens_configured_browser_only_after_successful_checks(self):
        self.existing_venv()
        result = self.launch(wrapper=True, STUB_OS="Darwin", DJANGO_HOST="localhost", DJANGO_PORT="8123")
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 2
        while not self.browser.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.browser.read_text().strip(), "http://localhost:8123/")
        self.assertEqual(self.server.read_text().strip(), "localhost:8123")
        self.assertLess(self.calls().index("preflight"), self.calls().index("migrate"))

    def test_browser_opt_out_is_preserved(self):
        self.existing_venv()
        result = self.launch(STUB_OS="Darwin", OPEN_BROWSER="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.browser.exists())

    def test_wrapper_preserves_server_failure_status(self):
        self.existing_venv()
        result = self.launch(wrapper=True, STUB_FAIL="runserver", OPEN_BROWSER="0")
        self.assertEqual(result.returncode, 23)

    def test_legacy_folders_and_existing_files_are_preserved(self):
        self.existing_venv()
        for name in ("incoming", "active_final", "old_versions"):
            folder = self.root / "data" / name
            folder.mkdir(parents=True)
            (folder / "keep.txt").write_text("keep")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("incoming", "active_final", "old_versions"):
            self.assertEqual((self.root / "data" / name / "keep.txt").read_text(), "keep")

    def test_checker_receives_explicit_transport_and_model_override_unchanged(self):
        self.existing_venv()
        result = self.launch(CFM_COMPARISON_TRANSPORT="service", CFM_RAPIDOCR_MODEL_DIR="/opt/approved models")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text().splitlines(), ["service", "/opt/approved models"])


class WindowsStartupStaticTests(SimpleTestCase):
    """Source checks only: this suite does not execute Windows cmd.exe."""
    def setUp(self):
        self.source = (ROOT / "start_windows.bat").read_text()

    def test_both_bootstrap_paths_validate_python_before_venv_creation(self):
        for command in ("py -3", "python"):
            version_check = command + ' -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || goto python_version_error'
            self.assertIn(version_check, self.source)
            self.assertLess(self.source.index(version_check), self.source.index(command + " -m venv .venv"))
        self.assertIn('if exist ".venv" goto validate_venv', self.source)
        self.assertIn('if not exist "%PYTHON%" goto venv_error', self.source)
        self.assertIn("sys.prefix != sys.base_prefix", self.source)
        self.assertIn("Rename only .venv", self.source)

    def test_checked_startup_pipeline_precedes_data_migration_and_browser(self):
        ordered = [
            '"%PYTHON%" -m pip install -r requirements.txt || goto error',
            '"%PYTHON%" -m pip check || goto error',
            '"%PYTHON%" scripts\\check_comparison_runtime.py || goto error',
            '"%PYTHON%" manage.py check || goto error',
            "echo Preparing local data folders...",
            '"%PYTHON%" manage.py migrate || goto error',
            'if not "%OPEN_BROWSER%"=="0"',
            '"%PYTHON%" manage.py runserver "%DJANGO_HOST%:%DJANGO_PORT%" || goto error',
        ]
        positions = [self.source.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('if not exist "scripts\\check_comparison_runtime.py" goto checker_missing', self.source)
        for legacy in ("incoming", "active_final", "old_versions"):
            self.assertNotIn('"data\\' + legacy + '"', self.source)
        self.assertIn("exit /b 1", self.source)

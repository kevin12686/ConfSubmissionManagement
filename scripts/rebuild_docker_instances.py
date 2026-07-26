#!/usr/bin/env python3
"""Rebuild and restart existing Docker Compose instances for this checkout."""

from __future__ import annotations

import argparse
import http.cookiejar
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from docker_instance_tools import (  # noqa: E402
    DockerCommandError,
    GATEWAY_VERSION,
    GatewayOperationStatus,
    compose_command,
    compose_env,
    exclusive_lock,
    inspect_compose_containers,
    matching_instances,
    run,
    temporary_env_file,
    wait_until_ready,
)

PROXY_HOST_DIRECTIVE = "proxy_set_header Host $http_host;"
STATIC_SMOKE_PATH = "/static/submissions/vendor/tabler-1.4.0.min.css"


class CsrfTokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input" or self.token:
            return
        values = dict(attrs)
        if values.get("name") == "csrfmiddlewaretoken":
            self.token = values.get("value") or ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild Docker Compose projects that already have an application "
            "container created from this repository checkout."
        )
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only rebuild the named Compose project. Can be used more than once.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the inferred settings and commands without running them.",
    )
    args = parser.parse_args()

    root = SCRIPT_DIR.parent
    lock_path = root / "runtime" / ".docker-data-operation.lock"
    try:
        with exclusive_lock(lock_path):
            instances = matching_instances(
                inspect_compose_containers(),
                root,
                set(args.project),
            )
            if not instances:
                print(
                    "No matching Docker Compose application instances were found "
                    "for this checkout.",
                    file=sys.stderr,
                )
                print(
                    "Start an instance first, for example: "
                    "docker compose --env-file .env.conference-a "
                    "-p sms-conf-a up -d --build",
                    file=sys.stderr,
                )
                return 1

            for instance in instances:
                rebuild_instance(instance, root, dry_run=args.dry_run)
    except (DockerCommandError, RuntimeError, ValueError) as exc:
        print(f"Rebuild failed: {exc}", file=sys.stderr)
        return 1
    return 0


def rebuild_instance(instance: dict, root: Path, *, dry_run: bool) -> None:
    env_values = compose_env(instance)
    bind_override = instance.get("mount_type") == "bind"
    build_command = compose_command(
        root,
        Path("<generated>"),
        instance["project"],
        "build",
        "web",
        bind_override=bind_override,
    )
    web_command = compose_command(
        root,
        Path("<generated>"),
        instance["project"],
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "web",
        bind_override=bind_override,
    )
    print(f"Project: {instance['project']} ({instance['name']})")
    print(f"  public endpoint source: {instance['public_service']}")
    print(
        "  data mount retained as: "
        f"{'host bind' if bind_override else 'project named volume'}"
    )
    if instance.get("mount_type") == "volume":
        print(f"  SMS_DATA_VOLUME={instance.get('volume_name', '')}")
    for key in sorted(env_values):
        display_value = "***" if key == "SMS_SECRET_KEY" else env_values[key]
        print(f"  {key}={display_value}")
    print(f"  build:   {shlex.join(build_command)}")
    print(f"  cutover: {shlex.join(web_command)}")
    print(
        "  proxy:   "
        + (
            "validated in-place configuration reload"
            if instance.get("gateway_version") == GATEWAY_VERSION
            else "one-time gateway recreation"
        )
    )
    if dry_run:
        return

    status = GatewayOperationStatus(instance, "update")
    status.start("build")
    cutover_started = False
    with temporary_env_file(env_values) as env_file:
        try:
            run(
                compose_command(
                    root,
                    env_file,
                    instance["project"],
                    "config",
                    "--quiet",
                    bind_override=bind_override,
                ),
                cwd=root,
                capture=True,
            )
            run(
                compose_command(
                    root,
                    env_file,
                    instance["project"],
                    "build",
                    "web",
                    bind_override=bind_override,
                ),
                cwd=root,
                capture=False,
            )
            status.update("apply")
            cutover_started = True
            run(
                compose_command(
                    root,
                    env_file,
                    instance["project"],
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "web",
                    bind_override=bind_override,
                ),
                cwd=root,
                capture=False,
            )
            rebuilt = current_project_instance(root, instance["project"])
            if not rebuilt:
                raise RuntimeError(
                    f"{instance['project']}: rebuilt web container was not found."
                )
            status.bind_instance(rebuilt)
            status.update("health")
            wait_until_ready(rebuilt["id"])
            status.update("proxy")
            refresh_proxy(
                rebuilt,
                root,
                env_file,
                bind_override=bind_override,
            )
            rebuilt = current_project_instance(root, instance["project"])
            if not rebuilt or not rebuilt.get("proxy_id"):
                raise RuntimeError(
                    f"{instance['project']}: proxy container was not found."
                )
            status.bind_instance(rebuilt)
            wait_until_ready(rebuilt["proxy_id"])
            status.update("smoke")
            verify_rebuilt_instance(
                rebuilt,
                root,
                env_file,
                bind_override=bind_override,
            )
        except Exception:
            if cutover_started:
                status.fail()
                status.close(clear=False)
            else:
                status.close(clear=True)
            raise
        else:
            status.close(clear=True)
    print(f"  verified: proxy config, static asset, and same-origin CSRF POST")


def refresh_proxy(
    instance: dict,
    root: Path,
    env_file: Path,
    *,
    bind_override: bool,
) -> None:
    if (
        instance.get("gateway_version") != GATEWAY_VERSION
        or not instance.get("proxy_running")
    ):
        run(
            compose_command(
                root,
                env_file,
                instance["project"],
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "proxy",
                bind_override=bind_override,
            ),
            cwd=root,
            capture=False,
        )
        return
    run(
        compose_command(
            root,
            env_file,
            instance["project"],
            "exec",
            "-T",
            "proxy",
            "/bin/sh",
            "/srv/fallback/reload.sh",
            bind_override=bind_override,
        ),
        cwd=root,
        capture=True,
    )


def verify_rebuilt_instance(
    instance: dict,
    root: Path,
    env_file: Path,
    *,
    bind_override: bool,
) -> None:
    config_result = run(
        compose_command(
            root,
            env_file,
            instance["project"],
            "exec",
            "-T",
            "proxy",
            "cat",
            "/etc/nginx/conf.d/default.conf",
            bind_override=bind_override,
        ),
        cwd=root,
        capture=True,
    )
    if PROXY_HOST_DIRECTIVE not in config_result.stdout:
        raise RuntimeError(
            f"{instance['project']}: proxy did not load the expected Host/port "
            "forwarding configuration."
        )
    smoke_test_public_endpoint(instance)


def smoke_test_public_endpoint(instance: dict, *, attempts: int = 20) -> None:
    connect_base_url, browser_origin, host_header = probe_endpoint(instance)
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    reports_url = f"{connect_base_url}/reports/"
    common_headers = {
        "Host": host_header,
        "User-Agent": "ConferenceFinalManager-DockerRebuild/1",
    }

    response_html = ""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(reports_url, headers=common_headers)
            with opener.open(request, timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"GET {reports_url} returned HTTP {response.status}."
                    )
                response_html = response.read().decode("utf-8", errors="replace")
            break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1)
    else:
        raise RuntimeError(
            f"{instance['project']}: public endpoint did not become ready at "
            f"{connect_base_url}: {last_error}"
        )

    parser = CsrfTokenParser()
    parser.feed(response_html)
    if not parser.token:
        raise RuntimeError(
            f"{instance['project']}: reports page did not provide a CSRF token."
        )

    static_request = urllib.request.Request(
        f"{connect_base_url}{STATIC_SMOKE_PATH}",
        headers=common_headers,
    )
    try:
        with opener.open(static_request, timeout=10) as response:
            if response.status != 200 or not response.read(1):
                raise RuntimeError(
                    f"{instance['project']}: static asset smoke check failed."
                )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{instance['project']}: static asset smoke check returned "
            f"HTTP {exc.code}."
        ) from exc

    payload = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": parser.token,
            "action": "docker_proxy_csrf_smoke",
        }
    ).encode("utf-8")
    post_request = urllib.request.Request(
        reports_url,
        data=payload,
        headers={
            **common_headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": browser_origin,
            "Referer": f"{browser_origin}/reports/",
        },
        method="POST",
    )
    try:
        with opener.open(post_request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"{instance['project']}: CSRF smoke POST returned "
                    f"HTTP {response.status}."
                )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{instance['project']}: CSRF smoke POST returned HTTP {exc.code}."
        ) from exc


def probe_endpoint(instance: dict) -> tuple[str, str, str]:
    bind_host = instance["sms_bind_host"] or "127.0.0.1"
    connect_host = bind_host
    browser_host = bind_host
    if bind_host in {"0.0.0.0", "::", "[::]"}:
        connect_host = "127.0.0.1"
        browser_host = preferred_allowed_host(
            instance.get("env", {}).get("SMS_ALLOWED_HOSTS", "")
        )
    port = str(instance["sms_port"])
    connect_netloc = host_port(connect_host, port)
    browser_netloc = host_port(browser_host, port)
    return (
        f"http://{connect_netloc}",
        f"http://{browser_netloc}",
        browser_netloc,
    )


def preferred_allowed_host(allowed_hosts: str) -> str:
    candidates = [
        value.strip()
        for value in allowed_hosts.split(",")
        if value.strip()
        and value.strip() != "*"
        and not value.strip().startswith(".")
        and value.strip() != "testserver"
    ]
    for preferred in ("127.0.0.1", "localhost"):
        if preferred in candidates:
            return preferred
    return candidates[0] if candidates else "127.0.0.1"


def host_port(host: str, port: str) -> str:
    normalized_host = host.strip("[]")
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return normalized_host if port == "80" else f"{normalized_host}:{port}"


def current_project_instance(root: Path, project: str) -> dict | None:
    instances = matching_instances(
        inspect_compose_containers(),
        root,
        {project},
    )
    return instances[0] if instances else None


if __name__ == "__main__":
    raise SystemExit(main())

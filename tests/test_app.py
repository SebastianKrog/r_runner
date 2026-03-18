import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

import app


def test_health_endpoint():
    client = TestClient(app.app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_system_packages_endpoint(monkeypatch, tmp_path):
    pkg_file = tmp_path / "r-packages.txt"
    pkg_file.write_text("dplyr\nggplot2\n", encoding="utf-8")
    monkeypatch.setattr(app, "SYSTEM_PACKAGES_PATH", pkg_file)

    client = TestClient(app.app)
    response = client.get("/system")

    assert response.status_code == 200
    assert response.json() == {"packages": ["dplyr", "ggplot2"]}


def test_system_packages_endpoint_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SYSTEM_PACKAGES_PATH", tmp_path / "missing.txt")

    client = TestClient(app.app)
    response = client.get("/system")

    assert response.status_code == 500
    assert response.json()["detail"] == "Package list is unavailable"


def test_schema_endpoint():
    client = TestClient(app.app)
    response = client.get("/schema")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert '"title": "RunResponse"' in response.text
    assert '"runtime_stderr"' in response.text


def test_openapi_gpt_schema_uses_http_bearer_auth():
    spec = json.loads(Path("openapi.gpt.json").read_text(encoding="utf-8"))

    assert spec["paths"]["/run"]["post"]["security"] == [{"BearerAuth": []}]
    assert spec["components"]["securitySchemes"]["BearerAuth"] == {"type": "http", "scheme": "bearer"}


def test_privacy_page():
    client = TestClient(app.app)
    response = client.get("/privacy")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Privacy Policy" in response.text


def test_run_requires_auth():
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)
    response = client.post("/run", json={"script": "print('hello')"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token"


def test_run_rejects_invalid_token():
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)
    response = client.post(
        "/run",
        headers={"Authorization": "Bearer wrong"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"


def test_run_script_success_with_artifacts(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    monkeypatch.setattr(app, "_resolve_docker_bin", lambda: app.RUNNER_DOCKER_BIN)

    def fake_run(cmd, cwd, **kwargs):
        if cmd[1] == "pull":
            return Mock(returncode=0, stdout="", stderr="pull warning\n")

        assert cmd[:4] == [app.RUNNER_DOCKER_BIN, "run", "--rm", "--network"]
        Path(cwd, app.SCRIPT_STDOUT_NAME).write_text("ok\n", encoding="utf-8")
        Path(cwd, app.SCRIPT_STDERR_NAME).write_text("script warning\n", encoding="utf-8")
        Path(cwd, "result.txt").write_text("hello\n", encoding="utf-8")
        Path(cwd, "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return Mock(returncode=0, stdout="", stderr="runtime warning\n")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "ok\n"
    assert payload["stderr"] == "script warning\n"
    assert payload["runtime_stderr"] == ""

    artifacts = {entry["filename"]: entry for entry in payload["artifacts"]}
    assert artifacts["result.txt"]["encoding"] == "utf-8"
    assert artifacts["result.txt"]["content"] == "hello\n"
    assert artifacts["plot.png"]["encoding"] == "base64"
    assert base64.b64decode(artifacts["plot.png"]["content"]) == b"\x89PNG\r\n\x1a\n"




def test_run_script_returns_runtime_stderr_when_script_never_starts(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    monkeypatch.setattr(app, "_resolve_docker_bin", lambda: app.RUNNER_DOCKER_BIN)

    def fake_run(cmd, cwd, **kwargs):
        if cmd[1] == "pull":
            return Mock(returncode=0, stdout="", stderr="")

        return Mock(returncode=125, stdout="", stderr="container runtime failed\n")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["exit_code"] == 125
    assert payload["stdout"] == ""
    assert payload["stderr"] == ""
    assert payload["runtime_stderr"] == "container runtime failed\n"


def test_pull_runtime_image_uses_local_image_when_pull_fails(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    monkeypatch.setattr(app, "_resolve_docker_bin", lambda: app.RUNNER_DOCKER_BIN)

    def fake_run(cmd, cwd, **kwargs):
        if cmd[1] == "pull":
            return Mock(returncode=1, stdout="", stderr="pull failed\n")
        if cmd[1:3] == ["image", "inspect"]:
            return Mock(returncode=0, stdout="present\n", stderr="")

        Path(cwd, app.SCRIPT_STDOUT_NAME).write_text("ok\n", encoding="utf-8")
        Path(cwd, app.SCRIPT_STDERR_NAME).write_text("", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_pull_runtime_image_returns_500_on_failure_without_local_image(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    monkeypatch.setattr(app, "_resolve_docker_bin", lambda: app.RUNNER_DOCKER_BIN)

    def fake_run(cmd, cwd, **kwargs):
        if cmd[1] == "pull":
            return Mock(returncode=1, stdout="", stderr="pull failed\n")
        if cmd[1:3] == ["image", "inspect"]:
            return Mock(returncode=1, stdout="", stderr="missing\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to pull runtime image"


def test_run_script_timeout(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    monkeypatch.setattr(app, "_resolve_docker_bin", lambda: app.RUNNER_DOCKER_BIN)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker run", timeout=1)

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "Sys.sleep(999)"},
    )

    assert response.status_code == 408
    assert "timed out" in response.json()["detail"]


def test_resolve_docker_bin_falls_back_to_docker_io(monkeypatch):
    monkeypatch.setattr(app, "RUNNER_DOCKER_BIN", "docker")

    def fake_which(binary):
        if binary == "docker":
            return None
        if binary == "docker.io":
            return "/usr/bin/docker.io"
        return None

    monkeypatch.setattr(app, "which", fake_which)

    assert app._resolve_docker_bin() == "/usr/bin/docker.io"


def test_run_returns_500_when_container_runtime_unavailable(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    monkeypatch.setattr(app, "RUNNER_DOCKER_BIN", "docker")
    monkeypatch.setattr(app, "which", lambda _binary: None)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 500
    assert "Container runtime binary is unavailable" in response.json()["detail"]


def test_prepare_shared_workdir_root(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "RUNNER_SHARED_DIR", tmp_path / "shared")

    resolved = app._prepare_shared_workdir_root()

    assert resolved.exists()
    assert resolved.is_dir()


def test_run_returns_500_when_shared_root_unavailable(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    class BrokenPath:
        def mkdir(self, *args, **kwargs):
            raise OSError("boom")

    monkeypatch.setattr(app, "RUNNER_SHARED_DIR", BrokenPath())

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "print('hello')"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Shared workdir root is unavailable"

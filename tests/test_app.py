import base64
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

    def fake_run(cmd, cwd, **kwargs):
        Path(cwd, "result.txt").write_text("hello\n", encoding="utf-8")
        Path(cwd, "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return Mock(returncode=0, stdout="ok\n", stderr="")

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

    artifacts = {entry["filename"]: entry for entry in payload["artifacts"]}
    assert artifacts["result.txt"]["encoding"] == "utf-8"
    assert artifacts["result.txt"]["content"] == "hello\n"
    assert artifacts["plot.png"]["encoding"] == "base64"
    assert base64.b64decode(artifacts["plot.png"]["content"]) == b"\x89PNG\r\n\x1a\n"


def test_run_script_timeout(monkeypatch):
    app.RUNNER_TOKEN = "secret"
    client = TestClient(app.app)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="Rscript", timeout=1)

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = client.post(
        "/run",
        headers={"Authorization": "Bearer secret"},
        json={"script": "Sys.sleep(999)"},
    )

    assert response.status_code == 408
    assert "timed out" in response.json()["detail"]

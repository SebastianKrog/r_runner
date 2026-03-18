import base64
import hmac
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from shutil import which
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field


MAX_SCRIPT_BYTES = int(os.getenv("MAX_SCRIPT_BYTES", "500000"))
MAX_ARTIFACT_BYTES = int(os.getenv("MAX_ARTIFACT_BYTES", "5000000"))
MAX_ARTIFACT_COUNT = int(os.getenv("MAX_ARTIFACT_COUNT", "10"))
RUN_TIMEOUT_SECONDS = int(os.getenv("RUN_TIMEOUT_SECONDS", "30"))
RUNNER_TOKEN = os.getenv("RUNNER_TOKEN")
RUNNER_SCRIPT_IMAGE = os.getenv("RUNNER_SCRIPT_IMAGE", "r-runner-r-base:latest")
RUNNER_DOCKER_BIN = os.getenv("RUNNER_DOCKER_BIN", "docker")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
RUNNER_SHARED_DIR = Path(os.getenv("RUNNER_SHARED_DIR", "/tmp/r-runner-shared"))

SYSTEM_PACKAGES_PATH = Path(os.getenv("SYSTEM_PACKAGES_PATH", "/app/system/r-packages.txt"))
SCRIPT_STDOUT_NAME = ".script.stdout"
SCRIPT_STDERR_NAME = ".script.stderr"


class SystemPackagesResponse(BaseModel):
    packages: List[str] = Field(..., description="Installed R package names available in the execution image.")


class HealthResponse(BaseModel):
    ok: bool = Field(..., description="Indicates whether the service reports itself as healthy.")


class RunRequest(BaseModel):
    script: str = Field(..., description="The complete R source code to execute.")


class Artifact(BaseModel):
    filename: str = Field(..., description="The filename assigned to the generated artifact.")
    mime_type: str = Field(..., description="The MIME type describing the artifact content.")
    encoding: Literal["utf-8", "base64"] = Field(
        ...,
        description="The encoding used for the artifact content field.",
    )
    content: str = Field(
        ...,
        description=(
            "The artifact payload, represented either as plain UTF-8 text or as "
            "base64-encoded binary data, depending on the encoding field."
        ),
    )


class RunResponse(BaseModel):
    success: bool = Field(
        ...,
        description="True when the R process exits with code 0; false when it exits with a non-zero code.",
    )
    exit_code: int = Field(..., description="The exit status returned by the R process.")
    stdout: str = Field(..., description="All text written to the script's standard output during execution.")
    stderr: str = Field(..., description="All text written to the script's standard error during execution.")
    runtime_stderr: str = Field(
        ...,
        description=(
            "Container runtime diagnostics. Empty when the script started successfully, even if the runtime emitted "
            "non-fatal warnings while preparing the container."
        ),
    )
    artifacts: List[Artifact] = Field(
        ...,
        description="Files produced by the script and captured by the runner for inclusion in the response.",
    )


app = FastAPI(
    openapi_version="3.1.0",
    title="R Runner API",
    description=(
        "Authenticated API for running user-provided R scripts in isolated "
        "container executions and returning the resulting exit status, console "
        "output, and any generated files."
    ),
    version="v1.0.0",
    servers=[{"url": PUBLIC_BASE_URL}],
)

bearer_scheme = HTTPBearer(scheme_name="BearerAuth", auto_error=False)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True)


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)) -> None:
    if not RUNNER_TOKEN:
        raise HTTPException(status_code=500, detail="Server is missing RUNNER_TOKEN")

    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = credentials.credentials.strip()
    if not hmac.compare_digest(token, RUNNER_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


def _encode_artifact(path: Path) -> Artifact:
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = mime_type or "application/octet-stream"
    data = path.read_bytes()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail=f"Artifact {path.name} exceeded MAX_ARTIFACT_BYTES")

    try:
        text = data.decode("utf-8")
        return Artifact(filename=path.name, mime_type=mime_type, encoding="utf-8", content=text)
    except UnicodeDecodeError:
        return Artifact(
            filename=path.name,
            mime_type=mime_type,
            encoding="base64",
            content=base64.b64encode(data).decode("ascii"),
        )



def _collect_artifacts(workdir: Path) -> List[Artifact]:
    ignored_names = {"script.R", SCRIPT_STDOUT_NAME, SCRIPT_STDERR_NAME}
    files = [p for p in workdir.rglob("*") if p.is_file() and p.name not in ignored_names]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [_encode_artifact(path) for path in files[:MAX_ARTIFACT_COUNT]]



def _resolve_docker_bin() -> str:
    configured = RUNNER_DOCKER_BIN.strip()

    candidates: List[str] = []
    if configured:
        candidates.append(configured)

    if configured == "docker":
        candidates.extend(["docker.io", "/usr/bin/docker", "/usr/bin/docker.io", "/usr/local/bin/docker"])

    checked: List[str] = []
    for candidate in candidates:
        checked.append(candidate)
        candidate_path = Path(candidate)
        if candidate_path.is_absolute():
            if candidate_path.exists() and os.access(candidate_path, os.X_OK):
                return str(candidate_path)
            continue

        found = which(candidate)
        if found:
            return found

    checked_values = ", ".join(checked) if checked else "<none>"
    raise HTTPException(
        status_code=500,
        detail=(
            "Container runtime binary is unavailable "
            f"(configured RUNNER_DOCKER_BIN='{RUNNER_DOCKER_BIN}', checked: {checked_values})"
        ),
    )



def _prepare_shared_workdir_root() -> Path:
    try:
        RUNNER_SHARED_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Shared workdir root is unavailable") from exc

    return RUNNER_SHARED_DIR



def _run_docker_command(command: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )



def _runtime_image_exists_locally(workdir_path: Path) -> bool:
    command = [_resolve_docker_bin(), "image", "inspect", RUNNER_SCRIPT_IMAGE]
    completed = _run_docker_command(command, cwd=workdir_path)
    return completed.returncode == 0



def _pull_runtime_image(workdir_path: Path) -> None:
    command = [_resolve_docker_bin(), "pull", RUNNER_SCRIPT_IMAGE]
    completed = _run_docker_command(command, cwd=workdir_path)
    if completed.returncode == 0:
        return

    if _runtime_image_exists_locally(workdir_path):
        return

    raise HTTPException(status_code=500, detail="Failed to pull runtime image")



def _read_output(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")



def _run_script_in_container(workdir_path: Path) -> subprocess.CompletedProcess[str]:
    command = [
        _resolve_docker_bin(),
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{workdir_path}:/workspace",
        "-w",
        "/workspace",
        RUNNER_SCRIPT_IMAGE,
        "sh",
        "-lc",
        f"Rscript /workspace/script.R > /workspace/{SCRIPT_STDOUT_NAME} 2> /workspace/{SCRIPT_STDERR_NAME}",
    ]
    return _run_docker_command(command, cwd=workdir_path)


@app.get("/system", response_model=SystemPackagesResponse)
def system_packages() -> SystemPackagesResponse:
    if not SYSTEM_PACKAGES_PATH.exists():
        raise HTTPException(status_code=500, detail="Package list is unavailable")

    packages = [line.strip() for line in SYSTEM_PACKAGES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return SystemPackagesResponse(packages=packages)




@app.get("/schema", response_class=PlainTextResponse)
def schema() -> PlainTextResponse:
    return PlainTextResponse(json.dumps(RunResponse.model_json_schema(), indent=2))


@app.post("/run", response_model=RunResponse)
def run_script(payload: RunRequest, _: None = Depends(require_auth)) -> RunResponse:
    script_bytes = payload.script.encode("utf-8")
    if len(script_bytes) > MAX_SCRIPT_BYTES:
        raise HTTPException(status_code=413, detail="Script exceeded MAX_SCRIPT_BYTES")

    shared_root = _prepare_shared_workdir_root()
    workdir_path = Path(tempfile.mkdtemp(prefix="r-run-", dir=shared_root))
    try:
        script_path = workdir_path / "script.R"
        script_path.write_text(payload.script, encoding="utf-8")

        _pull_runtime_image(workdir_path)
        completed = _run_script_in_container(workdir_path=workdir_path)
        stdout = _read_output(workdir_path / SCRIPT_STDOUT_NAME)
        stderr = _read_output(workdir_path / SCRIPT_STDERR_NAME)
        script_started = (workdir_path / SCRIPT_STDOUT_NAME).exists() or (workdir_path / SCRIPT_STDERR_NAME).exists()
        artifacts = _collect_artifacts(workdir_path)
        return RunResponse(
            success=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            runtime_stderr="" if script_started else completed.stderr,
            artifacts=artifacts,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=408, detail=f"Script timed out after {RUN_TIMEOUT_SECONDS}s") from exc
    finally:
        shutil.rmtree(workdir_path, ignore_errors=True)


@app.get("/")
def root() -> dict:
    return {
        "service": "r-runner",
        "endpoints": ["GET /", "GET /health", "GET /privacy", "GET /schema", "GET /system", "POST /run"],
        "response_format": "JSON with stdout/stderr/runtime_stderr/exit_code and file artifacts",
    }


@app.get("/privacy", response_class=HTMLResponse)
def privacy() -> str:
    return """
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Privacy Policy - R Runner</title>
  </head>
  <body>
    <h1>Privacy Policy</h1>
    <p class="muted">Last updated: 2026-03-11</p>
    <p>This service ("R Runner") executes R scripts sent by authorized clients and returns execution output.</p>
  </body>
</html>
""".strip()

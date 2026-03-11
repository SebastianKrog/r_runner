import base64
import hmac
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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

SYSTEM_PACKAGES_PATH = Path(os.getenv("SYSTEM_PACKAGES_PATH", "/app/system/r-packages.txt"))


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
    stdout: str = Field(..., description="All text written to standard output during execution.")
    stderr: str = Field(..., description="All text written to standard error during execution.")
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
    files = [p for p in workdir.rglob("*") if p.is_file() and p.name != "script.R"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [_encode_artifact(path) for path in files[:MAX_ARTIFACT_COUNT]]


def _run_script_in_container(script_path: Path, workdir_path: Path) -> subprocess.CompletedProcess[str]:
    command = [
        RUNNER_DOCKER_BIN,
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{workdir_path}:/workspace",
        "-w",
        "/workspace",
        RUNNER_SCRIPT_IMAGE,
        "Rscript",
        str(script_path),
    ]
    return subprocess.run(
        command,
        cwd=workdir_path,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )


@app.get("/system", response_model=SystemPackagesResponse)
def system_packages() -> SystemPackagesResponse:
    if not SYSTEM_PACKAGES_PATH.exists():
        raise HTTPException(status_code=500, detail="Package list is unavailable")

    packages = [line.strip() for line in SYSTEM_PACKAGES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return SystemPackagesResponse(packages=packages)


@app.post("/run", response_model=RunResponse)
def run_script(payload: RunRequest, _: None = Depends(require_auth)) -> RunResponse:
    script_bytes = payload.script.encode("utf-8")
    if len(script_bytes) > MAX_SCRIPT_BYTES:
        raise HTTPException(status_code=413, detail="Script exceeded MAX_SCRIPT_BYTES")

    workdir_path = Path(tempfile.mkdtemp(prefix="r-run-"))
    try:
        script_path = workdir_path / "script.R"
        script_path.write_text(payload.script, encoding="utf-8")

        completed = _run_script_in_container(script_path=Path("/workspace/script.R"), workdir_path=workdir_path)
        artifacts = _collect_artifacts(workdir_path)
        return RunResponse(
            success=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
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
        "endpoints": ["GET /", "GET /health", "GET /privacy", "GET /system", "POST /run"],
        "response_format": "JSON with stdout/stderr/exit_code and file artifacts",
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

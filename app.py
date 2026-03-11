import base64
import hmac
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


MAX_SCRIPT_BYTES = int(os.getenv("MAX_SCRIPT_BYTES", "500000"))
MAX_ARTIFACT_BYTES = int(os.getenv("MAX_ARTIFACT_BYTES", "5000000"))
MAX_ARTIFACT_COUNT = int(os.getenv("MAX_ARTIFACT_COUNT", "10"))
RUN_TIMEOUT_SECONDS = int(os.getenv("RUN_TIMEOUT_SECONDS", "30"))
RUNNER_TOKEN = os.getenv("RUNNER_TOKEN")


class RunRequest(BaseModel):
    script: str = Field(..., description="Raw R script to execute")


class Artifact(BaseModel):
    path: str
    mime_type: str
    encoding: str
    content: str


class RunResponse(BaseModel):
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    artifacts: List[Artifact]


app = FastAPI(title="R Runner", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not RUNNER_TOKEN:
        raise HTTPException(status_code=500, detail="Server is missing RUNNER_TOKEN")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, RUNNER_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


def _encode_artifact(path: Path) -> Artifact:
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = mime_type or "application/octet-stream"
    data = path.read_bytes()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Artifact {path.name} exceeded MAX_ARTIFACT_BYTES",
        )

    try:
        text = data.decode("utf-8")
        return Artifact(path=path.name, mime_type=mime_type, encoding="utf-8", content=text)
    except UnicodeDecodeError:
        return Artifact(
            path=path.name,
            mime_type=mime_type,
            encoding="base64",
            content=base64.b64encode(data).decode("ascii"),
        )


def _collect_artifacts(workdir: Path) -> List[Artifact]:
    files = [p for p in workdir.rglob("*") if p.is_file() and p.name != "script.R"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    selected = files[:MAX_ARTIFACT_COUNT]
    return [_encode_artifact(path) for path in selected]


@app.post("/run", response_model=RunResponse)
def run_script(payload: RunRequest, _: None = Depends(require_auth)) -> RunResponse:
    script_bytes = payload.script.encode("utf-8")
    if len(script_bytes) > MAX_SCRIPT_BYTES:
        raise HTTPException(status_code=413, detail="Script exceeded MAX_SCRIPT_BYTES")

    workdir_path = Path(tempfile.mkdtemp(prefix="r-run-"))
    try:
        script_path = workdir_path / "script.R"
        script_path.write_text(payload.script, encoding="utf-8")

        completed = subprocess.run(
            ["Rscript", str(script_path)],
            cwd=workdir_path,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )

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
        "endpoints": ["GET /health", "POST /run"],
        "response_format": "JSON with stdout/stderr/exit_code and file artifacts",
    }

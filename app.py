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
from fastapi.responses import HTMLResponse
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
        "endpoints": ["GET /", "GET /health", "GET /privacy", "POST /run"],
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
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; margin: 2rem auto; max-width: 46rem; padding: 0 1rem; color: #222; }
      h1, h2 { line-height: 1.25; }
      code { background: #f6f8fa; padding: 0.1rem 0.3rem; border-radius: 4px; }
      .muted { color: #555; }
    </style>
  </head>
  <body>
    <h1>Privacy Policy</h1>
    <p class="muted">Last updated: 2026-03-11</p>

    <p>
      This service ("R Runner") executes R scripts sent by authorized clients and returns execution output.
    </p>

    <h2>What data is processed</h2>
    <ul>
      <li>R scripts you submit to <code>POST /run</code>.</li>
      <li>Execution output such as <code>stdout</code>, <code>stderr</code>, and generated artifacts.</li>
      <li>Basic request metadata needed to operate and secure the service (for example, timing and error logs).</li>
    </ul>

    <h2>How data is used</h2>
    <p>
      Submitted scripts are run in a temporary working directory for request processing. Temporary files are deleted
      after execution completes or times out.
    </p>

    <h2>Data sharing</h2>
    <p>
      We do not sell your data. Data may be shared only with infrastructure providers required to host and operate
      this service.
    </p>

    <h2>Security</h2>
    <p>
      Access to execution endpoints requires a bearer token. Please avoid submitting secrets unless strictly necessary.
    </p>

    <h2>Your responsibility</h2>
    <p>
      You are responsible for the content of submitted scripts and any data they process.
    </p>

    <h2>Contact</h2>
    <p>
      If you have privacy questions, contact the service operator for this deployment.
    </p>
  </body>
</html>
""".strip()

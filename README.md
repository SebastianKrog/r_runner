# R Runner

Token-protected web service that executes posted R scripts in **ephemeral Docker containers**.

- Web API runs in a small Python image.
- Each `POST /run` request runs in its own R container (sandboxed per request).

## API

- `GET /health` → health probe
- `GET /system` → package inventory metadata
- `POST /run` → execute an R script

`POST /run` attempts to pre-pull the configured runtime image before launching the container, but it will continue with an already-cached local image if the pull fails transiently. Successful runs return only script `stdout`/`stderr`; non-fatal Docker warnings are suppressed from `runtime_stderr`, which is populated only when the runtime fails before the script starts.

## Auth

Set `RUNNER_TOKEN` on the server and send:

```http
Authorization: Bearer <RUNNER_TOKEN>
```

## Runtime images

- `Dockerfile` → web API image (`r-runner-web`)
- `Dockerfile.r-base` → tiny R image for CI request execution checks
- `Dockerfile.r-full` → full R image (analytics/modeling packages) for deployment/runtime

The API uses `RUNNER_SCRIPT_IMAGE` to select the script runtime image.

## Local run

```bash
docker build -f Dockerfile -t r-runner-web .
docker build -f Dockerfile.r-base -t r-runner-r-base .
docker run --rm -p 8000:8000 \
  -e RUNNER_TOKEN=supersecret \
  -e RUNNER_SCRIPT_IMAGE=r-runner-r-base \
  -e PUBLIC_BASE_URL=http://localhost:8000 \
  -e RUNNER_SHARED_DIR=/tmp/r-runner-shared \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /tmp/r-runner-shared:/tmp/r-runner-shared \
  r-runner-web
```

## Docker Compose

`compose.yaml` now contains only the `r-runner` app service and joins a shared external proxy network.

```env
RUNNER_TOKEN=replace-me
WEB_IMAGE=ghcr.io/your-org/r-runner-web:latest
SCRIPT_IMAGE=ghcr.io/your-org/r-runner-r-full:latest
RUNNER_DOCKER_BIN=/usr/bin/docker
PUBLIC_BASE_URL=example.com
RUNNER_SHARED_DIR=/tmp/r-runner-shared
SHARED_PROXY_NETWORK=shared-proxy
SITE_DOMAIN=example.com
```

- `RUNNER_SHARED_DIR` must be mounted at the same absolute path in the web container and host so script files are visible to Docker-launched runtime containers.
- `/usr/bin/docker` is mounted read-only into the web container and used by `RUNNER_DOCKER_BIN` so `/run` can launch per-request runtime containers via the host Docker daemon.
- `SHARED_PROXY_NETWORK` must match other projects that attach to the shared Caddy.

## Shared Caddy bootstrap

Use `deploy/bootstrap.sh` to support either standalone startup or attaching to an already-running shared Caddy:

```bash
SITE_DOMAIN=r.example.com ./deploy/bootstrap.sh
```

What the script does:

1. Ensures shared Docker resources exist (`shared-proxy`, `shared-caddy-data`, `shared-caddy-config`).
2. Starts/updates the `r-runner` app service.
3. Starts `shared-caddy` only if it is not already running.
4. Copies this repo's route snippet into `/etc/caddy/sites/r_runner.caddy`.
5. Reloads Caddy config via the admin API.

To remove only this project's route and optionally stop shared Caddy when no routes remain:

```bash
./deploy/teardown.sh
# optional
STOP_SHARED_CADDY_IF_EMPTY=true ./deploy/teardown.sh
```

## CI/CD behavior

- PR workflow builds `r-runner-r-base` + web image and validates `/health` plus `/run`.
- Deploy workflow builds/pushes `r-runner-web` and `r-runner-r-full`.
- Build caches for both R Dockerfiles are stored in GHCR and reused automatically.
- Deploy checks run with the **full R image** to match runtime behavior.

## Environment variables

- `RUNNER_TOKEN` (required)
- `RUNNER_SCRIPT_IMAGE` (default `r-runner-r-base:latest`)
- `RUNNER_DOCKER_BIN` (default `docker`)
- `PUBLIC_BASE_URL` (default `http://localhost:8000`)
- `RUNNER_SHARED_DIR` (default `/tmp/r-runner-shared`)
- `RUN_TIMEOUT_SECONDS` (default `30`)
- `MAX_SCRIPT_BYTES` (default `500000`)
- `MAX_ARTIFACT_COUNT` (default `10`)
- `MAX_ARTIFACT_BYTES` (default `5000000` per artifact)
- `SITE_DOMAIN` (used by `deploy/bootstrap.sh` to render the route snippet)

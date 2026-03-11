# R Runner

Simple token-protected web service that executes posted R scripts inside a container based on `rocker/tidyverse`.

## API

- `GET /health` → health probe
- `POST /run` → execute an R script

### Auth

Set `RUNNER_TOKEN` on the server and send:

```http
Authorization: Bearer <RUNNER_TOKEN>
```

### Request

```json
{
  "script": "print(summary(cars)); png('plot.png'); plot(cars); dev.off()"
}
```

### Response

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "artifacts": [
    {
      "path": "plot.png",
      "mime_type": "image/png",
      "encoding": "base64",
      "content": "iVBORw0KGgo..."
    }
  ]
}
```

Artifacts are files created by the script in the temp working directory. Text files are returned as UTF-8, binary as base64.

## Run with Docker

```bash
docker build -t r-runner .
docker run --rm -p 8000:8000 -e RUNNER_TOKEN=supersecret r-runner
```

## Example call

```bash
curl -X POST http://localhost:8000/run \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer supersecret' \
  -d '{"script":"print(mean(cars$speed)); png(\"plot.png\"); plot(cars); dev.off()"}'
```

## Production HTTPS on Hetzner (r.krogmaier.dk)

The included `compose.yaml` runs the API behind Caddy, exposing only ports `80` and `443`.
Caddy obtains and renews a Let's Encrypt certificate for `r.krogmaier.dk` automatically.

1. Point DNS `A`/`AAAA` records for `r.krogmaier.dk` to your Hetzner server public IP(s).
2. Allow inbound `80/tcp` and `443/tcp` in Hetzner Cloud Firewall.
3. Deploy with Docker Compose (the GitHub deploy workflow already copies `compose.yaml` and `Caddyfile`).
4. Remove any public `8000` firewall rule; traffic should go through HTTPS only.

Smoke tests:

```bash
curl -I http://r.krogmaier.dk/health
curl -I https://r.krogmaier.dk/health
```

## CI/CD Workflows

The repository includes two GitHub Actions workflows:

- **PR startup check** (`.github/workflows/pr-startup-check.yml`): builds the container image and verifies the container serves `GET /health`.
- **Build and deploy** (`.github/workflows/deploy.yml`): on `main`, builds and pushes image tags (`latest` and commit SHA) to GHCR, then deploys remotely over SSH using `compose.yaml`.

### Required GitHub Secrets

- `DEPLOY_SSH_KEY`: private key used for SSH deployment.
- `DEPLOY_HOST`: hostname/IP of deployment server.
- `DEPLOY_USER`: SSH username on deployment server.
- `RUNNER_TOKEN`: production bearer token used by the API.

Deploy workflow writes `.env` on the server with `RUNNER_TOKEN` and an `IMAGE_NAME` pinned to the pushed commit SHA.

## Notes

Environment variables:

- `RUNNER_TOKEN` (required)
- `RUN_TIMEOUT_SECONDS` (default `30`)
- `MAX_SCRIPT_BYTES` (default `500000`)
- `MAX_ARTIFACT_COUNT` (default `10`)
- `MAX_ARTIFACT_BYTES` (default `5000000` per artifact)

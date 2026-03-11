FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && if [ ! -x /usr/bin/docker ] && [ -x /usr/bin/docker.io ]; then ln -s /usr/bin/docker.io /usr/bin/docker; fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /app/system && printf 'Configured image: %s\n' "$RUNNER_SCRIPT_IMAGE" > /app/system/r-packages.txt

EXPOSE 8000

CMD ["/opt/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

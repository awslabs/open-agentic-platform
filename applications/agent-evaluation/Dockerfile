# Agent evaluation reconciler. Same two-stage uv build as strands-agent-base.
FROM ghcr.io/astral-sh/uv:0.9.30-python3.13-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

# Patch CVEs in base image system packages
RUN apt-get update && \
    apt-get upgrade -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app/ ./app/

# Numeric uid, not just a non-root name: the kubelet cannot verify runAsNonRoot
# from a named USER, so a restricted namespace would refuse to start the pod.
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER 1000

# Metrics and probes. Published by a ClusterIP Service only — no hostPort and no
# Ingress, so this listener never leaves the cluster.
EXPOSE 9090

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9090/healthz')"

ENTRYPOINT ["python", "-m", "app.main"]

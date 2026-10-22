# s3-vectors-mcp: read-only semantic search over one S3 Vectors index, over MCP
# streamable HTTP. Same base images as strands-agent-base, pinned by digest.
FROM ghcr.io/astral-sh/uv:0.9.30-python3.13-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml ./
COPY app/ ./app/
RUN uv pip install --system --no-cache .

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY app/ ./app/
RUN useradd -m -u 10001 app
USER 10001
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app.server"]

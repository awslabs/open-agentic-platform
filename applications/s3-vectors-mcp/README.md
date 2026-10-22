# s3-vectors-mcp

Read-only MCP server (streamable HTTP, stateless) for semantic search over one
existing Amazon S3 Vectors index. Deploy it with the `s3-vectors-mcp` OAM component,
which sets every variable below and the IAM policy. Design and pipeline contract:
[docs/s3-vectors/DESIGN.md](../../docs/s3-vectors/DESIGN.md).

## Tools

- `search(query, top_k?, filter?)`: embeds the query with Bedrock, calls
  `QueryVectors`, returns `key`, `distance`, `text` and the remaining `metadata` per hit.
- `describe_index()`: dimension, distance metric, model, non-filterable keys.

## Configuration

| Variable | Default | |
|---|---|---|
| `S3VECTORS_BUCKET` | required | Existing vector bucket |
| `S3VECTORS_INDEX` | required | Existing index |
| `S3VECTORS_REGION` | `AWS_REGION` | Region of the bucket |
| `EMBEDDING_MODEL` | `amazon.titan-embed-text-v2:0` | Must match the pipeline |
| `TEXT_METADATA_KEY` | `text` | Metadata key holding chunk text |
| `INDEX_DESCRIPTION` | | Shown to agents in the tool description |
| `DEFAULT_TOP_K` / `MAX_TOP_K` | `5` / `20` | |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | The component sets `HOST=0.0.0.0` for the pod network |

`/healthz` is liveness; `/readyz` is 503 with a reason until the index is described
and the model checked against its dimension.

## Develop

```bash
uv venv -p 3.13 .venv && uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q
podman build --platform linux/amd64,linux/arm64 --manifest public.ecr.aws/z0a4o2j5/s3-vectors-mcp:<tag> .
```

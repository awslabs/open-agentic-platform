# Demo: PDFs → Docling → S3 Vectors → agent

`ingest.py` stands in for the ingestion pipeline, which in production runs outside
OAP. It converts PDFs with Docling for IBM watsonx, chunks them along the document
structure, embeds each chunk with Bedrock, and writes the vectors to S3 Vectors. An
agent then answers from them through the `s3-vectors-mcp` server.

```
PDFs ──► Docling SaaS ──► DoclingDocument JSON ──► HybridChunker ──► Titan v2 ──► S3 Vectors
                                                                                    ▲
agent ──► gateway ──► s3-vectors-mcp (embeds the question, QueryVectors) ───────────┘
```

## 1. Ingest

Needs AWS credentials (S3 Vectors and `bedrock:InvokeModel` on Titan Text Embeddings
v2) and a Docling service URL and API key. Keep the key out of git, for example in
`.local/docling.env`, which is ignored.

```bash
cd examples/s3-vectors-rag
uv venv -p 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt
mkdir -p pdfs && curl -sL -o pdfs/docling-paper.pdf https://arxiv.org/pdf/2408.09869 \
  && curl -sL -o pdfs/docling-report-v2.pdf https://arxiv.org/pdf/2501.17887

export DOCLING_SERVICE_URL=... DOCLING_API_KEY=...
.venv/bin/python ingest.py --bucket oap-rag-example --index product-docs --create pdfs
```

`--create` makes the vector bucket and a 1024-dim cosine index with `text`
non-filterable. Converted documents are cached in `.docling-cache/`, so re-runs only
re-embed. Each chunk is stored as:

| Field | Value |
|---|---|
| key | `<file>#<n>` |
| `text` (non-filterable) | Chunk text, prefixed with its heading path |
| `source` | File name |
| `page` | First page the chunk comes from |
| `headings` | Heading path, e.g. `3 Processing pipeline > 3.1 PDF backends` |

## 2. Deploy the server and an agent

`platform/oam/examples/example-s3-vectors-mcp.yaml` points at the same bucket and
index. Change its `description` to say what you ingested (it is shown to the agent),
then apply it to a cluster that has the `s3-vectors-mcp` definition.

## 3. Ask

Questions whose answers sit on a known page work best for a demo. With the two
Docling papers:

- "Which PDF backends does Docling support?" → `docling-paper.pdf` p2, *3.1 PDF backends*
- "What OCR engines can Docling use?" → `docling-paper.pdf` p4, *OCR*
- "How does Docling recognise table structure?" → both papers, *TableFormer*

## Moving this into a pipeline

The four functions map one-to-one onto a Lambda triggered when Docling writes a
converted document: drop `convert` (Docling's batch API can write JSON straight to an
S3 prefix), and run `chunk`, `embed` and `put_vectors` per object. Keep the model,
dimension and `text` key in step with the `s3-vectors-mcp` component.

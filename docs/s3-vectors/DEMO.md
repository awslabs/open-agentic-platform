# Demo: ask an agent about your PDFs (Docling + S3 Vectors + OAP)

**Story.** PDFs go in, an agent answers questions about them and cites file and page.
IBM Docling understands the documents, Amazon S3 Vectors stores them cheaply, and the
Open Agentic Platform (OAP) gives agents a governed search tool.

**Demo content.** Docling's own two technical papers
([2408.09869](https://arxiv.org/abs/2408.09869), [2501.17887](https://arxiv.org/abs/2501.17887)),
so the agent answers questions about Docling from Docling-processed documents.

## Flow

```
PDFs ─► Docling (IBM SaaS) ─► split into sections ─► Bedrock embeddings ─► S3 Vectors
                                                                              ▲
user ─► OAP agent ─► OAP gateway ─► search tool (MCP server, read-only) ──────┘
```

## 1. Index the documents (about 1 minute)

*Say:* "Docling converts each PDF into a structured document that keeps headings,
tables and pages. We split it along that structure, turn each section into a vector
with Amazon Bedrock, and store it in S3 Vectors with its file name and page. In
production this runs as a pipeline, for example a Lambda on S3 uploads; here it is one
command."

```bash
cd examples/s3-vectors-rag
.venv/bin/python ingest.py --bucket oap-rag-example --index product-docs --create pdfs
```

*Show:* each file converting in seconds, then about 30 sections per paper written to
the index.

## 2. Give agents a search tool (one OAM file)

*Say:* "A team deploys a read-only search server pointed at the index. They name the
bucket, the index and the embedding model; the platform generates AWS permissions for
that one index and model only, and the gateway decides which agents may call it."

```bash
kubectl apply -f platform/oam/examples/example-s3-vectors-mcp.yaml
```

*Show:* the file is about 40 lines with no region, account or credentials in it.

## 3. Ask the agent

| Question | Expected source |
|---|---|
| Which PDF backends does Docling support? | docling-paper.pdf, p2, *3.1 PDF backends* |
| What OCR engines can Docling use? | docling-paper.pdf, p4, *OCR* |
| How does Docling recognise table structure? | both papers, *TableFormer* |

*Say:* "The agent called the search tool, got the matching sections, and answered from
them with the source and page."

## Talking points

- **Docling** splits by document structure, which retrieves better than plain text splits.
- **S3 Vectors** means no vector database to run, at S3 cost.
- **OAP** makes search a governed tool: scoped IAM, agent-level access control, and the
  same OAM file deploys unchanged to any region or account.
- **Security:** IBM gets no AWS credentials. The demo uploads the PDFs directly; the
  pipeline design hands Docling short-lived S3 links instead.

## Status

| Step | State |
|---|---|
| Index the documents | Working against real AWS (60 vectors in `oap-rag-example/product-docs`) |
| Search tool | Working against that index; component on test cluster pending rollout |
| Agent Q&A on the cluster | Next, after rollout |
| Ingest as a job in AWS (instead of a laptop) | Planned: EKS Job for the demo, Lambda for production |

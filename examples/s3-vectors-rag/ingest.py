"""Demo ingestion: PDFs -> Docling (IBM watsonx SaaS) -> chunks -> Bedrock embeddings -> S3 Vectors.

Stands in for the ingestion pipeline, which in production runs outside OAP (for
example a Lambda per converted document). The steps are the same:

  1. convert   Docling turns each PDF into a DoclingDocument (JSON), keeping headings,
               tables and page numbers.
  2. chunk     docling-core's HybridChunker splits along that structure, bounded by tokens.
  3. embed     Bedrock Titan Text Embeddings v2, at the index's dimension.
  4. write     PutVectors. Chunk text goes in metadata under "text" (non-filterable);
               source, page and headings are filterable.

The s3-vectors-mcp server must query with the same model and dimension, and reads the
chunk text from the same "text" key (docs/s3-vectors/DESIGN.md, pipeline contract).

  export DOCLING_SERVICE_URL=... DOCLING_API_KEY=...
  python ingest.py --bucket oap-rag-example --index product-docs --create ./pdfs/*.pdf

Re-running is safe: vector keys are <file>#<n>, so a document's chunks overwrite their
previous version. If a document gets SHORTER, its old trailing chunks remain; delete
them (or the index) when that matters.
"""

import argparse
import json
import os
import pathlib
import sys
import time

import boto3
import httpx
import tiktoken
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
from docling_core.types.doc import DoclingDocument

TEXT_KEY = "text"
PUT_BATCH = 100  # PutVectors accepts up to 500 per call (S3 Vectors limits)


# ---- 1. convert ---------------------------------------------------------------

def convert(pdf: pathlib.Path, cache: pathlib.Path) -> DoclingDocument:
    """Convert one PDF with Docling and return its DoclingDocument. Cached on disk."""
    cached = cache / f"{pdf.name}.json"
    if cached.exists():
        print(f"  convert  {pdf.name}: cached")
        return DoclingDocument.model_validate_json(cached.read_text())

    url = os.environ.get("DOCLING_SERVICE_URL", "").rstrip("/")
    key = os.environ.get("DOCLING_API_KEY", "")
    if not url or not key:
        sys.exit("set DOCLING_SERVICE_URL and DOCLING_API_KEY")
    headers = {"X-Api-Key": key}
    with httpx.Client(timeout=120) as http:
        started = time.monotonic()
        with pdf.open("rb") as f:
            # The service honours the docling-serve form field `to_formats`; the
            # documented JSON `options` field was ignored when tested (Markdown only).
            r = http.post(f"{url}/v1/convert/file/async", headers=headers,
                          files={"files": (pdf.name, f, "application/pdf")},
                          data={"to_formats": "json"})
        r.raise_for_status()
        task = r.json()["task_id"]
        # ?wait= blocks server-side until the task finishes or the wait expires.
        for _ in range(20):
            status = http.get(f"{url}/v1/status/poll/{task}", params={"wait": 60},
                              headers=headers, timeout=90).json()
            if status["task_status"] in ("success", "failure"):
                break
        if status["task_status"] != "success":
            sys.exit(f"docling failed for {pdf.name}: {status.get('error_message') or status['task_status']}")
        result = http.get(f"{url}/v1/result/{task}", headers=headers).json()
        doc = result["documents"][0]
        if doc["status"] != "success":
            print(f"  convert  {pdf.name}: docling status {doc['status']} {doc.get('errors')}")
        artifact = next(a for a in doc["artifacts"] if a["artifact_type"] == "json")
        body = http.get(artifact["uri"]).text  # presigned URL, no API key
    cached.write_text(body)
    print(f"  convert  {pdf.name}: {time.monotonic() - started:.0f}s "
          f"(quality {doc.get('confidence', {}).get('mean_grade', '?')})")
    return DoclingDocument.model_validate_json(body)


# ---- 2. chunk -------------------------------------------------------------------

def chunk(doc: DoclingDocument, source: str, max_tokens: int) -> list[dict]:
    # tiktoken counts tokens approximately for Titan; it only bounds chunk size here.
    chunker = HybridChunker(
        tokenizer=OpenAITokenizer(tokenizer=tiktoken.get_encoding("cl100k_base"), max_tokens=max_tokens)
    )
    out = []
    for n, c in enumerate(chunker.chunk(doc)):
        pages = sorted({p.page_no for item in c.meta.doc_items for p in item.prov})
        headings = " > ".join(c.meta.headings or [])
        out.append({
            "key": f"{source}#{n:04d}",
            # contextualize() prefixes the heading path, which improves retrieval.
            "embed_text": chunker.contextualize(c),
            "metadata": {
                TEXT_KEY: chunker.contextualize(c),
                "source": source,
                "page": pages[0] if pages else 0,
                # Filterable metadata is capped at 2 KB per vector in total.
                "headings": headings[:500],
            },
        })
    return out


# ---- 3. embed + 4. write --------------------------------------------------------

def embed(bedrock, model: str, dimension: int, text: str) -> list[float]:
    body = {"inputText": text, "dimensions": dimension, "normalize": True}
    r = bedrock.invoke_model(modelId=model, body=json.dumps(body),
                             contentType="application/json", accept="application/json")
    return json.loads(r["body"].read())["embedding"]


def ensure_index(s3v, bucket: str, index: str, dimension: int) -> int:
    try:
        s3v.get_vector_bucket(vectorBucketName=bucket)
    except s3v.exceptions.NotFoundException:
        s3v.create_vector_bucket(vectorBucketName=bucket)
        print(f"created vector bucket {bucket}")
    try:
        return int(s3v.get_index(vectorBucketName=bucket, indexName=index)["index"]["dimension"])
    except s3v.exceptions.NotFoundException:
        s3v.create_index(vectorBucketName=bucket, indexName=index, dataType="float32",
                         dimension=dimension, distanceMetric="cosine",
                         metadataConfiguration={"nonFilterableMetadataKeys": [TEXT_KEY]})
        print(f"created index {index} ({dimension} dims, cosine, '{TEXT_KEY}' non-filterable)")
        return dimension


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdfs", nargs="+", type=pathlib.Path, help="PDF files or directories of PDFs")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--create", action="store_true", help="create the bucket and index if missing")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--model", default="amazon.titan-embed-text-v2:0")
    ap.add_argument("--dimension", type=int, default=1024, help="used only when creating the index")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--cache", type=pathlib.Path, default=pathlib.Path(".docling-cache"))
    args = ap.parse_args()

    pdfs = sorted({p for a in args.pdfs for p in ([a] if a.is_file() else a.glob("*.pdf"))})
    if not pdfs:
        sys.exit("no PDFs found")
    args.cache.mkdir(exist_ok=True)
    s3v = boto3.client("s3vectors", region_name=args.region)
    bedrock = boto3.client("bedrock-runtime", region_name=args.region)

    if args.create:
        dimension = ensure_index(s3v, args.bucket, args.index, args.dimension)
    else:
        dimension = int(s3v.get_index(vectorBucketName=args.bucket, indexName=args.index)["index"]["dimension"])

    total = 0
    for pdf in pdfs:
        print(pdf.name)
        chunks = chunk(convert(pdf, args.cache), pdf.name, args.max_tokens)
        print(f"  chunk    {len(chunks)} chunks")
        vectors = [{"key": c["key"], "data": {"float32": embed(bedrock, args.model, dimension, c["embed_text"])},
                    "metadata": c["metadata"]} for c in chunks]
        for i in range(0, len(vectors), PUT_BATCH):
            s3v.put_vectors(vectorBucketName=args.bucket, indexName=args.index, vectors=vectors[i:i + PUT_BATCH])
        print(f"  write    {len(vectors)} vectors -> {args.bucket}/{args.index}")
        total += len(vectors)
    print(f"done: {total} vectors from {len(pdfs)} document(s), model {args.model}, {dimension} dims")


if __name__ == "__main__":
    main()

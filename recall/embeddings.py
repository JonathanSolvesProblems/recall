"""Embeddings via Amazon Bedrock (Titan Text Embeddings V2).

Titan V2 emits 1024 dimensions, which is what `VECTOR(1024)` in the schema is
sized for. Swapping the embedding model means altering that column width and
re-embedding, so the dimension is asserted here rather than discovered at
insert time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import struct

import boto3
from botocore.config import Config

from recall.config import settings

log = logging.getLogger(__name__)

_client = None


def client():
    global _client
    if _client is None:
        cfg = settings()
        _client = boto3.client(
            "bedrock-runtime",
            region_name=cfg.aws_region,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )
    return _client


def embed(text: str) -> list[float]:
    """Embed one string. Returns a 1024-dimension unit vector."""
    cfg = settings()

    if os.environ.get("RECALL_ALLOW_FAKE_EMBEDDINGS") == "1":
        return _deterministic_stub(text, cfg.bedrock_embed_dim)

    body = json.dumps(
        {"inputText": text, "dimensions": cfg.bedrock_embed_dim, "normalize": True}
    )
    resp = client().invoke_model(modelId=cfg.bedrock_embed_model_id, body=body)
    vec = json.loads(resp["body"].read())["embedding"]

    if len(vec) != cfg.bedrock_embed_dim:
        raise ValueError(
            f"{cfg.bedrock_embed_model_id} returned {len(vec)} dimensions, "
            f"but the schema declares VECTOR({cfg.bedrock_embed_dim})"
        )
    return vec


def embed_many(texts: list[str]) -> list[list[float]]:
    """Titan has no batch endpoint, so this is a loop by necessity."""
    return [embed(t) for t in texts]


def to_sql(vec: list[float]) -> str:
    """Render a vector in the literal form CockroachDB's VECTOR type parses."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def _deterministic_stub(text: str, dim: int) -> list[float]:
    """Offline stand-in so the pipeline can be exercised without AWS credentials.

    Deliberately gated behind an environment variable and never a silent
    fallback. Semantic retrieval quality with this is meaningless; it exists so
    that schema, provenance, and sweep logic can be tested and unit-tested
    without network access. Every measured number in the README comes from
    Bedrock, not from this.
    """
    log.warning("using STUB embeddings: retrieval quality is meaningless")
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        block = hashlib.sha256(seed + struct.pack(">I", counter)).digest()
        for i in range(0, len(block), 4):
            if len(out) >= dim:
                break
            out.append((struct.unpack(">I", block[i : i + 4])[0] / 2**32) - 0.5)
        counter += 1
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]

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
import random
import struct
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from unsay.config import settings

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


THROTTLE_CODES = {
    "ThrottlingException", "TooManyRequestsException",
    "ServiceUnavailableException", "ModelTimeoutException",
    "InternalServerException",
}


def embed(text: str, *, max_attempts: int = 6) -> list[float]:
    """Embed one string. Returns a 1024-dimension unit vector.

    Retries throttling explicitly rather than trusting botocore's adaptive
    mode alone. Titan has no batch endpoint, so a bulk ingest is thousands of
    sequential calls, and any other Bedrock work running at the same time
    shares the same account quota. A long ingest dying two thirds of the way
    through because something else was also talking to Bedrock is not a
    failure worth propagating.
    """
    cfg = settings()

    if os.environ.get("UNSAY_ALLOW_FAKE_EMBEDDINGS") == "1":
        return _deterministic_stub(text, cfg.bedrock_embed_dim)

    body = json.dumps(
        {"inputText": text, "dimensions": cfg.bedrock_embed_dim, "normalize": True}
    )

    for attempt in range(1, max_attempts + 1):
        try:
            resp = client().invoke_model(modelId=cfg.bedrock_embed_model_id, body=body)
            break
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in THROTTLE_CODES or attempt == max_attempts:
                raise
            delay = min(0.5 * (2 ** (attempt - 1)), 20.0)
            delay += random.uniform(0, delay)
            log.warning("embed: %s on attempt %d/%d, retrying in %.1fs",
                        code, attempt, max_attempts, delay)
            time.sleep(delay)

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


_stub_warned = False


def _warn_stub_once() -> None:
    """Say it once per process, not once per call.

    The contention test embeds 64 times and the ingest thousands, so a
    per-call warning buried the actual result under identical lines. A warning
    repeated 64 times is not 64 times as informative; it is less, because the
    reader stops reading it and scrolls past the answer too.
    """
    global _stub_warned
    if not _stub_warned:
        _stub_warned = True
        log.warning(
            "using STUB embeddings for this process: retrieval quality is "
            "meaningless. Structural results (versioning, provenance, sweep) "
            "are unaffected."
        )


def _deterministic_stub(text: str, dim: int) -> list[float]:
    """Offline stand-in so the pipeline can be exercised without AWS credentials.

    Deliberately gated behind an environment variable and never a silent
    fallback. Semantic retrieval quality with this is meaningless; it exists so
    that schema, provenance, and sweep logic can be tested and unit-tested
    without network access. Every measured number in the README comes from
    Bedrock, not from this.
    """
    _warn_stub_once()
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

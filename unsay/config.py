"""Runtime configuration, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # CockroachDB
    unsay_dsn: str = (
        "postgresql://root@localhost:26257,localhost:26258,localhost:26259"
        "/unsay?sslmode=disable"
    )
    unsay_cloud_dsn: str = ""

    # Managed MCP Server
    crdb_mcp_endpoint: str = "https://cockroachlabs.cloud/mcp"
    crdb_mcp_api_key: str = ""
    crdb_cluster_id: str = ""

    # AWS
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    # Bulk mechanical work (turning chat turns into keyed facts) does not
    # need the reasoning model, and extraction dominates benchmark cost by
    # roughly 40:1 on call volume.
    bedrock_extract_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
    bedrock_embed_dim: int = 1024
    unsay_s3_bucket: str = ""

    # Ingestion
    openfda_api_key: str = ""


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()

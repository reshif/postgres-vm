from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_", extra="ignore")

    # Pooled path (API only). See the topology note in docker-compose.yml.
    database_url: str = "postgresql+psycopg://memory_app:change-me-app@localhost:6432/memory"
    # Direct path (workers, scheduler, migrations).
    database_url_direct: str = "postgresql+psycopg://memory_app:change-me-app@localhost:5432/memory"
    db_prepare_threshold: int = 0

    embedding_provider: str = "local"
    embedding_url: str = "http://localhost:8090"
    embedding_model: str = "bge-m3@1"
    embedding_dim: int = 1024
    embedding_startup_grace_s: int = 180

    # Cross-encoder rerank (ADR-0013): off by default, enabled per project only
    # after the eval suite shows it wins.
    rerank_enabled: bool = False
    rerank_url: str = "http://reranker:80"
    rerank_top_k: int = 40

    # Plane A poll ingestion. The scheduler reconciles the .memory/ tree on this
    # interval so committed knowledge reaches the platform without anyone running
    # a command. Off unless a repo path and a scope binding are configured.
    ingest_enabled: bool = False
    ingest_repo_path: str = "/repo"
    ingest_interval_s: int = 60

    # Scope for unattended work (poll ingestion). Same dev-binding caveat as the
    # MCP gateway: ADR-0004 wants this from a token, and a background loop has no
    # request to carry one, so a service identity is the eventual answer.
    dev_tenant_id: str = ""
    dev_project_id: str = ""
    dev_principal_id: str = ""

    # Admission control (Phase 9). Off by default: a limit nobody tuned for the
    # deployment is a self-inflicted outage waiting for a traffic spike.
    limits_enabled: bool = False
    rate_limit_read_rps: int = 50
    rate_limit_write_rps: int = 10
    max_queue_depth: int = 10_000
    max_memories_per_tenant: int = 0      # 0 = unlimited

    # OAuth (ADR-0004). Empty issuer = disabled, and the gateway then falls back
    # to the dev binding, loudly. Algorithms are pinned here and never read from
    # the token header.
    oauth_issuer: str = ""
    oauth_audience: str = ""
    oauth_jwks_url: str = ""
    oauth_algorithms: str = "RS256,ES256"
    oauth_leeway_s: int = 30
    oauth_org_claim: str = "org"
    oauth_project_claim: str = "project"

    # Per-tenant partial HNSW index. 01-SCHEMA.sql suggests ~50k memories;
    # that is a guess about someone else's hardware, so it is a knob rather
    # than a hard-coded wait. 0 disables; any positive value is the row count
    # at which the index is built automatically.
    partial_index_threshold: int = 5000

    # LLM extraction (Phase 5). `none` is the correct default and the only safe
    # one until a curator exists: ADR-0015 makes the Inbox a precondition for the
    # extractor, not a companion to it.
    llm_provider: str = "none"
    llm_model: str = ""
    llm_url: str = "http://localhost:11434"
    llm_timeout_s: float = 120.0
    # A hard ceiling on proposals per extraction call. The prompt asks for
    # restraint; this enforces it, because a model that ignores "be conservative"
    # would otherwise turn one session into forty inbox items.
    llm_max_candidates: int = 5

    role: str = "api"
    log_level: str = "info"
    worker_queues: str = "embedding,ingestion,extraction"


@lru_cache
def settings() -> Settings:
    return Settings()

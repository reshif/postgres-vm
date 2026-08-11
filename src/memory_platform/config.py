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
    # Raw cross-encoder scores corroborate direct relevance evidence, but do not
    # stand alone. The floor is independent of profile weights, which also
    # contain trust/recency and cannot establish answerability.
    evidence_rerank_min_score: float = 0.05

    # Plane A poll ingestion. The scheduler reconciles the .memory/ tree on this
    # interval so committed knowledge reaches the platform without anyone running
    # a command. Off unless a repo path and a scope binding are configured.
    ingest_enabled: bool = False
    ingest_repo_path: str = "/repo"
    ingest_interval_s: int = 60

    # GitHub-native evidence is the replacement authority for the legacy
    # checkout/.memory ingestion path. It is deliberately disabled by default:
    # an endpoint which accepts signed external events is only useful once the
    # GitHub App and its webhook secret have been provisioned for the project.
    # Enabling it never makes webhook text retrievable; handlers store a small,
    # signed delivery envelope and queue deterministic processing.
    github_enabled: bool = False
    github_webhook_secret: str = ""
    github_app_id: str = ""
    github_private_key: str = ""
    github_api_url: str = "https://api.github.com"
    # Deployment-owned Fernet key for the Console's project-scoped PAT store.
    # This is intentionally separate from the GitHub App private key and must
    # be identical on every API/worker replica that may use a credential.
    github_pat_encryption_key: str = ""
    github_evidence_suffix: str = "-evidence"
    github_webhook_max_bytes: int = 1_048_576
    github_max_blob_bytes: int = 262_144
    github_max_sync_files: int = 250

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

    # Browser console OAuth client. This is a public SPA client: it uses
    # authorization-code + PKCE and keeps the access token in session storage.
    # The client id and endpoints are safe to return from /v1/console/config;
    # no client secret is valid or accepted for this flow.
    console_oidc_client_id: str = ""
    console_oidc_scopes: str = "openid profile"
    console_oidc_redirect_uri: str = ""
    console_oidc_resource: str = ""
    console_oidc_authorization_endpoint: str = ""
    console_oidc_token_endpoint: str = ""

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

    # Consolidation (00-MASTER-BLUEPRINT §6.5). The blueprint's ">= 20 similar
    # episodes older than 30 days" is right for a busy project and wrong for a
    # new one, where it means the pass never fires and the feature quietly does
    # not exist. Defaults follow the blueprint; every one is a knob, and each run
    # records the values it used so old audit rows stay interpretable.
    consolidation_enabled: bool = True
    # Tighter than retrieval's MMR dedup (0.94) on purpose: MMR hides one result
    # from one response, this archives a row.
    consolidation_dedup_cosine: float = 0.97
    consolidation_compact_cosine: float = 0.88
    consolidation_min_episodes: int = 20
    consolidation_age_days: int = 30
    # Bounds one pass. Consolidation is O(n) similarity probes against an index
    # and runs on the scheduler alongside everything else; an unbounded first run
    # on a large project would hold a transaction open for a very long time.
    consolidation_batch_size: int = 2000

    # Procedure distillation (§6.5 item 3). Its output is a PULL REQUEST, never a
    # memory row — see distillation.py for why that is structural rather than a
    # convention. Default 4 successful runs, as the blueprint specifies.
    distillation_enabled: bool = True
    distillation_min_episodes: int = 4
    distillation_branch_prefix: str = "memory/procedure-"
    # Where the reviewable patch is written when no remote accepts the branch.
    # Inside the container by default; mount it to keep proposals across restarts.
    distillation_output_dir: str = "/tmp/memory-distillation"
    distillation_remote: str = "origin"

    # ADR-0008's keep-or-cut rule for retrieval arms: "an arm contributing under
    # ~3% of returned items over a month should be removed rather than tuned".
    # The floor and the window are the blueprint's; both are settings because the
    # rule licenses DELETING an arm, and that decision should be made against
    # numbers an operator chose rather than ones compiled in.
    arm_contribution_floor: float = 0.03
    arm_contribution_window_days: int = 30
    # Below this many attributed events the report withholds its verdict instead
    # of recommending deletion from a handful of queries.
    arm_contribution_min_events: int = 200

    # ADR-0012: cross-project generalisation is built last and SHIPS DISABLED.
    # False here closes the proposal path outright rather than queueing
    # promotions that would go through the moment somebody flips the flag.
    org_entities_enabled: bool = False

    role: str = "api"
    log_level: str = "info"
    worker_queues: str = "embedding,ingestion,extraction,github"


@lru_cache
def settings() -> Settings:
    return Settings()

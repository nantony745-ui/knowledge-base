from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx
import httpx2
from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    MCP_AUTH_ENV_KEYS: ClassVar[frozenset[str]] = frozenset({'MCP_DEMO_BEARER_TOKEN', 'MCP_TAVILY_API_KEY'})
    MCP_CREDENTIALS_FILE: ClassVar[Path] = ROOT / '.env.mcp'
    MODEL_SETTINGS_FILE: ClassVar[Path] = ROOT / '.env.models'
    MODEL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        'CHAT_BASE_URL', 'CHAT_API_KEY', 'CHAT_MODEL',
        'EMBED_BASE_URL', 'EMBED_API_KEY', 'EMBED_MODEL',
        'RERANK_URL', 'RERANK_API_KEY', 'RERANK_MODEL',
    })
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True,
        populate_by_name=True,
    )
    CHAT_BASE_URL: str | None = None
    CHAT_API_KEY: SecretStr | None = None
    CHAT_MODEL: str | None = None
    # SiliconFlow DeepSeek-V4-Flash: https://www.siliconflow.cn/models (contextLen=1048576).
    CHAT_CONTEXT_WINDOW: int = Field(1048576, gt=0)
    CHAT_MAX_OUTPUT_TOKENS: int = Field(8192, gt=0)
    CONTEXT_SAFETY_TOKENS: int = Field(1024, ge=0)
    CONTEXT_KEEP_TURNS: int = Field(2, ge=0)
    TOOL_RESULT_MAX_CHARS: int = Field(8000, gt=0)
    SEARCH_KNOWLEDGE_CONCURRENCY: int = Field(4, ge=1)
    MCP_DEMO_BEARER_TOKEN: SecretStr | None = None
    MCP_TAVILY_API_KEY: SecretStr | None = None
    MCP_OPERATION_TIMEOUT: float = Field(30, gt=0)
    MCP_CLEANUP_TIMEOUT: float = Field(5, gt=0)
    MCP_MAX_LIST_PAGES: int = Field(100, gt=0)
    MCP_MAX_TOOLS: int = Field(2000, gt=0)
    MCP_MAX_SCHEMA_BYTES: int = Field(256 * 1024, gt=0)
    EMBED_BASE_URL: str | None = Field(None, validation_alias=AliasChoices("EMBED_BASE_URL", "Embedding_Model_URL"))
    EMBED_API_KEY: SecretStr | None = None
    EMBED_MODEL: str | None = Field(None, validation_alias=AliasChoices("EMBED_MODEL", "Embedding_Model"))
    EMBED_DIMENSION: int | None = Field(None, gt=0)
    RERANK_URL: str | None = Field(None, validation_alias=AliasChoices("RERANK_URL", "Rerank_Model_URL"))
    RERANK_API_KEY: SecretStr | None = None
    RERANK_MODEL: str | None = Field(None, validation_alias=AliasChoices("RERANK_MODEL", "Rerank_Model"))
    MINERU_BASE_URL: str | None = Field(None, validation_alias=AliasChoices("MINERU_BASE_URL", "Mineru_url"))
    MINERU_API_KEY: SecretStr | None = Field(None, validation_alias=AliasChoices("MINERU_API_KEY", "Mineru_API_KEY"))
    MINERU_MODEL: str | None = None
    LANGFUSE_BASE_URL: str | None = None
    LANGFUSE_PUBLIC_KEY: SecretStr | None = None
    LANGFUSE_SECRET_KEY: SecretStr | None = None
    CHAT_CONNECT_TIMEOUT: float = Field(10, gt=0)
    CHAT_READ_TIMEOUT: float = Field(120, gt=0)
    CHAT_WRITE_TIMEOUT: float = Field(60, gt=0)
    CHAT_POOL_TIMEOUT: float = Field(10, gt=0)
    EMBED_CONNECT_TIMEOUT: float = Field(10, gt=0)
    EMBED_READ_TIMEOUT: float = Field(120, gt=0)
    EMBED_WRITE_TIMEOUT: float = Field(60, gt=0)
    EMBED_POOL_TIMEOUT: float = Field(10, gt=0)
    RERANK_CONNECT_TIMEOUT: float = Field(10, gt=0)
    RERANK_READ_TIMEOUT: float = Field(120, gt=0)
    RERANK_WRITE_TIMEOUT: float = Field(60, gt=0)
    RERANK_POOL_TIMEOUT: float = Field(10, gt=0)
    MINERU_CONNECT_TIMEOUT: float = Field(10, gt=0)
    MINERU_READ_TIMEOUT: float = Field(120, gt=0)
    MINERU_WRITE_TIMEOUT: float = Field(60, gt=0)
    MINERU_POOL_TIMEOUT: float = Field(10, gt=0)
    MINERU_POLL_INTERVAL: float = Field(3, gt=0)
    MINERU_POLL_TIMEOUT: float = Field(600, gt=0)
    LANGFUSE_QUERY_TIMEOUT: float = Field(60, gt=0)
    CHAT_PROBE_MAX_TOKENS: int = Field(256, gt=0)
    CHAT_PROBE_ENABLE_THINKING: bool = False
    MYSQL_HOST: str = '127.0.0.1'
    MYSQL_PORT: int = 3316
    MYSQL_DATABASE: str = 'kb'
    MYSQL_USER: str = 'kb'
    MYSQL_PASSWORD: SecretStr | None = None
    REDIS_HOST: str = '127.0.0.1'
    REDIS_PORT: int = 6380
    REDIS_PASSWORD: SecretStr | None = None
    REDIS_DB: int = 0
    INGEST_QUEUE: str = 'personal_knora_ingest'
    JOB_TIMEOUT: int = 900
    ENQUEUE_GRACE_SECONDS: int = 60
    LOCK_LEASE_SECONDS: int = 60
    MILVUS_URI: str = 'http://127.0.0.1:19540'
    MILVUS_COLLECTION: str = 'personal_knora_chunks'
    VECTOR_TIMEOUT: float = 15
    VERIFY_TIMEOUT: float = 60
    EMBED_BATCH_SIZE: int = Field(16, gt=0)
    CHUNK_SIZE: int = Field(512, gt=80)
    CHUNK_OVERLAP: int = Field(80, ge=0, lt=512)
    MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024
    DATA_DIR: Path = ROOT / 'DATA/runtime'
    SKILL_DIRS: tuple[Path, ...] = (ROOT / 'skills',)
    ADMIN_USERNAME: str | None = None
    ADMIN_PASSWORD: SecretStr | None = None
    JWT_SECRET: SecretStr | None = None
    APP_ORIGIN: str = 'http://127.0.0.1:8000'
    COOKIE_SECURE: bool = False
    SESSION_COOKIE_NAME: str = 'knora_session'
    SESSION_TTL_SECONDS: int = Field(30 * 24 * 60 * 60, gt=0)
    INGEST_WORKER_NAME: str = 'personal-knora-worker'
    RETRIEVAL_ROUTE_LIMIT: int = Field(20, ge=1, le=20)
    RETRIEVAL_FUSED_LIMIT: int = Field(20, ge=1, le=20)
    RETRIEVAL_FINAL_LIMIT: int = Field(5, ge=1, le=5)
    RETRIEVAL_RRF_K: int = Field(60, ge=1)
    BM25_MAX_CHUNKS_PER_KB: int = Field(25000, ge=1, le=25000)
    QUICK_HISTORY_TURNS: int = Field(6, ge=1, le=20)
    QUICK_MAX_TOKENS: int = Field(8192, ge=128, le=10000)
    RUN_EVENT_RETENTION_SECONDS: int = Field(3600, ge=60)
    RUN_COMPLETED_LIMIT: int = Field(100, ge=1)

    @field_validator("CHAT_BASE_URL", "EMBED_BASE_URL", "RERANK_URL", "MINERU_BASE_URL", "LANGFUSE_BASE_URL")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("需要不含凭据或查询参数的 HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("EMBED_BASE_URL", "MINERU_BASE_URL")
    @classmethod
    def normalize_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        url = urlsplit(value)
        path = url.path
        for suffix in ("/embeddings", "/file-urls/batch"):
            if path.endswith(suffix):
                path = path.removesuffix(suffix)
        return urlunsplit((url.scheme, url.netloc, path, "", ""))

    def timeout(self, service: str, sdk: bool):
        timeout_type = httpx2.Timeout if sdk else httpx.Timeout
        return timeout_type(**{part: getattr(self, f"{service}_{part.upper()}_TIMEOUT") for part in ("connect", "read", "write", "pool")})

    def require(self, *names: str) -> None:
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            from app.adapters.common import AdapterError
            raise AdapterError("configuration_missing", False, "缺少配置：" + ", ".join(missing), None)


def load_settings() -> Settings:
    try:
        overrides = dotenv_values(Settings.MODEL_SETTINGS_FILE, interpolate=False)
        return Settings(**{key: value for key, value in overrides.items() if key in Settings.MODEL_FIELDS})
    except ValidationError as exc:
        from app.adapters.common import AdapterError
        fields = sorted({str(error["loc"][0]) for error in exc.errors(include_input=False)})
        raise AdapterError("configuration_invalid", False, "配置无效：" + ", ".join(fields), None) from None

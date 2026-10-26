from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Record(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator('created_at', 'updated_at', check_fields=False)
    @classmethod
    def utc_date(cls, value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class KBCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class KBUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None

    @field_validator('name')
    @classmethod
    def name_not_null(cls, value):
        if value is None:
            raise ValueError('名称不能为空')
        return value


class KBOut(Record):
    id: str
    name: str
    description: str | None
    status: str
    index_version: int
    created_at: datetime
    updated_at: datetime


class DocumentOut(Record):
    id: str
    kb_id: str
    original_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    parse_status: str
    stage: str
    job_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class Queued(BaseModel):
    document_id: str
    job_id: str
    status: str


class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    query: str = Field(min_length=1, max_length=8192)
    top_k: int = Field(5, ge=1, le=20)


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    kb_id: str
    document_name: str
    content: str
    title: str | None
    score: float
    score_metric: str = 'COSINE'
    page_start: int | None
    page_end: int | None
    source_meta: dict[str, Any]
    download_url: str

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, INTEGER, JSON, LONGTEXT, MEDIUMTEXT, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def uuid_column():
    return CHAR(36, charset='ascii', collation='ascii_bin')


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))


class MCPService(Base):
    __tablename__ = 'mcp_services'
    __table_args__ = (Index('ix_mcp_owner', 'owner_id', 'is_enabled'),
                      {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4',
                       'mysql_collate': 'utf8mb4_0900_ai_ci'})
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    owner_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('users.id', ondelete='RESTRICT'))
    name: Mapped[str] = mapped_column(VARCHAR(128))
    endpoint: Mapped[str] = mapped_column(VARCHAR(2048))
    auth_env_key: Mapped[str | None] = mapped_column(VARCHAR(128))
    tools_catalog: Mapped[list[dict]] = mapped_column(JSON)
    enabled_tools: Mapped[list[str]] = mapped_column(JSON)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text('1'))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)'), server_onupdate=text('CURRENT_TIMESTAMP(6)'))


class KnowledgeBase(Base):
    __tablename__ = 'knowledge_bases'
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    owner_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('users.id'))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'active'"))
    index_version: Mapped[int] = mapped_column(BIGINT(unsigned=True), server_default=text('0'))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)'), server_onupdate=text('CURRENT_TIMESTAMP(6)'))


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    kb_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('knowledge_bases.id'))
    original_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    sha256: Mapped[str] = mapped_column(CHAR(64, charset='ascii', collation='ascii_general_ci'))
    original_path: Mapped[str] = mapped_column(String(1024))
    parsed_path: Mapped[str | None] = mapped_column(String(1024))
    parse_status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    stage: Mapped[str] = mapped_column(String(32), server_default=text("'parse'"))
    job_id: Mapped[str | None] = mapped_column(String(64))
    mineru_batch_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)'), server_onupdate=text('CURRENT_TIMESTAMP(6)'))


class Chunk(Base):
    __tablename__ = 'chunks'
    __table_args__ = (UniqueConstraint('document_id', 'ordinal', name='uq_chunk_doc_order'),)
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    kb_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('knowledge_bases.id'))
    document_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('documents.id', ondelete='CASCADE'))
    ordinal: Mapped[int] = mapped_column(INTEGER(unsigned=True))
    content: Mapped[str] = mapped_column(MEDIUMTEXT)
    title: Mapped[str | None] = mapped_column(String(255))
    page_start: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    page_end: Mapped[int | None] = mapped_column(INTEGER(unsigned=True))
    source_meta: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))


class ConversationSession(Base):
    __tablename__ = 'sessions'
    __table_args__ = (Index('ix_sessions_owner', 'owner_id', 'updated_at'),)
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    owner_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('users.id', ondelete='RESTRICT'))
    title: Mapped[str] = mapped_column(String(255))
    next_message_seq: Mapped[int] = mapped_column(BIGINT(unsigned=True), server_default=text('1'))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (
        UniqueConstraint('session_id', 'seq', name='uq_messages_session_seq'),
        Index('ix_messages_turn', 'session_id', 'turn_id', 'seq'),
        CheckConstraint("role IN ('system','user','assistant','tool')", name='ck_messages_role'),
        CheckConstraint("status IN ('complete','partial','failed')", name='ck_messages_status'),
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    session_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('sessions.id', ondelete='CASCADE'))
    seq: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    turn_id: Mapped[str] = mapped_column(uuid_column())
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str | None] = mapped_column(LONGTEXT)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    run_id: Mapped[str | None] = mapped_column(uuid_column())
    citations: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'complete'"))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))


class StoredRun(Base):
    __tablename__ = 'runs'
    __table_args__ = {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_0900_ai_ci'}
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    session_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('sessions.id', ondelete='CASCADE'))


class ContextCheckpoint(Base):
    __tablename__ = 'context_summaries'
    __table_args__ = (UniqueConstraint('session_id', 'covered_message_seq', name='uq_summary_checkpoint'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_0900_ai_ci'})
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    session_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('sessions.id', ondelete='CASCADE'))
    summary: Mapped[str] = mapped_column(MEDIUMTEXT)
    covered_message_seq: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    sources: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), server_default=text('CURRENT_TIMESTAMP(6)'))


class RunSource(Base):
    __tablename__ = 'run_sources'
    __table_args__ = (UniqueConstraint('run_id', 'chunk_id', name='uq_run_chunk'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_0900_ai_ci'})
    run_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey('runs.id', ondelete='CASCADE'), primary_key=True)
    handle: Mapped[str] = mapped_column(VARCHAR(16, charset='ascii', collation='ascii_bin'), primary_key=True)
    chunk_id: Mapped[str | None] = mapped_column(uuid_column(), ForeignKey('chunks.id', ondelete='SET NULL'))
    document_id: Mapped[str | None] = mapped_column(uuid_column(), ForeignKey('documents.id', ondelete='SET NULL'))
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    evidence_granted: Mapped[bool] = mapped_column(Boolean, server_default=text('0'))

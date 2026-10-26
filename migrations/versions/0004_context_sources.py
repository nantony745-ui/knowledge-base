"""摘要检查点与来源；runs仅保存会话归属，不保存执行或事件状态。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, JSON, MEDIUMTEXT, VARCHAR

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def uid():
    return CHAR(36, charset='ascii', collation='ascii_bin')


def upgrade():
    options = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_ai_ci')
    op.create_table('runs', sa.Column('id', uid(), primary_key=True),
        sa.Column('session_id', uid(), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False), **options)
    op.create_table('context_summaries', sa.Column('id', uid(), primary_key=True),
        sa.Column('session_id', uid(), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('summary', MEDIUMTEXT(), nullable=False),
        sa.Column('covered_message_seq', BIGINT(unsigned=True), nullable=False),
        sa.Column('sources', JSON(), nullable=False),
        sa.Column('created_at', DATETIME(fsp=6), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(6)')),
        sa.UniqueConstraint('session_id', 'covered_message_seq', name='uq_summary_checkpoint'), **options)
    op.create_table('run_sources',
        sa.Column('run_id', uid(), sa.ForeignKey('runs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('handle', VARCHAR(16, charset='ascii', collation='ascii_bin'), primary_key=True),
        sa.Column('chunk_id', uid(), sa.ForeignKey('chunks.id', ondelete='SET NULL'), nullable=True),
        sa.Column('document_id', uid(), sa.ForeignKey('documents.id', ondelete='SET NULL'), nullable=True),
        sa.Column('source_snapshot', JSON(), nullable=False),
        sa.Column('evidence_granted', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.UniqueConstraint('run_id', 'chunk_id', name='uq_run_chunk'), **options)


def downgrade():
    raise RuntimeError('不自动删除摘要、运行归属或历史来源')

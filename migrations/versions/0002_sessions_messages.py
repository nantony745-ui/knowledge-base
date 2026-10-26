"""会话及消息历史；run/events仍仅在内存。"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade():
    from app.db.models import ConversationSession, Message
    for model in (ConversationSession, Message):
        model.__table__.create(op.get_bind(), checkfirst=False)
    op.execute('ALTER TABLE sessions MODIFY updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)')

def downgrade():
    raise RuntimeError('不自动删除会话历史')

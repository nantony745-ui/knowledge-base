"""仅在新隔离数据库复现已验收四表，不修改旧项目。"""
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    from app.db.models import User, KnowledgeBase, Document, Chunk
    for model in (User, KnowledgeBase, Document, Chunk):
        model.__table__.create(op.get_bind(), checkfirst=False)

def downgrade():
    raise RuntimeError('不自动执行破坏性回退；使用专属新数据库')

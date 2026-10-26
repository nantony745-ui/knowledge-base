"""补齐新隔离基线原本已有的时间默认值；不访问旧kb。"""
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

def upgrade():
    for table in ('documents','knowledge_bases'):
        op.execute(f'ALTER TABLE {table} MODIFY updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)')

def downgrade():
    raise RuntimeError('不移除已核实的原基线时间行为')

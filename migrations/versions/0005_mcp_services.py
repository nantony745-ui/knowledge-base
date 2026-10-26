"""外部只读MCP服务；不变更既有业务表。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import CHAR, DATETIME, JSON, VARCHAR

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    uid = lambda: CHAR(36, charset='ascii', collation='ascii_bin')
    op.create_table('mcp_services',
        sa.Column('id', uid(), primary_key=True),
        sa.Column('owner_id', uid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('name', VARCHAR(128), nullable=False),
        sa.Column('endpoint', VARCHAR(2048), nullable=False),
        sa.Column('auth_env_key', VARCHAR(128), nullable=True),
        sa.Column('tools_catalog', JSON(), nullable=False),
        sa.Column('enabled_tools', JSON(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', DATETIME(fsp=6), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(6)')),
        sa.Column('updated_at', DATETIME(fsp=6), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)')),
        mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_ai_ci')
    op.create_index('ix_mcp_owner', 'mcp_services', ['owner_id', 'is_enabled'])


def downgrade():
    raise RuntimeError('不自动删除MCP服务配置')

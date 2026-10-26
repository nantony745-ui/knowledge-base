from alembic import context
from app.db.session import engine
from sqlalchemy import inspect

external = context.config.attributes.get('connection')
if context.is_offline_mode():
    raise RuntimeError('数据库迁移需要在线连接')

def migrate(connection):
    tables = inspect(connection).get_table_names()
    if tables and 'alembic_version' not in tables:
        raise RuntimeError('目标数据库非空且未登记迁移，请使用空数据库')
    if external is None:
        connection.commit()
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if external is not None:
    migrate(external)
else:
    with engine().connect() as connection:
        migrate(connection)

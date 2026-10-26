from sqlalchemy import select

from app.adapters.mcp import tool_alias, validate_catalog
from app.api.common import fail, identifier
from app.db.models import MCPService
from app.db.session import session
from app.services.conversations import check_session

FIELDS = ('id', 'owner_id', 'name', 'endpoint', 'auth_env_key', 'tools_catalog',
          'enabled_tools', 'is_enabled', 'created_at', 'updated_at')


def snapshot(row: MCPService) -> dict:
    return {field: getattr(row, field) for field in FIELDS}


def owned(db, service_id: str, owner: str, lock: bool):
    statement = select(MCPService).where(MCPService.id == identifier(service_id), MCPService.owner_id == owner)
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        fail(404, 'mcp_service_unavailable', 'MCP服务不存在或无权限')
    return row


def list_services(owner: str) -> list[dict]:
    with session() as db:
        return [snapshot(row) for row in db.scalars(select(MCPService).where(
            MCPService.owner_id == owner).order_by(MCPService.created_at, MCPService.id))]


def get_service(service_id: str, owner: str) -> dict:
    with session() as db:
        return snapshot(owned(db, service_id, owner, False))


def save_service(owner: str, values: dict, previous: dict | None) -> dict:
    with session() as db:
        if previous is None:
            row = MCPService(owner_id=owner, **values)
        else:
            row = owned(db, previous['id'], owner, True)
            if row.updated_at != previous['updated_at']:
                fail(409, 'mcp_config_changed', '服务配置已变化，请重新加载')
            for key, value in values.items():
                setattr(row, key, value)
        validate_catalog(row.tools_catalog, row.id)
        available = {tool['name'] for tool in row.tools_catalog}
        if not set(row.enabled_tools).issubset(available):
            fail(422, 'mcp_unknown_tool', '白名单包含未发现的工具')
        aliases = set()
        others = db.scalars(select(MCPService).where(MCPService.owner_id == owner, MCPService.id != row.id))
        for item in [*others, row]:
            for name in item.enabled_tools:
                alias = tool_alias(item.id, name)
                if alias in aliases:
                    fail(409, 'mcp_alias_conflict', '工具别名冲突')
                aliases.add(alias)
        db.add(row)
        db.commit()
        db.refresh(row)
        return snapshot(row)


def delete_service(service_id: str, owner: str) -> None:
    with session() as db:
        db.delete(owned(db, service_id, owner, True))
        db.commit()


def recheck_service(previous: dict, remote_name: str, owner: str, session_id: str) -> None:
    check_session(session_id, owner)
    current = get_service(previous['id'], owner)
    if not current['is_enabled'] or remote_name not in current['enabled_tools']:
        fail(403, 'tool_disabled', 'MCP服务或工具已禁用')
    if any(current[key] != previous[key] for key in ('endpoint', 'auth_env_key', 'tools_catalog')):
        fail(409, 'mcp_config_changed', 'MCP配置已变化，请重新运行')

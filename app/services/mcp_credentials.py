from threading import Lock
import re
from uuid import uuid4

from dotenv import dotenv_values, set_key, unset_key
from pydantic import SecretStr

from app.adapters.common import AdapterError
from app.settings import Settings, load_settings

_write_lock = Lock()


def is_service_credential(key: str | None) -> bool:
    return bool(key and re.fullmatch(r'MCP_SERVICE_[a-f0-9]{32}', key))


def new_service_credential() -> str:
    return 'MCP_SERVICE_' + uuid4().hex


def load_mcp_settings(base: Settings) -> Settings:
    fresh = load_settings()
    online = dotenv_values(Settings.MCP_CREDENTIALS_FILE, interpolate=False)
    credentials = {key: SecretStr(online[key]) if online.get(key) else getattr(fresh, key)
                   for key in Settings.MCP_AUTH_ENV_KEYS}
    credentials.update({key: SecretStr(value) for key, value in online.items() if is_service_credential(key) and value})
    return base.model_copy(update=credentials)


def save_mcp_credential(key: str, token: SecretStr) -> None:
    if key not in Settings.MCP_AUTH_ENV_KEYS and not is_service_credential(key):
        raise AdapterError('mcp_auth_unregistered', False, '认证变量未登记为MCP凭据', None)
    try:
        with _write_lock:
            set_key(Settings.MCP_CREDENTIALS_FILE, key, token.get_secret_value(), quote_mode='always')
            Settings.MCP_CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        raise AdapterError('mcp_auth_save_failed', False, 'MCP认证凭据保存失败', None) from None


def remove_service_credential(key: str | None) -> None:
    if is_service_credential(key):
        with _write_lock:
            unset_key(Settings.MCP_CREDENTIALS_FILE, key)


def credential_status(settings: Settings) -> list[dict]:
    return [{'auth_env_key': key, 'configured': bool(getattr(settings, key) and getattr(settings, key).get_secret_value())}
            for key in sorted(Settings.MCP_AUTH_ENV_KEYS)]

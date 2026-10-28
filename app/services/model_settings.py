import json
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from app.adapters.common import AdapterError
from app.settings import Settings, load_settings

ModelService = Literal['chat', 'embedding', 'reranker']
MODEL_FIELDS = {
    'chat': ('CHAT_MODEL', 'CHAT_BASE_URL', 'CHAT_API_KEY'),
    'embedding': ('EMBED_MODEL', 'EMBED_BASE_URL', 'EMBED_API_KEY'),
    'reranker': ('RERANK_MODEL', 'RERANK_URL', 'RERANK_API_KEY'),
}
_write_lock = Lock()


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, hide_input_in_errors=True)
    model: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(None, max_length=4096)

    @field_validator('url')
    @classmethod
    def url_format(cls, value: str) -> str:
        if any(char.isspace() for char in value):
            raise ValueError('URL不能包含空白字符')
        return Settings.validate_url(value)

    @field_validator('model')
    @classmethod
    def model_single_line(cls, value: str) -> str:
        if '\n' in value or '\r' in value:
            raise ValueError('模型名不能包含换行')
        return value

    @field_validator('api_key')
    @classmethod
    def key_format(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        token = value.get_secret_value()
        if any(char.isspace() for char in token) or token.lower().startswith('bearer '):
            raise ValueError('仅填写密钥，不加Bearer前缀')
        return value


def public_config(settings: Settings, service: ModelService) -> dict:
    model, url, key = MODEL_FIELDS[service]
    return {'model': getattr(settings, model) or '', 'url': getattr(settings, url) or '',
            'key_configured': bool(getattr(settings, key) and getattr(settings, key).get_secret_value())}


def configured_settings(settings: Settings, service: ModelService, body: ModelConfig) -> Settings:
    model, url, key = MODEL_FIELDS[service]
    values = settings.model_dump()
    values.update({model: body.model, url: body.url})
    if body.api_key is not None:
        values[key] = body.api_key
    try:
        configured = Settings(_env_file=None, **values)
    except ValidationError:
        raise AdapterError('model_configuration_invalid', False, '模型配置无效，请检查URL和模型名', None) from None
    configured.require(model, url, key)
    return configured


def save_config(service: ModelService, body: ModelConfig) -> dict:
    with _write_lock:
        settings = configured_settings(load_settings(), service, body)
        path = Settings.MODEL_SETTINGS_FILE
        values = {key: value for key, value in dotenv_values(path, interpolate=False).items()
                  if key in Settings.MODEL_FIELDS}
        for name in MODEL_FIELDS[service]:
            value = getattr(settings, name)
            values[name] = value.get_secret_value() if isinstance(value, SecretStr) else value
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                output.write(''.join(f'{name}={json.dumps(value, ensure_ascii=False)}\n' for name, value in sorted(values.items())))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError:
            raise AdapterError('model_configuration_save_failed', False, '模型配置保存失败，原配置已保留', None) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return public_config(settings, service)

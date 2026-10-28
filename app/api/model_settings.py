import asyncio

from fastapi import APIRouter, Depends

from app.adapters.chat import ChatAdapter
from app.adapters.common import AdapterError
from app.adapters.embedding import EmbeddingAdapter
from app.adapters.observation import Observer
from app.adapters.rerank import RerankAdapter
from app.api.auth import current_user, require_origin
from app.services.model_settings import ModelConfig, ModelService, configured_settings, public_config, save_config
from app.settings import load_settings

router = APIRouter(prefix='/api/settings/models', tags=['模型设置'], dependencies=[Depends(current_user)])


@router.get('')
def get_models():
    settings = load_settings()
    return {service: public_config(settings, service) for service in ('chat', 'embedding', 'reranker')}


@router.put('/{service}', dependencies=[Depends(require_origin)])
def put_model(service: ModelService, body: ModelConfig):
    return save_config(service, body)


@router.post('/{service}/test', dependencies=[Depends(require_origin)])
async def test_model(service: ModelService, body: ModelConfig):
    settings = await asyncio.to_thread(load_settings)
    settings = configured_settings(settings, service, body)
    observer = Observer(settings.model_copy(update={'LANGFUSE_PUBLIC_KEY': None, 'LANGFUSE_SECRET_KEY': None}))
    adapters = {'chat': ChatAdapter, 'embedding': EmbeddingAdapter, 'reranker': RerankAdapter}
    adapter = adapters[service](settings, observer)
    try:
        async with asyncio.timeout(30):
            if service == 'chat':
                content = ''
                async for event in adapter.stream([{'role': 'user', 'content': '请只回复OK'}], None, None,
                        {'max_tokens': settings.CHAT_PROBE_MAX_TOKENS, 'extra_body': {'enable_thinking': False}}):
                    if event.type == 'content':
                        content += event.content
                if not content.strip():
                    raise AdapterError('empty_answer', False, '模型未返回有效文本', None)
            elif service == 'embedding':
                result = await adapter.embed(['连接检测'])
                return {'connected': True, 'message': f'连接有效，向量维度：{result.dimension}'}
            else:
                await adapter.rerank('连接检测', ['用于检测重排服务的文本'])
    except TimeoutError:
        raise AdapterError('model_test_timeout', True, '连接检测超时（30秒）', None) from None
    finally:
        await adapter.close()
    return {'connected': True, 'message': '连接有效'}

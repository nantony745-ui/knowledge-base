import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from langfuse import get_client, propagate_attributes

from app.adapters.common import Sanitizer
from app.adapters.observation import Observation, SafeSpan


class NullSpan:
    def update(self, **kwargs):
        return None


class RunTracer:
    def __init__(self, settings, observer):
        self.settings = settings
        self.sanitizer = Sanitizer([v.get_secret_value() for v in settings.__dict__.values()
                                    if hasattr(v,'get_secret_value')])
        self.warnings: list[str] = []
        self.client = None
        self.trace_id = None
        self.attributes = ContextVar('run_trace_attributes', default={})
        self.current_span = ContextVar('run_trace_span', default=None)
        self.started = None
        self.first_public_delta_seconds = None
        self.round_count = 0
        self.tool_call_count = 0
        self.stop_reason = None
        if not all((settings.LANGFUSE_BASE_URL,settings.LANGFUSE_PUBLIC_KEY,settings.LANGFUSE_SECRET_KEY)):
            self.warn('未配置Langfuse；回答继续，追踪失败')
        elif getattr(observer,'trace_error',None):
            self.warn('Langfuse客户端初始化失败；回答继续，追踪失败')
        else:
            try:
                self.client = get_client(public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value())
            except Exception:
                self.warn('Langfuse客户端不可用；回答继续，追踪失败')

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    async def prepare(self):
        if self.client:
            try:
                healthy = await asyncio.to_thread(self.client.auth_check)
                if not healthy:
                    self.warn('Langfuse认证或服务不可用；回答继续，追踪失败')
                    self.client = None
            except Exception:
                self.warn('Langfuse服务检查失败；回答继续，追踪失败')
                self.client = None

    @contextmanager
    def scope(self, *, metadata=None, user_id=None, session_id=None):
        attributes = {**self.attributes.get(), **(metadata or {})}
        token = self.attributes.set(attributes)
        context = None
        try:
            if self.client:
                try:
                    context = propagate_attributes(user_id=user_id, session_id=session_id,
                        metadata={key: str(value) for key, value in self.sanitizer.clean(attributes).items()})
                    context.__enter__()
                except Exception:
                    context = None
                    self.warn('Langfuse属性传播失败；追踪失败')
            yield
        finally:
            if context:
                try:
                    context.__exit__(None, None, None)
                except Exception:
                    self.warn('Langfuse属性传播结束失败；追踪失败')
            self.attributes.reset(token)

    def public_delta(self):
        if self.started is not None and self.first_public_delta_seconds is None:
            self.first_public_delta_seconds = monotonic() - self.started

    def record_context(self, kind, payload):
        span = self.current_span.get()
        if span is not None:
            span.update(metadata={f'context_{kind}': payload})

    @contextmanager
    def stage(self, name, payload, **kwargs):
        context,span = None,None
        started = monotonic()
        metadata = {**self.attributes.get(), **kwargs.pop('metadata', {}), 'status': 'running'}
        if self.client:
            try:
                context = self.client.start_as_current_observation(name=name,
                    input=self.sanitizer.clean(payload), metadata=self.sanitizer.clean(metadata), **kwargs)
                span = context.__enter__()
                if name == 'run':
                    self.trace_id = span.trace_id
            except Exception:
                context = None
                self.warn('Langfuse阶段追踪失败；回答继续，追踪失败')
        safe = SafeSpan(span, self.sanitizer, self.warn, metadata)
        token = self.current_span.set(safe)
        try:
            yield safe
        except BaseException as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            info = getattr(error, 'info', None)
            safe.update(level='WARNING' if cancelled else 'ERROR', status_message=info.message if info else type(error).__name__,
                        metadata={'status': 'cancelled' if cancelled else 'failed',
                                  'error_code': info.code if info else type(error).__name__,
                                  'error_type': type(error).__name__, 'retryable': info.retryable if info else None})
            raise
        finally:
            safe.update(metadata={'duration_seconds': monotonic() - started,
                'status': 'success' if safe.metadata['status'] == 'running' else safe.metadata['status']})
            self.current_span.reset(token)
            if context:
                try:
                    context.__exit__(None,None,None)
                except Exception:
                    self.warn('Langfuse阶段结束失败；追踪失败')

    async def flush(self):
        if self.client:
            try:
                await asyncio.to_thread(self.client.flush)
                if not await asyncio.to_thread(self.client.auth_check):
                    self.warn('Langfuse提交后服务不可用；追踪失败')
            except Exception:
                self.warn('Langfuse提交失败；追踪失败')

    def status(self):
        return {'status':'failed' if self.warnings else 'submitted',
                'trace_id':self.trace_id,'warnings':list(self.warnings)}


class PipelineObserver:
    def __init__(self, tracer):
        self.tracer = tracer
        self.sanitizer = tracer.sanitizer

    @contextmanager
    def observe(self, service, model, payload):
        kind = 'embedding' if service=='embedding' else 'generation'
        with self.tracer.stage('provider-'+service,payload,as_type=kind,model=model,
                               metadata={'service': service}) as span:
            record = Observation(span,self.sanitizer,model)
            record.metadata['service'] = service
            error = None
            try:
                yield record
            except BaseException as exception:
                error = exception
                raise
            finally:
                try:
                    record.finish(error)
                except Exception:
                    self.tracer.warn('Langfuse模型观测更新失败；回答继续，追踪失败')

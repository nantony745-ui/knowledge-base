import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from langfuse import Langfuse

from app.adapters.common import Sanitizer, Usage
from app.settings import Settings


def _invalid_constant(value):
    raise ValueError(value)


def observed_tool_call(call):
    try:
        arguments = json.loads(call.raw_arguments, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError):
        return {'id': call.id, 'name': call.name, 'arguments': None,
                'arguments_parse_error': 'invalid_json', 'raw_arguments_length': len(call.raw_arguments)}
    return {'id': call.id, 'name': call.name, 'arguments': arguments}


class SafeSpan:
    def __init__(self, span, sanitizer, warn, metadata):
        self.span, self.sanitizer, self.warn = span, sanitizer, warn
        self.metadata = dict(metadata)

    @property
    def trace_id(self):
        return self.span.trace_id if self.span else None

    def update(self, **values):
        self.metadata.update(values.pop('metadata', {}) or {})
        if self.span:
            try:
                self.span.update(**self.sanitizer.clean({**values, 'metadata': self.metadata}))
            except Exception:
                self.warn('Langfuse阶段更新失败；追踪失败')


class SafeLogFilter(logging.Filter):
    def __init__(self, sanitizer: Sanitizer):
        super().__init__()
        self.sanitizer = sanitizer

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.sanitizer.text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.msg += " [" + record.exc_info[0].__name__ + "]"
            record.exc_info = None
            record.exc_text = None
        return True


class Observation:
    def __init__(self, span: Any, sanitizer: Sanitizer, model: str):
        self.span = span
        self.sanitizer = sanitizer
        self.model = model
        self.started = monotonic()
        self.usage: Usage | None = None
        self.output: Any = None
        self.provider_request_id: str | None = None
        self.metadata: dict[str, Any] = {}
        self.model_parameters: dict[str, Any] = {}
        self.completion_start_time: datetime | None = None

    def first_output(self, kind: str) -> None:
        if self.completion_start_time is None:
            self.completion_start_time = datetime.now(timezone.utc)
            self.metadata.update(first_output_type=kind, ttft_seconds=monotonic() - self.started)

    def finish(self, error: BaseException | None) -> None:
        from app.agent.messages import PublicText

        metadata = {**self.metadata, "model": self.model, "duration_seconds": monotonic() - self.started, "usage": self.usage.model_dump() if self.usage else "unknown", "provider_request_id": self.provider_request_id,
                    "cost_source": "langfuse_model_definition", "usage_source": "provider" if self.usage else "unknown"}
        output = self.output
        if isinstance(output, dict) and isinstance(output.get('content'), str):
            output = {**output, 'content': PublicText().feed(output['content'])}
        update = {"metadata": self.sanitizer.clean(metadata), "output": self.sanitizer.clean(output)}
        if self.model_parameters:
            update['model_parameters'] = self.sanitizer.clean(self.model_parameters)
        if self.completion_start_time is not None:
            update['completion_start_time'] = self.completion_start_time
        if self.usage and self.usage.observation():
            update["usage_details"] = self.usage.observation()
        if error:
            update.update(level="ERROR", status_message=self.sanitizer.text(str(error)))
        self.span.update(**update)


class Observer:
    def __init__(self, settings: Settings):
        secrets = [value.get_secret_value() for value in settings.__dict__.values() if hasattr(value, "get_secret_value")]
        self.sanitizer = Sanitizer(secrets)
        logging.basicConfig(level=logging.WARNING)
        for handler in logging.getLogger().handlers:
            handler.addFilter(SafeLogFilter(self.sanitizer))
        self.trace_error = None
        try:
            settings.require("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
            self.client = Langfuse(base_url=settings.LANGFUSE_BASE_URL, public_key=settings.LANGFUSE_PUBLIC_KEY.get_secret_value(), secret_key=settings.LANGFUSE_SECRET_KEY.get_secret_value(), mask=self.sanitizer.clean, timeout=10)
        except Exception:
            self.trace_error = 'Langfuse配置或客户端初始化失败'
            self.client = NullTraceClient()
        self.records: list[dict[str, Any]] = []

    @contextmanager
    def observe(self, service: str, model: str, payload: Any):
        kind = {"chat": "generation", "embedding": "embedding", "rerank": "generation"}.get(service, "span")
        kwargs = {"model": model} if kind != "span" else {}
        with self.client.start_as_current_observation(name=f"step02-{service}", as_type=kind, input=self.sanitizer.clean(payload), metadata={"model": model}, **kwargs) as span:
            record = Observation(span, self.sanitizer, model)
            record.metadata['service'] = service
            entry = {"service": service, "model": model, "trace_id": span.trace_id, "observation_id": span.id}
            self.records.append(entry)
            try:
                yield record
            except Exception as exc:
                record.finish(exc)
                raise
            else:
                record.finish(None)
            finally:
                entry["expected_usage"] = record.usage.observation() if record.usage else "unknown"


class NullTraceClient:
    @contextmanager
    def start_as_current_observation(self, **kwargs):
        class Span:
            trace_id = None
            id = None
            def update(self, **data):return None
        yield Span()

    def flush(self):return None
    def shutdown(self):return None

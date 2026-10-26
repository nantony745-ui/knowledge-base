import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.adapters.common import AdapterError
from app.agent.refs import RunRefs
from app.db.context_repository import register_run, save_checkpoint
from app.api.common import fail, identifier
from app.rag.generation import validate_citations
from app.rag.retrieval import authorize, clean_query
from app.rag.tracing import PipelineObserver, RunTracer
from app.services.conversations import (
    append_run_message, check_session, delete_history, repair_tool_results,
)


async def atomic_thread(function, *args, committed=None):
    """取消不能停止MySQL线程；等待提交结束后再允许删除历史。"""
    task = asyncio.create_task(asyncio.to_thread(function,*args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if committed:
        committed(result)
    if cancelled:
        raise asyncio.CancelledError()
    return result


async def atomic_thread_wait(task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


@dataclass
class Run:
    id: str
    session_id: str
    owner: str
    events: list[dict] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    terminal: bool = False
    partial_text: str = ''
    finalized: bool = False
    final_payload: dict | None = None
    tracing: dict | None = None
    finished_at: float | None = None
    turn_id: str | None = None
    failure_saved: bool = False
    mode: str = 'quick_answer'
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    config_snapshot: object | None = None
    context: object | None = None
    user_seq: int | None = None
    sources: object | None = None
    refs: RunRefs = field(default_factory=RunRefs)
    loaded_skills: set[str] = field(default_factory=set)
    enabled_skills: frozenset[str] | None = None
    skill_catalog: object | None = None
    tracer: object | None = None

    async def emit(self, kind, payload):
        if kind in ('round.started', 'assistant.tool_calls'):
            self.partial_text = ''
        if kind == 'model.delta':
            self.partial_text += payload['text']
            if payload['text'] and self.tracer is not None:
                self.tracer.public_delta()
        self.events.append({'run_id':self.id,'seq':len(self.events)+1,'type':kind,'payload':payload})
        if kind in ('run.completed','run.failed'):
            self.terminal = True
            self.finished_at = time.monotonic()
        self.changed.set()


class RunManager:
    """单API进程临时运行状态；不提供跨进程或重启回放。"""
    def __init__(self, settings, pipeline, observer, reasoning_engine=None):
        self.settings,self.pipeline,self.observer = settings,pipeline,observer
        self.reasoning_engine = reasoning_engine
        self.runs: dict[str,Run] = {}
        self.active: dict[str,str] = {}
        self.gates: dict[str,asyncio.Lock] = {}
        self.deleting: set[str] = set()
        self.pending: set[asyncio.Task] = set()
        self.closing = False

    def gate(self, session_id):
        return self.gates.setdefault(session_id,asyncio.Lock())

    def prune(self):
        now = time.monotonic()
        active = set(self.active.values())
        completed = sorted((run for run in self.runs.values() if run.terminal and run.id not in active),
                           key=lambda run: (run.finished_at or 0, run.id))
        retained = []
        for run in completed:
            if now - (run.finished_at or 0) >= self.settings.RUN_EVENT_RETENTION_SECONDS:
                self.runs.pop(run.id, None)
            else:
                retained.append(run)
        excess = len(retained) - self.settings.RUN_COMPLETED_LIMIT
        for run in retained[:max(0, excess)]:
            self.runs.pop(run.id, None)

    async def create(self, session_id, owner, query, kb_ids, mode='quick_answer'):
        if self.closing:
            fail(503,'service_stopping','服务正在关闭，请稍后重试',True)
        task = asyncio.create_task(self.register(session_id,owner,query,kb_ids,mode))
        self.pending.add(task)
        try:
            return await task
        finally:
            self.pending.discard(task)

    async def register(self, session_id, owner, query, kb_ids, mode='quick_answer'):
        self.prune()
        session_id = identifier(session_id)
        query = await asyncio.to_thread(clean_query,query)
        await asyncio.to_thread(check_session,session_id,owner)
        await asyncio.to_thread(authorize,owner,kb_ids)
        async with self.gate(session_id):
            if session_id in self.deleting:
                fail(409,'session_deleting','会话正在删除')
            if session_id in self.active:
                fail(409,'session_busy','同一会话已有运行，请等待结束',True)
            from app.agent.context import freeze_config
            from app.agent.history import load_history
            await atomic_thread(repair_tool_results,session_id,owner,None,'interrupted')
            history = await atomic_thread(load_history,session_id,owner)
            run = Run(str(uuid4()),session_id,owner,mode=mode)
            run.config_snapshot = freeze_config(self.settings, kb_ids)
            if mode == 'smart_reasoning' and self.reasoning_engine is not None:
                self.reasoning_engine.freeze_skills(run)
            await atomic_thread(register_run, session_id, owner, run.id)
            def committed(user):
                run.turn_id = user['id']
                run.user_seq = user.get('seq')
                self.runs[run.id] = run
                self.active[session_id] = run.id
                run.task = asyncio.create_task(self.execute(run,user.get('content', query),kb_ids,history,user['id']))
            await self.save(run, 'user', query, None, 'complete', None, committed)
            return run.id

    async def save(self, run, role, content, turn_id, status, citations, committed):
        return await atomic_thread(append_run_message,run.session_id,run.owner,role,content,
            turn_id,status,citations,run.id,None,None,committed=committed)

    async def execute(self, run, query, kb_ids, history, turn_id):
        try:
            await run.emit('run.started',{'mode':run.mode,'session_id':run.session_id,'turn_id':turn_id,
                                        'persistence':'memory','restart_replay':False})
            result = await self.invoke_pipeline(run,query,kb_ids,history)
            if run.sources is not None:
                result.text, result.citations = await run.sources.validate(result.text)
            def committed(message):
                run.finalized = True
                run.final_payload = {'message_id':message['id'],'text':result.text,
                                     'citations':result.citations,'status':'complete'}
            await self.save(run,'assistant',result.text,turn_id,'complete',result.citations,committed)
            await self.completed(run)
        except asyncio.CancelledError:
            if run.finalized:
                await self.completed(run)
            else:
                await self.failed(run,turn_id,'cancelled','运行已取消')
        except Exception as error:
            code = error.info.code if isinstance(error,AdapterError) else 'run_error'
            message = error.info.message if isinstance(error,AdapterError) else '问答运行失败'
            await self.failed(run,turn_id,code,message)
        finally:
            if self.active.get(run.session_id)==run.id:
                self.active.pop(run.session_id,None)
            self.prune()

    async def completed(self, run):
        if not any(event['type'] == 'answer.finalized' for event in run.events):
            await run.emit('answer.finalized',run.final_payload)
        await run.emit('run.completed',{'message_id':run.final_payload['message_id'],'tracing':run.tracing})

    async def invoke_pipeline(self, run, query, kb_ids, history):
        from app.agent.context import ContextController, freeze_config
        if run.config_snapshot is None:
            run.config_snapshot = freeze_config(self.settings, kb_ids)
        if run.context is None:
            from app.agent.sources import RunSources
            run.sources = RunSources(run.id, run.owner, run.config_snapshot.kb_ids, run.refs)
            async def checkpoint_sink(previous_seq, groups, summary):
                return await atomic_thread(save_checkpoint, run.session_id, run.owner, previous_seq,
                    groups, summary, run.config_snapshot.tool_max_chars)
            run.context = ContextController(run.config_snapshot, history, run.emit, checkpoint_sink, run.sources,
                                            run.refs if run.mode == 'smart_reasoning' else None)
            run.context.current_range = {'turn_id': run.turn_id, 'start_seq': run.user_seq, 'end_seq': run.user_seq}
        kb_ids = list(run.config_snapshot.kb_ids)
        tracer = RunTracer(self.settings,self.observer)
        tracer.context = run.context
        tracer.sources = run.sources
        run.tracer = run.context.tracer = tracer
        await tracer.prepare()
        try:
            with tracer.scope(user_id=run.owner, session_id=run.session_id,
                              metadata={'run_id': run.id, 'mode': run.mode}), tracer.stage('run',
                    {'run_id':run.id,'session_id':run.session_id,'query':query,'mode':run.mode,'kb_ids':kb_ids},
                    as_type='agent', metadata={'duration_scope': 'pipeline', 'output_scope': 'pipeline_return'}) as span:
                tracer.started = time.monotonic()
                try:
                    if run.mode == 'smart_reasoning':
                        result = await self.reasoning_engine.run(query,kb_ids,run,history,PipelineObserver(tracer))
                    else:
                        result = await self.pipeline.run(query,kb_ids,run.owner,history,run.emit,PipelineObserver(tracer),tracer)
                        tracer.stop_reason = 'no_tool_calls'
                    span.update(output={'text': result.text, 'citations': result.citations})
                    return result
                except BaseException as error:
                    tracer.stop_reason = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'error'
                    raise
                finally:
                    span.update(metadata={'round_count': tracer.round_count, 'tool_call_count': tracer.tool_call_count,
                        'stop_reason': tracer.stop_reason, 'first_public_delta_seconds': tracer.first_public_delta_seconds})
        finally:
            await tracer.flush()
            run.tracing = tracer.status()
            run.tracer = None

    async def failed(self,run,turn_id,code,message):
        run.cancel_event.set()
        if run.mode == 'smart_reasoning':
            repair = asyncio.create_task(atomic_thread(repair_tool_results,run.session_id,run.owner,
                turn_id,'cancelled' if code == 'cancelled' else 'interrupted'))
            try:
                repaired = await atomic_thread_wait(repair)
                if repaired:
                    run.partial_text = ''
            except Exception:
                code,message = 'message_persistence_failed','工具消息未能补齐，请检查数据库后重试'
        text, citations = validate_citations(run.partial_text, [])
        if run.sources is not None:
            try:
                text, citations = await run.sources.validate(run.partial_text)
            except Exception:
                code, message = 'source_validation_failed', '来源校验失败，已移除未经校验的引用'
        if not run.finalized and not run.failure_saved:
            status = 'partial' if text else 'failed'
            def committed(message):
                run.failure_saved = True
                run.final_payload = {'message_id': message['id'], 'text': text,
                                     'citations': citations, 'status': status}
            try:
                await self.save(run,'assistant',text,turn_id,status,citations,committed)
            except asyncio.CancelledError:
                if not run.failure_saved:
                    raise
            except Exception:
                code,message = 'message_persistence_failed','运行失败且消息未能保存，请检查数据库'
        if run.failure_saved and not any(event['type'] == 'answer.finalized' for event in run.events):
            await run.emit('answer.finalized', run.final_payload)
        await run.emit('run.failed',{'code':code,'message':message,'tracing':run.tracing})

    def owned_run(self,run_id,owner):
        self.prune()
        run = self.runs.get(identifier(run_id))
        if run is None or run.owner != owner:
            fail(404,'run_not_found','运行不存在、事件已过期或服务已重启；已保存的消息可在会话历史查看')
        return run

    async def stop(self, run_id, owner):
        run = self.owned_run(run_id, owner)
        async with self.gate(run.session_id):
            run = self.owned_run(run_id, owner)
            if not run.terminal:
                run.cancel_event.set()
                if run.task and not run.task.done():
                    run.task.cancel()
            if run.task:
                try:
                    await atomic_thread_wait(run.task)
                except asyncio.CancelledError:
                    pass
            if not run.terminal:
                await self.failed(run, run.turn_id, 'cancelled', '运行已取消')
            if self.active.get(run.session_id) == run.id:
                self.active.pop(run.session_id, None)
            terminal = run.events[-1]
            if terminal['type'] == 'run.failed' and terminal['payload']['code'] != 'cancelled':
                fail(503, terminal['payload']['code'], terminal['payload']['message'])

    async def delete_session(self,session_id,owner):
        session_id=identifier(session_id)
        self.deleting.add(session_id)
        try:
            async with self.gate(session_id):
                active=self.active.get(session_id)
                if active:
                    run=self.runs[active]
                    task=run.task
                    run.cancel_event.set()
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
                    if not run.terminal:
                        await run.emit('run.failed',{'code':'cancelled','message':'会话已取消运行'})
                    if self.active.get(session_id)==active:
                        self.active.pop(session_id,None)
                await atomic_thread(delete_history,session_id,owner)
                self.runs={k:v for k,v in self.runs.items() if v.session_id!=session_id}
        finally:
            self.deleting.discard(session_id)

    async def shutdown(self):
        self.closing = True
        pending = list(self.pending)
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        running = [r for r in self.runs.values() if r.task and not r.task.done()]
        for run in running:
            run.cancel_event.set()
            run.task.cancel()
        await asyncio.gather(*(run.task for run in running),return_exceptions=True)
        for run in running:
            if not run.terminal and run.turn_id:
                await self.failed(run,run.turn_id,'cancelled','服务关闭，运行已取消')
            if self.active.get(run.session_id)==run.id:
                self.active.pop(run.session_id,None)
        self.prune()

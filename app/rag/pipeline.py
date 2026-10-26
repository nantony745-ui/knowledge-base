import asyncio
from contextlib import nullcontext
from dataclasses import dataclass

from app.adapters.chat import ChatAdapter
from app.agent.context import ContextChat, ContextController, freeze_config
from app.rag.generation import generate_answer
from app.rag.retrieval import authorize, clean_query
from app.rag.rewrite import rewrite_query


@dataclass
class QuickAnswer:
    text: str
    citations: list[dict]
    intent: str
    standalone_query: str


class QuickAnswerPipeline:
    """只负责固定问答编排，不管理HTTP、会话写入或run存储。"""
    def __init__(self, settings, retrieval):
        self.settings = settings
        self.retrieval = retrieval

    async def run(self, query, kb_ids, owner, history, emit, observer, tracer=None) -> QuickAnswer:
        query = await asyncio.to_thread(clean_query, query)
        await asyncio.to_thread(authorize, owner, kb_ids)
        stage = tracer.stage if tracer else lambda name,payload:nullcontext()
        controller = tracer.context if tracer and hasattr(tracer, 'context') else ContextController(freeze_config(self.settings, kb_ids), history, emit)
        sources = controller.sources
        raw_chat = ChatAdapter(controller.snapshot.chat_settings(self.settings), observer)
        chat = ContextChat(raw_chat, controller)
        try:
            chat.phase = 'rewrite'
            with stage('rewrite', {'query':query,'history_messages':len(history)}):
                rewritten = await rewrite_query(chat,query,[])
            hits = []
            if rewritten.intent == 'kb_search':
                result = await self.retrieval.search_knowledge(rewritten.standalone_query,kb_ids,owner,observer,tracer)
                hits = result['reranked']
            await emit('retrieval.finished', {'intent':rewritten.intent,
                'standalone_query':rewritten.standalone_query,'count':len(hits),
                'chunk_ids':[h['chunk_id'] for h in hits], 'skipped':rewritten.intent!='kb_search'})
            with stage('generation', {'query':rewritten.standalone_query,'chunk_ids':[h['chunk_id'] for h in hits]}):
                chat.phase = 'generation:' + rewritten.intent
                text,citations = await generate_answer(chat,rewritten.intent,rewritten.standalone_query,
                    [],hits,self.settings,emit,sources)
            return QuickAnswer(text,citations,rewritten.intent,rewritten.standalone_query)
        finally:
            await raw_chat.close()

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.chat import ChatAdapter
from app.adapters.common import AdapterError
from app.adapters.observation import Observer
from app.agent import ChatAgent
from app.agent.engine import ReasoningEngine
from app.agent.tools import create_knowledge_registry
from app.settings import load_settings
from app.api.auth import current_user, initialize_admin, require_origin, router as auth_router
from app.api.knowledge_bases import router as kb_router
from app.api.documents import router as document_router
from app.api.search import router as search_router, debug_router, chunks_router
from app.rag.retrieval import RetrievalService
from app.api.sessions import router as session_router
from app.api.runs import router as runs_router
from app.api.mcp_services import router as mcp_router
from app.api.skills import router as skills_router
from app.rag.pipeline import QuickAnswerPipeline
from app.services.runs import RunManager

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    settings.require("CHAT_BASE_URL", "CHAT_API_KEY", "CHAT_MODEL")
    settings.require('JWT_SECRET')
    await asyncio.to_thread(initialize_admin)
    observer = Observer(settings)
    chat = ChatAdapter(settings, observer)
    application.state.agent = ChatAgent(chat)
    application.state.observer = observer
    application.state.retrieval = RetrievalService(settings)
    application.state.tools = create_knowledge_registry(settings, application.state.retrieval, observer)
    reasoning = await asyncio.to_thread(ReasoningEngine, settings, application.state.tools)
    application.state.skills = reasoning.skills
    application.state.runs = RunManager(settings, QuickAnswerPipeline(settings,application.state.retrieval),observer,
        reasoning)
    try:
        yield
    finally:
        await application.state.runs.shutdown()
        try:
            await chat.close()
        finally:
            # Langfuse资源按项目共享，应用lifespan可重复进入；进程退出由SDK关闭。
            await asyncio.to_thread(observer.client.flush)


app = FastAPI(title="知识库", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(kb_router)
app.include_router(document_router)
app.include_router(search_router)
app.include_router(debug_router)
app.include_router(session_router)
app.include_router(chunks_router)
app.include_router(runs_router)
app.include_router(mcp_router)
app.include_router(skills_router)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, error: RequestValidationError):
    if request.url.path.startswith('/api/mcp-services'):
        return JSONResponse(status_code=422, content={'detail': [
            {'loc': item['loc'], 'type': item['type'], 'msg': 'MCP配置字段无效'} for item in error.errors()]})
    return await request_validation_exception_handler(request, error)


@app.exception_handler(AdapterError)
async def adapter_error(request: Request, error: AdapterError):
    return JSONResponse(status_code=503, content={'detail': error.info.model_dump()})
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get('/quick-answer',include_in_schema=False)
async def quick_answer_page():
    return RedirectResponse('/')


@app.get('/mcp-services', response_class=FileResponse, include_in_schema=False)
async def mcp_services_page():
    return FileResponse(FRONTEND_DIR / 'mcp-services.html')


@app.get('/toolbox', include_in_schema=False)
async def toolbox_page():
    return RedirectResponse('/skills')


@app.get('/skills', response_class=FileResponse, include_in_schema=False)
async def skills_page():
    return FileResponse(FRONTEND_DIR / 'skills.html')


@app.get("/api/chat/model", dependencies=[Depends(current_user)])
async def chat_model(request: Request) -> dict[str, str]:
    return {"model": request.app.state.agent.chat.model}


class ChatMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)


async def stream_reply(agent: ChatAgent, messages: list[dict[str, str]]) -> AsyncIterator[str]:
    try:
        async with aclosing(agent.stream(messages)) as events:
            async for event in events:
                yield json.dumps(event, ensure_ascii=False) + "\n"
    except AdapterError as exc:
        yield json.dumps({"type": "error", "message": exc.info.message}, ensure_ascii=False) + "\n"


@app.post("/api/chat", response_class=StreamingResponse, dependencies=[Depends(current_user), Depends(require_origin)])
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    messages = [message.model_dump() for message in body.messages]
    return StreamingResponse(
        stream_reply(request.app.state.agent, messages),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

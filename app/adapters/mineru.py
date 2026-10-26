import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.adapters.common import AdapterError, MinerUResult, normalize_error
from app.adapters.observation import Observer
from app.settings import Settings


class Envelope(BaseModel):
    code: int | str
    msg: str
    trace_id: str | None = None
    data: dict[str, Any] | None = None


class UploadData(BaseModel):
    batch_id: str = Field(min_length=1)
    file_urls: list[str] = Field(min_length=1, max_length=1)


class Extraction(BaseModel):
    file_name: str
    data_id: str | None = None
    state: Literal["waiting-file", "uploading", "pending", "running", "converting", "done", "failed"]
    full_zip_url: str | None = None
    err_msg: str | None = None
    err_code: int | str | None = None


class BatchResult(BaseModel):
    batch_id: str
    extract_result: list[Extraction]


def unpack_result(content: bytes) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        markdown_files = [name for name in names if Path(name).name == "full.md"]
        if len(markdown_files) != 1:
            raise AdapterError("invalid_mineru_archive", False, "MinerU 结果缺少唯一 full.md", None)
        markdown = archive.read(markdown_files[0]).decode("utf-8").strip()
        metadata = {name: json.loads(archive.read(name)) for name in names if name.endswith(".json")}
        if not markdown or not metadata:
            raise AdapterError("empty_mineru_result", False, "MinerU 正文或元数据为空", None)
        return markdown, metadata


class MinerUAdapter:
    def __init__(self, settings: Settings, observer: Observer):
        settings.require("MINERU_BASE_URL", "MINERU_API_KEY", "MINERU_MODEL")
        self.settings, self.observer, self.model = settings, observer, settings.MINERU_MODEL
        self.client = httpx.AsyncClient(base_url=settings.MINERU_BASE_URL + "/", timeout=settings.timeout("MINERU", False), headers={"Authorization": "Bearer " + settings.MINERU_API_KEY.get_secret_value()})
        self.files = httpx.AsyncClient(timeout=settings.timeout("MINERU", False), follow_redirects=True)
        self.provider_id: str | None = None

    async def _request(self, method: str, path: str, payload: dict | None) -> dict:
        response = await self.client.request(method, path, json=payload)
        response.raise_for_status()
        envelope = Envelope.model_validate(response.json())
        self.provider_id = envelope.trace_id
        if envelope.code != 0:
            raise AdapterError(str(envelope.code), str(envelope.code) in {"-10001", "-60001"}, self.observer.sanitizer.text(envelope.msg), envelope.trace_id)
        if envelope.data is None:
            raise AdapterError("invalid_response", False, "MinerU 响应缺少 data", envelope.trace_id)
        return envelope.data

    async def _poll(self, batch_id: str, data_id: str, filename: str) -> Extraction:
        async with asyncio.timeout(self.settings.MINERU_POLL_TIMEOUT):
            while True:
                batch = BatchResult.model_validate(await self._request("GET", f"extract-results/batch/{batch_id}", None))
                matches = [item for item in batch.extract_result if item.data_id == data_id or (item.data_id is None and item.file_name == filename)]
                if batch.batch_id != batch_id or len(matches) > 1:
                    raise AdapterError("invalid_mineru_batch", False, "MinerU 返回的任务标识不匹配", self.provider_id)
                if matches:
                    item = matches[0]
                    if item.state == "done":
                        if not item.full_zip_url:
                            raise AdapterError("invalid_response", False, "MinerU 完成状态缺少下载 URL", self.provider_id)
                        return item
                    if item.state == "failed":
                        raise AdapterError(str(item.err_code or "mineru_failed"), False, self.observer.sanitizer.text(item.err_msg or "MinerU 解析失败"), self.provider_id)
                await asyncio.sleep(self.settings.MINERU_POLL_INTERVAL)

    async def parse(self, file_path: Path) -> MinerUResult:
        self.provider_id = None
        with self.observer.observe("mineru", self.model, {"filename": file_path.name}) as observation:
            try:
                data_id = uuid4().hex
                upload = UploadData.model_validate(await self._request("POST", "file-urls/batch", {"files": [{"name": file_path.name, "data_id": data_id}], "model_version": self.model}))
                content = await asyncio.to_thread(file_path.read_bytes)
                response = await self.files.put(upload.file_urls[0], content=content)
                response.raise_for_status()
                item = await self._poll(upload.batch_id, data_id, file_path.name)
                response = await self.files.get(item.full_zip_url)
                response.raise_for_status()
                markdown, metadata = await asyncio.to_thread(unpack_result, response.content)
                result = MinerUResult(markdown=markdown, metadata=metadata, model=self.model, batch_id=upload.batch_id, provider_request_id=self.provider_id)
                observation.output, observation.provider_request_id = {"batch_id": upload.batch_id, "markdown": markdown}, self.provider_id
                return result
            except Exception as exc:
                raise normalize_error(exc, self.observer.sanitizer, self.provider_id) from None

    async def close(self) -> None:
        await self.client.aclose()
        await self.files.aclose()

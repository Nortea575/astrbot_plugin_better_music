"""HTTP client for the configured singing-cover service."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

import aiohttp


API_VERSION = 1
PIPELINE_VERSION = "2"
_HEALTH_TIMEOUT_SECONDS = 10.0
_REQUEST_TIMEOUT_SECONDS = 60.0
_UPLOAD_TIMEOUT_SECONDS = 30 * 60.0
_MAX_AUDIO_BYTES = 500 * 1024 * 1024


class CoverServiceError(RuntimeError):
    """A safe-to-display cover service failure without credentials or bodies."""


class CoverClient:
    def __init__(self, settings_getter: Callable[[], dict[str, Any]]):
        self._settings_getter = settings_getter

    def _config(self) -> tuple[str, str]:
        try:
            settings = self._settings_getter()
            cover = settings.get("cover_service", {}) if isinstance(settings, dict) else {}
        except Exception:
            raise CoverServiceError("无法读取翻唱服务设置") from None
        if not isinstance(cover, dict):
            raise CoverServiceError("翻唱服务设置无效")
        base_url = cover.get("url", "")
        token = cover.get("token", "")
        if not isinstance(base_url, str) or not base_url.strip():
            raise CoverServiceError("请先在插件设置中填写翻唱服务 URL")
        if not isinstance(token, str):
            raise CoverServiceError("翻唱服务密钥设置无效")
        base_url = base_url.strip().rstrip("/")
        try:
            parsed = urlparse(base_url)
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise CoverServiceError("翻唱服务 URL 必须是有效的 HTTP 或 HTTPS 地址")
        return base_url, token

    @staticmethod
    def _url(base_url: str, path: str) -> str:
        return f"{base_url}/v1/{path.lstrip('/')}"

    @staticmethod
    def _safe_payload(value: Any, token: str) -> Any:
        if isinstance(value, str):
            return value.replace(token, "[hidden]") if token else value
        if isinstance(value, list):
            return [CoverClient._safe_payload(item, token) for item in value]
        if isinstance(value, dict):
            return {key: CoverClient._safe_payload(item, token) for key, item in value.items()}
        return value

    @staticmethod
    def _check_version(payload: dict[str, Any]) -> None:
        version = payload.get("api_version")
        if type(version) is not int or version != API_VERSION:
            raise CoverServiceError("翻唱服务 API 版本不兼容（需要 API v1）")

    async def _json_request(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        method: str,
        path: str,
        *,
        data: Any = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with session.request(
                method,
                self._url(base_url, path),
                data=data,
                headers=headers,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
                    if response.status < 200 or response.status >= 300:
                        raise CoverServiceError(
                            f"翻唱服务请求失败（HTTP {response.status}）"
                        ) from None
                    raise CoverServiceError("翻唱服务返回了无效的 JSON 响应") from None
                if not isinstance(payload, dict):
                    raise CoverServiceError("翻唱服务返回了无效的 JSON 响应")
                self._check_version(payload)
                if response.status < 200 or response.status >= 300:
                    detail = payload.get("error")
                    if not isinstance(detail, str) or not detail.strip():
                        detail = payload.get("message")
                    if isinstance(detail, str) and detail.strip():
                        safe_detail = self._safe_payload(detail.strip(), token)[:200]
                        raise CoverServiceError(
                            f"翻唱服务请求失败（HTTP {response.status}）：{safe_detail}"
                        )
                    raise CoverServiceError(f"翻唱服务请求失败（HTTP {response.status}）")
                return self._safe_payload(payload, token)
        except CoverServiceError:
            raise
        except asyncio.TimeoutError:
            raise CoverServiceError("翻唱服务请求超时") from None
        except (aiohttp.ClientError, OSError):
            raise CoverServiceError("无法连接翻唱服务，请检查 URL 与服务状态") from None

    @staticmethod
    def _voices(payload: dict[str, Any]) -> list[dict[str, Any]]:
        voices = payload.get("voices")
        if not isinstance(voices, list):
            raise CoverServiceError("翻唱服务音色响应格式无效")
        return [dict(voice) for voice in voices if isinstance(voice, dict) and isinstance(voice.get("id"), str)]

    async def check(self) -> dict[str, Any]:
        base_url, token = self._config()

        async def run() -> dict[str, Any]:
            timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                health = await self._json_request(session, base_url, token, "GET", "health")
                voices_response = await self._json_request(session, base_url, token, "GET", "voices")
                return {"health": health, "voices": self._voices(voices_response)}

        try:
            return await asyncio.wait_for(run(), timeout=_HEALTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise CoverServiceError("翻唱服务检测超过 10 秒") from None

    async def voices(self) -> list[dict[str, Any]]:
        base_url, token = self._config()
        timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            response = await self._json_request(session, base_url, token, "GET", "voices")
        return self._voices(response)

    async def require_cleanup_support(self) -> dict[str, Any]:
        """Require the v2 inference pipeline and its ephemeral-job lifecycle API."""
        base_url, token = self._config()
        timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            health = await self._json_request(session, base_url, token, "GET", "health")
        capabilities = health.get("capabilities")
        if (
            str(health.get("pipeline_version", "")) != PIPELINE_VERSION
            or not isinstance(capabilities, dict)
            or capabilities.get("job_ack") is not True
            or capabilities.get("job_cancel") is not True
            or capabilities.get("ephemeral_jobs") is not True
        ):
            raise CoverServiceError(
                "翻唱服务版本过旧；请升级至推理管线 v2 并启用任务确认、取消与临时任务清理"
            )
        return health

    @staticmethod
    def _job_id(payload: dict[str, Any]) -> str:
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
            job = payload.get("job")
            if isinstance(job, dict):
                job_id = job.get("job_id") or job.get("id")
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
            raise CoverServiceError("翻唱服务没有返回有效的任务编号")
        return job_id.strip()

    async def submit_job(
        self,
        audio_path: Path,
        *,
        voice_id: str,
        max_seconds: int,
        request_id: str,
    ) -> str:
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise CoverServiceError("请先选择翻唱音色")
        if not isinstance(max_seconds, int) or isinstance(max_seconds, bool) or max_seconds < 0:
            raise ValueError("max_seconds 必须是非负整数")
        path = Path(audio_path)
        if not path.is_file() or path.is_symlink():
            raise CoverServiceError("找不到待翻唱的原始音频")
        base_url, token = self._config()
        timeout = aiohttp.ClientTimeout(total=_UPLOAD_TIMEOUT_SECONDS)
        form = aiohttp.FormData()
        form.add_field("voice_id", voice_id.strip())
        form.add_field("max_seconds", str(max_seconds))
        form.add_field("request_id", request_id)
        try:
            with path.open("rb") as audio_file:
                form.add_field(
                    "file",
                    audio_file,
                    filename=path.name,
                    content_type="application/octet-stream",
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    response = await self._json_request(
                        session, base_url, token, "POST", "jobs", data=form
                    )
        except CoverServiceError:
            raise
        except asyncio.CancelledError:
            raise
        except OSError:
            raise CoverServiceError("无法读取待翻唱的原始音频") from None
        return self._job_id(response)

    async def job_status(self, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
            raise CoverServiceError("翻唱服务任务编号无效")
        base_url, token = self._config()
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
        path = f"jobs/{quote(job_id.strip(), safe='')}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = await self._json_request(session, base_url, token, "GET", path)
        return payload

    async def ack_job(self, job_id: str) -> dict[str, Any]:
        """Release a terminal service job after its output has been validated."""
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
            raise CoverServiceError("翻唱服务任务编号无效")
        base_url, token = self._config()
        path = f"jobs/{quote(job_id.strip(), safe='')}/ack"
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = await self._json_request(session, base_url, token, "POST", path)
        if type(payload.get("released")) is not bool or type(payload.get("pending")) is not bool:
            raise CoverServiceError("翻唱服务没有确认任务清理状态")
        return {"released": payload["released"], "pending": payload["pending"]}

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Cancel or discard a job owned by this plugin request."""
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
            raise CoverServiceError("翻唱服务任务编号无效")
        base_url, token = self._config()
        path = f"jobs/{quote(job_id.strip(), safe='')}"
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = await self._json_request(session, base_url, token, "DELETE", path)
        if payload.get("released") is not True:
            raise CoverServiceError("翻唱服务没有确认任务取消")
        return {"released": True, "pending": payload.get("pending") is True, "cancelled": payload.get("cancelled") is True}

    async def download_audio(self, job_id: str, destination: Path) -> Path:
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 200:
            raise CoverServiceError("翻唱服务任务编号无效")
        base_url, token = self._config()
        path = f"jobs/{quote(job_id.strip(), safe='')}/audio"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        timeout = aiohttp.ClientTimeout(total=_UPLOAD_TIMEOUT_SECONDS)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._url(base_url, path), headers=headers) as response:
                    if response.status < 200 or response.status >= 300:
                        raise CoverServiceError(f"下载翻唱音频失败（HTTP {response.status}）")
                    if response.content_type in {"application/json", "text/html", "application/xhtml+xml"}:
                        raise CoverServiceError("翻唱服务没有返回音频文件")
                    if response.content_length and response.content_length > _MAX_AUDIO_BYTES:
                        raise CoverServiceError("翻唱音频超过 500 MiB 限制")
                    with temporary.open("wb") as output:
                        async for block in response.content.iter_chunked(1024 * 1024):
                            total += len(block)
                            if total > _MAX_AUDIO_BYTES:
                                raise CoverServiceError("翻唱音频超过 500 MiB 限制")
                            output.write(block)
            if total <= 0:
                raise CoverServiceError("翻唱服务返回了空音频")
            temporary.replace(target)
            return target
        except CoverServiceError:
            raise
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise CoverServiceError("下载翻唱音频超时") from None
        except (aiohttp.ClientError, OSError):
            raise CoverServiceError("无法下载翻唱音频，请检查翻唱服务状态") from None
        finally:
            temporary.unlink(missing_ok=True)

    async def import_voice(
        self,
        model_path: Path,
        *,
        name: str,
        index_path: Path | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise CoverServiceError("音色名称必须是 1 到 80 个字符")
        model = Path(model_path)
        index = Path(index_path) if index_path is not None else None
        if not model.is_file() or model.is_symlink():
            raise CoverServiceError("找不到 RVC 模型文件")
        if index is not None and (not index.is_file() or index.is_symlink()):
            raise CoverServiceError("找不到 RVC 索引文件")
        base_url, token = self._config()
        timeout = aiohttp.ClientTimeout(total=_UPLOAD_TIMEOUT_SECONDS)
        form = aiohttp.FormData()
        form.add_field("name", name.strip())
        try:
            with ExitStack() as stack:
                model_file = stack.enter_context(model.open("rb"))
                form.add_field(
                    "model",
                    model_file,
                    filename=model.name,
                    content_type="application/octet-stream",
                )
                if index is not None:
                    index_file = stack.enter_context(index.open("rb"))
                    form.add_field(
                        "index",
                        index_file,
                        filename=index.name,
                        content_type="application/octet-stream",
                    )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    response = await self._json_request(
                        session, base_url, token, "POST", "voices", data=form
                    )
        except CoverServiceError:
            raise
        except asyncio.CancelledError:
            raise
        except OSError:
            raise CoverServiceError("无法读取待导入的 RVC 文件") from None
        voice = response.get("voice")
        if not isinstance(voice, dict) and isinstance(response.get("id"), str):
            voice = response
        if not isinstance(voice, dict) or not isinstance(voice.get("id"), str):
            raise CoverServiceError("翻唱服务没有返回有效音色信息")
        return self._safe_payload(dict(voice), token)

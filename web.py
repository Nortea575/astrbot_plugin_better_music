"""Authenticated web API for the Better Music plugin view."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import time
import wave
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Record
from astrbot.api.web import error_response, file_response, json_response, request

from .providers import PLATFORMS
from .environment import EnvironmentManager, validate_song_log_settings
from .cover import CoverClient, CoverServiceError
from .passive import capture_current_scope
from .paths import data_subdirectory, is_path_link
from .templates import (
    DEFAULT_TEMPLATES,
    REPLY_ENABLED_DEFAULTS,
    REPLY_ENABLED_KEYS,
    TEMPLATE_CATEGORIES,
    TEMPLATE_VARIABLES,
    validate_templates,
)
from .song_log import send_song_log


PLUGIN_NAME = "astrbot_plugin_better_music"
TOOL_IDS = ("ffmpeg", "node", "netease", "qq", "kugou")
PROVIDER_IDS = {"netease", "qq", "kugou"}
MAX_QUERY_LENGTH = 1000
MAX_TARGET_LENGTH = 128
MAX_SONG_ID_LENGTH = 200
SEARCH_SELECTION_TTL_SECONDS = 30 * 60
MAX_COVER_MODEL_BYTES = 1024 * 1024 * 1024
MAX_COVER_INDEX_BYTES = 512 * 1024 * 1024
COVER_UPLOAD_TTL_SECONDS = 30 * 60


class WebAPI:
    """Register page endpoints and keep plugin-owned work reload-safe."""

    def __init__(self, context, providers, runtime, passive_memory=None):
        self.context = context
        self.providers = providers
        self.runtime = runtime
        self.passive_memory = passive_memory
        self._active = False
        self._registered = False
        self._record_tests: dict[str, dict] = {}
        self._environment_snapshot: dict | None = None
        self._target_snapshot: list[dict] | None = None
        self._jobs: dict[str, dict] = {}
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._active_job_keys: dict[str, str] = {}
        self._request_jobs: dict[str, str] = {}
        self._search_selections: dict[tuple[str, str, str], float] = {}
        runtime_data_dir = getattr(runtime, "data_dir", None)
        self._cover_staging_dir = (
            Path(runtime_data_dir).expanduser().absolute() / ".cover-staging"
            if runtime_data_dir is not None
            else None
        )
        self._cover_uploads: dict[str, dict[str, Any]] = {}
        self._clear_cover_staging()

    def register(self) -> None:
        self._active = True
        if self._registered:
            return
        routes = (
            ("overview", self.get_overview, ["GET"], "Better Music overview"),
            ("environment/check", self.check_environment, ["POST"], "Check Better Music dependencies"),
            ("jobs/status", self.job_status, ["GET"], "Read a Better Music background job"),
            ("login/start", self.start_login, ["POST"], "Start music provider login"),
            ("login/status", self.login_status, ["GET"], "Read music provider login status"),
            ("login/cancel", self.cancel_login, ["POST"], "Cancel music provider login"),
            ("login/logout", self.logout, ["POST"], "Log out of a music provider"),
            ("environment/install", self.install_tool, ["POST"], "Install or supplement an audio tool"),
            ("settings", self.save_settings, ["POST"], "Save Better Music settings"),
            ("search", self.search, ["POST"], "Search music candidates"),
            ("download", self.download, ["POST"], "Download a music file"),
            ("files", self.get_files, ["GET"], "List downloaded music files"),
            ("files/download", self.download_file, ["GET"], "Download a stored music file"),
            ("files/send", self.send_song, ["POST"], "Send a stored music file as voice segments"),
            ("cover/check", self.check_cover_service, ["POST"], "Check the singing-cover service"),
            ("cover/deployment", self.download_cover_deployment_guide, ["GET"], "Download cover service integration and API guide"),
            ("cover/test", self.test_cover, ["POST"], "Run an uncached singing-cover validation"),
            ("cover/voices", self.get_cover_voices, ["GET"], "List singing-cover voices"),
            ("cover/upload", self.upload_cover_file, ["POST"], "Upload an RVC model or index"),
            ("cover/import", self.import_cover_voice, ["POST"], "Import an RVC voice"),
            ("test-audio", self.test_audio, ["POST"], "Send a manually requested test audio"),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )
        self._registered = True

    async def close(self) -> None:
        """Disable routes and cancel every plugin-owned background job on reload."""
        self._active = False
        tasks = list(self._job_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._job_tasks.clear()
        self._active_job_keys.clear()
        self._request_jobs.clear()
        self._jobs.clear()
        self._record_tests.clear()
        self._search_selections.clear()
        self._clear_cover_staging()

    def _clear_cover_staging(self) -> None:
        self._cover_uploads.clear()
        root = self._cover_staging_dir
        if root is not None and root.exists() and not is_path_link(root):
            shutil.rmtree(root, ignore_errors=True)

    def _ensure_cover_staging_dir(self) -> Path:
        root = self._cover_staging_dir
        if root is None:
            raise RuntimeError("当前运行环境未提供插件数据目录")
        parent = Path(self.runtime.data_dir).expanduser().absolute()
        if root.parent != parent:
            raise RuntimeError("音色暂存目录路径无效")
        root = data_subdirectory(parent, ".cover-staging")
        if os.name != "nt":
            root.chmod(0o700)
        return root

    def _prune_cover_uploads(self) -> None:
        cutoff = time.monotonic() - COVER_UPLOAD_TTL_SECONDS
        expired = [
            upload_id
            for upload_id, record in self._cover_uploads.items()
            if float(record.get("created_at", 0)) <= cutoff
        ]
        for upload_id in expired:
            self._discard_cover_upload(upload_id)

    def _discard_cover_upload(self, upload_id: str) -> None:
        record = self._cover_uploads.pop(upload_id, None)
        path = Path(record.get("path", "")) if record else None
        root = self._cover_staging_dir
        if path is None or root is None:
            return
        try:
            if (
                not is_path_link(root)
                and not is_path_link(path)
                and path.resolve().is_relative_to(root.resolve())
            ):
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    def _inactive(self):
        if not self._active:
            return error_response("插件页面 API 已停用", status_code=503)
        return None

    def _start_job_response(self, kind: str, task_key: str, request_id: str, operation):
        self._prune_jobs()
        request_key = f"{kind}:{request_id}"
        previous_id = self._request_jobs.get(request_key)
        if previous_id and previous_id in self._jobs:
            return self._job_response(self._jobs[previous_id])
        active_id = self._active_job_keys.get(task_key)
        if active_id and active_id in self._jobs:
            active_job = self._jobs[active_id]
            if active_job.get("request_id") == request_id:
                return self._job_response(active_job)
            return error_response("同类任务正在进行，请等待完成后再操作。", status_code=409)

        job_id = uuid4().hex
        now = asyncio.get_running_loop().time()
        job = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "result": None,
            "error": "",
            "created_at": now,
            "updated_at": now,
            "request_id": request_id,
            "task_key": task_key,
        }
        self._jobs[job_id] = job
        self._request_jobs[request_key] = job_id
        self._active_job_keys[task_key] = job_id
        self._job_tasks[job_id] = asyncio.create_task(
            self._run_job(job_id, task_key, operation),
            name=f"better_music_{kind}_{job_id[:8]}",
        )
        return self._job_response(job)

    async def _run_job(self, job_id: str, task_key: str, operation) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["updated_at"] = asyncio.get_running_loop().time()
        try:
            job["result"] = await operation()
            job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc) or exc.__class__.__name__
        finally:
            job["updated_at"] = asyncio.get_running_loop().time()
            self._job_tasks.pop(job_id, None)
            if self._active_job_keys.get(task_key) == job_id:
                self._active_job_keys.pop(task_key, None)

    def _job_response(self, job: dict):
        return json_response({"job": self._public_job(job)})

    @staticmethod
    def _public_job(job: dict) -> dict:
        result = {
            "job_id": str(job.get("id", "")),
            "kind": str(job.get("kind", "")),
            "status": str(job.get("status", "unknown")),
        }
        if job.get("status") == "completed":
            result["result"] = job.get("result")
        if job.get("status") == "failed":
            result["error"] = str(job.get("error", "任务失败"))
        return result

    def _prune_jobs(self) -> None:
        cutoff = asyncio.get_running_loop().time() - 3600
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.get("status") not in {"queued", "running"}
            and float(job.get("updated_at", 0)) < cutoff
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
            self._job_tasks.pop(job_id, None)
        if expired:
            expired_set = set(expired)
            self._request_jobs = {
                key: job_id
                for key, job_id in self._request_jobs.items()
                if job_id not in expired_set
            }

    @staticmethod
    def _request_id(payload) -> str:
        value = payload.get("request_id") if isinstance(payload, dict) else None
        value = str(value or "").strip()
        if not value or len(value) > 80 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in value):
            return uuid4().hex
        return value

    async def get_overview(self):
        if inactive := self._inactive():
            return inactive
        try:
            settings = EnvironmentManager.public_settings(self.runtime.settings())
            accounts = await self.providers.accounts(refresh=False)
            account_map = self._accounts_by_provider(accounts)
            targets = self._target_snapshot or await self._platform_targets(probe=False)
        except Exception as exc:
            return error_response(f"读取插件状态失败：{exc}", status_code=500)
        return json_response(
            {
                "environment": self._environment_snapshot,
                "settings": settings,
                "template_schema": {
                    "defaults": DEFAULT_TEMPLATES,
                    "variables": TEMPLATE_VARIABLES,
                    "enabled_defaults": REPLY_ENABLED_DEFAULTS,
                    "categories": TEMPLATE_CATEGORIES,
                },
                "providers": [
                    {
                        "id": platform_id,
                        "name": name,
                        "account": self._public_account(account_map.get(platform_id)),
                    }
                    for platform_id, name in PLATFORMS
                ],
                "targets": targets,
            }
        )

    async def check_environment(self):
        if inactive := self._inactive():
            return inactive
        request_id = self._request_id(await request.json(default={}))

        async def work():
            environment = await self.runtime.check_environment()
            if not isinstance(environment, dict):
                raise RuntimeError("环境检测返回结果格式无效")
            targets = await self._platform_targets(probe=True)
            self._environment_snapshot = environment
            self._target_snapshot = targets
            return {"environment": environment, "targets": targets}

        return self._start_job_response("environment_check", "environment_check", request_id, work)

    async def job_status(self):
        if inactive := self._inactive():
            return inactive
        job_id = str(request.query.get("job_id", "")).strip()
        if not job_id or len(job_id) > 64:
            return error_response("job_id 无效", status_code=400)
        self._prune_jobs()
        job = self._jobs.get(job_id)
        if job is None:
            return error_response("任务不存在或已清理", status_code=404)
        return json_response({"job": self._public_job(job)})

    async def start_login(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        provider = self._provider_id(payload.get("provider"))
        if not provider:
            return error_response("请选择有效的音乐平台", status_code=400)
        try:
            result = await self.providers.start_login(provider)
        except Exception as exc:
            return error_response(f"启动登录失败：{exc}", status_code=502)
        return json_response(self._public_login_result(result))

    async def login_status(self):
        if inactive := self._inactive():
            return inactive
        provider = self._provider_id(request.query.get("provider", ""))
        if not provider:
            return error_response("请选择有效的音乐平台", status_code=400)
        try:
            result = await self.providers.login_status(provider)
        except Exception as exc:
            return error_response(f"读取登录状态失败：{exc}", status_code=502)
        public_result = self._public_login_result(result)
        if public_result.get("status") == "success":
            accounts = await self.providers.accounts(refresh=False)
            account_map = self._accounts_by_provider(accounts)
            public_result["account"] = self._public_account(account_map.get(provider))
        return json_response(public_result)

    async def cancel_login(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        provider = self._provider_id(payload.get("provider")) if isinstance(payload, dict) else None
        if not provider:
            return error_response("请选择有效的音乐平台", status_code=400)
        try:
            result = await self.providers.cancel_login(provider)
        except Exception as exc:
            return error_response(f"取消登录失败：{exc}", status_code=502)
        return json_response(self._public_login_result(result))

    async def logout(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        provider = self._provider_id(payload.get("provider")) if isinstance(payload, dict) else None
        if not provider:
            return error_response("请选择有效的音乐平台", status_code=400)
        try:
            result = await self.providers.logout(provider)
            accounts = await self.providers.accounts(refresh=False)
        except Exception as exc:
            return error_response(f"退出登录失败：{exc}", status_code=502)
        account = self._accounts_by_provider(accounts).get(provider)
        return json_response(
            {
                "logged_out": bool(result.get("logged_out", True)) if isinstance(result, dict) else bool(result),
                "account": self._public_account(account),
                "message": result.get("message", "已退出登录。") if isinstance(result, dict) else "已退出登录。",
            }
        )

    async def install_tool(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        tool_id = str(payload.get("tool_id", "")).strip()
        url = str(payload.get("url") or "").strip()
        if tool_id not in TOOL_IDS:
            return error_response("请选择有效的工具", status_code=400)
        if not self._valid_custom_url(url, allow_empty=True):
            return error_response("下载地址必须是有效的 HTTP 或 HTTPS URL", status_code=400)
        request_id = self._request_id(payload)

        async def work():
            result = await self.runtime.install_tool(tool_id, url)
            if not isinstance(result, dict):
                raise RuntimeError("工具返回结果格式无效")
            return result

        return self._start_job_response("install_tool", f"install_tool:{tool_id}", request_id, work)

    async def save_settings(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        allowed_fields = {
            "max_songs", "tool_urls", "active_llm", "passive_llm",
            "reply_templates", "reply_enabled", "passive_playback",
            "active_playback", "cover_service", "song_log",
        }
        unknown_fields = set(payload) - allowed_fields
        if unknown_fields:
            return error_response(f"未知设置字段：{', '.join(sorted(map(str, unknown_fields)))}", status_code=400)
        values = {}
        if "max_songs" in payload:
            max_songs = payload["max_songs"]
            if not isinstance(max_songs, int) or isinstance(max_songs, bool) or max_songs < 1:
                return error_response("歌曲总数上限必须是正整数", status_code=400)
            values["max_songs"] = max_songs
        if "active_llm" in payload:
            if type(payload["active_llm"]) is not bool:
                return error_response("主动LLM设置必须是布尔值", status_code=400)
            values["active_llm"] = payload["active_llm"]
        if "passive_llm" in payload:
            if type(payload["passive_llm"]) is not bool:
                return error_response("被动LLM设置必须是布尔值", status_code=400)
            values["passive_llm"] = payload["passive_llm"]
        if "song_log" in payload:
            try:
                existing = self.runtime.settings()
                song_log = validate_song_log_settings(
                    payload["song_log"],
                    base=existing.get("song_log") if isinstance(existing, dict) else None,
                    strict=True,
                )
            except ValueError as exc:
                return error_response(str(exc) or "点歌日志设置无效", status_code=400)
            if song_log["enabled"]:
                try:
                    log_platform = self.context.get_platform_inst(song_log["platform_id"])
                except Exception:
                    log_platform = None
                if log_platform is None:
                    return error_response("点歌日志的平台实例不可用，请刷新页面后重新选择", status_code=409)
            values["song_log"] = song_log
        if "tool_urls" in payload:
            urls = payload["tool_urls"]
            if not isinstance(urls, dict):
                return error_response("tool_urls 必须是对象", status_code=400)
            normalized = {}
            for tool_id, value in urls.items():
                if tool_id not in TOOL_IDS:
                    return error_response(f"未知工具下载地址：{tool_id}", status_code=400)
                if not isinstance(value, str):
                    return error_response(f"{tool_id} 的地址必须是字符串", status_code=400)
                if not self._valid_custom_url(value, allow_empty=True):
                    return error_response(f"{tool_id} 的地址必须是有效的 HTTP 或 HTTPS URL", status_code=400)
                normalized[tool_id] = value.strip()
            values["tool_urls"] = normalized
        if "reply_templates" in payload:
            templates = payload["reply_templates"]
            if not isinstance(templates, dict) or any(
                not isinstance(name, str) or not isinstance(template, str)
                for name, template in templates.items()
            ):
                return error_response("回复模板必须是字符串映射", status_code=400)
            try:
                checked_templates = validate_templates(templates)
                values["reply_templates"] = {
                    name: checked_templates[name]
                    for name in templates
                }
            except (TypeError, ValueError) as exc:
                return error_response(str(exc) or "回复模板格式无效", status_code=400)
        if "reply_enabled" in payload:
            enabled = payload["reply_enabled"]
            if not isinstance(enabled, dict):
                return error_response("回复提示开关必须是布尔值映射", status_code=400)
            unknown = set(enabled) - set(REPLY_ENABLED_KEYS)
            if unknown:
                return error_response(
                    f"未知回复提示开关键：{', '.join(sorted(map(str, unknown)))}",
                    status_code=400,
                )
            if any(type(value) is not bool for value in enabled.values()):
                return error_response("回复提示开关必须是布尔值", status_code=400)
            values["reply_enabled"] = dict(enabled)
        nested_fields = {
            "passive_playback": {"max_seconds", "gap_seconds"},
            "active_playback": {"voice_id", "max_seconds", "gap_seconds", "incremental_output"},
            "cover_service": {"url", "token"},
        }
        for field, allowed_keys in nested_fields.items():
            if field not in payload:
                continue
            section = payload[field]
            if not isinstance(section, dict):
                return error_response(f"{field} 必须是对象", status_code=400)
            unknown = set(section) - allowed_keys
            if unknown:
                return error_response(
                    f"{field} 含未知设置字段：{', '.join(sorted(map(str, unknown)))}",
                    status_code=400,
                )
            normalized_section = {}
            for key, value in section.items():
                if key == "max_seconds":
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        return error_response(f"{field}.max_seconds 必须是非负整数", status_code=400)
                    normalized_section[key] = value
                elif key == "gap_seconds":
                    try:
                        numeric_value = float(value)
                    except (TypeError, ValueError, OverflowError):
                        return error_response(f"{field}.gap_seconds 必须是有限非负数", status_code=400)
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(numeric_value)
                        or numeric_value < 0
                    ):
                        return error_response(f"{field}.gap_seconds 必须是有限非负数", status_code=400)
                    normalized_section[key] = numeric_value
                elif key == "voice_id":
                    if not isinstance(value, str) or len(value.strip()) > 200:
                        return error_response("active_playback.voice_id 必须是不超过200字符的字符串", status_code=400)
                    normalized_section[key] = value.strip()
                elif key == "incremental_output":
                    if type(value) is not bool:
                        return error_response("active_playback.incremental_output 必须是布尔值", status_code=400)
                    normalized_section[key] = value
                elif key == "url":
                    if not isinstance(value, str) or not self._valid_custom_url(value, allow_empty=True):
                        return error_response("cover_service.url 必须是有效的 HTTP 或 HTTPS URL", status_code=400)
                    normalized_section[key] = value.strip().rstrip("/")
                elif key == "token":
                    if not isinstance(value, str) or len(value) > 4096:
                        return error_response("cover_service.token 必须是不超过4096字符的字符串", status_code=400)
                    normalized_section[key] = value
            values[field] = normalized_section
        try:
            settings = await self.runtime.save_settings(values)
        except ValueError as exc:
            return error_response(str(exc) or "设置内容无效", status_code=400)
        except Exception as exc:
            return error_response(f"保存设置失败：{exc}", status_code=500)
        return json_response({
            "saved": True,
            "settings": EnvironmentManager.public_settings(settings),
        })

    async def search(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        provider = self._provider_id(payload.get("platform"))
        query = str(payload.get("query") or "").strip()
        if not provider:
            return error_response("请选择有效的音乐平台", status_code=400)
        if not query or len(query) > MAX_QUERY_LENGTH:
            return error_response(f"关键词或链接长度必须在 1 到 {MAX_QUERY_LENGTH} 个字符之间", status_code=400)
        request_id = self._request_id(payload)

        async def work():
            results = await self.providers.search(provider, query, limit=10, page=1)
            if not isinstance(results, list):
                raise RuntimeError("搜索结果格式无效")
            candidates = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                exact_query = str(item.get("query") or "").strip()
                candidate_id = str(item.get("id") or "").strip()
                if not exact_query or not candidate_id:
                    continue
                candidates.append({
                    "platform": provider,
                    "id": candidate_id,
                    "query": exact_query,
                    "title": str(item.get("title") or "未知歌曲"),
                    "artist": str(item.get("artist") or "未知歌手"),
                    "album": str(item.get("album") or "未知专辑"),
                    "duration": item.get("duration"),
                })
            now = asyncio.get_running_loop().time()
            self._prune_search_selections(now)
            for candidate in candidates:
                key = (provider, candidate["id"], candidate["query"])
                self._search_selections[key] = now + SEARCH_SELECTION_TTL_SECONDS
            return {"candidates": candidates}

        return self._start_job_response("search", f"search:{provider}", request_id, work)

    async def download(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        provider = self._provider_id(payload.get("platform"))
        query = str(payload.get("query") or "").strip()
        candidate_id = str(payload.get("id") or "").strip()
        if not provider:
            return error_response("请选择有效的音乐平台", status_code=400)
        if not query or len(query) > MAX_QUERY_LENGTH:
            return error_response(f"歌曲精确选择键长度必须在 1 到 {MAX_QUERY_LENGTH} 个字符之间", status_code=400)
        if not candidate_id or len(candidate_id) > MAX_QUERY_LENGTH:
            return error_response("请先搜索歌曲，再选择一首候选歌曲下载。", status_code=400)
        now = asyncio.get_running_loop().time()
        self._prune_search_selections(now)
        if (provider, candidate_id, query) not in self._search_selections:
            return error_response("所选候选已失效或不匹配，请重新搜索后选择歌曲。", status_code=400)
        request_id = self._request_id(payload)

        async def work():
            song = await self.runtime.download_selected(provider, query)
            if not isinstance(song, dict):
                raise RuntimeError("下载器返回结果格式无效")
            return {"song": song}

        return self._start_job_response("download", "download", request_id, work)

    def _prune_search_selections(self, now: float) -> None:
        expired = [key for key, expires_at in self._search_selections.items() if expires_at <= now]
        for key in expired:
            self._search_selections.pop(key, None)

    async def get_files(self):
        if inactive := self._inactive():
            return inactive
        try:
            requested_platform = str(request.query.get("platform", "")).strip()
            platform = self._provider_id(requested_platform) if requested_platform else None
            if requested_platform and platform is None:
                return error_response("请选择有效的音乐平台", status_code=400)
            query = str(request.query.get("query", "")).strip()
            if len(query) > MAX_QUERY_LENGTH:
                return error_response(f"本地搜索长度不能超过 {MAX_QUERY_LENGTH} 个字符", status_code=400)
            files = self.runtime.files(platform=platform, query=query)
        except Exception as exc:
            return error_response(f"读取文件列表失败：{exc}", status_code=500)
        return json_response({"files": files})

    async def download_file(self):
        if inactive := self._inactive():
            return inactive
        song_id = str(request.query.get("song_id", "")).strip()
        filename = str(request.query.get("filename", "")).strip()
        cover_id = str(request.query.get("cover_id", "")).strip()
        if (
            not song_id
            or len(song_id) > MAX_SONG_ID_LENGTH
            or not filename
            or len(filename) > 255
            or len(cover_id) > MAX_SONG_ID_LENGTH
        ):
            return error_response("歌曲和文件名无效", status_code=400)
        try:
            if cover_id:
                path = Path(self.runtime.cover_file_path(song_id, cover_id, filename))
            else:
                path = Path(self.runtime.file_path(song_id, filename))
        except (KeyError, ValueError, FileNotFoundError, OSError):
            return error_response("找不到请求的音频文件", status_code=404)
        if not path.is_file():
            return error_response("找不到请求的音频文件", status_code=404)
        return file_response(path, filename=filename)

    async def send_song(self):
        """Send only an existing downloaded song after an explicit target selection."""
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)

        request_id = self._request_id(payload)
        self._prune_jobs()
        previous_id = self._request_jobs.get(f"manual_song_send:{request_id}")
        if previous_id and previous_id in self._jobs:
            return self._job_response(self._jobs[previous_id])

        song_id = str(payload.get("song_id") or "").strip()
        cover_id = str(payload.get("cover_id") or "").strip()
        platform_id = str(payload.get("platform_id") or "").strip()
        target_type = str(payload.get("target_type") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        if not song_id or len(song_id) > MAX_SONG_ID_LENGTH:
            return error_response("歌曲记录编号无效", status_code=400)
        if len(cover_id) > MAX_SONG_ID_LENGTH:
            return error_response("翻唱缓存编号无效", status_code=400)
        if not platform_id or ":" in platform_id or "!" in platform_id:
            return error_response("请选择有效的平台实例", status_code=400)
        if target_type not in {"private", "group"}:
            return error_response("目标类型必须是私聊或群聊", status_code=400)
        if not target_id or len(target_id) > MAX_TARGET_LENGTH or ":" in target_id:
            return error_response(f"目标 ID 必须是 1 到 {MAX_TARGET_LENGTH} 个字符", status_code=400)

        try:
            platform = self.context.get_platform_inst(platform_id)
        except Exception:
            platform = None
        if platform is None:
            return error_response("所选平台实例已不可用，请刷新页面", status_code=409)

        try:
            song = next(
                (item for item in self.runtime.files() if str(item.get("id", "")) == song_id),
                None,
            )
        except Exception as exc:
            return error_response(f"读取歌曲记录失败：{exc}", status_code=500)
        if song is None:
            return error_response("歌曲记录不存在，可能已被清理；请刷新已下载文件列表。", status_code=404)
        segments = song.get("segments")
        cover = None
        if cover_id:
            try:
                cover = next(
                    (
                        item for item in self.runtime.list_covers(song_id)
                        if isinstance(item, dict) and str(item.get("id", "")) == cover_id
                    ),
                    None,
                )
            except Exception:
                cover = None
            if cover is None:
                return error_response("找不到所选翻唱缓存，请刷新已下载文件列表。", status_code=404)
            sendable_segments = cover.get("segments")
            if not isinstance(sendable_segments, list) or not sendable_segments:
                return error_response("翻唱缓存没有可发送的语音分段。", status_code=409)
            planned_total = len(sendable_segments)
        else:
            if not isinstance(segments, list) or not segments:
                return error_response("歌曲记录没有可用的语音分段，音频文件可能已缺失。", status_code=409)
            planned_total = len(segments)

        target_umo = self._target_umo(platform_id, target_type, target_id)
        title = str(song.get("title") or "未命名歌曲")
        artist = str(song.get("artist") or "未知歌手")
        requested_at = time.time()
        observed_sent = 0
        observed_seconds = 0.0
        music_platform = str(song.get("platform") or "unknown")
        music_platform_name = dict(PLATFORMS).get(music_platform, music_platform)
        voice_label = (
            str(cover.get("voice_name") or cover.get("voice_id") or "原唱")
            if cover is not None
            else "原唱"
        )

        async def send_audio(path: Path) -> None:
            nonlocal observed_sent, observed_seconds
            record = Record(file=str(path), url=str(path))
            chain = MessageChain(chain=[record])
            sent = await self.context.send_message(target_umo, chain)
            if sent is False:
                raise RuntimeError("AstrBot 未找到目标平台实例。")
            observed_sent += 1
            try:
                with wave.open(str(path), "rb") as audio:
                    observed_seconds += audio.getnframes() / audio.getframerate()
            except (OSError, EOFError, wave.Error, ZeroDivisionError):
                pass

        async def emit_log(result=None, reason=""):
            result = result if isinstance(result, dict) else {}
            try:
                settings = self.runtime.settings()
            except Exception:
                settings = {}
            sent_value = result.get("sent", observed_sent)
            total_value = result.get("total") or planned_total
            failure_reason = reason
            if not failure_reason:
                failed = result.get("failed_segment")
                try:
                    complete_result = (
                        int(total_value) > 0
                        and int(sent_value or 0) == int(total_value)
                        and not failed
                    )
                except (TypeError, ValueError, OverflowError):
                    complete_result = False
                if not complete_result:
                    failure_reason = str(result.get("message") or "发送过程未完成")
            await send_song_log(
                self.context,
                settings,
                title=title,
                artist=artist,
                platform=music_platform_name,
                trigger="页面手动",
                voice=str(result.get("voice_name") or voice_label),
                source_session=f"页面手动发送至 {target_umo}",
                sent=sent_value,
                total=total_value,
                sent_seconds=result.get("sent_seconds"),
                fallback_seconds=observed_seconds,
                failure_reason=failure_reason,
            )

        async def work():
            started_at = time.time()
            passive_scope = None
            if self.passive_memory is not None and self._passive_enabled():
                try:
                    passive_scope = await capture_current_scope(
                        self.context, target_umo
                    )
                except Exception:
                    logger.exception("捕获网页点歌目标对话失败，继续发送歌曲。")
            try:
                if cover_id:
                    result = await self.runtime.send_cached_cover(song_id, cover_id, send_audio)
                else:
                    result = await self.runtime.send_song(song_id, send_audio)
            except asyncio.CancelledError:
                await emit_log(reason="发送任务已取消")
                raise
            except Exception as exc:
                await emit_log(reason=str(exc) or "音频发送失败")
                raise
            if not isinstance(result, dict):
                await emit_log(reason="音频发送返回结果格式无效")
                raise RuntimeError("歌曲发送返回结果格式无效")
            try:
                sent_count = max(0, int(result.get("sent", observed_sent) or 0))
                total_count = max(0, int(result.get("total") or planned_total))
            except (TypeError, ValueError, OverflowError):
                sent_count, total_count = observed_sent, planned_total
            if total_count <= 0:
                await emit_log(result, reason=str(result.get("message") or "没有可发送的语音分段"))
                raise RuntimeError(str(result.get("message") or "歌曲没有可发送的语音分段"))
            failed_segment = result.get("failed_segment")
            complete = sent_count == total_count and failed_segment is None
            await emit_log(result)
            if (
                passive_scope
                and sent_count > 0
                and self.passive_memory is not None
                and self._passive_enabled()
            ):
                try:
                    await self.passive_memory.record(
                        umo=passive_scope[0],
                        conversation_id=passive_scope[1],
                        requested_at=requested_at,
                        started_at=started_at,
                        title=title,
                        artist=artist,
                        platform=str(song.get("platform") or "unknown"),
                        source="web",
                        sent=sent_count,
                        total=total_count,
                        failed_segment=(
                            str(failed_segment) if failed_segment else None
                        ),
                        voice_id=(str(result.get("voice_id") or cover.get("voice_id") or "") if cover else None),
                        voice_name=(str(result.get("voice_name") or cover.get("voice_name") or "") if cover else None),
                        voice_version=(str(result.get("voice_version") or cover.get("voice_version") or "") if cover else None),
                        sent_seconds=result.get("sent_seconds"),
                    )
                except Exception:
                    logger.exception("保存网页点歌发送记录失败。")
            return {
                "ok": complete,
                "sent": sent_count,
                "total": total_count,
                "failed_segment": failed_segment,
                "message": str(result.get("message") or ""),
                "song": {"id": song_id, "title": title, "artist": artist},
                "voice": {
                    "id": result.get("voice_id") or (cover.get("voice_id") if cover else ""),
                    "name": result.get("voice_name") or (cover.get("voice_name") if cover else "原唱"),
                    "version": result.get("voice_version") or (cover.get("voice_version") if cover else ""),
                },
                "target": {
                    "platform_id": platform_id,
                    "target_type": target_type,
                    "target_id": target_id,
                },
            }

        return self._start_job_response(
            "manual_song_send",
            "manual_song_send",
            request_id,
            work,
        )

    def _cover_client(self) -> CoverClient:
        return CoverClient(self.runtime.settings)

    async def check_cover_service(self):
        if inactive := self._inactive():
            return inactive
        try:
            result = await self._cover_client().check()
        except CoverServiceError as exc:
            return error_response(str(exc), status_code=502)
        except Exception:
            return error_response("检测翻唱服务失败", status_code=502)
        return json_response({"health": result["health"], "voices": result["voices"]})

    async def download_cover_deployment_guide(self):
        if inactive := self._inactive():
            return inactive
        guide = Path(__file__).resolve().parent / "cover_deployment.md"
        if not guide.is_file():
            return error_response("翻唱服务接入说明当前不可用", status_code=404)
        return file_response(guide, filename="翻唱服务接入与接口说明.md")

    async def test_cover(self):
        """Queue an uncached cover validation without sending or recording memory."""
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        song_id = str(payload.get("song_id") or "").strip()
        voice_id = str(payload.get("voice_id") or "").strip()
        if not song_id or len(song_id) > MAX_SONG_ID_LENGTH:
            return error_response("歌曲记录编号无效", status_code=400)
        if not voice_id or len(voice_id) > 200:
            return error_response("请先选择有效的翻唱音色", status_code=400)
        try:
            song_exists = any(
                isinstance(item, dict) and str(item.get("id", "")) == song_id
                for item in self.runtime.files()
            )
        except Exception:
            song_exists = False
        if not song_exists:
            return error_response("歌曲记录不存在，可能已被清理；请刷新已下载文件列表。", status_code=404)
        request_id = self._request_id(payload)

        async def work():
            result = await self.runtime.test_cover(song_id, voice_id)
            if not isinstance(result, dict):
                raise RuntimeError("翻唱测试返回结果格式无效")
            return result

        return self._start_job_response("cover_test", "cover_test", request_id, work)

    async def get_cover_voices(self):
        if inactive := self._inactive():
            return inactive
        try:
            voices = await self._cover_client().voices()
        except CoverServiceError as exc:
            return error_response(str(exc), status_code=502)
        except Exception:
            return error_response("读取翻唱音色失败", status_code=502)
        return json_response({"voices": voices})

    async def upload_cover_file(self):
        if inactive := self._inactive():
            return inactive
        try:
            files = await request.files()
        except Exception:
            return error_response("无法读取上传文件", status_code=400)
        upload = files.get("file") if hasattr(files, "get") else None
        try:
            from astrbot.api.web import PluginUploadFile
        except ImportError:
            PluginUploadFile = None
        if upload is None or not callable(getattr(upload, "save", None)):
            return error_response("请选择一个 RVC 模型或索引文件", status_code=400)
        if PluginUploadFile is not None and not isinstance(upload, PluginUploadFile):
            return error_response("上传文件格式无效", status_code=400)

        submitted_name = str(getattr(upload, "filename", "") or "")
        filename = submitted_name.replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or filename in {".", ".."} or len(filename) > 255:
            return error_response("上传文件名无效", status_code=400)
        extension = Path(filename).suffix.lower()
        if extension in {".pt", ".pth"}:
            kind = "model"
            size_limit = MAX_COVER_MODEL_BYTES
        elif extension == ".index":
            kind = "index"
            size_limit = MAX_COVER_INDEX_BYTES
        else:
            return error_response("只支持 .pt、.pth 模型和 .index 索引文件", status_code=400)

        self._prune_cover_uploads()
        try:
            root = self._ensure_cover_staging_dir()
        except Exception:
            return error_response("准备音色暂存目录失败", status_code=500)
        upload_id = uuid4().hex
        target = root / f"{upload_id}{extension}"
        try:
            await upload.save(target)
            if not self._active:
                target.unlink(missing_ok=True)
                return error_response("插件页面 API 已停用", status_code=503)
            if not target.is_file() or target.is_symlink():
                raise OSError("上传文件未写入")
            size = target.stat().st_size
            if size <= 0:
                raise ValueError("上传文件为空")
            if size > size_limit:
                target.unlink(missing_ok=True)
                return error_response("上传文件超过允许大小", status_code=413)
        except asyncio.CancelledError:
            target.unlink(missing_ok=True)
            raise
        except ValueError as exc:
            target.unlink(missing_ok=True)
            return error_response(str(exc), status_code=400)
        except Exception:
            target.unlink(missing_ok=True)
            return error_response("保存上传文件失败", status_code=400)

        self._cover_uploads[upload_id] = {
            "path": target,
            "kind": kind,
            "filename": filename,
            "size": size,
            "created_at": time.monotonic(),
        }
        return json_response({"upload_id": upload_id, "kind": kind, "filename": filename})

    async def import_cover_voice(self):
        if inactive := self._inactive():
            return inactive
        self._prune_cover_uploads()
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        name = str(payload.get("name") or "").strip()
        model_upload_id = str(payload.get("model_upload_id") or "").strip()
        index_upload_id = str(payload.get("index_upload_id") or "").strip()
        if not name or len(name) > 80:
            return error_response("音色名称必须是 1 到 80 个字符", status_code=400)
        request_id = self._request_id(payload)
        self._prune_jobs()
        previous_id = self._request_jobs.get(f"cover_import:{request_id}")
        if previous_id and previous_id in self._jobs:
            return self._job_response(self._jobs[previous_id])

        model_upload = self._cover_uploads.get(model_upload_id)
        if not model_upload or model_upload.get("kind") != "model":
            return error_response("模型上传编号无效或已过期，请重新上传文件", status_code=400)
        index_upload = self._cover_uploads.get(index_upload_id) if index_upload_id else None
        if index_upload_id and (not index_upload or index_upload.get("kind") != "index"):
            return error_response("索引上传编号无效或已过期，请重新上传文件", status_code=400)
        model_path = Path(model_upload["path"])
        index_path = Path(index_upload["path"]) if index_upload else None
        settings_snapshot = self.runtime.settings()

        async def work():
            try:
                client = CoverClient(lambda: settings_snapshot)
                voice = await client.import_voice(
                    model_path,
                    name=name,
                    index_path=index_path,
                )
                return {"voice": voice}
            finally:
                self._discard_cover_upload(model_upload_id)
                if index_upload_id:
                    self._discard_cover_upload(index_upload_id)

        return self._start_job_response("cover_import", "cover_import", request_id, work)

    def _passive_enabled(self) -> bool:
        getter = getattr(self.runtime, "settings", None)
        try:
            settings = getter() if callable(getter) else {}
        except Exception:
            return False
        return isinstance(settings, dict) and settings.get("passive_llm") is True

    async def test_audio(self):
        if inactive := self._inactive():
            return inactive
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        platform_id = str(payload.get("platform_id") or "").strip()
        target_type = str(payload.get("target_type") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        if not platform_id or ":" in platform_id or "!" in platform_id:
            return error_response("请选择有效的平台实例", status_code=400)
        if target_type not in {"private", "group"}:
            return error_response("目标类型必须是私聊或群聊", status_code=400)
        if not target_id or len(target_id) > MAX_TARGET_LENGTH or ":" in target_id:
            return error_response(f"目标 ID 必须是 1 到 {MAX_TARGET_LENGTH} 个字符", status_code=400)
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return error_response("所选平台实例已不可用，请刷新页面", status_code=409)
        target_umo = self._target_umo(platform_id, target_type, target_id)

        async def send_audio(path: Path) -> None:
            record = Record(file=str(path), url=str(path))
            chain = MessageChain(chain=[record])
            sent = await self.context.send_message(target_umo, chain)
            if sent is False:
                raise RuntimeError("AstrBot 未找到目标平台实例。")

        request_id = self._request_id(payload)

        async def work():
            try:
                result = await self.runtime.test_audio(send_audio)
            except Exception as exc:
                record_test = {"status": "failed", "message": str(exc)}
                self._record_tests[platform_id] = record_test
                self._update_record_test_snapshot(platform_id, record_test)
                raise RuntimeError(f"音频测试发送失败：{exc}") from exc
            if not isinstance(result, dict):
                raise RuntimeError("音频测试返回结果格式无效")
            sent_count = int(result.get("sent", 0) or 0)
            total_count = int(result.get("total", 0) or 0)
            complete = (
                result.get("ok") is True
                and total_count > 0
                and sent_count == total_count
                and not result.get("failed_segment")
            )
            self._record_tests[platform_id] = {
                "status": "ok" if complete else "failed",
                "message": result.get("message", ""),
                "sent": sent_count,
                "total": total_count,
            }
            self._update_record_test_snapshot(platform_id, self._record_tests[platform_id])
            return {
                **result,
                "target": {"platform_id": platform_id, "target_type": target_type, "target_id": target_id},
            }

        return self._start_job_response("test_audio", "test_audio", request_id, work)

    async def _platform_targets(self, *, probe: bool) -> list[dict]:
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        if not callable(get_insts):
            return []
        targets = []
        for platform in get_insts():
            try:
                meta = platform.meta()
                platform_id = str(meta.id or "")
                adapter_name = str(meta.name or "")
                display_name = str(getattr(meta, "adapter_display_name", None) or adapter_name)
                status_value = getattr(platform.status, "value", platform.status)
                status = str(status_value or "unknown")
                if probe:
                    bot = await self._probe_bot(platform, adapter_name, status)
                else:
                    record_test = self._record_tests.get(platform_id, {"status": "not_tested"})
                    bot = {"online": None, "version": "", "record_test": record_test, "can_send_record": True if record_test.get("status") == "ok" else None}
                targets.append(
                    {
                        "id": platform_id,
                        "name": display_name,
                        "adapter": adapter_name,
                        "status": status,
                        "bot": bot,
                    }
                )
            except Exception:
                continue
        return targets

    async def _probe_bot(self, platform, adapter_name: str, platform_status: str) -> dict:
        record_test = self._record_tests.get(platform.meta().id, {"status": "not_tested"})
        can_send_record = True if record_test.get("status") == "ok" else None
        result = {
            "online": None,
            "version": "",
            "record_test": record_test,
            "can_send_record": can_send_record,
        }
        if adapter_name != "aiocqhttp":
            result["online_source"] = "AstrBot 平台实例状态"
            return result
        try:
            client = platform.get_client()
        except Exception as exc:
            result.update({"online": None, "can_send_record": None, "message": f"无法获取 OneBot 客户端：{exc}"})
            return result
        call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            result.update({"online": None, "can_send_record": None, "message": "客户端未提供 OneBot action 接口。"})
            return result
        try:
            status = await asyncio.wait_for(call_action(action="get_status"), timeout=5)
            if isinstance(status, dict) and "online" in status:
                online_value = status["online"]
                if isinstance(online_value, bool):
                    result["online"] = online_value
                elif str(online_value).strip().lower() in {"1", "true", "yes", "online"}:
                    result["online"] = True
                elif str(online_value).strip().lower() in {"0", "false", "no", "offline"}:
                    result["online"] = False
                else:
                    result["online"] = None
            else:
                result["online"] = None
        except Exception as exc:
            result.update({"online": None, "message": f"OneBot 在线状态检查失败：{exc}"})
        try:
            version = await asyncio.wait_for(call_action(action="get_version_info"), timeout=5)
            if isinstance(version, dict):
                app_name = str(version.get("app_name") or "")
                app_version = str(version.get("app_version") or "")
                result["version"] = " ".join(part for part in (app_name, app_version) if part)
        except Exception as exc:
            result["version_message"] = f"OneBot 版本信息检查失败：{exc}"
        return result

    def _update_record_test_snapshot(self, platform_id: str, record_test: dict) -> None:
        if self._target_snapshot is None:
            return
        for target in self._target_snapshot:
            if target.get("id") == platform_id:
                target.setdefault("bot", {})["record_test"] = dict(record_test)
                target["bot"]["can_send_record"] = True if record_test.get("status") == "ok" else None

    @staticmethod
    def _target_umo(platform_id: str, target_type: str, target_id: str) -> str:
        message_type = "GroupMessage" if target_type == "group" else "FriendMessage"
        return f"{platform_id}:{message_type}:{target_id}"

    @staticmethod
    def _provider_id(value) -> str | None:
        provider = str(value or "").strip().lower()
        return provider if provider in PROVIDER_IDS else None

    @staticmethod
    def _valid_custom_url(value, *, allow_empty: bool) -> bool:
        if value is None:
            return allow_empty
        value = str(value).strip()
        if not value:
            return allow_empty
        if len(value) > 2048:
            return False
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password

    @staticmethod
    def _accounts_by_provider(accounts) -> dict[str, dict]:
        if isinstance(accounts, dict):
            return {str(key): value for key, value in accounts.items() if isinstance(value, dict)}
        if isinstance(accounts, list):
            return {
                str(item.get("platform")): item
                for item in accounts
                if isinstance(item, dict) and item.get("platform")
            }
        return {}

    @staticmethod
    def _public_account(account) -> dict | None:
        if not isinstance(account, dict):
            return None
        membership = account.get("membership")
        if not isinstance(membership, dict):
            membership = {"status": "unknown", "label": "未知", "expires_at": None}
        status = membership.get("status")
        if status not in {"active", "inactive", "unknown"}:
            status = "unknown"
        membership_public = {
            "status": status,
            "label": str(membership.get("label") or "未知"),
            "expires_at": membership.get("expires_at"),
        }
        return {
            "logged_in": bool(account.get("logged_in", False)),
            "nickname": str(account.get("nickname") or ""),
            "user_id": str(account.get("user_id") or ""),
            "membership": membership_public,
            "message": str(account.get("message") or ""),
        }

    @classmethod
    def _public_login_result(cls, result) -> dict:
        if not isinstance(result, dict):
            return {"status": "error", "message": "登录接口返回结果格式无效。"}
        public = {
            "status": str(result.get("status") or result.get("state") or "error"),
            "message": str(result.get("message") or ""),
        }
        if result.get("qr_image"):
            qr_image = str(result["qr_image"])
            if qr_image.startswith("data:image/"):
                public["qr_image"] = qr_image
        if result.get("expires_at"):
            public["expires_at"] = result["expires_at"]
        if result.get("account"):
            public["account"] = cls._public_account(result["account"])
        return public

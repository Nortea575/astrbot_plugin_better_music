"""Conversation-scoped LLM tools for active vocal rendering."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
import wave
from pathlib import Path
from typing import Any
from uuid import uuid4

from .providers import PLATFORMS
from .templates import DEFAULT_TEMPLATES, REPLY_ENABLED_DEFAULTS, render_template, validate_templates


ACTIVE_TOOL_NAMES = frozenset({"better_music_search", "better_music_sing"})
ACTIVE_SEARCH_PRIORITY = ("netease", "kugou", "qq")
ACTIVE_SEARCH_TTL_SECONDS = 5 * 60
ACTIVE_JOB_RETENTION_SECONDS = 60 * 60
MAX_ACTIVE_JOBS_PER_SCOPE = 5
_PLATFORM_NAMES = dict(PLATFORMS)
_PLATFORM_NAMES.update({"netease": "网易云音乐", "kugou": "酷狗音乐", "qq": "QQ音乐"})


def _tool_result(status: str, display_text: str, **fields: Any) -> str:
    return json.dumps(
        {"status": status, **fields, "display_text": display_text},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_active_tools(controller, function_tool_class=None) -> list[Any]:
    """Build plugin-owned native FunctionTool subclasses with strict schemas."""
    if function_tool_class is None:
        from astrbot.core.agent.tool import FunctionTool as function_tool_class

    def tool_type(name: str):
        return type(
            f"BetterMusic{name.title().replace('_', '')}FunctionTool",
            (function_tool_class,),
            {"__module__": __name__},
        )

    search_tool = tool_type("search")(
        name="better_music_search",
        description=(
            "搜索已验证登录的平台，返回精确候选及candidate_id。你可以根据当前聊天内容自行决定是否演唱及选曲；"
            "需要演唱时，先搜索，再按聊天意图自行选择准确候选并用当前音色演唱或翻唱；"
            "若用户已指定歌曲则遵循指定内容。sing只接受本工具返回的candidate_id，不可传URL或本地路径。"
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=controller.search_handler,
        handler_module_path=__name__,
        is_background_task=False,
    )
    sing_tool = tool_type("sing")(
        name="better_music_sing",
        description=(
            "使用当前会话配置的音色演唱或翻唱一首由better_music_search返回的候选歌曲。"
            "只接受本会话候选的candidate_id；任务会异步排队，不能把排队当作已完成。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "minLength": 32,
                    "maxLength": 32,
                    "pattern": "^[a-f0-9]{32}$",
                }
            },
            "required": ["candidate_id"],
            "additionalProperties": False,
        },
        handler=controller.sing_handler,
        handler_module_path=__name__,
        is_background_task=False,
    )
    return [search_tool, sing_tool]


class ActiveMusicController:
    """Manage short-lived candidate handles and plugin-owned singing jobs."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._lock = asyncio.Lock()
        self._generation: dict[tuple[str, str, str], int] = {}
        self._selections: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._inflight: dict[tuple[str, str], str] = {}
        self._candidate_jobs: dict[tuple[tuple[str, str, str], str], tuple[str, float]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._recent: dict[tuple[str, str], list[str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def bind_request(self, event: Any, conversation_id: str) -> None:
        cid = str(conversation_id or "").strip()
        if cid:
            try:
                setattr(event, "_better_music_conversation_id", cid)
            except (AttributeError, TypeError):
                pass

    @staticmethod
    def _tool_names(function_tools: Any) -> set[str]:
        if function_tools is None:
            return set()
        if hasattr(function_tools, "tools"):
            tools = getattr(function_tools, "tools", [])
        elif isinstance(function_tools, dict):
            tools = function_tools.values()
        elif isinstance(function_tools, (list, tuple, set)):
            tools = function_tools
        else:
            tools = []
        return {
            str(getattr(tool, "name", ""))
            for tool in tools
            if getattr(tool, "name", None)
        }

    @classmethod
    def tools_available(cls, request: Any) -> bool:
        return ACTIVE_TOOL_NAMES.issubset(cls._tool_names(getattr(request, "func_tool", None)))

    @classmethod
    def gate_request_tools(cls, request: Any, active_enabled: bool) -> None:
        """Hide only these tools on this request without mutating shared allowlists."""
        if active_enabled:
            return
        function_tools = getattr(request, "func_tool", None)
        if function_tools is None:
            return
        if hasattr(function_tools, "tools"):
            cloned = copy.copy(function_tools)
            cloned.tools = [
                tool for tool in function_tools.tools
                if getattr(tool, "name", None) not in ACTIVE_TOOL_NAMES
            ]
            request.func_tool = cloned
        elif isinstance(function_tools, dict):
            request.func_tool = {
                name: tool for name, tool in function_tools.items()
                if name not in ACTIVE_TOOL_NAMES
            }
        elif isinstance(function_tools, (list, tuple, set)):
            filtered = [
                tool for tool in function_tools
                if getattr(tool, "name", None) not in ACTIVE_TOOL_NAMES
            ]
            request.func_tool = type(function_tools)(filtered)

    def settings(self) -> dict[str, Any]:
        getter = getattr(self.plugin, "_settings", None)
        if callable(getter):
            result = getter()
        else:
            runtime = getattr(self.plugin, "runtime", None)
            result = runtime.settings() if callable(getattr(runtime, "settings", None)) else {}
        return result if isinstance(result, dict) else {}

    def active_config(self) -> tuple[bool, dict[str, Any]]:
        settings = self.settings()
        playback = settings.get("active_playback", {})
        if not isinstance(playback, dict):
            playback = {}
        voice_id = str(playback.get("voice_id") or "").strip()
        max_seconds = playback.get("max_seconds", 60)
        if not isinstance(max_seconds, int) or isinstance(max_seconds, bool) or max_seconds < 0:
            max_seconds = 60
        gap_seconds = playback.get("gap_seconds", 0)
        try:
            gap_seconds = float(gap_seconds)
        except (TypeError, ValueError, OverflowError):
            gap_seconds = 0.0
        if gap_seconds < 0 or not _finite_number(gap_seconds):
            gap_seconds = 0.0
        return settings.get("active_llm") is True, {
            "voice_id": voice_id,
            "max_seconds": max_seconds,
            "gap_seconds": gap_seconds,
        }

    async def _scope(self, event: Any) -> tuple[str, str, str] | None:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        cid = str(getattr(event, "_better_music_conversation_id", "") or "").strip()
        if not cid:
            cid = await self._current_cid(umo)
        getter = getattr(event, "get_sender_id", None)
        try:
            actor = str(getter() if callable(getter) else getattr(event, "sender_id", "") or "").strip()
        except Exception:
            actor = ""
        if not umo or not cid or not actor:
            return None
        if await self._current_cid(umo) != cid:
            return None
        return umo, cid, actor

    async def _current_cid(self, umo: str) -> str:
        manager = getattr(getattr(self.plugin, "context", None), "conversation_manager", None)
        getter = getattr(manager, "get_curr_conversation_id", None)
        if not umo or not callable(getter):
            return ""
        try:
            value = getter(umo)
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            return ""
        return str(value or "").strip()

    async def _scope_is_current(self, scope: tuple[str, str, str]) -> bool:
        return await self._current_cid(scope[0]) == scope[1]

    async def search_handler(self, event: Any, query: str) -> str:
        active_enabled, playback = self.active_config()
        if self._closed or not active_enabled:
            return _tool_result("rejected", "主动演唱模式当前已关闭。", query=str(query or ""), platform="", candidates=[])
        if not playback["voice_id"]:
            return _tool_result("rejected", "请先在Better Music设置中选择一个可用音色，再搜索歌曲。", query=str(query or ""), platform="", candidates=[])
        text = str(query or "").strip()
        if not text or len(text) > 200:
            return _tool_result("rejected", "搜索关键词必须为1至200个字符。", query=text, platform="", candidates=[])
        scope = await self._scope(event)
        if scope is None:
            return _tool_result("rejected", "无法确认当前会话和用户，请在当前对话中重新发起搜索。", query=text, platform="", candidates=[])

        async with self._lock:
            self._prune_locked(time.monotonic())
            generation = self._generation.get(scope, 0) + 1
            self._generation[scope] = generation
            self._selections.pop(scope, None)

        platform = None
        for candidate_platform in ACTIVE_SEARCH_PRIORITY:
            checker = getattr(self.plugin.providers, "chat_account_status", None)
            if not callable(checker):
                break
            try:
                status = checker(candidate_platform)
                if inspect.isawaitable(status):
                    status = await status
            except Exception:
                continue
            if isinstance(status, dict) and status.get("status") == "valid":
                platform = candidate_platform
                break
        if platform is None:
            return _tool_result("unavailable", "当前没有状态验证通过的已登录音乐平台，请先登录后再搜索。", query=text, platform="", candidates=[])
        if not self._is_active():
            return _tool_result("rejected", "主动演唱模式已关闭，本次搜索没有继续。", query=text, platform=platform, candidates=[])
        try:
            results = await self.plugin.providers.search(platform, text, limit=5, page=1)
        except Exception as exc:
            return _tool_result("failed", self._failure_text("搜索", str(exc)), query=text, platform=platform, candidates=[])
        if not isinstance(results, list):
            results = []
        selected: dict[str, dict[str, Any]] = {}
        for item in results[:5]:
            candidate = self._public_candidate(item)
            if candidate is None:
                continue
            handle = uuid4().hex
            selected[handle] = candidate
        if not self._is_active() or not await self._scope_is_current(scope):
            return _tool_result("rejected", "会话或主动演唱设置已变化，本次搜索结果已丢弃。", query=text, platform=platform, candidates=[])
        async with self._lock:
            if self._generation.get(scope) != generation:
                return _tool_result("superseded", "这次搜索已被更新的搜索替代，请使用最新返回的候选。", query=text, platform=platform, candidates=[])
            self._selections[scope] = {
                "generation": generation,
                "expires_at": time.monotonic() + ACTIVE_SEARCH_TTL_SECONDS,
                "platform": platform,
                "candidates": selected,
            }
        candidates = [
            {"candidate_id": handle, **candidate}
            for handle, candidate in selected.items()
        ]
        status = "ok" if candidates else "empty"
        display_text = (
            f"{_PLATFORM_NAMES.get(platform, platform)}搜索“{text}”找到{len(candidates)}首候选。"
            if candidates
            else f"{_PLATFORM_NAMES.get(platform, platform)}没有找到可用候选。"
        )
        return _tool_result(
            status,
            display_text,
            platform=platform,
            platform_name=_PLATFORM_NAMES.get(platform, platform),
            query=text,
            candidates=candidates,
        )

    async def sing_handler(self, event: Any, candidate_id: str) -> str:
        active_enabled, playback = self.active_config()
        if self._closed or not active_enabled:
            return self._sing_result("rejected", "主动演唱模式当前已关闭。")
        if not playback["voice_id"]:
            return self._sing_result("rejected", "请先选择主动演唱音色，再提交候选歌曲。")
        handle = str(candidate_id or "").strip()
        if len(handle) != 32 or any(character not in "0123456789abcdef" for character in handle):
            return self._sing_result("rejected", "candidate_id格式无效，请先使用better_music_search获取候选。")
        scope = await self._scope(event)
        if scope is None:
            return self._sing_result("rejected", "无法确认当前会话和用户，请在当前对话中重新搜索歌曲。")
        async with self._lock:
            if self._closed:
                return self._sing_result("rejected", "主动演唱插件正在卸载，本次任务没有启动。")
            now = time.monotonic()
            self._prune_locked(now)
            job_key = (scope, handle)
            existing = self._candidate_jobs.get(job_key)
            if existing is not None:
                existing_job = self._jobs.get(existing[0])
                if existing_job is not None:
                    return self._queued_text(existing_job, duplicate=True)
            selection_set = self._selections.get(scope)
            candidate = (
                selection_set.get("candidates", {}).get(handle)
                if selection_set and selection_set.get("expires_at", 0) > now
                else None
            )
            if candidate is None:
                return self._sing_result("rejected", "这个候选不属于当前用户和对话，或已过期；请重新搜索。")
            playback = self.active_config()[1]
            if not playback["voice_id"]:
                return self._sing_result("rejected", "主动演唱音色已清除，请重新选择音色。")
            conversation_scope = (scope[0], scope[1])
            in_flight_id = self._inflight.get(conversation_scope)
            if in_flight_id is not None:
                in_flight = self._jobs.get(in_flight_id)
                if in_flight and in_flight.get("candidate_id") == handle:
                    return self._queued_text(in_flight, duplicate=True)
                return self._sing_result("rejected", "当前对话已有一首歌曲正在处理，完成后再提交下一首。")
            if not await self._scope_is_current(scope):
                return self._sing_result("rejected", "当前对话已切换，请在新对话中重新搜索歌曲。")
            job_id = uuid4().hex
            job = {
                "job_id": job_id,
                "scope": scope,
                "candidate_id": handle,
                "candidate": dict(candidate),
                "platform": selection_set["platform"],
                "voice_id": playback["voice_id"],
                "max_seconds": playback["max_seconds"],
                "gap_seconds": playback["gap_seconds"],
                "event": event,
                "status": "queued",
                "message": "任务已排队",
                "sent": 0,
                "total": 0,
                "sent_seconds": 0.0,
                "created_at": now,
                "updated_at": now,
            }
            self._jobs[job_id] = job
            self._inflight[conversation_scope] = job_id
            self._candidate_jobs[job_key] = (
                job_id, now + ACTIVE_JOB_RETENTION_SECONDS
            )
            recent = self._recent.setdefault(conversation_scope, [])
            recent.append(job_id)
            del recent[:-MAX_ACTIVE_JOBS_PER_SCOPE]
            task = asyncio.create_task(self._run_job(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return self._queued_text(job)

    def _is_active(self) -> bool:
        return not self._closed and self.active_config()[0]

    async def _run_job(self, job: dict[str, Any]) -> None:
        scope = job["scope"]
        candidate = job["candidate"]
        event = job["event"]
        sent_by_callback = 0
        audio_result_received = False
        audio_fact_recorded = False
        audio_result: dict[str, Any] = {}
        audio_started_at = 0.0
        audio_sent = 0
        audio_total = 0
        audio_failed_segment: str | None = None
        callback_audio_seconds: float | None = 0.0
        try:
            job["status"] = "running"
            job["message"] = "正在准备演唱"
            job["updated_at"] = time.monotonic()
            if not await self._can_send(job):
                self._finish(job, "failed", "主动演唱已关闭或当前对话已切换，任务没有发送音频。")
                return
            templates = self._templates()
            reply_enabled = self._reply_enabled()
            downloaded = await self.plugin.runtime.download_selected(
                job["platform"], candidate["query"]
            )
            if not isinstance(downloaded, dict) or not downloaded.get("id"):
                raise RuntimeError("音源未返回可用歌曲记录")
            job["song_id"] = str(downloaded["id"])
            job["download_reused"] = downloaded.get("reused") is True
            if not await self._can_send(job):
                self._finish(job, "failed", "主动演唱已关闭或当前对话已切换，任务停止。")
                return
            if reply_enabled["play_sending"]:
                text = self._render("play_sending", templates, self._template_values(job, sent=0, total=0))
                await self._send_text(event, job, text)

            async def send_audio(path: Path) -> None:
                nonlocal sent_by_callback, callback_audio_seconds
                if not await self._can_send(job):
                    raise RuntimeError("主动演唱已关闭或当前对话已切换，停止后续音频发送")
                from astrbot.api.event import MessageChain
                from astrbot.api.message_components import Record

                chain = MessageChain(chain=[Record(file=str(path), url=str(path))])
                sender = getattr(event, "send", None)
                if not callable(sender):
                    raise RuntimeError("当前事件不支持发送语音消息")
                outcome = sender(chain)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if outcome is False:
                    raise RuntimeError("AstrBot未发送语音消息")
                sent_by_callback += 1
                job["sent"] = sent_by_callback
                duration = _wav_duration(path)
                if duration is None:
                    callback_audio_seconds = None
                    job["sent_seconds"] = None
                elif callback_audio_seconds is not None:
                    callback_audio_seconds += duration
                    job["sent_seconds"] = callback_audio_seconds

            audio_started_at = time.time()
            job["started_at"] = audio_started_at
            result = await self.plugin.runtime.send_cover(
                job["song_id"],
                send_audio,
                voice_id=job["voice_id"],
                max_seconds=job["max_seconds"],
                gap_seconds=job["gap_seconds"],
            )
            if not isinstance(result, dict):
                raise RuntimeError("翻唱发送任务返回格式无效")
            audio_result_received = True
            audio_result = result
            sent = _count(result.get("sent"), sent_by_callback)
            total = _count(result.get("total"), sent)
            failed_segment = result.get("failed_segment")
            audio_sent = sent
            audio_total = total
            audio_failed_segment = str(failed_segment) if failed_segment else None
            job.update({
                "sent": sent,
                "total": total,
                "sent_seconds": _optional_nonnegative(result.get("sent_seconds")),
                "planned_seconds": _optional_nonnegative(result.get("planned_seconds")),
                "capped": result.get("capped") is True,
                "voice_name": _bounded_text(result.get("voice_name"), 200),
                "voice_version": _bounded_text(result.get("voice_version"), 200),
                "failed_segment": _bounded_text(failed_segment, 200) if failed_segment else None,
            })
            if total <= 0 or sent <= 0:
                self._finish(job, "failed", "没有成功发送音频片段，整首演唱未完成。")
            elif failed_segment or sent < total:
                self._finish(job, "partial", f"仅成功发送{sent}/{total}段，整首演唱未完成。")
            else:
                self._finish(job, "sent", "演唱已完成。")
            if sent > 0 and total > 0:
                await self._record_active_event(
                    job,
                    started_at=audio_started_at,
                    sent=sent,
                    total=total,
                    failed_segment=audio_failed_segment,
                    result=result,
                )
                audio_fact_recorded = True
            if job["status"] == "failed":
                if await self._can_send(job):
                    await self._notify_failure(job, job["message"])
                return
            if job["status"] == "partial":
                if await self._can_send(job):
                    await self._notify_failure(job, job["message"])
                return
            if self._reply_enabled()["play_complete"] and await self._can_send(job):
                text = self._render("play_complete", self._templates(), self._template_values(job, sent=sent, total=total))
                await self._send_text(event, job, text)
        except asyncio.CancelledError:
            if audio_result_received:
                if audio_sent > 0 and audio_total > 0 and not audio_fact_recorded:
                    await self._record_active_event(
                        job,
                        started_at=audio_started_at,
                        sent=audio_sent,
                        total=audio_total,
                        failed_segment=audio_failed_segment,
                        result=audio_result,
                    )
                    audio_fact_recorded = True
            elif sent_by_callback > 0:
                await self._record_active_event(
                    job,
                    started_at=job.get("started_at", time.time()),
                    sent=sent_by_callback,
                    total=max(sent_by_callback + 1, _count(job.get("total"), 0)),
                    failed_segment="cancelled",
                    result={
                        "voice_id": job.get("voice_id"),
                        "sent_seconds": job.get("sent_seconds"),
                        "capped": job.get("capped") is True,
                    },
                )
                self._finish(job, "partial", f"演唱任务已取消，实际发送{sent_by_callback}段；未完成整首演唱。")
            else:
                self._finish(job, "cancelled", "演唱任务已取消，未发送音频。")
            raise
        except Exception as exc:
            total = max(sent_by_callback + 1, _count(job.get("total"), 0)) if sent_by_callback else 0
            if sent_by_callback > 0 and not audio_fact_recorded and not audio_result_received:
                await self._record_active_event(
                    job,
                    started_at=job.get("started_at", time.time()),
                    sent=sent_by_callback,
                    total=total,
                    failed_segment="unknown",
                    result={
                        "voice_id": job.get("voice_id"),
                        "sent_seconds": job.get("sent_seconds"),
                        "capped": job.get("capped") is True,
                    },
                )
                audio_fact_recorded = True
            self._finish(job, "partial" if sent_by_callback else "failed", str(exc))
            if await self._can_send(job):
                await self._notify_failure(job, str(exc))
        finally:
            try:
                await self._send_job_song_log(
                    job,
                    audio_result_received=audio_result_received,
                    audio_result=audio_result,
                    audio_sent=audio_sent,
                    audio_total=audio_total,
                    sent_by_callback=sent_by_callback,
                    callback_audio_seconds=callback_audio_seconds,
                )
            except Exception:
                pass
            finally:
                conversation_scope = (scope[0], scope[1])
                async with self._lock:
                    if self._inflight.get(conversation_scope) == job["job_id"]:
                        self._inflight.pop(conversation_scope, None)

    async def _send_job_song_log(
        self,
        job: dict[str, Any],
        *,
        audio_result_received: bool,
        audio_result: dict[str, Any],
        audio_sent: int,
        audio_total: int,
        sent_by_callback: int,
        callback_audio_seconds: float | None,
    ) -> None:
        """Log one active playback from its audio outcome, independent of feedback."""
        from .song_log import send_song_log

        candidate = job.get("candidate") if isinstance(job.get("candidate"), dict) else {}
        if audio_result_received:
            sent = audio_sent
            total = audio_total
            sent_seconds = audio_result.get("sent_seconds")
            audio_succeeded = (
                audio_sent == audio_total > 0
                and not audio_result.get("failed_segment")
            )
            failure_reason = "" if audio_succeeded else str(
                job.get("message") or audio_result.get("message") or "主动演唱未完成"
            )
        else:
            sent = sent_by_callback
            total = (
                max(sent_by_callback + 1, _count(job.get("total"), 0))
                if sent_by_callback
                else _count(job.get("total"), 0)
            )
            sent_seconds = None
            failure_reason = str(job.get("message") or "主动演唱未完成")

        settings_getter = getattr(self.plugin, "_settings", None)
        settings = settings_getter() if callable(settings_getter) else {}
        if not isinstance(settings, dict):
            settings = {}
        voice = str(job.get("voice_name") or job.get("voice_id") or "原唱")
        await send_song_log(
            getattr(self.plugin, "context", None),
            settings,
            title=str(candidate.get("title") or "未知歌曲"),
            artist=str(candidate.get("artist") or "未知歌手"),
            platform=_PLATFORM_NAMES.get(job.get("platform"), str(job.get("platform") or "未知平台")),
            trigger="主动LLM",
            voice=voice,
            source_session=str((job.get("scope") or ("",))[0] or ""),
            sent=sent,
            total=total,
            sent_seconds=sent_seconds,
            fallback_seconds=callback_audio_seconds,
            failure_reason=failure_reason,
        )

    async def _can_send(self, job: dict[str, Any]) -> bool:
        return self._is_active() and await self._scope_is_current(job["scope"])

    async def _send_text(self, event: Any, job: dict[str, Any], text: str) -> bool:
        if not text.strip() or not await self._can_send(job):
            return False
        sender = getattr(event, "send", None)
        if not callable(sender):
            return False
        result_builder = getattr(event, "plain_result", None)
        message = result_builder(text) if callable(result_builder) else text
        try:
            result = sender(message)
            if inspect.isawaitable(result):
                result = await result
            return result is not False
        except Exception:
            return False

    async def _notify_failure(self, job: dict[str, Any], reason: str) -> None:
        text = self._failure_text("演唱", reason, job)
        await self._send_text(job["event"], job, text)

    async def _record_active_event(
        self,
        job: dict[str, Any],
        *,
        started_at: float,
        sent: int,
        total: int,
        failed_segment: str | None,
        result: dict[str, Any],
    ) -> None:
        memory = getattr(self.plugin, "passive_memory", None)
        recorder = getattr(memory, "record", None)
        if not callable(recorder):
            return
        candidate = job["candidate"]
        scope = job["scope"]
        event = job["event"]
        message_obj = getattr(event, "message_obj", None)
        requested_at = getattr(message_obj, "timestamp", None)
        if not isinstance(requested_at, (int, float)) or isinstance(requested_at, bool) or requested_at <= 0:
            requested_at = time.time()
        message_id = getattr(message_obj, "message_id", None)
        try:
            await recorder(
                umo=scope[0],
                conversation_id=scope[1],
                requested_at=requested_at,
                started_at=started_at,
                title=candidate["title"],
                artist=candidate["artist"],
                platform=job["platform"],
                source="active",
                actor_id=scope[2],
                source_message_id=str(message_id) if message_id else None,
                sent=sent,
                total=total,
                failed_segment=failed_segment,
                voice_id=str(result.get("voice_id") or job["voice_id"]),
                voice_name=result.get("voice_name"),
                voice_version=result.get("voice_version"),
                sent_seconds=result.get("sent_seconds"),
                capped=result.get("capped") is True,
            )
        except Exception:
            pass

    def recent_context(self, umo: str, conversation_id: str) -> str:
        scope = (str(umo or "").strip(), str(conversation_id or "").strip())
        if not all(scope):
            return ""
        now = time.monotonic()
        ids = self._recent.get(scope, [])
        jobs = []
        for job_id in ids:
            job = self._jobs.get(job_id)
            if job is None or now - job.get("updated_at", now) > ACTIVE_JOB_RETENTION_SECONDS:
                continue
            jobs.append({
                "job_id": job_id,
                "status": job.get("status"),
                "title": job.get("candidate", {}).get("title", ""),
                "artist": job.get("candidate", {}).get("artist", ""),
                "voice_name": job.get("voice_name", ""),
                "sent_segments": job.get("sent", 0),
                "total_segments": job.get("total", 0),
                "sent_seconds": job.get("sent_seconds"),
                "capped": job.get("capped", False),
                "message": job.get("message", ""),
            })
        if not jobs:
            return ""
        return (
            "当前对话最近的主动演唱任务状态；只有status=sent表示全部发送成功，partial表示部分发送，"
            "failed/cancelled表示没有完成：\n"
            + json.dumps(jobs, ensure_ascii=False, separators=(",", ":"))
        )

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _prune_locked(self, now: float) -> None:
        expired_scopes = [
            scope for scope, value in self._selections.items()
            if value.get("expires_at", 0) <= now
        ]
        for scope in expired_scopes:
            self._selections.pop(scope, None)
        for key, (_job_id, expires_at) in list(self._candidate_jobs.items()):
            if expires_at <= now:
                self._candidate_jobs.pop(key, None)
        expired_jobs = [
            job_id for job_id, job in self._jobs.items()
            if now - job.get("updated_at", now) > ACTIVE_JOB_RETENTION_SECONDS
        ]
        for job_id in expired_jobs:
            self._jobs.pop(job_id, None)

    def _public_candidate(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        exact_query = str(item.get("query") or "").strip()
        provider_id = str(item.get("id") or "").strip()
        if not exact_query or len(exact_query) > 500 or not provider_id:
            return None
        return {
            "query": exact_query,
            "provider_id": provider_id[:200],
            "title": _bounded_text(item.get("title") or "未知歌曲", 500),
            "artist": _bounded_text(item.get("artist") or "未知歌手", 500),
            "album": _bounded_text(item.get("album") or "未知专辑", 500),
            "duration": item.get("duration"),
        }

    def _templates(self) -> dict[str, str]:
        getter = getattr(self.plugin, "_templates", None)
        if callable(getter):
            return getter(self.settings())
        try:
            return validate_templates(self.settings().get("reply_templates"))
        except (TypeError, ValueError):
            return dict(DEFAULT_TEMPLATES)

    def _reply_enabled(self) -> dict[str, bool]:
        getter = getattr(self.plugin, "_reply_enabled", None)
        if callable(getter):
            return getter(self.settings())
        result = dict(REPLY_ENABLED_DEFAULTS)
        values = self.settings().get("reply_enabled")
        if isinstance(values, dict):
            for key in result:
                if type(values.get(key)) is bool:
                    result[key] = values[key]
        return result

    def _render(self, name: str, templates: dict[str, str], values: dict[str, Any]) -> str:
        renderer = getattr(self.plugin, "_play_reply", None)
        if callable(renderer):
            try:
                return renderer(name, templates, values) or ""
            except Exception:
                pass
        try:
            return render_template(name, templates, values)
        except (TypeError, ValueError):
            return render_template(name, DEFAULT_TEMPLATES, values)

    def _template_values(self, job: dict[str, Any], *, sent: int, total: int) -> dict[str, Any]:
        candidate = job["candidate"]
        return {
            "唤醒词": "",
            "歌名": candidate.get("title", "未知歌曲"),
            "歌手": candidate.get("artist", "未知歌手"),
            "平台": _PLATFORM_NAMES.get(job.get("platform", ""), job.get("platform", "")),
            "已发送段数": sent,
            "总段数": total,
        }

    def _failure_text(self, action: str, reason: str, job: dict[str, Any] | None = None) -> str:
        templates = self._templates()
        values = {
            "唤醒词": "",
            "动作": action,
            "歌名": job.get("candidate", {}).get("title", "") if job else "",
            "错误原因": _bounded_text(reason, 500),
            "已发送段数": job.get("sent", 0) if job else 0,
            "总段数": job.get("total", 0) if job else 0,
        }
        return self._render("failure", templates, values)

    def _queued_text(self, job: dict[str, Any], *, duplicate: bool = False) -> str:
        candidate = job["candidate"]
        if duplicate:
            display_text = (
                f"《{candidate['title']}》的后台演唱任务已存在，当前状态：{job['status']}，"
                f"job_id={job['job_id']}。"
            )
        else:
            selected = ""
            if self._reply_enabled()["play_selected"]:
                selected = self._render(
                    "play_selected", self._templates(), self._template_values(job, sent=0, total=0)
                )
            queued = (
                f"已安排使用当前音色演唱《{candidate['title']}》；任务处于队列中，不能视为已发送。"
                f"job_id={job['job_id']}。"
            )
            display_text = f"{selected}\n{queued}" if selected else queued
        return self._sing_result(
            "queued",
            display_text,
            job_id=job["job_id"],
            title=candidate.get("title", ""),
            voice_id=job.get("voice_id", ""),
            duplicate=duplicate,
        )

    @staticmethod
    def _sing_result(
        status: str,
        display_text: str,
        *,
        job_id: str = "",
        title: str = "",
        voice_id: str = "",
        duplicate: bool = False,
    ) -> str:
        return _tool_result(
            status,
            display_text,
            job_id=job_id,
            title=title,
            voice_id=voice_id,
            duplicate=duplicate,
        )

    @staticmethod
    def _finish(job: dict[str, Any], status: str, message: str) -> None:
        job["status"] = status
        job["message"] = _bounded_text(message, 500)
        job["updated_at"] = time.monotonic()

    @staticmethod
    def _format_duration(value: Any) -> str:
        try:
            seconds = max(0, int(float(value or 0)))
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "未知"


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _finite_number(value: Any) -> bool:
    try:
        return float("-inf") < float(value) < float("inf")
    except (TypeError, ValueError, OverflowError):
        return False


def _count(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _optional_nonnegative(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if _finite_number(numeric) and numeric >= 0 else None


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            return audio.getnframes() / rate if rate > 0 else None
    except (OSError, EOFError, wave.Error):
        return None

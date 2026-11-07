"""AstrBot entry point for Better Music."""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import aclosing
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.event import MessageChain

from .audio import wav_duration as _wav_duration
from .active import ActiveMusicController, build_active_tools
from .providers import PLATFORMS, ProviderManager
from .passive import PassiveMusicMemory, capture_current_scope, format_context
from .paths import PLUGIN_NAME, data_subdirectory
from .quota import CHECKER_FAILURE, MusicQuotaService, QuotaReservation, scope_from_event
from .runtime import MusicRuntime
from .song_log import send_song_log
from .templates import (
    DEFAULT_TEMPLATES,
    REPLY_ENABLED_DEFAULTS,
    REPLY_ENABLED_KEYS,
    render_template,
    validate_templates,
)
from .web import WebAPI


SELECTION_TTL_SECONDS = 5 * 60
CHAT_PRIORITY = ("netease", "kugou", "qq")
CHAT_NAMES = dict(PLATFORMS)
CHAT_NAMES["netease"] = "网易云音乐"
CHAT_NAMES["kugou"] = "酷狗音乐"
CHAT_NAMES["qq"] = "QQ音乐"


@register(
    PLUGIN_NAME,
    "Xing",
    "更好的点歌：支持网易云音乐、QQ音乐和酷狗，并提供登录与文件管理页面。",
    "1.1.0",
)
class BetterMusicPlugin(Star):
    """Search provider catalogs and send only an explicitly selected track."""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = data_subdirectory(StarTools.get_data_dir(PLUGIN_NAME))
        self.downloads_dir = data_subdirectory(self.data_dir, "downloads")
        self.music_quota = MusicQuotaService(
            self.data_dir / "music_usage.json",
            logger=logger,
        )
        self._balance_quota_peer = None
        self._connect_balance_quota()
        self.providers = ProviderManager(self.data_dir)
        self.runtime = MusicRuntime(
            self.data_dir,
            self.providers,
            downloads_dir=self.downloads_dir,
        )
        self.passive_memory = PassiveMusicMemory(
            self.data_dir / "passive_music_events.json"
        )
        self.active_music = ActiveMusicController(self)
        from astrbot.core.agent.tool import FunctionTool

        self.active_tools = build_active_tools(self.active_music, FunctionTool)
        self.context.add_llm_tools(*self.active_tools)
        self.web = WebAPI(
            context,
            self.providers,
            self.runtime,
            passive_memory=self.passive_memory,
        )
        self.web.register()
        self._pending_selections: dict[tuple[str, str], dict] = {}
        self._search_generations: dict[tuple[str, str], int] = {}
        self._selection_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Migrate legacy files before cleaning and starting runtime tasks."""
        await self.runtime.migrate_legacy()
        await self.runtime.cleanup()
        self.runtime.start_cleanup()

    def get_music_quota_api(self) -> MusicQuotaService | None:
        """Return the stable in-process quota bridge for another AstrBot plugin."""
        return getattr(self, "music_quota", None)

    def _balance_plugin(self):
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter("astrbot_plugin_better_balance")
        except Exception:
            return None
        return getattr(metadata, "star_cls", None)

    def _connect_balance_quota(self) -> None:
        """Offer the public quota API to an already loaded Balance plugin."""
        api = self.get_music_quota_api()
        balance = self._balance_plugin()
        callback = getattr(balance, "on_music_quota_ready", None)
        if api is None or not callable(callback):
            return
        try:
            previous = getattr(self, "_balance_quota_peer", None)
            if previous is not None and previous is not balance:
                unavailable = getattr(previous, "on_music_quota_unavailable", None)
                if callable(unavailable):
                    unavailable(api)
            callback(api)
            self._balance_quota_peer = balance
        except Exception:
            logger.exception("连接 Better Balance 点歌额度失败。")

    def _disconnect_balance_quota(self) -> None:
        """Notify the current and previously connected Balance instances."""
        api = self.get_music_quota_api()
        if api is None:
            return
        current = self._balance_plugin()
        previous = getattr(self, "_balance_quota_peer", None)
        peers = []
        for balance in (current, previous):
            if balance is not None and not any(balance is peer for peer in peers):
                peers.append(balance)
        for balance in peers:
            callback = getattr(balance, "on_music_quota_unavailable", None)
            if not callable(callback):
                continue
            try:
                callback(api)
            except Exception:
                logger.exception("通知 Better Balance 点歌额度接口卸载失败。")
        self._balance_quota_peer = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """Retry the one-time public handshake after plugin instances are registered."""
        self._connect_balance_quota()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """Handle matching commands with direct sends, without invoking an LLM."""
        wake_flag = getattr(event, "is_at_or_wake_command", True)
        if callable(wake_flag):
            try:
                wake_flag = wake_flag()
            except Exception:
                wake_flag = False
        if wake_flag is False:
            return
        settings = self._settings()
        if settings.get("active_llm", False) is True:
            return

        prefixes = await self._wake_prefixes(event)
        raw_message = self._raw_message_text(event).lstrip()
        matched_prefix = next((prefix for prefix in prefixes if raw_message.startswith(prefix)), None)
        if matched_prefix is None:
            return

        command, argument = self._parse_programmatic_command(raw_message[len(matched_prefix):])
        if command is None:
            return

        event.should_call_llm(True)
        try:
            templates = self._templates(settings)
            identity = self._selection_identity(event)

            if command == "menu":
                if argument:
                    await event.send(event.plain_result(
                        self._failure(templates, "点歌", f"用法：{matched_prefix}点歌。", wake_prefix=matched_prefix)
                    ))
                    return
                await event.send(event.plain_result(await self._render_menu(matched_prefix, templates)))
                return

            if command == "search":
                if identity is None:
                    await event.send(event.plain_result(
                        self._failure(templates, "搜索", "无法识别当前用户和会话。", wake_prefix=matched_prefix)
                    ))
                    return
                generation = await self._begin_search(identity)
                query = argument.strip()
                if not query:
                    await event.send(event.plain_result(
                        self._failure(templates, "搜索", f"用法：{matched_prefix}搜索 歌名。", wake_prefix=matched_prefix)
                    ))
                    return
                async for response in self._search(event, identity, generation, query, matched_prefix, templates):
                    await event.send(response)
                return

            if command == "play":
                if not re.fullmatch(r"[1-5]", argument.strip()):
                    await event.send(event.plain_result(
                        self._failure(templates, "播放", f"用法：{matched_prefix}播放 1 至 5。", wake_prefix=matched_prefix)
                    ))
                    return
                if identity is None:
                    await event.send(event.plain_result(self._failure(
                        templates, "播放", "无法识别当前用户和会话，请重新搜索。", wake_prefix=matched_prefix
                    )))
                    return
                candidate, error = await self._take_selection(identity, int(argument.strip()))
                if candidate is None:
                    await event.send(event.plain_result(self._failure(
                        templates, "播放", error, wake_prefix=matched_prefix
                    )))
                    return
                selection = candidate["selection"]
                reply_enabled = self._reply_enabled(settings)
                if reply_enabled["play_selected"]:
                    notice = self._play_reply("play_selected", templates, {
                        "唤醒词": matched_prefix,
                        "歌名": selection.get("title") or "未知歌曲",
                        "歌手": selection.get("artist"),
                        "平台": CHAT_NAMES.get(candidate["platform"], candidate["platform"]),
                    })
                    if notice is not None:
                        await event.send(event.plain_result(notice))
                playback = self._download_and_send(
                    event,
                    candidate["platform"],
                    selection,
                    templates,
                    matched_prefix,
                    reply_enabled,
                    passive_enabled=settings.get("passive_llm", False) is True,
                )
                async with aclosing(playback):
                    async for response in playback:
                        await event.send(response)
        finally:
            event.stop_event()

    def _settings(self) -> dict:
        try:
            value = self.runtime.settings()
        except Exception:
            logger.exception("读取 Better Music 设置失败，使用安全默认值。")
            return {}
        return value if isinstance(value, dict) else {}

    def _templates(self, settings: dict) -> dict[str, str]:
        try:
            return validate_templates(settings.get("reply_templates"))
        except ValueError as exc:
            logger.warning(f"Better Music 回复模板无效，暂时使用默认模板：{exc}")
            return dict(DEFAULT_TEMPLATES)

    @staticmethod
    def _reply_enabled(settings: dict) -> dict[str, bool]:
        values = dict(REPLY_ENABLED_DEFAULTS)
        stored = settings.get("reply_enabled")
        if isinstance(stored, dict):
            for key in REPLY_ENABLED_KEYS:
                if type(stored.get(key)) is bool:
                    values[key] = stored[key]
        return values

    @staticmethod
    def _play_reply(name: str, templates: dict[str, str], values: dict) -> str | None:
        rendered = render_template(name, templates, values)
        return rendered if rendered.strip() else None

    async def _wake_prefixes(self, event) -> list[str]:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return []
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        try:
            config = getter(umo) if umo else getter()
        except TypeError:
            config = getter()
        except Exception:
            return []
        get_value = getattr(config, "get", None)
        try:
            wake_prefix = get_value("wake_prefix", []) if callable(get_value) else getattr(config, "wake_prefix", [])
        except Exception:
            return []
        if isinstance(wake_prefix, str):
            values = [wake_prefix]
        elif isinstance(wake_prefix, (list, tuple, set)):
            values = list(wake_prefix)
        else:
            return []
        prefixes = {value for value in values if isinstance(value, str) and value}
        return sorted(prefixes, key=len, reverse=True)

    @staticmethod
    def _raw_message_text(event) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "message_str", None)
        if isinstance(raw, str):
            return raw
        raw = getattr(event, "message_str", "")
        return raw if isinstance(raw, str) else str(raw or "")

    @staticmethod
    def _parse_programmatic_command(text: str) -> tuple[str | None, str]:
        match = re.fullmatch(r"\s*(点歌|搜索|播放)(?:\s+(.*))?\s*", str(text or ""), flags=re.DOTALL)
        if not match:
            return None, ""
        name = match.group(1)
        argument = (match.group(2) or "").strip()
        if name == "点歌":
            return ("menu", argument)
        if name == "搜索":
            return ("search", argument)
        return ("play", argument)

    async def _render_menu(self, wake_prefix: str, templates: dict[str, str]) -> str:
        statuses = {}
        for platform in CHAT_PRIORITY:
            try:
                statuses[platform] = await self.providers.chat_account_status(platform)
            except Exception as exc:
                statuses[platform] = {"status": "unknown", "message": str(exc), "account": {}}

        values = {"唤醒词": wake_prefix}
        for index, platform in enumerate(CHAT_PRIORITY, start=1):
            state = statuses[platform]
            account = state.get("account") if isinstance(state.get("account"), dict) else {}
            status = state.get("status")
            values[f"登录{index}"] = {
                "valid": "已登录",
                "invalid": "登录已失效",
                "not_logged_in": "未登录",
                "unknown": "状态未知",
            }.get(status, "状态未知")
            membership = account.get("membership") if isinstance(account.get("membership"), dict) else {}
            if status == "valid":
                values[f"会员{index}"] = str(membership.get("label") or "未知")
            elif status == "not_logged_in":
                values[f"会员{index}"] = "未登录"
            else:
                values[f"会员{index}"] = "状态未知"
        return render_template("menu", templates, values)

    async def _search(
        self,
        event,
        identity: tuple[str, str],
        generation: int,
        query: str,
        wake_prefix: str,
        templates: dict[str, str],
    ):
        selected_platform = None
        account_states = []
        for platform in CHAT_PRIORITY:
            try:
                state = await self.providers.chat_account_status(platform)
            except Exception as exc:
                state = {"status": "unknown", "message": str(exc)}
            account_states.append((platform, state))
            if state.get("status") == "valid":
                selected_platform = platform
                break
            if state.get("status") == "unknown":
                break

        if selected_platform is None:
            status_text = "；".join(
                f"{CHAT_NAMES[platform]}：{self._account_status_text(state)}"
                for platform, state in account_states
            ) or "没有可用的已登录账号。"
            yield event.plain_result(self._failure(templates, "搜索", status_text, wake_prefix=wake_prefix))
            return

        try:
            results = await self.providers.search(selected_platform, query, limit=5, page=1)
        except Exception as exc:
            yield event.plain_result(
                self._failure(templates, "搜索", f"{CHAT_NAMES[selected_platform]}搜索失败：{exc}", wake_prefix=wake_prefix)
            )
            return
        if not isinstance(results, (list, tuple)):
            results = []

        candidates = [
            candidate
            for item in results if isinstance(item, dict)
            if (candidate := self._public_candidate(item)) is not None
        ][:5]
        if not candidates:
            yield event.plain_result(
                self._failure(templates, "搜索", f"{CHAT_NAMES[selected_platform]}没有找到可选歌曲。", wake_prefix=wake_prefix)
            )
            return

        if not await self._store_selections(identity, generation, selected_platform, candidates):
            yield event.plain_result(
                self._failure(templates, "搜索", "这次搜索已被更新，请查看最新搜索结果。", wake_prefix=wake_prefix)
            )
            return
        item_lines = []
        for index, candidate in enumerate(candidates, start=1):
            item_lines.append(render_template("search_item", templates, {
                "唤醒词": wake_prefix,
                "序号": index,
                "歌名": candidate.get("title"),
                "歌手": candidate.get("artist"),
                "专辑": candidate.get("album"),
                "时长": self._format_duration(candidate.get("duration")),
                "平台": CHAT_NAMES[selected_platform],
            }))
        yield event.plain_result(render_template("search_results", templates, {
            "唤醒词": wake_prefix,
            "关键词": query,
            "平台": CHAT_NAMES[selected_platform],
            "结果数量": len(candidates),
            "结果列表": "\n".join(item_lines),
        }))

    @staticmethod
    def _account_status_text(state: dict) -> str:
        status = state.get("status")
        message = str(state.get("message") or "").strip()
        if status == "unknown" and message and "状态未知" not in message and "无法确认" not in message:
            return f"状态未知（{message}）"
        return message or {
            "valid": "已验证登录",
            "invalid": "账号凭证已失效",
            "not_logged_in": "尚未登录",
            "unknown": "状态未知",
        }.get(status, "状态未知")

    def _failure(
        self,
        templates: dict[str, str],
        action: str,
        reason: str,
        *,
        wake_prefix: str = "",
        title: str = "",
        sent: int = 0,
        total: int = 0,
    ) -> str:
        return render_template("failure", templates, {
            "唤醒词": wake_prefix,
            "动作": action,
            "歌名": title,
            "错误原因": reason,
            "已发送段数": sent,
            "总段数": total,
        })

    async def _download_and_send(
        self,
        event,
        provider_id: str,
        selection: dict,
        templates: dict[str, str],
        wake_prefix: str,
        reply_enabled: dict[str, bool],
        *,
        passive_enabled: bool = False,
    ):
        reservation: QuotaReservation | None = None
        quota_api = self.get_music_quota_api()
        if quota_api is not None:
            identity = self._selection_identity(event)
            if identity is None:
                yield event.plain_result(self._failure(
                    templates,
                    "播放",
                    "无法识别当前用户和会话，未开始下载歌曲。",
                    title=str(selection.get("title") or "未知歌曲"),
                    wake_prefix=wake_prefix,
                ))
                return
            try:
                reservation, reason = await quota_api.reserve(
                    identity[0],
                    source="command",
                    kind="original",
                    actor_id=identity[1],
                    scope=scope_from_event(event),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("确认点歌额度失败，已停止本次播放。")
                reservation, reason = None, CHECKER_FAILURE
            if reservation is None:
                yield event.plain_result(self._failure(
                    templates,
                    "播放",
                    reason or CHECKER_FAILURE,
                    title=str(selection.get("title") or "未知歌曲"),
                    wake_prefix=wake_prefix,
                ))
                return

        try:
            playback = self._download_and_send_admitted(
                event,
                provider_id,
                selection,
                templates,
                wake_prefix,
                reply_enabled,
                passive_enabled=passive_enabled,
                reservation=reservation,
            )
            async with aclosing(playback):
                async for response in playback:
                    yield response
        finally:
            if reservation is not None:
                await reservation.release()

    async def _download_and_send_admitted(
        self,
        event,
        provider_id: str,
        selection: dict,
        templates: dict[str, str],
        wake_prefix: str,
        reply_enabled: dict[str, bool],
        *,
        passive_enabled: bool = False,
        reservation: QuotaReservation | None = None,
    ):
        playback_started_at = time.time()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        title = str(selection.get("title") or "未知歌曲")
        artist = str(selection.get("artist") or "未知歌手")
        platform_name = CHAT_NAMES.get(provider_id, provider_id)
        sent_by_callback = 0
        fallback_audio_seconds: float | None = 0.0
        song_log_sent = False

        async def log_playback(
            sent: int,
            total: int,
            *,
            failure_reason: str = "",
            sent_seconds=None,
        ) -> None:
            nonlocal song_log_sent
            if song_log_sent:
                return
            song_log_sent = True
            await send_song_log(
                self.context,
                self._settings(),
                title=title,
                artist=artist,
                platform=platform_name,
                trigger="聊天指令",
                voice="原唱",
                source_session=umo,
                sent=sent,
                total=total,
                sent_seconds=sent_seconds,
                fallback_seconds=fallback_audio_seconds,
                failure_reason=failure_reason,
            )

        passive_scope = None
        message_obj = getattr(event, "message_obj", None)
        requested_at = getattr(message_obj, "timestamp", None)
        if (
            not isinstance(requested_at, (int, float))
            or isinstance(requested_at, bool)
            or requested_at <= 0
        ):
            requested_at = getattr(event, "created_at", None)
        if (
            not isinstance(requested_at, (int, float))
            or isinstance(requested_at, bool)
            or requested_at <= 0
        ):
            requested_at = playback_started_at
        source_message_id = getattr(message_obj, "message_id", None)
        identity = self._selection_identity(event)
        actor_id = identity[1] if identity else None
        if passive_enabled:
            try:
                passive_scope = await self._capture_passive_scope(
                    umo
                )
            except asyncio.CancelledError:
                await log_playback(0, 0, failure_reason="播放任务已取消")
                raise
            except Exception:
                logger.exception("捕获当前点歌对话失败，继续发送歌曲。")

        try:
            song = await self.runtime.download_selected(provider_id, str(selection["query"]))
        except asyncio.CancelledError:
            await log_playback(0, 0, failure_reason="播放任务已取消")
            raise
        except Exception as exc:
            await log_playback(0, 0, failure_reason=str(exc))
            yield event.plain_result(self._failure(templates, "播放", str(exc), title=title, wake_prefix=wake_prefix))
            return

        title = str(song.get("title") or title)
        artist = str(song.get("artist") or "未知歌手")
        segments = song.get("segments") or []
        if not segments:
            await log_playback(0, 0, failure_reason="已下载，但没有可发送的音频片段。")
            yield event.plain_result(
                self._failure(templates, "播放", "已下载，但没有可发送的音频片段。", title=title, wake_prefix=wake_prefix)
            )
            return

        if reply_enabled["play_sending"]:
            notice = self._play_reply("play_sending", templates, {
                "唤醒词": wake_prefix,
                "歌名": title,
                "歌手": artist,
                "平台": platform_name,
                "总段数": len(segments),
                "已发送段数": 0,
            })
            if notice is not None:
                try:
                    yield event.plain_result(notice)
                except (asyncio.CancelledError, GeneratorExit):
                    await log_playback(0, len(segments), failure_reason="播放任务已取消")
                    raise

        async def send_audio(path: Path) -> None:
            nonlocal sent_by_callback, fallback_audio_seconds
            if not umo:
                raise RuntimeError("无法识别当前会话，未发送音频。")
            record = Record(file=str(path), url=str(path))
            sent = await self.context.send_message(umo, MessageChain(chain=[record]))
            if sent is False:
                raise RuntimeError("AstrBot 未找到当前会话的平台实例。")
            if reservation is not None:
                try:
                    await reservation.confirm()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("保存点歌额度失败，继续处理已成功发送的语音。")
            sent_by_callback += 1
            duration = _wav_duration(path)
            if duration is None:
                fallback_audio_seconds = None
            elif fallback_audio_seconds is not None:
                fallback_audio_seconds += duration

        try:
            if passive_enabled:
                playback = self._settings().get("passive_playback", {})
                if not isinstance(playback, dict):
                    playback = {}
                result = await self.runtime.send_song(
                    str(song.get("id", "")),
                    send_audio,
                    max_seconds=playback.get("max_seconds", 0),
                    gap_seconds=playback.get("gap_seconds", 0),
                )
            else:
                result = await self.runtime.send_song(str(song.get("id", "")), send_audio)
        except asyncio.CancelledError:
            await log_playback(
                sent_by_callback,
                len(segments),
                failure_reason="播放任务已取消",
            )
            raise
        except Exception as exc:
            await log_playback(
                sent_by_callback,
                len(segments),
                failure_reason=f"音频发送中断：{exc}",
            )
            yield event.plain_result(
                self._failure(
                    templates,
                    "播放",
                    f"音频发送中断，已发送 {sent_by_callback}/{len(segments)} 段。{exc}",
                    title=title,
                    sent=sent_by_callback,
                    total=len(segments),
                    wake_prefix=wake_prefix,
                )
            )
            return

        sent_count = int(result.get("sent", 0) or 0)
        total_count = int(result.get("total", len(segments)) or len(segments))
        failed_segment = result.get("failed_segment")
        playback_failure = ""
        if failed_segment is not None or sent_count < total_count:
            playback_failure = str(result.get("message") or "发送过程未完成")
        await log_playback(
            sent_count,
            total_count,
            failure_reason=playback_failure,
            sent_seconds=result.get("sent_seconds"),
        )
        memory = getattr(self, "passive_memory", None)
        if (
            passive_enabled
            and self._settings().get("passive_llm", False) is True
            and passive_scope
            and memory is not None
            and sent_count > 0
        ):
            try:
                await memory.record(
                    umo=passive_scope[0],
                    conversation_id=passive_scope[1],
                    requested_at=requested_at,
                    started_at=playback_started_at,
                    title=title,
                    artist=artist,
                    platform=provider_id,
                    source="chat",
                    actor_id=actor_id,
                    source_message_id=source_message_id,
                    sent=sent_count,
                    total=total_count,
                    failed_segment=str(failed_segment) if failed_segment else None,
                )
            except Exception:
                logger.exception("保存点歌发送记录失败，继续处理播放反馈。")
        if failed_segment is not None or sent_count < total_count:
            detail = result.get("message") or "发送过程未完成"
            yield event.plain_result(self._failure(
                templates,
                "播放",
                f"音频发送中断，已发送 {sent_count}/{total_count} 段。{detail}",
                title=title,
                sent=sent_count,
                total=total_count,
                wake_prefix=wake_prefix,
            ))
            return
        if reply_enabled["play_complete"]:
            notice = self._play_reply("play_complete", templates, {
                "唤醒词": wake_prefix,
                "歌名": title,
                "歌手": artist,
                "平台": platform_name,
                "总段数": total_count,
                "已发送段数": sent_count,
            })
            if notice is not None:
                yield event.plain_result(notice)

    async def _capture_passive_scope(self, umo: str) -> tuple[str, str] | None:
        """Bind an event to the session's current conversation before sending."""
        return await capture_current_scope(self.context, umo)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """Gate active tools and attach only current-CID playback facts."""
        settings = self._settings()
        active_enabled = settings.get("active_llm", False) is True
        passive_enabled = settings.get("passive_llm", False) is True
        active_music = getattr(self, "active_music", None)
        conversation = getattr(req, "conversation", None)
        conversation_id = str(getattr(conversation, "cid", "") or "").strip()
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if active_music is not None:
            active_music.bind_request(event, conversation_id)
            active_music.gate_request_tools(req, active_enabled)

        memory = getattr(self, "passive_memory", None)
        parts = getattr(req, "extra_user_content_parts", None)
        if not umo or not conversation_id or not isinstance(parts, list):
            return
        try:
            notes = []
            if memory is not None and (passive_enabled or active_enabled):
                sources = None if passive_enabled else ("active",)
                events = await memory.events_for(umo, conversation_id, sources=sources)
                if events:
                    notes.append(format_context(events))
            if active_music is not None and (active_enabled or passive_enabled):
                job_context = active_music.recent_context(umo, conversation_id)
                if job_context:
                    notes.append(job_context)
                if active_enabled and active_music.tools_available(req):
                    notes.append(
                        "主动演唱能力已启用。你可以根据当前聊天内容自行决定是否唱歌及选曲；"
                        "这项行为是你用所选音色唱歌或翻唱。需要演唱时先调用better_music_search，"
                        "再按聊天意图自行选择准确候选并调用better_music_sing；用户已指定歌曲时遵循指定内容。"
                        "queued只代表已排队，后续状态以实际送达为准。"
                    )
            if not notes:
                return
            from astrbot.core.agent.message import TextPart

            parts.append(TextPart(text="\n\n".join(notes)).mark_as_temp())
        except Exception:
            logger.exception("注入 Better Music 当前对话状态失败，继续处理正常 LLM 请求。")

    @staticmethod
    def _selection_identity(event) -> tuple[str, str] | None:
        session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        getter = getattr(event, "get_sender_id", None)
        try:
            user_id = str(getter() if callable(getter) else getattr(event, "sender_id", "") or "").strip()
        except Exception:
            user_id = ""
        if not session_id or not user_id:
            return None
        return session_id, user_id

    @staticmethod
    def _public_candidate(item: dict) -> dict | None:
        exact_query = str(item.get("query") or "").strip()
        candidate_id = str(item.get("id") or "").strip()
        if not exact_query or not candidate_id:
            return None
        return {
            "query": exact_query,
            "id": candidate_id,
            "title": str(item.get("title") or "未知歌曲"),
            "artist": str(item.get("artist") or "未知歌手"),
            "album": str(item.get("album") or "未知专辑"),
            "duration": item.get("duration"),
        }

    async def _begin_search(self, identity: tuple[str, str]) -> int:
        now = time.monotonic()
        async with self._selection_lock:
            self._prune_selections(now)
            generation = self._search_generations.get(identity, 0) + 1
            self._search_generations[identity] = generation
            self._pending_selections.pop(identity, None)
            return generation

    async def _store_selections(
        self,
        identity: tuple[str, str],
        generation: int,
        provider_id: str,
        candidates: list[dict],
    ) -> bool:
        now = time.monotonic()
        async with self._selection_lock:
            self._prune_selections(now)
            if self._search_generations.get(identity) != generation:
                return False
            self._pending_selections[identity] = {
                "platform": provider_id,
                "expires_at": now + SELECTION_TTL_SECONDS,
                "candidates": candidates,
            }
            return True

    async def _take_selection(self, identity: tuple[str, str], number: int) -> tuple[dict | None, str]:
        now = time.monotonic()
        async with self._selection_lock:
            self._prune_selections(now)
            pending = self._pending_selections.get(identity)
            if pending is None:
                return None, "没有可用的搜索结果，或 5 分钟选择时间已过。请重新搜索歌曲。"
            candidates = pending["candidates"]
            if number < 1 or number > len(candidates):
                return None, f"编号无效；请从 1 到 {len(candidates)} 中选择。"
            del self._pending_selections[identity]
            return {
                "platform": pending["platform"],
                "selection": candidates[number - 1],
            }, ""

    def _prune_selections(self, now: float) -> None:
        expired = [
            identity
            for identity, pending in self._pending_selections.items()
            if float(pending.get("expires_at", 0)) <= now
        ]
        for identity in expired:
            self._pending_selections.pop(identity, None)

    @staticmethod
    def _format_duration(value) -> str:
        try:
            seconds = max(0, int(float(value or 0)))
        except (TypeError, ValueError):
            seconds = 0
        return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "未知"

    async def terminate(self) -> None:
        """Cancel plugin-owned jobs before releasing shared runtime resources."""
        active_music = getattr(self, "active_music", None)
        if active_music is not None:
            await active_music.close()
        quota_api = self.get_music_quota_api()
        if quota_api is not None:
            await quota_api.close()
        self._disconnect_balance_quota()
        await self.web.close()
        async with self._selection_lock:
            self._pending_selections.clear()
        try:
            await self.runtime.close()
        finally:
            try:
                await self.providers.close()
            except Exception:
                logger.exception("关闭 Better Music 登录任务失败。")
